import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import accounts, backtesting, billing, connector, growth, hosted_mt5, journal, overview, prop_firm, risk

logger = logging.getLogger("tradvoryx.hosted_mt5.scheduler")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    worker_task = None
    account_id = os.getenv("HOSTED_MT5_ACCOUNT_ID", "").strip()
    if account_id:
        from app.workers.hosted_mt5_scheduler import run_scheduler
        worker_task = asyncio.create_task(run_scheduler(account_id))
        logger.info("Hosted MT5 scheduler enabled for account reference ...%s", account_id[-8:])
    else:
        logger.warning("Hosted MT5 scheduler is disabled: HOSTED_MT5_ACCOUNT_ID is not configured.")
    try:
        yield
    finally:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Tradvoryx API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts.router)
app.include_router(backtesting.router)
app.include_router(connector.router)
app.include_router(overview.router)
app.include_router(journal.router)
app.include_router(growth.router)
app.include_router(prop_firm.router)
app.include_router(risk.router)
app.include_router(hosted_mt5.router)
app.include_router(billing.router)


@app.get("/health")
def health():
    return {"status": "ok"}
