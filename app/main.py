from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import accounts, connector, growth, journal, overview, prop_firm, risk

settings = get_settings()

app = FastAPI(title="Trading Intelligence API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(accounts.router)
app.include_router(connector.router)
app.include_router(overview.router)
app.include_router(journal.router)
app.include_router(growth.router)
app.include_router(prop_firm.router)
app.include_router(risk.router)


@app.get("/health")
def health():
    return {"status": "ok"}
