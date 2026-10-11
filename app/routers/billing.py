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


def _apply_verified_charge(db: Client, reference: str, secret: str) -> None:
    intent_result = (
        db.table("billing_checkout_intents")
        .select("*")
        .eq("reference", reference)
        .limit(1)
        .execute()
    )
    if not intent_result.data:
        # A valid provider transaction must still be tied to a TradeGrid-created intent.
        raise RuntimeError("checkout_intent_not_found")
    intent = intent_result.data[0]
    if intent.get("status") == "succeeded":
        return

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
    if amount != int(intent["expected_amount"]):
        raise RuntimeError("transaction_amount_mismatch")

    remote_plan = transaction.get("plan")
    if isinstance(remote_plan, dict):
        remote_plan_code = remote_plan.get("plan_code")
        if remote_plan_code and str(remote_plan_code) != str(intent["provider_plan_code"]):
            raise RuntimeError("transaction_plan_mismatch")

    customer = transaction.get("customer") if isinstance(transaction.get("customer"), dict) else {}
    customer_code = str(customer.get("customer_code") or intent.get("provider_customer_code") or "")
    incoming_email = str(customer.get("email") or "").strip().lower()
    expected_email = str(intent.get("user_email") or "").strip().lower()
    if incoming_email and expected_email and incoming_email != expected_email:
        raise RuntimeError("transaction_email_mismatch")

    old = _current_subscription(db, str(intent["user_id"])) or {}
    subscription = transaction.get("subscription") if isinstance(transaction.get("subscription"), dict) else {}
    next_payment_date = subscription.get("next_payment_date") or transaction.get("next_payment_date")
    plan_row = {
        "user_id": intent["user_id"],
        "plan": intent["plan"],
        "status": "active",
        "billing_period": intent["billing_period"],
        "provider": "paystack",
        "provider_customer_code": customer_code or old.get("provider_customer_code"),
        "provider_subscription_code": str(subscription.get("subscription_code") or old.get("provider_subscription_code") or "") or None,
        "provider_subscription_email_token": str(subscription.get("email_token") or old.get("provider_subscription_email_token") or "") or None,
        "provider_plan_code": intent["provider_plan_code"],
        "checkout_reference": reference,
        "current_period_start": _now(),
        "current_period_end": next_payment_date,
        "cancel_at_period_end": False,
        "last_payment_at": _now(),
        "last_event_at": _now(),
        "updated_at": _now(),
    }
    db.table("user_subscriptions").upsert(plan_row, on_conflict="user_id").execute()
    db.table("billing_checkout_intents").update({
        "status": "succeeded",
        "provider_customer_code": customer_code or None,
        "completed_at": _now(),
        "safe_error_code": None,
    }).eq("reference", reference).execute()


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


@router.post("/checkout")
def start_checkout(body: CheckoutRequest, user: CurrentUser = Depends(get_current_user), db: Client = Depends(get_supabase)):
    if not user.email:
        raise HTTPException(status_code=422, detail="A verified email address is required before paid checkout.")
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
            _apply_verified_charge(db, reference, secret)
        elif event_type == "subscription.create":
            _link_subscription_event(db, data)
        elif event_type in ("subscription.disable", "invoice.payment_failed"):
            code = str(data.get("subscription_code") or data.get("subscription", {}).get("subscription_code") if isinstance(data.get("subscription"), dict) else data.get("subscription_code") or "")
            row = _find_subscription_by_code(db, code)
            if row:
                new_status = "cancelled" if event_type == "subscription.disable" else "past_due"
                db.table("user_subscriptions").update({
                    "status": new_status,
                    "cancel_at_period_end": event_type == "subscription.disable",
                    "last_event_at": _now(),
                    "updated_at": _now(),
                }).eq("user_id", row["user_id"]).execute()
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
            "transaction_email_mismatch", "reference_missing", "subscription_link_not_found",
        } else "webhook_processing_failed"
        db.table("billing_webhook_events").update({
            "processing_status": "failed",
            "safe_error_code": error_code,
        }).eq("event_key", event_key).execute()
        raise HTTPException(status_code=502, detail="The signed payment event could not be verified and applied. Provider delivery may be retried.") from None
