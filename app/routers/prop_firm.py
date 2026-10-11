from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from supabase import Client

from app.db import get_supabase
from app.deps import require_account_owner
from app.schemas import PropFirmRulesIn
from app.services.analytics import _closed_trades
from app.services.prop_rules import evaluate_prop_status
from app.services.entitlements import require_account_feature

router = APIRouter(prefix="/api/v1/accounts", tags=["prop-firm rules"])


def _get_account(db: Client, account_id: str) -> dict:
    result = (
        db.table("trading_accounts")
        .select("*")
        .eq("id", account_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Trading account not found.")
    return result.data[0]


def _latest_rules(db: Client, account_id: str) -> dict | None:
    result = (
        db.table("prop_firm_rules")
        .select("*")
        .eq("account_id", account_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _latest_position_data(db: Client, account_id: str) -> tuple[dict | None, list[dict] | None]:
    snapshot_result = (
        db.table("mt5_position_snapshots")
        .select("id,snapshot_at,currency,is_complete")
        .eq("account_id", account_id)
        .eq("is_complete", True)
        .order("snapshot_at", desc=True)
        .limit(1)
        .execute()
    )
    if not snapshot_result.data:
        return None, None
    snapshot = snapshot_result.data[0]
    positions_result = (
        db.table("mt5_open_positions")
        .select("mt5_position_id,symbol,direction,volume,unrealized_profit,swap,stop_loss,risk_to_stop,opened_at")
        .eq("account_id", account_id)
        .eq("snapshot_id", snapshot["id"])
        .execute()
    )
    return snapshot, positions_result.data or []


def _check_rules(body: PropFirmRulesIn) -> None:
    for label, value, mode in (
        ("daily_drawdown_limit", body.daily_drawdown_limit, body.daily_drawdown_mode),
        ("max_drawdown_limit", body.max_drawdown_limit, body.max_drawdown_mode),
    ):
        if value is not None and mode == "percentage" and value > 100:
            raise HTTPException(status_code=422, detail=f"{label} cannot exceed 100% when its mode is percentage.")


@router.get("/{account_id}/prop-rules")
def get_prop_rules(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    account = _get_account(db, account_id)
    require_account_feature(db, account_id, "prop_firm_cockpit")
    return {
        "account_id": account_id,
        "account_label": account.get("label"),
        "account_type": account.get("account_type"),
        "currency": account.get("currency"),
        "prop_firm": account.get("prop_firm"),
        "challenge_phase": account.get("challenge_phase"),
        "rules": _latest_rules(db, account_id),
    }


@router.put("/{account_id}/prop-rules")
def save_prop_rules(
    body: PropFirmRulesIn,
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    _check_rules(body)
    account = _get_account(db, account_id)
    require_account_feature(db, account_id, "prop_firm_cockpit")
    if body.prop_firm is not None and account.get("account_type") not in ("prop_evaluation", "prop_funded", "prop"):
        # Do not silently change the account type; allow setting a prop firm on
        # a custom account, but require an explicit prop account type for clarity.
        if account.get("account_type") != "custom":
            raise HTTPException(status_code=422, detail="Select a prop-firm evaluation/funded account type before assigning prop-firm rules.")

    account_update = {}
    if "prop_firm" in body.model_fields_set:
        account_update["prop_firm"] = body.prop_firm
    if "challenge_phase" in body.model_fields_set:
        account_update["challenge_phase"] = body.challenge_phase
    if account_update:
        db.table("trading_accounts").update(account_update).eq("id", account_id).execute()

    row = body.model_dump(exclude={"prop_firm", "challenge_phase"})
    row["account_id"] = account_id
    row["created_at"] = datetime.now(timezone.utc).isoformat()
    result = db.table("prop_firm_rules").insert(row).execute()
    if not result.data:
        raise HTTPException(status_code=500, detail="Prop-firm rules could not be saved.")
    return {
        "account_id": account_id,
        "prop_firm": body.prop_firm if "prop_firm" in body.model_fields_set else account.get("prop_firm"),
        "challenge_phase": body.challenge_phase if "challenge_phase" in body.model_fields_set else account.get("challenge_phase"),
        "rules": result.data[0],
    }


@router.get("/{account_id}/prop-status")
def get_prop_status(
    account_id: str = Depends(require_account_owner),
    db: Client = Depends(get_supabase),
):
    account = _get_account(db, account_id)
    require_account_feature(db, account_id, "prop_firm_cockpit")
    rules = _latest_rules(db, account_id)
    if not rules:
        return evaluate_prop_status(account, None, [], None, None)
    snapshot, positions = _latest_position_data(db, account_id)
    trades = _closed_trades(db, account_id)
    return evaluate_prop_status(
        account,
        rules,
        trades,
        positions,
        snapshot.get("snapshot_at") if snapshot else None,
    )
