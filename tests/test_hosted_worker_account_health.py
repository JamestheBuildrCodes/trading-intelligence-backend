from pathlib import Path


def test_worker_health_is_scoped_to_each_account():
    route = Path(__file__).parents[1] / "app" / "routers" / "hosted_mt5.py"
    source = route.read_text(encoding="utf-8")
    assert "def _worker_health(db: Client, account_id: str)" in source
    assert '.eq("account_id", account_id)' in source
    worker = Path(__file__).parents[1] / "app" / "workers" / "hosted_mt5_sync.py"
    worker_source = worker.read_text(encoding="utf-8")
    assert '"account_id": account_id' in worker_source
