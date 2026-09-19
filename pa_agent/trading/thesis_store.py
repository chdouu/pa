"""SQLite persistence for OKX theses, revisions, orders and fills."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ACTIVE_STATES = ("PENDING_SUBMIT", "PENDING_ENTRY", "OPEN", "EXITING", "RECONCILE")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ThesisStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _init_schema(self) -> None:
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS theses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    watch_id TEXT NOT NULL DEFAULT '',
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    source_bar_ts INTEGER NOT NULL,
                    last_bar_ts INTEGER NOT NULL,
                    elapsed_bars INTEGER NOT NULL DEFAULT 0,
                    entry_zone_low TEXT NOT NULL,
                    entry_zone_high TEXT NOT NULL,
                    original_entry TEXT NOT NULL,
                    current_entry TEXT NOT NULL,
                    original_stop TEXT NOT NULL,
                    current_stop TEXT NOT NULL,
                    tp1 TEXT NOT NULL,
                    tp2 TEXT NOT NULL,
                    current_tp1 TEXT NOT NULL DEFAULT '',
                    current_tp2 TEXT NOT NULL DEFAULT '',
                    total_size TEXT NOT NULL,
                    tp1_size TEXT NOT NULL,
                    tp2_size TEXT NOT NULL,
                    filled_size TEXT NOT NULL DEFAULT '0',
                    protected_size TEXT NOT NULL DEFAULT '0',
                    position_seen INTEGER NOT NULL DEFAULT 0,
                    zero_position_checks INTEGER NOT NULL DEFAULT 0,
                    entry_order_id TEXT NOT NULL DEFAULT '',
                    entry_algo_id TEXT NOT NULL DEFAULT '',
                    cl_ord_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL DEFAULT '',
                    confidence INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_thesis
                    ON theses(account_id, profile, inst_id)
                    WHERE status IN ('PENDING_SUBMIT','PENDING_ENTRY','OPEN','EXITING','RECONCILE');
                CREATE TABLE IF NOT EXISTS thesis_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thesis_id INTEGER NOT NULL REFERENCES theses(id),
                    bar_ts INTEGER NOT NULL,
                    timeframe TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(thesis_id, bar_ts, action)
                );
                CREATE TABLE IF NOT EXISTS exchange_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thesis_id INTEGER NOT NULL REFERENCES theses(id),
                    role TEXT NOT NULL,
                    ord_id TEXT NOT NULL DEFAULT '',
                    algo_id TEXT NOT NULL DEFAULT '',
                    client_id TEXT NOT NULL DEFAULT '',
                    size TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    protection_leg TEXT NOT NULL DEFAULT '',
                    trigger_price TEXT NOT NULL DEFAULT '',
                    stop_price TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thesis_id INTEGER NOT NULL REFERENCES theses(id),
                    trade_id TEXT NOT NULL,
                    ord_id TEXT NOT NULL,
                    price TEXT NOT NULL,
                    size TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(thesis_id, trade_id)
                );
                CREATE TABLE IF NOT EXISTS tp_amendments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thesis_id INTEGER NOT NULL REFERENCES theses(id),
                    bar_ts INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target_price TEXT NOT NULL,
                    req_id TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(thesis_id, bar_ts, action)
                );
            """)
            self._ensure_column(db, "theses", "current_tp1", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "theses", "current_tp2", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "theses", "watch_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "exchange_orders", "protection_leg", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "exchange_orders", "trigger_price", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "exchange_orders", "stop_price", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "tp_amendments", "reason", "TEXT NOT NULL DEFAULT ''")
            db.execute("UPDATE theses SET current_tp1=tp1 WHERE current_tp1='' OR current_tp1 IS NULL")
            db.execute("UPDATE theses SET current_tp2=tp2 WHERE current_tp2='' OR current_tp2 IS NULL")

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        existing = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def active(self, profile: str, inst_id: str, account_id: str = "") -> dict[str, Any] | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        with self._db() as db:
            row = db.execute(
                f"SELECT * FROM theses WHERE account_id=? AND profile=? AND inst_id=? AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (account_id, profile, inst_id, *ACTIVE_STATES),
            ).fetchone()
            return dict(row) if row else None

    def active_all(self, profile: str | None = None, account_id: str | None = None) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        sql = f"SELECT * FROM theses WHERE status IN ({placeholders})"
        args: tuple[Any, ...] = ACTIVE_STATES
        if profile:
            sql += " AND profile=?"
            args = (*args, profile)
        if account_id:
            sql += " AND account_id=?"
            args = (*args, account_id)
        with self._db() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY id", args)]

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM theses ORDER BY id DESC LIMIT ?", (limit,)
            )]

    def latest(self, account_id: str, profile: str, inst_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM theses WHERE account_id=? AND profile=? AND inst_id=? "
                "ORDER BY id DESC LIMIT 1", (account_id, profile, inst_id),
            ).fetchone()
            return dict(row) if row else None

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        data = {**values, "created_at": now, "updated_at": now}
        data.setdefault("current_tp1", str(data.get("tp1") or ""))
        data.setdefault("current_tp2", str(data.get("tp2") or ""))
        columns = ",".join(data)
        params = ",".join("?" for _ in data)
        with self._db() as db:
            cur = db.execute(
                f"INSERT INTO theses ({columns}) VALUES ({params})", tuple(data.values())
            )
            row = db.execute("SELECT * FROM theses WHERE id=?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def update(self, thesis_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignment = ",".join(f"{key}=?" for key in values)
        with self._db() as db:
            db.execute(
                f"UPDATE theses SET {assignment} WHERE id=?", (*values.values(), thesis_id)
            )

    def revision(
        self, thesis_id: int, bar_ts: int, timeframe: str,
        decision: dict[str, Any], action: str,
    ) -> bool:
        with self._db() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO thesis_revisions "
                "(thesis_id,bar_ts,timeframe,decision_json,action,created_at) VALUES (?,?,?,?,?,?)",
                (thesis_id, bar_ts, timeframe, json.dumps(decision, ensure_ascii=False), action, utc_now()),
            )
            return cur.rowcount > 0

    def order(
        self, thesis_id: int, role: str, *, ord_id: str = "", algo_id: str = "",
        client_id: str = "", size: str = "", state: str = "", raw: dict | None = None,
        protection_leg: str = "", trigger_price: str = "", stop_price: str = "",
    ) -> None:
        now = utc_now()
        with self._db() as db:
            db.execute(
                "INSERT INTO exchange_orders "
                "(thesis_id,role,ord_id,algo_id,client_id,size,state,protection_leg,"
                "trigger_price,stop_price,raw_json,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (thesis_id, role, ord_id, algo_id, client_id, size, state,
                 protection_leg, trigger_price, stop_price,
                 json.dumps(raw or {}, ensure_ascii=False), now, now),
            )

    def orders(self, thesis_id: int) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM exchange_orders WHERE thesis_id=? ORDER BY id", (thesis_id,)
            )]

    def mark_protection_canceled(self, thesis_id: int, algo_ids: list[str]) -> None:
        if not algo_ids:
            return
        placeholders = ",".join("?" for _ in algo_ids)
        with self._db() as db:
            db.execute(
                "UPDATE exchange_orders SET state='canceled',updated_at=? "
                f"WHERE thesis_id=? AND role='PROTECTION' AND algo_id IN ({placeholders})",
                (utc_now(), thesis_id, *algo_ids),
            )

    def update_order(self, order_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignment = ",".join(f"{key}=?" for key in values)
        with self._db() as db:
            db.execute(
                f"UPDATE exchange_orders SET {assignment} WHERE id=?",
                (*values.values(), order_id),
            )

    def queue_tp_amendment(
        self, thesis_id: int, bar_ts: int, action: str, target_price: str, req_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO tp_amendments "
                "(thesis_id,bar_ts,action,target_price,req_id,reason,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,'PENDING',?,?)",
                (thesis_id, bar_ts, action, target_price, req_id, reason, now, now),
            )
            row = db.execute(
                "SELECT * FROM tp_amendments WHERE thesis_id=? AND bar_ts=? AND action=?",
                (thesis_id, bar_ts, action),
            ).fetchone()
            return dict(row)

    def pending_tp_amendments(self, thesis_id: int) -> list[dict[str, Any]]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM tp_amendments WHERE thesis_id=? AND status IN ('PENDING','RECONCILE') "
                "ORDER BY id", (thesis_id,)
            )]

    def update_tp_amendment(self, amendment_id: int, **values: Any) -> None:
        values["updated_at"] = utc_now()
        assignment = ",".join(f"{key}=?" for key in values)
        with self._db() as db:
            db.execute(
                f"UPDATE tp_amendments SET {assignment} WHERE id=?",
                (*values.values(), amendment_id),
            )
