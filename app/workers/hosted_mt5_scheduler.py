"""In-process read-only hosted MT5 sync scheduler for the single-instance Render API."""
from __future__ import annotations

import asyncio
import logging
import os

from app.workers.hosted_mt5_sync import run_once

log = logging.getLogger("tradvoryx.hosted_mt5.scheduler")

SAFE_CODES = {
    "target_account_id_required",
    "max_history_chunks_must_be_between_1_and_50",
    "worker_run_health_record_could_not_be_created",
    "target_account_not_found",
    "target_account_disappeared_during_sync",
    "target_account_status_unavailable",
    "target_account_id_mismatch",
    "target_account_not_hosted",
    "provider_account_id_missing",
    "target_account_disconnected",
    "target_account_status_invalid",
    "demo_accounts_are_not_allowed",
    "target_account_is_not_headway",
    "target_account_is_not_live",
    "target_account_identity_missing",
    "account_sync_failed_after_retries",
    "backfill_incomplete_more_runs_required",
    "configuration_invalid: check required Actions secrets and key lengths",
}


async def run_scheduler(account_id: str) -> None:
    """Run a bounded history/snapshot sync immediately and then at a safe interval.

    The worker validates the selected account as Headway REAL/LIVE before contacting
    MetaApi. All provider operations are read-only; no order placement API is used.
    """
    interval = max(300, int(os.getenv("HOSTED_MT5_WORKER_INTERVAL_SECONDS", "600")))
    max_chunks = max(1, min(2, int(os.getenv("HOSTED_MT5_MAX_HISTORY_CHUNKS", "1"))))
    account_ref = account_id[-8:]
    # Let Uvicorn finish serving health/API requests before synchronous Supabase
    # calls inside the legacy worker can briefly occupy the event loop.
    await asyncio.sleep(15)
    while True:
        try:
            log.info("Scheduled hosted MT5 sync starting for account reference ...%s", account_ref)
            await run_once(account_id=account_id, max_chunks=max_chunks, max_attempts=2)
            log.info("Scheduled hosted MT5 sync finished for account reference ...%s", account_ref)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            candidate = str(exc)
            code = candidate if candidate in SAFE_CODES or candidate.startswith("missing_required_configuration: ") else "scheduled_worker_run_failed"
            log.error("Scheduled hosted MT5 sync failed safely: error_code=%s account_reference=...%s", code, account_ref)
        await asyncio.sleep(interval)
