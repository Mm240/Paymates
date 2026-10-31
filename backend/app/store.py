"""SQLite-backed store for merchants, payments, tickets, escalations and the audit log.

SQLite keeps the demo zero-setup. The schema is plain SQL, so moving to
PostgreSQL in production is a connection-string change plus minor type tweaks.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .config import SEED


def now_ms() -> int:
    return int(time.time() * 1000)


SCHEMA = """
CREATE TABLE merchants (
  id TEXT PRIMARY KEY, name TEXT, owner TEXT, city TEXT, category TEXT, lang TEXT, phone TEXT,
  plan TEXT, settlement_cycle TEXT, kyc_status TEXT, kyc_missing TEXT, bank TEXT,
  bank_verified INTEGER, soundbox TEXT
);
CREATE TABLE transactions (
  id TEXT PRIMARY KEY, merchant_id TEXT, amount INTEGER, method TEXT, payer TEXT, created_at INTEGER,
  gateway_status TEXT, bank_debited INTEGER, bank_status TEXT, bank_checked INTEGER DEFAULT 0,
  recovered INTEGER DEFAULT 0
);
CREATE TABLE settlements (
  id TEXT PRIMARY KEY, merchant_id TEXT, amount INTEGER, status TEXT, hold_reason TEXT,
  expected_at INTEGER, settled_at INTEGER, utr TEXT, destination TEXT
);
CREATE TABLE refunds (
  id TEXT PRIMARY KEY, merchant_id TEXT, txn_id TEXT, amount INTEGER, status TEXT, reason TEXT,
  initiated_at INTEGER, arn TEXT, eta_at INTEGER
);
CREATE TABLE tickets (
  id TEXT PRIMARY KEY, merchant_id TEXT, message TEXT, lang TEXT, intent TEXT, confidence REAL,
  status TEXT, outcome TEXT, summary TEXT, reply TEXT, created_at INTEGER, resolved_at INTEGER,
  duration_ms INTEGER, recovered_amount INTEGER DEFAULT 0, models TEXT, source TEXT,
  subject TEXT, facts TEXT
);
CREATE TABLE ops_tickets (
  id TEXT PRIMARY KEY, merchant_id TEXT, category TEXT, priority TEXT, summary TEXT,
  created_at INTEGER, parent_ticket TEXT
);
CREATE TABLE escalations (
  id TEXT PRIMARY KEY, ticket_id TEXT, merchant_id TEXT, reason TEXT, briefing TEXT,
  proposed_action TEXT, status TEXT, created_at INTEGER, decided_at INTEGER, decided_by TEXT, note TEXT
);
CREATE TABLE audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ticket_id TEXT, actor TEXT, action TEXT, detail TEXT
);
CREATE TABLE decisions (
  id TEXT PRIMARY KEY, esc_id TEXT, merchant_id TEXT, intent TEXT, amount INTEGER, tool TEXT,
  decision TEXT, specialist TEXT, note TEXT, decided_at INTEGER
);
CREATE TABLE settings (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, merchant_id TEXT, role TEXT, text TEXT, ts INTEGER, ticket_id TEXT
);
"""

JSON_COLS = {"kyc_missing", "soundbox", "models", "briefing", "proposed_action", "detail", "facts"}
BOOL_COLS = {"bank_verified", "bank_debited", "recovered", "bank_checked"}


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.lock = threading.RLock()
        self.counters: dict[str, int] = {}
        self.reset()

    # ------------------------------------------------------------------ setup
    def reset(self) -> None:
        with self.lock:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            cur = self.db.cursor()
            for table in ("merchants", "transactions", "settlements", "refunds", "tickets",
                          "ops_tickets", "escalations", "audit", "messages", "settings", "decisions"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.executescript(SCHEMA)
            self.counters = {"ticket": 2400, "ops": 5100, "esc": 300, "utr": 880000, "ref": 61000}
            self._seed()
            self.db.commit()

    def _seed(self) -> None:
        t0 = now_ms()
        at = lambda mins: None if mins is None else t0 + int(mins) * 60_000  # noqa: E731
        for m in SEED["merchants"]:
            self.insert("merchants", {**m, "kyc_status": m["kyc_status"]})
        for t in SEED["transactions"]:
            self.insert("transactions", {
                "id": t["id"], "merchant_id": t["merchant_id"], "amount": t["amount"], "method": t["method"],
                "payer": t["payer"], "created_at": at(t["at_min"]), "gateway_status": t["gateway_status"],
                "bank_debited": t["bank_debited"], "bank_status": t["bank_status"],
            })
        for s in SEED["settlements"]:
            self.insert("settlements", {
                "id": s["id"], "merchant_id": s["merchant_id"], "amount": s["amount"], "status": s["status"],
                "hold_reason": s["hold_reason"], "expected_at": at(s["expected_min"]),
                "settled_at": at(s["settled_min"]), "utr": s["utr"], "destination": s["destination"],
            })
        for r in SEED["refunds"]:
            self.insert("refunds", {
                "id": r["id"], "merchant_id": r["merchant_id"], "txn_id": r["txn_id"], "amount": r["amount"],
                "status": r["status"], "reason": r["reason"], "initiated_at": at(r["initiated_min"]),
                "arn": r["arn"], "eta_at": at(r["eta_min"]),
            })
        for h in SEED["ticket_history"]:
            self.insert("tickets", {
                "id": h["id"], "merchant_id": h["merchant_id"], "message": "", "lang": "en", "intent": h["intent"],
                "confidence": 1.0, "status": h["status"], "outcome": "HISTORY", "summary": h["summary"],
                "reply": "", "created_at": at(h["at_min"]), "resolved_at": at(h["at_min"]), "duration_ms": None,
                "recovered_amount": 0, "models": {}, "source": "history",
            })

    # ------------------------------------------------------------------ generic helpers
    def next_id(self, kind: str, prefix: str) -> str:
        with self.lock:
            self.counters[kind] = self.counters.get(kind, 10000) + 1
            return f"{prefix}{self.counters[kind]}"

    def insert(self, table: str, row: dict) -> None:
        row = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLS else (int(v) if k in BOOL_COLS and v is not None else v))
               for k, v in row.items()}
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.lock:
            self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
            self.db.commit()

    def update(self, table: str, id_: str, changes: dict) -> None:
        changes = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLS else (int(v) if k in BOOL_COLS and v is not None else v))
                   for k, v in changes.items()}
        sets = ", ".join(f"{k} = ?" for k in changes)
        with self.lock:
            self.db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*changes.values(), id_])
            self.db.commit()

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        d = dict(row)
        for k in list(d):
            if k in JSON_COLS and isinstance(d[k], str):
                d[k] = json.loads(d[k])
            elif k in BOOL_COLS and d[k] is not None:
                d[k] = bool(d[k])
        return d

    def one(self, sql: str, args: tuple = ()) -> Optional[dict]:
        with self.lock:
            return self._row(self.db.execute(sql, args).fetchone())

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.db.execute(sql, args).fetchall()]

    # ------------------------------------------------------------------ domain reads
    def merchant(self, mid: str) -> Optional[dict]:
        return self.one("SELECT * FROM merchants WHERE id = ?", (mid,))

    def merchants(self) -> list[dict]:
        return self.all("SELECT * FROM merchants ORDER BY id")

    def transaction(self, tid: str) -> Optional[dict]:
        return self.one("SELECT * FROM transactions WHERE id = ?", (tid,))

    def transactions(self, mid: str, status: Optional[str] = None, limit: int = 10) -> list[dict]:
        if status:
            return self.all("SELECT * FROM transactions WHERE merchant_id = ? AND gateway_status = ? "
                            "ORDER BY created_at DESC LIMIT ?", (mid, status, limit))
        return self.all("SELECT * FROM transactions WHERE merchant_id = ? ORDER BY created_at DESC LIMIT ?", (mid, limit))

    def settlement(self, sid: str) -> Optional[dict]:
        return self.one("SELECT * FROM settlements WHERE id = ?", (sid,))

    def settlements(self, mid: str, limit: int = 10) -> list[dict]:
        return self.all("SELECT * FROM settlements WHERE merchant_id = ? ORDER BY expected_at DESC LIMIT ?", (mid, limit))

    def refund(self, rid: str) -> Optional[dict]:
        return self.one("SELECT * FROM refunds WHERE id = ?", (rid,))

    def refunds(self, mid: str, limit: int = 10) -> list[dict]:
        return self.all("SELECT * FROM refunds WHERE merchant_id = ? ORDER BY initiated_at DESC LIMIT ?", (mid, limit))

    def ticket(self, tid: str) -> Optional[dict]:
        return self.one("SELECT * FROM tickets WHERE id = ?", (tid,))

    def tickets(self, limit: int = 50, include_history: bool = False) -> list[dict]:
        where = "" if include_history else "WHERE t.source != 'history'"
        return self.all(f"SELECT t.*, m.name AS merchant_name FROM tickets t LEFT JOIN merchants m ON m.id = t.merchant_id "
                        f"{where} ORDER BY t.created_at DESC, t.id DESC LIMIT ?", (limit,))

    def past_tickets(self, mid: str, limit: int = 3) -> list[dict]:
        return self.all("SELECT id, intent, summary, status, created_at FROM tickets WHERE merchant_id = ? "
                        "AND status != 'OPEN' ORDER BY created_at DESC LIMIT ?", (mid, limit))

    def escalation(self, eid: str) -> Optional[dict]:
        return self.one("SELECT * FROM escalations WHERE id = ?", (eid,))

    def escalations(self, limit: int = 50) -> list[dict]:
        return self.all("SELECT * FROM escalations ORDER BY (status = 'OPEN') DESC, created_at DESC LIMIT ?", (limit,))

    def audit_log(self, limit: int = 80) -> list[dict]:
        return self.all("SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (limit,))

    def conversation(self, mid: str, limit: int = 8) -> list[dict]:
        rows = self.all("SELECT role, text, ts FROM messages WHERE merchant_id = ? ORDER BY seq DESC LIMIT ?", (mid, limit))
        return list(reversed(rows))

    # ------------------------------------------------------------------ domain writes
    def add_message(self, mid: str, role: str, text: str, ticket_id: Optional[str]) -> None:
        self.insert("messages", {"merchant_id": mid, "role": role, "text": text, "ts": now_ms(), "ticket_id": ticket_id})

    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self.db.commit()

    def audit(self, ticket_id: Optional[str], actor: str, action: str, detail: Any) -> None:
        self.insert("audit", {"ts": now_ms(), "ticket_id": ticket_id, "actor": actor, "action": action, "detail": detail})


store = Store()
