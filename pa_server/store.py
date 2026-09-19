from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


DEFAULT_TRADING_SETTINGS = {
    "enabled": False,
    "profile": "demo",
    "api_region": "global",
    "margin_mode": "isolated",
    "leverage": 1,
    "sizing_mode": "fixed_notional",
    "fixed_notional_usdt": 0.0,
    "fixed_margin_usdt": 0.0,
    "risk_percent": 0.0,
    "max_notional_usdt": 0.0,
    "pending_expiry_bars": 3,
    "poll_interval_seconds": 2,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._last_watch_id: str | None = None
        self._init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS watches (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, exchange TEXT NOT NULL,
              symbol TEXT NOT NULL, timeframe TEXT NOT NULL, bar_count INTEGER NOT NULL,
              extended_session INTEGER NOT NULL, state TEXT NOT NULL,
              last_seen_ts INTEGER, settings_version TEXT NOT NULL, error TEXT,
              last_success_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, watch_id TEXT REFERENCES watches(id), target_ts INTEGER,
              request_json TEXT NOT NULL, snapshot_json TEXT, settings_version TEXT NOT NULL,
              status TEXT NOT NULL, progress TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              result_json TEXT, record_json TEXT, error_json TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE(watch_id, target_ts)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
              key TEXT PRIMARY KEY, kind TEXT NOT NULL, resource_id TEXT NOT NULL,
              request_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_watch ON jobs(watch_id, target_ts);
            CREATE INDEX IF NOT EXISTS idx_notifications_queue ON notifications(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS trading_settings (
              id INTEGER PRIMARY KEY CHECK(id=1), settings_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_tasks (
              id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
              watch_id TEXT NOT NULL REFERENCES watches(id), status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
              result_json TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trade_tasks_queue
              ON trade_tasks(status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS trade_events (
              id TEXT PRIMARY KEY, event TEXT NOT NULL, data_json TEXT NOT NULL,
              notify_status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL, last_error TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_trade_events_notify
              ON trade_events(notify_status, next_attempt_at);
            CREATE TABLE IF NOT EXISTS trade_actions (
              key TEXT PRIMARY KEY, action TEXT NOT NULL, thesis_id INTEGER NOT NULL,
              status TEXT NOT NULL, result_json TEXT, error TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(watches)").fetchall()}
            if "last_success_at" not in columns:
                db.execute("ALTER TABLE watches ADD COLUMN last_success_at TEXT")
            if "trading_enabled" not in columns:
                db.execute("ALTER TABLE watches ADD COLUMN trading_enabled INTEGER NOT NULL DEFAULT 0")
            if "okx_instrument" not in columns:
                db.execute("ALTER TABLE watches ADD COLUMN okx_instrument TEXT NOT NULL DEFAULT ''")
            db.execute(
                "INSERT OR IGNORE INTO trading_settings(id,settings_json,updated_at) VALUES (1,?,?)",
                (json.dumps(DEFAULT_TRADING_SETTINGS), utc_now()),
            )
            db.execute("UPDATE jobs SET status='queued', progress='recovered' WHERE status='running'")
            db.execute("UPDATE notifications SET status='pending' WHERE status='sending'")
            db.execute("UPDATE trade_tasks SET status='pending' WHERE status='running'")
            db.execute("UPDATE trade_events SET notify_status='pending' WHERE notify_status='sending'")

    @staticmethod
    def decode(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        item = dict(row)
        for key in tuple(item):
            if key.endswith("_json"):
                value = item.pop(key)
                item[key[:-5]] = json.loads(value) if value else None
        if "extended_session" in item:
            item["extended_session"] = bool(item["extended_session"])
        if "trading_enabled" in item:
            item["trading_enabled"] = bool(item["trading_enabled"])
        return item

    def create_watch(self, request: dict, version: str, key: str | None) -> tuple[dict, bool]:
        canonical = json.dumps(request, sort_keys=True, ensure_ascii=False)
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                old = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
                if old:
                    if old["kind"] != "watch" or old["request_json"] != canonical:
                        raise ValueError("Idempotency-Key was used with different parameters")
                    return self.decode(db.execute("SELECT * FROM watches WHERE id=?", (old["resource_id"],)).fetchone()), False
            watch_id, stamp = str(uuid.uuid4()), utc_now()
            trading_enabled = int(request.get("trading_enabled", False))
            okx_instrument = str(request.get("okx_instrument") or "").upper()
            if trading_enabled and okx_instrument:
                conflict = db.execute(
                    "SELECT id FROM watches WHERE deleted_at IS NULL AND trading_enabled=1 "
                    "AND okx_instrument=?",
                    (okx_instrument,),
                ).fetchone()
                if conflict:
                    raise ValueError("This OKX contract is already assigned to another trading watch")
            db.execute("""INSERT INTO watches
                       (id,source,exchange,symbol,timeframe,bar_count,extended_session,state,
                        last_seen_ts,settings_version,error,last_success_at,created_at,updated_at,deleted_at,
                        trading_enabled,okx_instrument)
                       VALUES (?, 'tradingview', ?, ?, ?, ?, ?, 'active', NULL, ?, NULL, NULL, ?, ?, NULL,?,?)""",
                       (watch_id, request["exchange"], request["symbol"], request["timeframe"],
                        request["bar_count"], int(request.get("extended_session", False)),
                        version, stamp, stamp, trading_enabled, okx_instrument))
            if key:
                db.execute("INSERT INTO idempotency VALUES (?, 'watch', ?, ?)", (key, watch_id, canonical))
            return self.decode(db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()), True

    def list_watches(self, include_deleted: bool = False) -> list[dict]:
        sql = "SELECT * FROM watches" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY created_at"
        with self.connect() as db:
            return [self.decode(r) for r in db.execute(sql).fetchall()]

    def get_watch(self, watch_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
            item = self.decode(row)
            if item:
                item["queued_count"] = db.execute("SELECT COUNT(*) FROM jobs WHERE watch_id=? AND status IN ('queued','running')", (watch_id,)).fetchone()[0]
                latest = db.execute("SELECT result_json FROM jobs WHERE watch_id=? AND status='succeeded' ORDER BY target_ts DESC LIMIT 1", (watch_id,)).fetchone()
                item["latest_result"] = json.loads(latest[0]) if latest and latest[0] else None
            return item

    def set_watch_state(self, watch_id: str, state: str, error: str | None = None) -> bool:
        with self.connect() as db:
            cur = db.execute("UPDATE watches SET state=?, error=?, updated_at=? WHERE id=? AND deleted_at IS NULL", (state, error, utc_now(), watch_id))
            return cur.rowcount > 0

    def update_watch_trading(
        self, watch_id: str, *, enabled: bool, okx_instrument: str,
    ) -> bool:
        instrument = okx_instrument.strip().upper()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if enabled:
                conflict = db.execute(
                    "SELECT id FROM watches WHERE id<>? AND deleted_at IS NULL "
                    "AND trading_enabled=1 AND okx_instrument=?",
                    (watch_id, instrument),
                ).fetchone()
                if conflict:
                    raise ValueError("This OKX contract is already assigned to another trading watch")
            cur = db.execute(
                "UPDATE watches SET trading_enabled=?,okx_instrument=?,updated_at=? "
                "WHERE id=? AND deleted_at IS NULL",
                (int(enabled), instrument, utc_now(), watch_id),
            )
            return cur.rowcount > 0

    def delete_watch(self, watch_id: str) -> bool:
        stamp = utc_now()
        with self.connect() as db:
            cur = db.execute("UPDATE watches SET state='deleted', deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL", (stamp, stamp, watch_id))
            if cur.rowcount:
                error = json.dumps({"code": "watch_deleted", "message": "Watch was deleted before analysis started"})
                db.execute("UPDATE jobs SET status='failed',progress='finished',error_json=?,updated_at=? WHERE watch_id=? AND status='queued'", (error, stamp, watch_id))
            return cur.rowcount > 0

    def enqueue_watch_jobs(self, watch_id: str, request: dict, snapshots: list[tuple[int, list[dict]]], version: str) -> int:
        stamp, created = utc_now(), 0
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for target_ts, snapshot in snapshots:
                job_id = str(uuid.uuid4())
                cur = db.execute("""INSERT OR IGNORE INTO jobs
                    (id,watch_id,target_ts,request_json,snapshot_json,settings_version,status,progress,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,'queued','queued',?,?)""",
                    (job_id, watch_id, target_ts, json.dumps(request, ensure_ascii=False),
                     json.dumps(snapshot, ensure_ascii=False), version, stamp, stamp))
                created += cur.rowcount
            if snapshots:
                db.execute("UPDATE watches SET last_seen_ts=?, error=NULL, updated_at=? WHERE id=?", (snapshots[-1][0], stamp, watch_id))
        return created

    def create_manual_job(self, request: dict, version: str, key: str | None) -> tuple[dict, bool]:
        canonical, stamp = json.dumps(request, sort_keys=True, ensure_ascii=False), utc_now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if key:
                old = db.execute("SELECT * FROM idempotency WHERE key=?", (key,)).fetchone()
                if old:
                    if old["kind"] != "analysis" or old["request_json"] != canonical:
                        raise ValueError("Idempotency-Key was used with different parameters")
                    return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (old["resource_id"],)).fetchone()), False
            job_id = str(uuid.uuid4())
            db.execute("""INSERT INTO jobs
                (id,watch_id,target_ts,request_json,snapshot_json,settings_version,status,progress,created_at,updated_at)
                VALUES (?,NULL,NULL,?,NULL,?,'queued','queued',?,?)""", (job_id, canonical, version, stamp, stamp))
            if key:
                db.execute("INSERT INTO idempotency VALUES (?, 'analysis', ?, ?)", (key, job_id, canonical))
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()), True

    def claim_job(self) -> dict | None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT j.* FROM jobs j LEFT JOIN watches w ON w.id=j.watch_id "
                "WHERE j.status='queued' AND (j.watch_id IS NULL OR w.state='active') "
                "ORDER BY CASE WHEN j.watch_id IS NOT NULL AND j.watch_id=? THEN 1 ELSE 0 END, j.created_at, j.rowid LIMIT 1",
                (self._last_watch_id,),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running', progress='starting', attempts=attempts+1, updated_at=? WHERE id=?", (utc_now(), row["id"]))
            self._last_watch_id = row["watch_id"]
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def update_job_progress(self, job_id: str, progress: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET progress=?, updated_at=? WHERE id=?", (progress, utc_now(), job_id))

    def set_job_snapshot(self, job_id: str, target_ts: int, snapshot: list[dict]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET target_ts=?,snapshot_json=?,updated_at=? WHERE id=?",
                (target_ts, json.dumps(snapshot, ensure_ascii=False), utc_now(), job_id),
            )

    def finish_job(
        self, job_id: str, result: dict, record: dict, notify: bool, trade: bool = False,
    ) -> None:
        stamp = utc_now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='succeeded',progress='finished',result_json=?,record_json=?,error_json=NULL,updated_at=? WHERE id=?",
                       (json.dumps(result, ensure_ascii=False), json.dumps(record, ensure_ascii=False), stamp, job_id))
            db.execute("UPDATE watches SET last_success_at=?,updated_at=? WHERE id=(SELECT watch_id FROM jobs WHERE id=?)", (stamp, stamp, job_id))
            if notify:
                db.execute("INSERT OR IGNORE INTO notifications VALUES (?,?,'pending',0,?,NULL,?,?)", (str(uuid.uuid4()), job_id, stamp, stamp, stamp))
            if trade:
                watch_id = db.execute("SELECT watch_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
                db.execute(
                    "INSERT OR IGNORE INTO trade_tasks "
                    "(id,job_id,watch_id,status,attempts,next_attempt_at,result_json,last_error,created_at,updated_at) "
                    "VALUES (?,?,?,'pending',0,?,NULL,NULL,?,?)",
                    (str(uuid.uuid4()), job_id, watch_id, stamp, stamp, stamp),
                )

    def fail_job(self, job_id: str, code: str, message: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='failed',progress='finished',error_json=?,updated_at=? WHERE id=?", (json.dumps({"code": code, "message": message}, ensure_ascii=False), utc_now(), job_id))

    def get_job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_analyses(self, watch_id: str, limit: int, offset: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE watch_id=? ORDER BY target_ts DESC LIMIT ? OFFSET ?", (watch_id, limit, offset)).fetchall()
            return [self.decode(r) for r in rows]

    def previous_record(self, watch_id: str, target_ts: int, version: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT record_json FROM jobs WHERE watch_id=? AND target_ts<? AND settings_version=? AND status='succeeded' AND record_json IS NOT NULL ORDER BY target_ts DESC LIMIT 1", (watch_id, target_ts, version)).fetchone()
            return json.loads(row[0]) if row else None

    def claim_notification(self) -> dict | None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM notifications WHERE status='pending' AND next_attempt_at<=? ORDER BY created_at LIMIT 1", (utc_now(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE notifications SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (utc_now(), row["id"]))
            return dict(db.execute("SELECT * FROM notifications WHERE id=?", (row["id"],)).fetchone())

    def finish_notification(self, notification_id: str, status: str, error: str | None = None, next_attempt: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE notifications SET status=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?", (status, error, next_attempt or utc_now(), utc_now(), notification_id))
            row = db.execute("SELECT job_id FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if row:
                job = db.execute("SELECT result_json FROM jobs WHERE id=?", (row[0],)).fetchone()
                if job and job[0]:
                    result = json.loads(job[0])
                    result["notification_status"] = status
                    if status == "stale":
                        result["stale"] = True
                    db.execute("UPDATE jobs SET result_json=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), utc_now(), row[0]))

    def get_trading_settings(self) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT settings_json FROM trading_settings WHERE id=1").fetchone()
            return {**DEFAULT_TRADING_SETTINGS, **(json.loads(row[0]) if row else {})}

    def save_trading_settings(self, settings: dict) -> dict:
        value = {**DEFAULT_TRADING_SETTINGS, **settings}
        with self.connect() as db:
            db.execute(
                "UPDATE trading_settings SET settings_json=?,updated_at=? WHERE id=1",
                (json.dumps(value, ensure_ascii=False), utc_now()),
            )
        return value

    def claim_trade_task(self) -> dict | None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM trade_tasks WHERE status='pending' AND next_attempt_at<=? "
                "ORDER BY created_at LIMIT 1",
                (utc_now(),),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE trade_tasks SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
            return self.decode(db.execute("SELECT * FROM trade_tasks WHERE id=?", (row["id"],)).fetchone())

    def finish_trade_task(
        self, task_id: str, status: str, *, result: dict | None = None,
        error: str | None = None, next_attempt: str | None = None,
    ) -> None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE trade_tasks SET status=?,result_json=?,last_error=?,next_attempt_at=?,updated_at=? "
                "WHERE id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    next_attempt or utc_now(),
                    utc_now(),
                    task_id,
                ),
            )
            row = db.execute(
                "SELECT job_id FROM trade_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                job = db.execute(
                    "SELECT result_json FROM jobs WHERE id=?", (row["job_id"],)
                ).fetchone()
                if job and job["result_json"]:
                    analysis_result = json.loads(job["result_json"])
                    analysis_result["trading_status"] = status
                    if result is not None:
                        analysis_result["trading_result"] = result
                    if error:
                        analysis_result["trading_error"] = error
                    db.execute(
                        "UPDATE jobs SET result_json=?,updated_at=? WHERE id=?",
                        (
                            json.dumps(analysis_result, ensure_ascii=False),
                            utc_now(),
                            row["job_id"],
                        ),
                    )

    def list_trade_tasks(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [
                self.decode(row) for row in db.execute(
                    "SELECT * FROM trade_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def record_trade_event(self, event: str, data: dict) -> str:
        event_id, stamp = str(uuid.uuid4()), utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO trade_events VALUES (?,?,?,'pending',0,?,NULL,?,?)",
                (event_id, event, json.dumps(data, ensure_ascii=False), stamp, stamp, stamp),
            )
        return event_id

    def list_trade_events(self, limit: int = 100) -> list[dict]:
        with self.connect() as db:
            return [
                self.decode(row) for row in db.execute(
                    "SELECT * FROM trade_events ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            ]

    def claim_trade_event_notification(self) -> dict | None:
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM trade_events WHERE notify_status='pending' AND next_attempt_at<=? "
                "ORDER BY created_at LIMIT 1",
                (utc_now(),),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE trade_events SET notify_status='sending',attempts=attempts+1,updated_at=? "
                "WHERE id=?",
                (utc_now(), row["id"]),
            )
            return self.decode(db.execute("SELECT * FROM trade_events WHERE id=?", (row["id"],)).fetchone())

    def finish_trade_event_notification(
        self, event_id: str, status: str, error: str | None = None,
        next_attempt: str | None = None,
    ) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE trade_events SET notify_status=?,last_error=?,next_attempt_at=?,updated_at=? "
                "WHERE id=?",
                (status, error, next_attempt or utc_now(), utc_now(), event_id),
            )

    def begin_trade_action(self, key: str, action: str, thesis_id: int) -> tuple[dict, bool]:
        stamp = utc_now()
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM trade_actions WHERE key=?", (key,)).fetchone()
            if row:
                item = self.decode(row)
                if item["action"] != action or int(item["thesis_id"]) != thesis_id:
                    raise ValueError("Idempotency-Key was used with different parameters")
                return item, False
            db.execute(
                "INSERT INTO trade_actions VALUES (?,?,?,'running',NULL,NULL,?,?)",
                (key, action, thesis_id, stamp, stamp),
            )
            return self.decode(db.execute("SELECT * FROM trade_actions WHERE key=?", (key,)).fetchone()), True

    def finish_trade_action(
        self, key: str, status: str, result: dict | None = None, error: str | None = None,
    ) -> dict:
        with self.connect() as db:
            db.execute(
                "UPDATE trade_actions SET status=?,result_json=?,error=?,updated_at=? WHERE key=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    utc_now(),
                    key,
                ),
            )
            return self.decode(db.execute("SELECT * FROM trade_actions WHERE key=?", (key,)).fetchone())

    def health_counts(self) -> dict:
        with self.connect() as db:
            return {
                "active_watches": db.execute("SELECT COUNT(*) FROM watches WHERE state='active' AND deleted_at IS NULL").fetchone()[0],
                "queued_jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0],
                "running_jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0],
                "pending_notifications": db.execute("SELECT COUNT(*) FROM notifications WHERE status IN ('pending','sending')").fetchone()[0],
                "pending_trade_tasks": db.execute("SELECT COUNT(*) FROM trade_tasks WHERE status IN ('pending','running')").fetchone()[0],
            }
