from pathlib import Path

from pa_server.store import Store


REQUEST = {
    "source": "tradingview", "exchange": "OKX", "symbol": "ETHUSDT",
    "timeframe": "15m", "bar_count": 100, "extended_session": False,
}


def test_watch_idempotency_and_job_deduplication(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    watch, created = store.create_watch(REQUEST, "v1", "same-key")
    again, created_again = store.create_watch(REQUEST, "v1", "same-key")
    assert created is True
    assert created_again is False
    assert again["id"] == watch["id"]

    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    assert store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1") == 1
    assert store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1") == 0


def test_running_work_is_requeued_after_restart(tmp_path: Path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    job, _ = store.create_manual_job(REQUEST, "v1", None)
    assert store.claim_job()["id"] == job["id"]
    recovered = Store(path)
    assert recovered.claim_job()["id"] == job["id"]


def test_success_and_notification_are_committed_together(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    job, _ = store.create_manual_job(REQUEST, "v1", None)
    claimed = store.claim_job()
    store.finish_job(claimed["id"], {"notification_status": "pending"}, {"ok": True}, True)
    note = store.claim_notification()
    assert note["job_id"] == job["id"]
    store.finish_notification(note["id"], "stale")
    result = store.get_job(job["id"])["result"]
    assert result["notification_status"] == "stale"
    assert result["stale"] is True


def test_paused_watch_holds_queue_and_delete_cancels_it(tmp_path: Path):
    store = Store(tmp_path / "db.sqlite3")
    watch, _ = store.create_watch(REQUEST, "v1", None)
    snapshot = [{"seq": 1, "ts_open": 1000, "open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "amount": 0, "pct_chg": None, "closed": True}]
    store.enqueue_watch_jobs(watch["id"], REQUEST, [(1000, snapshot)], "v1")
    store.set_watch_state(watch["id"], "paused")
    assert store.claim_job() is None
    store.set_watch_state(watch["id"], "active")
    assert store.claim_job()["watch_id"] == watch["id"]

    store.enqueue_watch_jobs(watch["id"], REQUEST, [(2000, snapshot)], "v1")
    store.delete_watch(watch["id"])
    jobs = store.list_analyses(watch["id"], 10, 0)
    deleted_job = next(item for item in jobs if item["target_ts"] == 2000)
    assert deleted_job["error"]["code"] == "watch_deleted"
