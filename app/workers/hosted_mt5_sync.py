"""Bounded hosted MT5 history synchronizer for one explicitly selected MetaApi-hosted MT5 account (demo or live).

Run through the scheduled GitHub Actions hosted history workflow after configuring
repository secrets. The worker requires an exact target account UUID:
  python -m app.workers.hosted_mt5_sync --account-id <trading_accounts UUID>

The job performs read-only RPC operations only. Broker passwords are handled by
MetaApi at account provisioning time; Tradvoryx never stores them.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from metaapi_cloud_sdk import MetaApi
from supabase import Client

from app.config import get_settings
from app.db import get_supabase
from app.services.aggregation import aggregate_position
from app.services.metaapi_adapter import fetch_all_deals_for_range, map_metaapi_deal, map_metaapi_position, parse_provider_time

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradvoryx.hosted_mt5")

INITIAL_LOOKBACK_DAYS = max(365, int(os.getenv("METAAPI_INITIAL_LOOKBACK_DAYS", "10000")))
HISTORY_CHUNK_DAYS = max(1, int(os.getenv("METAAPI_HISTORY_CHUNK_DAYS", "180")))
MAX_HISTORY_CHUNKS_PER_RUN = max(1, min(50, int(os.getenv("METAAPI_MAX_HISTORY_CHUNKS_PER_RUN", "10"))))
INCREMENTAL_OVERLAP_DAYS = max(1, int(os.getenv("METAAPI_SYNC_OVERLAP_DAYS", "2")))
POSITION_SNAPSHOT_STALE_SECONDS = max(60, int(os.getenv("METAAPI_POSITION_SNAPSHOT_MAX_AGE_SECONDS", "300")))


def _time(value: Any) -> datetime | None:
    try:
        return parse_provider_time(value) if value else None
    except (ValueError, TypeError, OverflowError):
        return None


def _currency(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe_status_error(db: Client, account_id: str, code: str) -> None:
    # Fixed allow-listed codes only; never persist arbitrary provider exception text.
    allowed = {
        "provider_account_lookup_failed",
        "provider_deployment_forbidden",
        "provider_connection_failed",
        "account_identity_mismatch",
        "history_sync_failed",
        "history_response_limit_reached",
        "position_snapshot_failed",
        "database_write_failed",
    }
    db.table("trading_accounts").update({
        "hosted_connection_status": "error",
        "hosted_last_error_code": code if code in allowed else "provider_connection_failed",
        "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", account_id).execute()
    db.table("trading_accounts").update({"connection_status": "error"}).eq("id", account_id).execute()


def _validate_hosted_account(row: dict, expected_account_id: str) -> None:
    """Validate the exact connected MetaApi account without restricting broker or demo/live type."""
    if str(row.get("id") or "") != expected_account_id:
        raise ValueError("target_account_id_mismatch")
    if str(row.get("connection_provider") or "").lower() != "metaapi":
        raise ValueError("target_account_not_hosted")
    if not str(row.get("provider_account_id") or "").strip():
        raise ValueError("provider_account_id_missing")
    status = str(row.get("hosted_connection_status") or "").lower()
    if status == "disconnected":
        raise ValueError("target_account_disconnected")
    if status not in {"provisioning", "connecting", "connected", "syncing", "error"}:
        raise ValueError("target_account_status_invalid")
    if not str(row.get("mt5_login") or "").strip() or not str(row.get("server") or "").strip():
        raise ValueError("target_account_identity_missing")


def _latest_position_snapshot(db: Client, account_id: str) -> dict | None:
    result = (
        db.table("mt5_position_snapshots")
        .select("id,snapshot_at,currency,balance,equity,is_complete")
        .eq("account_id", account_id)
        .eq("is_complete", True)
        .order("snapshot_at", desc=True)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def _upload_cash_flows(db: Client, account_id: str, cash_flows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(cash_flows), 1000):
        rows = []
        for cash_flow in cash_flows[offset:offset + 1000]:
            row = dict(cash_flow)
            row.update({"account_id": account_id, "source": "mt5"})
            rows.append(row)
        if rows:
            db.table("portfolio_cash_flows").upsert(
                rows, on_conflict="account_id,external_id", ignore_duplicates=True
            ).execute()


def _upload_deals(db: Client, account_id: str, deals: list[dict[str, Any]]) -> dict[str, int]:
    """Write deals idempotently in bounded batches and report existing/duplicate IDs."""
    seen_ids: set[str] = set()
    unique_deals: list[dict[str, Any]] = []
    duplicate_input_count = 0
    for deal in deals:
        deal_id = str(deal.get("mt5_deal_id") or "").strip()
        if not deal_id:
            raise ValueError("provider_deal_id_missing")
        if deal_id in seen_ids:
            duplicate_input_count += 1
            continue
        seen_ids.add(deal_id)
        unique_deals.append(deal)
    position_ids: set[str] = set()
    inserted_estimate = 0
    already_present = 0
    batch_size = 500
    total_batches = (len(unique_deals) + batch_size - 1) // batch_size
    for offset in range(0, len(unique_deals), batch_size):
        batch_number = offset // batch_size + 1
        source_batch = unique_deals[offset:offset + batch_size]
        ids = [str(deal["mt5_deal_id"]) for deal in source_batch]
        existing_result = (
            db.table("raw_deals")
            .select("mt5_deal_id")
            .eq("account_id", account_id)
            .in_("mt5_deal_id", ids)
            .execute()
        )
        existing_ids = {
            str(item["mt5_deal_id"]) for item in (existing_result.data or [])
            if item.get("mt5_deal_id") is not None
        }
        already_present += len(existing_ids)
        inserted_estimate += len(ids) - len(existing_ids)
        batch = []
        for deal in source_batch:
            row = dict(deal)
            row["account_id"] = account_id
            row["raw_payload"] = dict(deal)
            batch.append(row)
            position_ids.add(str(deal["mt5_position_id"]))
        db.table("raw_deals").upsert(
            batch, on_conflict="account_id,mt5_deal_id", ignore_duplicates=True
        ).execute()
        log.info(
            "History write batch %d/%d: deals_in_batch=%d existing_before_write=%d new_id_estimate=%d",
            batch_number, total_batches, len(batch), len(existing_ids), len(ids) - len(existing_ids),
        )
    for index, position_id in enumerate(sorted(position_ids), start=1):
        aggregate_position(db, account_id, position_id)
        if index % 100 == 0 or index == len(position_ids):
            log.info("Position aggregation progress: positions_checked=%d/%d", index, len(position_ids))
    return {
        "unique_deals": len(unique_deals),
        "duplicate_input_ids": duplicate_input_count,
        "already_present": already_present,
        "new_id_estimate": inserted_estimate,
        "positions_reconciled": len(position_ids),
    }


def _write_position_snapshot(
    db: Client, account: dict, info: dict, remote_positions: list[dict], captured_at: datetime
) -> int:
    account_id = str(account["id"])
    currency = _currency(info.get("currency"))
    saved_currency = _currency(account.get("currency"))
    remote_login = str(info.get("login") or "").strip()
    remote_server = str(info.get("server") or "").strip()
    saved_login = str(account.get("mt5_login") or "").strip()
    saved_server = str(account.get("server") or "").strip()

    if saved_login and remote_login and saved_login != remote_login:
        raise ValueError("account_identity_mismatch")
    if saved_server and remote_server and saved_server.casefold() != remote_server.casefold():
        raise ValueError("account_identity_mismatch")
    if saved_currency and currency and saved_currency != currency:
        raise ValueError("account_identity_mismatch")
    if not currency:
        raise ValueError("account_identity_mismatch")

    snapshot_result = db.table("mt5_position_snapshots").insert({
        "account_id": account_id,
        "snapshot_at": captured_at.isoformat(),
        "currency": currency,
        "balance": float(info.get("balance") or 0),
        "equity": float(info.get("equity") or 0),
        "is_complete": False,
    }).execute()
    if not snapshot_result.data:
        raise RuntimeError("database_write_failed")
    snapshot_id = snapshot_result.data[0]["id"]

    normalized_positions = []
    for position in remote_positions or []:
        mapped = map_metaapi_position(position)
        if mapped is not None:
            mapped.update({"snapshot_id": snapshot_id, "account_id": account_id})
            normalized_positions.append(mapped)

    try:
        for offset in range(0, len(normalized_positions), 1000):
            db.table("mt5_open_positions").insert(normalized_positions[offset:offset + 1000]).execute()
        marked = (
            db.table("mt5_position_snapshots")
            .update({"is_complete": True})
            .eq("id", snapshot_id)
            .eq("account_id", account_id)
            .execute()
        )
        if not marked.data:
            raise RuntimeError("database_write_failed")
    except Exception:
        # Incomplete snapshot remains excluded; previous complete state is preserved.
        raise

    # Keep only the current-state snapshot for this account.
    db.table("mt5_position_snapshots").delete().eq("account_id", account_id).neq("id", snapshot_id).execute()
    db.table("trading_accounts").update({
        "connection_status": "connected",
        "last_sync_at": captured_at.isoformat(),
        "current_balance": float(info.get("balance") or 0),
        "current_equity": float(info.get("equity") or 0),
    }).eq("id", account_id).execute()
    db.table("portfolio_snapshots").upsert({
        "account_id": account_id,
        "snapshot_date": captured_at.date().isoformat(),
        "captured_at": captured_at.isoformat(),
        "balance": float(info.get("balance") or 0),
        "equity": float(info.get("equity") or 0),
        "currency": currency,
        "source": "mt5_connector",
    }, on_conflict="account_id,snapshot_date").execute()
    return len(normalized_positions)


async def _sync_one(api: MetaApi, db: Client, row: dict, now: datetime, max_chunks: int) -> bool:
    account_id = str(row["id"])
    provider_id = str(row.get("provider_account_id") or "")
    if not provider_id:
        _safe_status_error(db, account_id, "provider_account_lookup_failed")
        return False

    remote = None
    connection = None
    stage = "provider_account_lookup"
    try:
        remote = await api.metatrader_account_api.get_account(provider_id)
        provider_state = str(getattr(remote, "state", "") or "").upper()
        provider_connection_status = str(
            getattr(remote, "connection_status", getattr(remote, "connectionStatus", "")) or ""
        ).upper()
        # DEPLOYED is not sufficient: the provider can report a disconnected
        # terminal. In that case explicitly redeploy the existing account to
        # restart its API server; do not create a second provider account.
        disconnected_states = {
            "DISCONNECTED", "DISCONNECTED_FROM_BROKER", "DISCONNECTING", "UNDEPLOYED"
        }
        if provider_state == "DEPLOYED" and provider_connection_status in disconnected_states:
            log.info(
                "Requesting MetaApi redeploy: provider_state=%s connection_status=%s",
                provider_state[:40] or "unknown",
                provider_connection_status[:40] or "unknown",
            )
            stage = "provider_redeploy"
            await remote.redeploy()
        elif provider_state not in {"DEPLOYED", "CONNECTED", "DEPLOYING"}:
            log.info(
                "Requesting MetaApi deploy: provider_state=%s connection_status=%s",
                provider_state[:40] or "unknown",
                provider_connection_status[:40] or "unknown",
            )
            stage = "provider_deploy"
            await remote.deploy()
        db.table("trading_accounts").update({
            "hosted_connection_status": "connecting",
            "hosted_last_error_code": None,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()
        stage = "provider_wait_connected"
        connect_timeout = max(30, min(300, int(os.getenv("METAAPI_CONNECT_TIMEOUT_SECONDS", "120"))))
        log.info(
            "MetaApi connection wait started: provider_state=%s connection_status=%s timeout_seconds=%d",
            str(getattr(remote, "state", "unknown") or "unknown")[:40],
            str(getattr(remote, "connection_status", getattr(remote, "connectionStatus", "unknown")) or "unknown")[:40],
            connect_timeout,
        )
        try:
            await asyncio.wait_for(remote.wait_connected(), timeout=connect_timeout)
        except asyncio.TimeoutError:
            # Refresh provider state for diagnostics without logging provider messages or credentials.
            try:
                current_remote = await api.metatrader_account_api.get_account(provider_id)
                current_state = str(getattr(current_remote, "state", "unknown") or "unknown")[:40]
                current_status = str(
                    getattr(current_remote, "connection_status", getattr(current_remote, "connectionStatus", "unknown")) or "unknown"
                )[:40]
                log.warning(
                    "MetaApi connection timed out: provider_state=%s connection_status=%s timeout_seconds=%d",
                    current_state, current_status, connect_timeout,
                )
            except Exception as diagnostic_exc:
                log.warning(
                    "MetaApi connection timed out and status refresh failed: exception_type=%s",
                    type(diagnostic_exc).__name__,
                )
            raise RuntimeError("provider_connection_failed") from None

        stage = "rpc_connect"
        connection = remote.get_rpc_connection()
        await connection.connect()
        stage = "rpc_wait_synchronized"
        await connection.wait_synchronized()

        stage = "account_information"
        info = await connection.get_account_information()
        if not isinstance(info, dict):
            raise ValueError("account_identity_mismatch")
        saved_login = str(row.get("mt5_login") or "").strip()
        incoming_login = str(info.get("login") or "").strip()
        saved_server = str(row.get("server") or "").strip()
        incoming_server = str(info.get("server") or "").strip()
        if (
            (saved_login and incoming_login and saved_login != incoming_login)
            or (saved_server and incoming_server and saved_server.casefold() != incoming_server.casefold())
            or (_currency(row.get("currency")) and _currency(info.get("currency")) != _currency(row.get("currency")))
        ):
            raise ValueError("account_identity_mismatch")

        db.table("trading_accounts").update({
            "hosted_connection_status": "syncing",
            "hosted_connection_verified_at": datetime.now(timezone.utc).isoformat(),
            "hosted_last_error_code": None,
            "hosted_status_updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", account_id).execute()

        complete = bool(row.get("hosted_initial_backfill_complete"))
        raw_cursor = row.get("hosted_history_cursor_at")
        cursor = _time(raw_cursor) if raw_cursor else None
        if cursor is None:
            cursor = now - timedelta(days=INITIAL_LOOKBACK_DAYS)
        elif complete:
            cursor = max(cursor - timedelta(days=INCREMENTAL_OVERLAP_DAYS), now - timedelta(days=INCREMENTAL_OVERLAP_DAYS))

        first_deal = _time(row.get("hosted_first_deal_at"))
        last_deal = _time(row.get("hosted_last_deal_at"))
        chunks_done = 0
        deals_uploaded_total = 0

        while cursor < now and chunks_done < max_chunks:
            window_end = min(cursor + timedelta(days=HISTORY_CHUNK_DAYS), now)
            # SDK RPC expects naive UTC timestamps. The adapter recursively
            # subdivides a saturated provider response before this cursor advances.
            provider_start = cursor.astimezone(timezone.utc).replace(tzinfo=None)
            provider_end = window_end.astimezone(timezone.utc).replace(tzinfo=None)
            log.info(
                "History window %d/%d started: start_utc=%s end_utc=%s",
                chunks_done + 1, max_chunks, cursor.isoformat(), window_end.isoformat(),
            )
            stage = "history_fetch"
            fetched = await fetch_all_deals_for_range(connection, provider_start, provider_end)
            if fetched is None:
                raise RuntimeError("history_sync_failed")

            directions: dict[str, str] = {}
            ordered = sorted(
                fetched,
                key=lambda deal: (
                    _time(deal.get("time") or deal.get("dealTime") if isinstance(deal, dict) else None)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    str(deal.get("id") or deal.get("ticket") or "") if isinstance(deal, dict) else "",
                ),
            )
            normalized_deals: list[dict[str, Any]] = []
            cash_flows: list[dict[str, Any]] = []
            observed_times: list[datetime] = []
            skipped_unrecognized = 0
            for deal in ordered:
                if not isinstance(deal, dict):
                    continue
                deal_time = _time(deal.get("time") or deal.get("dealTime"))
                if deal_time:
                    observed_times.append(deal_time)
                normalized, flow = map_metaapi_deal(deal, directions)
                if normalized is not None:
                    normalized_deals.append(normalized)
                elif flow is None:
                    skipped_unrecognized += 1
                if flow is not None:
                    cash_flows.append(flow)
            _upload_cash_flows(db, account_id, cash_flows)
            write_stats = _upload_deals(db, account_id, normalized_deals)
            deals_uploaded_total += write_stats["new_id_estimate"]
            log.info(
                "History window parsed: provider_rows=%d normalized_deals=%d cash_flows=%d skipped_unrecognized=%d "
                "duplicate_ids_in_response=%d already_present_estimate=%d new_deal_id_estimate=%d",
                len(fetched), len(normalized_deals), len(cash_flows), skipped_unrecognized,
                write_stats["duplicate_input_ids"], write_stats["already_present"], write_stats["new_id_estimate"],
            )
            if observed_times:
                first_observed = min(observed_times)
                last_observed = max(observed_times)
                first_deal = min(filter(None, [first_deal, first_observed]))
                last_deal = max(filter(None, [last_deal, last_observed]))

            # Cursor advances only after all writes for the window have succeeded.
            cursor = window_end
            chunks_done += 1
            current_now = datetime.now(timezone.utc)
            complete = cursor >= now
            db.table("trading_accounts").update({
                "hosted_history_cursor_at": cursor.isoformat(),
                "hosted_initial_backfill_complete": complete,
                "hosted_first_deal_at": first_deal.isoformat() if first_deal else None,
                "hosted_last_deal_at": last_deal.isoformat() if last_deal else None,
                "hosted_last_sync_at": current_now.isoformat(),
                "hosted_connection_status": "syncing" if not complete else "connected",
                "hosted_last_error_code": None,
                "hosted_status_updated_at": current_now.isoformat(),
            }).eq("id", account_id).execute()
            log.info(
                "History checkpoint saved: chunks_completed=%d cursor_utc=%s backfill_complete=%s",
                chunks_done, cursor.isoformat(), complete,
            )

        # Position sync always runs, even when this job is only part-way through backfill.
        stage = "positions_fetch"
        positions = await connection.get_positions()
        stage = "position_snapshot_write"
        _write_position_snapshot(db, row, info, positions or [], datetime.now(timezone.utc))
        final_status = "connected" if complete else "syncing"
        done = datetime.now(timezone.utc)
        db.table("trading_accounts").update({
            "hosted_connection_status": final_status,
            "hosted_last_sync_at": done.isoformat(),
            "hosted_last_error_code": None,
            "hosted_status_updated_at": done.isoformat(),
        }).eq("id", account_id).execute()
        log.info(
            "Hosted MT5 sync account_reference=...%s status=%s chunks=%s newly_seen_deal_ids_estimate=%s positions=%s backfill_complete=%s",
            account_id[-8:], final_status, chunks_done, deals_uploaded_total, len(positions or []), complete,
        )
        return True
    except Exception as exc:
        known_code = str(exc) if str(exc) in {
            "account_identity_mismatch", "history_sync_failed", "history_response_limit_reached", "position_snapshot_failed", "database_write_failed"
        } else None
        if type(exc).__name__ == "ForbiddenException" and stage in {"provider_deploy", "provider_redeploy"}:
            known_code = "provider_deployment_forbidden"
        stage_codes = {
            "provider_account_lookup": "provider_account_lookup_failed",
            "provider_deploy": "provider_deployment_forbidden",
            "provider_redeploy": "provider_deployment_forbidden",
            "provider_wait_connected": "provider_connection_failed",
            "rpc_connect": "provider_connection_failed",
            "rpc_wait_synchronized": "provider_connection_failed",
            "account_information": "provider_connection_failed",
            "history_fetch": "history_sync_failed",
            "positions_fetch": "position_snapshot_failed",
            "position_snapshot_write": "database_write_failed",
        }
        exc_code = known_code or stage_codes.get(stage, "provider_connection_failed")
        _safe_status_error(db, account_id, exc_code)
        # Log only the operation and exception class; never log provider/request text.
        log.warning(
            "Hosted MT5 sync failed for account_id=%s stage=%s error_code=%s exception_type=%s",
            account_id, stage, exc_code, type(exc).__name__,
        )
        return False
    finally:
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass


async def run_once(account_id: str, max_chunks: int = MAX_HISTORY_CHUNKS_PER_RUN, max_attempts: int = 2) -> None:
    if not account_id or not account_id.strip():
        raise RuntimeError("target_account_id_required")
    if max_chunks < 1 or max_chunks > 50:
        raise RuntimeError("max_history_chunks_must_be_between_1_and_50")

    required = ["METAAPI_TOKEN", "SUPABASE_URL", "CONNECTOR_TOKEN_PEPPER"]
    if not (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")):
        required.append("SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_ROLE_KEY)")
    missing = [name for name in required if not os.getenv(name.split(" ")[0])]
    if missing:
        raise RuntimeError("missing_required_configuration: " + ", ".join(missing))
    try:
        settings = get_settings()
    except Exception:
        # Pydantic validation errors can echo context; never log environment values.
        raise RuntimeError("configuration_invalid: check required Actions secrets and key lengths") from None
    if not settings.metaapi_token:
        raise RuntimeError("missing_required_configuration: METAAPI_TOKEN")

    db = get_supabase()
    run_result = db.table("hosted_mt5_worker_runs").insert({
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }).execute()
    if not run_result.data:
        raise RuntimeError("worker_run_health_record_could_not_be_created")
    run_id = run_result.data[0]["id"]
    target_label = account_id[-8:]
    succeeded = 0
    failed = 0
    complete = False
    safe_error = None
    run_status = "running"
    log.info("Hosted MT5 worker started: target_reference=...%s max_history_chunks=%d", target_label, max_chunks)
    try:
        target_result = (
            db.table("trading_accounts")
            .select("*")
            .eq("id", account_id)
            .limit(1)
            .execute()
        )
        if not target_result.data:
            raise RuntimeError("target_account_not_found")
        row = target_result.data[0]
        _validate_hosted_account(row, account_id)
        log.info(
            "Eligible account discovered: target_reference=...%s broker=%s server=%s status=%s",
            target_label,
            str(row.get("broker") or "unknown")[:60],
            str(row.get("server") or "unknown")[:80],
            str(row.get("hosted_connection_status") or "unknown"),
        )
        api = MetaApi(settings.metaapi_token)
        ok = False
        sync_attempted = False
        for attempt in range(1, max_attempts + 1):
            # Reload durable checkpoints before retrying a failed provider/database operation.
            refreshed = (
                db.table("trading_accounts")
                .select("*")
                .eq("id", account_id)
                .limit(1)
                .execute()
            )
            if not refreshed.data:
                raise RuntimeError("target_account_disappeared_during_sync")
            row = refreshed.data[0]
            _validate_hosted_account(row, account_id)
            log.info("Sync attempt %d/%d started for target_reference=...%s", attempt, max_attempts, target_label)
            sync_attempted = True
            ok = await _sync_one(api=api, db=db, row=row, now=datetime.now(timezone.utc), max_chunks=max_chunks)
            if ok:
                break
            if attempt < max_attempts:
                delay = min(5 * attempt, 15)
                log.warning("Sync attempt failed; retrying safely after %d seconds (attempt %d/%d).", delay, attempt + 1, max_attempts)
                await asyncio.sleep(delay)
        if not ok:
            failed = 1
            safe_error = "account_sync_failed_after_retries"
            raise RuntimeError(safe_error)

        final_query = (
            db.table("trading_accounts")
            .select("hosted_initial_backfill_complete,hosted_history_cursor_at,hosted_first_deal_at,hosted_last_deal_at,hosted_last_sync_at")
            .eq("id", account_id)
            .limit(1)
            .execute()
        )
        if not final_query.data:
            raise RuntimeError("target_account_status_unavailable")
        final_row = final_query.data[0]
        complete = bool(final_row.get("hosted_initial_backfill_complete"))
        succeeded = 1
        run_status = "succeeded" if complete else "partial"
        safe_error = None if complete else "backfill_incomplete_more_runs_required"
        log.info(
            "Sync attempt completed: target_reference=...%s backfill_complete=%s cursor_utc=%s earliest_observed_deal=%s latest_observed_deal=%s",
            target_label, complete, final_row.get("hosted_history_cursor_at"),
            final_row.get("hosted_first_deal_at"), final_row.get("hosted_last_deal_at"),
        )
        if not complete:
            log.warning("This bounded run completed successfully, but the initial lookback is incomplete. Rerun the workflow to continue from the saved cursor.")
        else:
            log.info("The configured lookback window has been scanned. This does not prove the broker/provider retains every lifetime deal.")
        log.info("Imported deal history is distinct from sync-time balance/equity snapshots; the worker does not reconstruct historical equity.")
    except Exception as exc:
        failed = 1 if succeeded == 0 and locals().get("sync_attempted", False) else failed
        run_status = "failed"
        if safe_error is None:
            candidate = str(exc)
            known_codes = {
                "target_account_id_required", "max_history_chunks_must_be_between_1_and_50",
                "configuration_invalid_check_required_Actions_secrets_and_key_lengths",
                "configuration_invalid: check required Actions secrets and key lengths",
                "worker_run_health_record_could_not_be_created", "target_account_not_found",
                "target_account_disappeared_during_sync", "target_account_status_unavailable",
                "target_account_id_mismatch", "target_account_not_hosted", "provider_account_id_missing",
                "target_account_disconnected", "target_account_status_invalid",
                "target_account_identity_missing",
                "provider_deployment_forbidden", "provider_deployment_forbidden", "account_sync_failed_after_retries", "backfill_incomplete_more_runs_required",
            }
            safe_error = candidate if candidate in known_codes else "worker_run_failed"
        log.error("Hosted MT5 run failed safely: error_code=%s target_reference=...%s", safe_error, target_label)
        raise RuntimeError(safe_error) from None
    finally:
        db.table("hosted_mt5_worker_runs").update({
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": run_status,
            "accounts_seen": 1 if succeeded or failed else 0,
            "accounts_succeeded": succeeded,
            "accounts_failed": failed,
            "safe_error_code": safe_error,
        }).eq("id", run_id).execute()
        log.info(
            "Hosted MT5 worker finished: run_status=%s accounts_seen=%d accounts_succeeded=%d accounts_failed=%d",
            run_status, 1 if succeeded or failed else 0, succeeded, failed,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import history for one explicitly selected MetaApi-hosted MT5 account (demo or live).")
    parser.add_argument("--account-id", required=True, help="Exact Supabase trading_accounts UUID for the MetaApi-hosted MT5 profile.")
    parser.add_argument(
        "--max-history-chunks", type=int, default=MAX_HISTORY_CHUNKS_PER_RUN,
        help="Maximum bounded history windows this invocation processes (1-50; default is 10).",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run_once(account_id=args.account_id, max_chunks=args.max_history_chunks))
    except Exception as exc:
        # Print only allow-listed error codes; never dump provider/database exceptions or secrets.
        candidate = str(exc)
        safe_codes = {
            "target_account_id_required", "max_history_chunks_must_be_between_1_and_50",
            "worker_run_health_record_could_not_be_created", "target_account_not_found",
            "target_account_disappeared_during_sync", "target_account_status_unavailable",
            "target_account_id_mismatch", "target_account_not_hosted", "provider_account_id_missing",
            "target_account_disconnected", "target_account_status_invalid",
            "target_account_is_not_headway", "target_account_identity_missing",
            "account_sync_failed_after_retries", "backfill_incomplete_more_runs_required",
            "configuration_invalid: check required Actions secrets and key lengths",
        }
        if candidate.startswith("missing_required_configuration: "):
            safe = candidate if all(
                name in candidate
                for name in ("METAAPI_TOKEN", "SUPABASE_URL", "CONNECTOR_TOKEN_PEPPER")
                if name in candidate
            ) else "missing_required_configuration"
        else:
            safe = candidate if candidate in safe_codes else "worker_run_failed_check_configuration_database_and_provider_status"
        log.error("Workflow should fail: error_code=%s", safe)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
