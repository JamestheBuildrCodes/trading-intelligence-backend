from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.deps import CurrentUser, get_current_user
from app.services.entitlements import get_user_entitlements

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])
PAYSTACK_API = "https://api.paystack.co"
CURRENCY = "USD"

# Amounts are in USD cents. The Paystack dashboard plan codes configured in
# Render must match these prices and billing intervals exactly.
PLANS: dict[tuple[str, str], dict[str, Any]] = {
    ("trader", "monthly"): {"amount": 1500, "setting": "paystack_trader_monthly_plan_code"},
    ("trader", "yearly"): {"amount": 14400, "setting": "paystack_trader_yearly_plan_code"},
    ("pro", "monthly"): {"amount": 2900, "setting": "paystack_pro_monthly_plan_code"},
    ("pro", "yearly"): {"amount": 28800, "setting": "paystack_pro_yearly_plan_code"},
}


class CheckoutRequest(BaseModel):
    plan: str = Field(pattern="^(trader|pro)$")
    billing_period: str = Field(pattern="^(monthly|yearly)$")


class DeveloperPreviewRequest(BaseModel):
    plan: str = Field(pattern="^(free|trader|pro|enterprise)$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secret() -> str:
    key = get_settings().paystack_secret_key
    if not key:
        raise HTTPException(status_code=503, detail="Paid checkout is not enabled. The service operator must configure Paystack and the four plan codes first.")
    return key


def _plan_code(plan: str, period: str) -> str:
    setting_name = PLANS[(plan, period)]["setting"]
    value = getattr(get_settings(), setting_name, None)
    if not value:
        raise HTTPException(status_code=503, detail="The selected subscription plan has not been configured by the service operator.")
    return str(value)


def _signed_webhook(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def _paystack_request(method: str, path: str, secret: str, payload: dict | None = None) -> dict:
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.request(
                method,
                PAYSTACK_API + path,
                headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code >= 400:
            raise RuntimeError("provider_rejected_request")
        result = response.json()
        if not isinstance(result, dict) or not result.get("status"):
            raise RuntimeError("provider_rejected_request")
        return result
    except Exception as exc:
        # Raw provider responses are intentionally not returned or logged.
        raise HTTPException(status_code=502, detail="The payment provider could not complete this request. No subscription entitlement was granted.") from None


def _current_subscription(db: Client, user_id: str) -> dict | None:
    result = (
        db.table("user_subscriptions")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _find_subscription_by_code(db: Client, code: str) -> dict | None:
    if not code:
        return None
    result = (
        db.table("user_subscriptions")
        .select("*")
        .eq("provider_subscription_code", code)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _add_billing_period(value: datetime, period: str) -> datetime:
    months = 12 if period == "yearly" else 1
    target_month = value.month - 1 + months
    target_year = value.year + target_month // 12
    target_month = target_month % 12 + 1
    import calendar
    target_day = min(value.day, calendar.monthrange(target_year, target_month)[1])
    return value.replace(year=target_year, month=target_month, day=target_day)


def _parse_provider_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _find_subscription_for_payment(db: Client, provider_code: str, customer_code: str) -> dict | None:
    row = _find_subscription_by_code(db, provider_code)
    if row is not None:
        return row
    if customer_code:
        result = db.table("user_subscriptions").select("*").eq("provider_customer_code", customer_code).limit(1).execute()
        return result.data[0] if result.data else None
    return None


def _apply_verified_charge(db: Client, reference: str, secret: str, event_data: dict | None = None) -> None:
    event_data = event_data or {}
    intent_result = (
        db.table("billing_checkout_intents")
        .select("*")
        .eq("reference", reference)
        .limit(1)
        .execute()
    )
    intent = intent_result.data[0] if intent_result.data else None

    # A recurring charge has a new provider reference and therefore has no
    # checkout-intent row. It must be linked to an existing verified subscription.
    event_subscription = event_data.get("subscription") if isinstance(event_data.get("subscription"), dict) else {}
    event_customer = event_data.get("customer") if isinstance(event_data.get("customer"), dict) else {}
    provider_subscription_code = str(
        event_subscription.get("subscription_code")
        or event_data.get("subscription_code")
        or ""
    )
    customer_code_hint = str(event_customer.get("customer_code") or "")

    verified_response = _paystack_request("GET", "/transaction/verify/" + reference, secret)
    transaction = verified_response.get("data") or {}
    if str(transaction.get("reference") or "") != reference or str(transaction.get("status") or "").lower() != "success":
        raise RuntimeError("transaction_not_verified")
    if str(transaction.get("currency") or "").upper() != CURRENCY:
        raise RuntimeError("transaction_currency_mismatch")
    try:
        amount = int(transaction.get("amount"))
    except (ValueError, TypeError):
        raise RuntimeError("transaction_amount_invalid") from None

    remote_plan = transaction.get("plan")
    remote_plan_code = remote_plan.get("plan_code") if isinstance(remote_plan, dict) else None
    customer = transaction.get("customer") if isinstance(transaction.get("customer"), dict) else {}
    customer_code = str(customer.get("customer_code") or customer_code_hint or "")
    incoming_email = str(customer.get("email") or "").strip().lower()

    if intent is not None:
        if amount != int(intent["expected_amount"]):
            raise RuntimeError("transaction_amount_mismatch")
        if remote_plan_code and str(remote_plan_code) != str(intent["provider_plan_code"]):
            raise RuntimeError("transaction_plan_mismatch")
        expected_email = str(intent.get("user_email") or "").strip().lower()
        if incoming_email and expected_email and incoming_email != expected_email:
            raise RuntimeError("transaction_email_mismatch")

        old = _current_subscription(db, str(intent["user_id"])) or {}
        if intent.get("status") == "succeeded":
            # Idempotent recovery after a partial database failure; do not reset
            # the start of an already-confirmed billing period on webhook retry.
            period_start = _parse_provider_datetime(old.get("current_period_start")) or datetime.now(timezone.utc)
        else:
            period_start = datetime.now(timezone.utc)
        period_end = (
            _parse_provider_datetime(event_subscription.get("next_payment_date"))
            or _parse_provider_datetime(transaction.get("next_payment_date"))
            or _add_billing_period(period_start, str(intent["billing_period"]))
        )
        row = {
            "user_id": intent["user_id"],
            "plan": intent["plan"],
            "status": "active",
            "billing_period": intent["billing_period"],
            "provider": "paystack",
            "provider_customer_code": customer_code or old.get("provider_customer_code"),
            "provider_subscription_code": provider_subscription_code or str(old.get("provider_subscription_code") or "") or None,
            "provider_subscription_email_token": str(event_subscription.get("email_token") or old.get("provider_subscription_email_token") or "") or None,
            "provider_plan_code": intent["provider_plan_code"],
            "checkout_reference": reference,
            "current_period_start": period_start.isoformat(),
            "current_period_end": period_end.isoformat(),
            "cancel_at_period_end": False,
            "last_payment_at": _now(),
            "last_event_at": _now(),
            "updated_at": _now(),
        }
        db.table("user_subscriptions").upsert(row, on_conflict="user_id").execute()
        db.table("billing_checkout_intents").update({
            "status": "succeeded",
            "provider_customer_code": customer_code or None,
            "completed_at": _now(),
            "safe_error_code": None,
        }).eq("reference", reference).execute()
        return

    # Subsequent billing cycle: reconcile the new transaction with the
    # existing subscription and its server-configured expected price.
    subscription_row = _find_subscription_for_payment(db, provider_subscription_code, customer_code)
    if subscription_row is None:
        if not provider_subscription_code and not customer_code:
            # Signed but unrelated Paystack charge; it cannot grant TradeGrid access.
            return
        raise RuntimeError("recurring_subscription_not_linked")

    plan = str(subscription_row.get("plan") or "")
    period = str(subscription_row.get("billing_period") or "")
    price = PLANS.get((plan, period))
    if not price:
        raise RuntimeError("recurring_plan_invalid")
    if amount != int(price["amount"]):
        raise RuntimeError("transaction_amount_mismatch")
    if remote_plan_code and str(remote_plan_code) != str(subscription_row.get("provider_plan_code")):
        raise RuntimeError("transaction_plan_mismatch")

    old_end = _parse_provider_datetime(subscription_row.get("current_period_end"))
    period_start = old_end or datetime.now(timezone.utc)
    period_end = (
        _parse_provider_datetime(event_subscription.get("next_payment_date"))
        or _parse_provider_datetime(transaction.get("next_payment_date"))
        or _add_billing_period(period_start, period)
    )
    db.table("user_subscriptions").update({
        "status": "active",
        "provider_customer_code": customer_code or subscription_row.get("provider_customer_code"),
        "provider_subscription_code": provider_subscription_code or subscription_row.get("provider_subscription_code"),
        "current_period_start": period_start.isoformat(),
        "current_period_end": period_end.isoformat(),
        "cancel_at_period_end": False,
        "last_payment_at": _now(),
        "last_event_at": _now(),
        "updated_at": _now(),
    }).eq("user_id", subscription_row["user_id"]).execute()


def _link_subscription_event(db: Client, data: dict) -> None:
    code = str(data.get("subscription_code") or data.get("sub_code") or "")
    email_token = str(data.get("email_token") or "")
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}
    customer_code = str(customer.get("customer_code") or "")
    customer_email = str(customer.get("email") or "").strip().lower()
    plan_obj = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    plan_code = str(plan_obj.get("plan_code") or data.get("plan_code") or "")

    row = _find_subscription_by_code(db, code)
    if row is None and customer_code:
        result = db.table("user_subscriptions").select("*").eq("provider_customer_code", customer_code).limit(1).execute()
        row = result.data[0] if result.data else None
    if row is None and customer_email and plan_code:
        intent = (
            db.table("billing_checkout_intents")
            .select("user_id,plan,billing_period,provider_plan_code,provider_customer_code,status")
            .eq("user_email", customer_email)
            .eq("provider_plan_code", plan_code)
            .in_("status", ["initialized", "succeeded"])
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if intent.data:
            pending = intent.data[0]
            row = _current_subscription(db, str(pending["user_id"]))
            if row is None:
                row = {
                    "user_id": pending["user_id"], "plan": pending["plan"], "status": "inactive",
                    "billing_period": pending["billing_period"], "provider": "paystack",
                    "provider_customer_code": customer_code or pending.get("provider_customer_code"),
                    "provider_plan_code": plan_code,
                }

    if row is None:
        raise RuntimeError("subscription_link_not_found")

    update = {
        "user_id": row["user_id"],
        "plan": row.get("plan") or "free",
        "status": row.get("status") or "inactive",
        "billing_period": row.get("billing_period"),
        "provider": "paystack",
        "provider_customer_code": customer_code or row.get("provider_customer_code"),
        "provider_subscription_code": code or row.get("provider_subscription_code"),
        "provider_subscription_email_token": email_token or row.get("provider_subscription_email_token"),
        "provider_plan_code": plan_code or row.get("provider_plan_code"),
        "checkout_reference": row.get("checkout_reference"),
        "current_period_start": row.get("current_period_start"),
        "current_period_end": data.get("next_payment_date") or row.get("current_period_end"),
        "cancel_at_period_end": bool(row.get("cancel_at_period_end")),
        "last_payment_at": row.get("last_payment_at"),
        "last_event_at": _now(),
        "updated_at": _now(),
    }
    db.table("user_subscriptions").upsert(update, on_conflict="user_id").execute()


@router.get("/status")
def billing_status(user: CurrentUser = Depends(get_current_user), db: Client = Depends(get_supabase)):
    entitlement = get_user_entitlements(db, user.id)
    current = _current_subscription(db, user.id)
    settings = get_settings()
    billing_configured = bool(
        settings.paystack_secret_key
        and all(getattr(settings, item["setting"], None) for item in PLANS.values())
    )
    return {
        **entitlement,
        "billing_provider": "paystack" if billing_configured else None,
        "checkout_enabled": billing_configured,
        "subscription": {
            "provider_subscription_code": current.get("provider_subscription_code"),
            "current_period_start": current.get("current_period_start"),
            "current_period_end": current.get("current_period_end"),
            "cancel_at_period_end": current.get("cancel_at_period_end", False),
        } if current else None,
    }


@router.post("/developer/preview")
def set_developer_preview(body: DeveloperPreviewRequest, user: CurrentUser = Depends(get_current_user), db: Client = Depends(get_supabase)):
    """Change the effective plan only for an account explicitly allowlisted by a migration."""
    current = _current_subscription(db, user.id)
    if not current or not current.get("developer_access"):
        raise HTTPException(status_code=403, detail="Developer preview is not enabled for this account.")
    db.table("user_subscriptions").update({
        "developer_preview_plan": body.plan,
        "updated_at": _now(),
    }).eq("user_id", user.id).eq("developer_access", True).execute()
    return get_user_entitlements(db, user.id)


@router.post("/checkout")
def start_checkout(body: CheckoutRequest, user: CurrentUser = Depends(get_current_user), db: Client = Depends(get_supabase)):
    current = get_user_entitlements(db, user.id)
    if current.get("developer_access"):
        raise HTTPException(status_code=403, detail="Developer accounts are exempt from paid checkout.")
    if not user.email or not user.email_verified:
        raise HTTPException(status_code=422, detail="Verify your email address before starting paid checkout.")
    current = get_user_entitlements(db, user.id)
    if current["paid"]:
        raise HTTPException(status_code=409, detail="An active paid subscription already exists. Cancel or change your current plan first.")
    secret = _secret()
    plan_code = _plan_code(body.plan, body.billing_period)
    configured_plan = PLANS[(body.plan, body.billing_period)]
    reference = "tg_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")
    intent = {
        "reference": reference,
        "user_id": user.id,
        "user_email": user.email.strip().lower(),
        "plan": body.plan,
        "billing_period": body.billing_period,
        "provider_plan_code": plan_code,
        "expected_amount": int(configured_plan["amount"]),
        "currency": CURRENCY,
        "status": "pending",
    }
    inserted = db.table("billing_checkout_intents").insert(intent).execute()
    if not inserted.data:
        raise HTTPException(status_code=500, detail="The checkout intent could not be created. No payment was started.")
    settings = get_settings()
    payload = {
        "email": user.email,
        "plan": plan_code,
        "reference": reference,
        "callback_url": settings.app_base_url.rstrip("/") + "/settings?billing=return",
        "metadata": {
            "product": "TradeGrid",
            "user_id": user.id,
            "plan": body.plan,
            "billing_period": body.billing_period,
        },
    }
    try:
        result = _paystack_request("POST", "/transaction/initialize", secret, payload)
        data = result.get("data") or {}
        authorization_url = data.get("authorization_url")
        if not authorization_url or not str(authorization_url).startswith("https://"):
            raise RuntimeError("invalid_authorization_url")
        db.table("billing_checkout_intents").update({
            "status": "initialized",
            "authorization_url": authorization_url,
        }).eq("reference", reference).execute()
        return {"reference": reference, "authorization_url": authorization_url, "plan": body.plan, "billing_period": body.billing_period}
    except Exception:
        db.table("billing_checkout_intents").update({
            "status": "failed",
            "safe_error_code": "provider_initialization_failed",
        }).eq("reference", reference).execute()
        raise HTTPException(status_code=502, detail="The payment provider could not initialize checkout. No subscription was activated.") from None


@router.post("/cancel")
def cancel_subscription(user: CurrentUser = Depends(get_current_user), db: Client = Depends(get_supabase)):
    current = _current_subscription(db, user.id)
    if not current or current.get("status") not in ("active", "trialing"):
        raise HTTPException(status_code=409, detail="There is no active subscription to cancel.")
    if current.get("cancel_at_period_end"):
        return {"status": current.get("status"), "cancel_at_period_end": True, "message": "Cancellation is already scheduled."}
    code = current.get("provider_subscription_code")
    email_token = current.get("provider_subscription_email_token")
    if not code or not email_token:
        raise HTTPException(status_code=409, detail="Paystack has not supplied the subscription cancellation token yet. Contact support; no cancellation was claimed.")
    secret = _secret()
    _paystack_request("POST", "/subscription/disable", secret, {"code": code, "token": email_token})
    db.table("user_subscriptions").update({
        "cancel_at_period_end": True,
        "last_event_at": _now(),
        "updated_at": _now(),
    }).eq("user_id", user.id).execute()
    return {"status": current.get("status"), "cancel_at_period_end": True, "message": "Cancellation request accepted by Paystack. Access remains active until provider confirmation or the current paid period ends."}


@router.post("/webhook")
async def paystack_webhook(request: Request, db: Client = Depends(get_supabase)):
    settings = get_settings()
    secret = settings.paystack_secret_key
    if not secret:
        raise HTTPException(status_code=503, detail="Paystack webhooks are disabled until billing is configured.")
    raw = await request.body()
    signature = request.headers.get("x-paystack-signature")
    if not _signed_webhook(raw, signature, secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")

    import json
    try:
        event = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid webhook JSON.") from None
    if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    event_type = str(event.get("event") or "unknown")
    data = event["data"]
    identity = data.get("id") or data.get("reference") or data.get("subscription_code") or hashlib.sha256(raw).hexdigest()
    event_key = hashlib.sha256((event_type + ":" + str(identity)).encode("utf-8")).hexdigest()

    try:
        db.table("billing_webhook_events").upsert({
            "event_key": event_key,
            "event_type": event_type,
            "processing_status": "received",
            "safe_error_code": None,
            "attempts": 1,
        }, on_conflict="event_key", ignore_duplicates=True).execute()
        event_row_result = db.table("billing_webhook_events").select("processing_status,attempts").eq("event_key", event_key).limit(1).execute()
        event_row = event_row_result.data[0] if event_row_result.data else {}
        if event_row.get("processing_status") == "processed":
            return {"received": True, "duplicate": True}
        db.table("billing_webhook_events").update({
            "attempts": int(event_row.get("attempts") or 1) + (1 if event_row.get("processing_status") == "failed" else 0),
            "processing_status": "received",
            "safe_error_code": None,
        }).eq("event_key", event_key).execute()

        if event_type == "charge.success":
            reference = str(data.get("reference") or "")
            if not reference:
                raise RuntimeError("reference_missing")
            _apply_verified_charge(db, reference, secret, data)
        elif event_type == "subscription.create":
            _link_subscription_event(db, data)
        elif event_type in ("subscription.disable", "invoice.payment_failed"):
            nested_subscription = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
            code = str(data.get("subscription_code") or nested_subscription.get("subscription_code") or "")
            row = _find_subscription_by_code(db, code)
            if row:
                if event_type == "subscription.disable":
                    end = (
                        _parse_provider_datetime(data.get("next_payment_date"))
                        or _parse_provider_datetime(nested_subscription.get("next_payment_date"))
                        or _parse_provider_datetime(row.get("current_period_end"))
                    )
                    # Stop renewal immediately, but preserve already-paid access
                    # through the stored period end when Paystack supplies one.
                    paid_through_end = bool(end and end > datetime.now(timezone.utc))
                    new_status = "active" if paid_through_end else "cancelled"
                    update = {
                        "status": new_status,
                        "cancel_at_period_end": paid_through_end,
                        "last_event_at": _now(),
                        "updated_at": _now(),
                    }
                    if end:
                        update["current_period_end"] = end.isoformat()
                else:
                    update = {
                        "status": "past_due",
                        "cancel_at_period_end": False,
                        "last_event_at": _now(),
                        "updated_at": _now(),
                    }
                db.table("user_subscriptions").update(update).eq("user_id", row["user_id"]).execute()
        elif event_type == "subscription.not_renew":
            code = str(data.get("subscription_code") or "")
            row = _find_subscription_by_code(db, code)
            if row:
                db.table("user_subscriptions").update({
                    "cancel_at_period_end": True,
                    "current_period_end": data.get("next_payment_date") or row.get("current_period_end"),
                    "last_event_at": _now(),
                    "updated_at": _now(),
                }).eq("user_id", row["user_id"]).execute()

        db.table("billing_webhook_events").update({
            "processing_status": "processed",
            "processed_at": _now(),
            "safe_error_code": None,
        }).eq("event_key", event_key).execute()
        return {"received": True}
    except Exception as exc:
        error_code = str(exc) if str(exc) in {
            "checkout_intent_not_found", "transaction_not_verified", "transaction_currency_mismatch",
            "transaction_amount_invalid", "transaction_amount_mismatch", "transaction_plan_mismatch",
            "transaction_email_mismatch", "reference_missing", "subscription_link_not_found", "recurring_subscription_not_linked", "recurring_plan_invalid",
        } else "webhook_processing_failed"
        db.table("billing_webhook_events").update({
            "processing_status": "failed",
            "safe_error_code": error_code,
        }).eq("event_key", event_key).execute()
        raise HTTPException(status_code=502, detail="The signed payment event could not be verified and applied. Provider delivery may be retried.") from None
