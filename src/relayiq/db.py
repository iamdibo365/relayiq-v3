"""SQLite system of record for the demo clinic (synthetic patients only - no real PHI).

Tables: patients, providers, slots, appointments, insurance_checks, refills, balances,
journey_events (cross-channel history), ledger (automations), agent_specs (Forge),
calls / turns (voice telemetry), eval_runs (Overseer), portal_jobs.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
  id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT, dob TEXT, phone TEXT UNIQUE,
  email TEXT, payer_id TEXT, payer_name TEXT, member_id TEXT, group_number TEXT,
  pcp_provider_id TEXT, preferred_language TEXT DEFAULT 'en'
);
CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY, name TEXT, specialty TEXT, location TEXT
);
CREATE TABLE IF NOT EXISTS slots (
  id TEXT PRIMARY KEY, provider_id TEXT, start TEXT, visit_type TEXT, location TEXT,
  status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS appointments (
  id TEXT PRIMARY KEY, patient_id TEXT, slot_id TEXT, provider_id TEXT, start TEXT,
  visit_type TEXT, reason TEXT, status TEXT, insurance_status TEXT, insurance_check_id TEXT,
  created_at TEXT, created_by TEXT
);
CREATE TABLE IF NOT EXISTS insurance_checks (
  id TEXT PRIMARY KEY, patient_id TEXT, payer_id TEXT, method TEXT, status TEXT,
  summary TEXT, details TEXT, created_at TEXT, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS refills (
  id TEXT PRIMARY KEY, patient_id TEXT, medication TEXT, pharmacy TEXT, status TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS balances (
  patient_id TEXT PRIMARY KEY, amount_due REAL, last_statement TEXT, line_items TEXT
);
CREATE TABLE IF NOT EXISTS journey_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT, channel TEXT, ts TEXT,
  summary TEXT, sentiment TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, call_sid TEXT, agent TEXT, tool TEXT,
  scope TEXT, decision TEXT, args TEXT, result TEXT, minutes_saved REAL, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS agent_specs (
  name TEXT PRIMARY KEY, display_name TEXT, purpose TEXT, instructions TEXT, tools TEXT,
  model TEXT, status TEXT, built_by TEXT, source TEXT, eval_report TEXT, created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_sid TEXT PRIMARY KEY, from_number TEXT, patient_id TEXT, started_at TEXT,
  ended_at TEXT, outcome TEXT, active_agent TEXT, escalated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_sid TEXT, ts TEXT, role TEXT, agent TEXT,
  text TEXT, interrupted INTEGER DEFAULT 0, stt_ms INTEGER, llm_first_token_ms INTEGER,
  tts_first_audio_ms INTEGER, total_ms INTEGER
);
CREATE TABLE IF NOT EXISTS eval_runs (
  id TEXT PRIMARY KEY, target TEXT, started_at TEXT, completed_at TEXT, passed INTEGER,
  score REAL, report TEXT
);
CREATE TABLE IF NOT EXISTS portal_jobs (
  id TEXT PRIMARY KEY, patient_id TEXT, payer_id TEXT, status TEXT, steps INTEGER,
  result TEXT, created_at TEXT, completed_at TEXT
);
"""

PAYERS = {
    "62308": "Cigna",
    "87726": "UnitedHealthcare",
    "60054": "Aetna",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Tiny thread-safe wrapper around sqlite3 (one connection, one lock)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid or 0

    def backup_to(self, path: str) -> "Database":
        """Copy the whole database (used by Overseer to run simulations in a sandbox)."""
        target = sqlite3.connect(path)
        with self._lock:
            self._conn.backup(target)
        target.close()
        return Database(path)

    # ----- convenience -----
    def patient_by_phone(self, phone: str) -> dict[str, Any] | None:
        digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
        if not digits:
            return None
        for p in self.query("SELECT * FROM patients"):
            if "".join(ch for ch in p["phone"] if ch.isdigit())[-10:] == digits:
                return p
        return None

    def add_journey(self, patient_id: str, channel: str, summary: str, sentiment: str = "neutral"):
        self.execute(
            "INSERT INTO journey_events(patient_id, channel, ts, summary, sentiment) VALUES (?,?,?,?,?)",
            (patient_id, channel, now_iso(), summary, sentiment),
        )


def seed(db: Database, tz: str = "America/Chicago", force: bool = False) -> None:
    """Seed synthetic clinic data. Member IDs are placeholders - for Stedi test mode,
    replace them with the values from Stedi's published mock eligibility requests."""
    if db.one("SELECT 1 AS x FROM patients LIMIT 1") and not force:
        return
    for t in ("patients", "providers", "slots", "appointments", "refills", "balances",
              "journey_events"):
        db.execute(f"DELETE FROM {t}")

    providers = [
        ("prv_chen", "Dr. Maya Chen", "Family Medicine", "Lakeside Main - 1200 Elm St"),
        ("prv_okafor", "Dr. Daniel Okafor", "Internal Medicine", "Lakeside Main - 1200 Elm St"),
        ("prv_rivera", "Sofia Rivera, NP", "Family Medicine", "Lakeside North - 88 Oak Ave"),
    ]
    for p in providers:
        db.execute("INSERT INTO providers VALUES (?,?,?,?)", p)

    patients = [
        ("pt_1001", "John", "Doe", "1980-04-12", "+15555550101", "john.doe@example.com",
         "87726", "UnitedHealthcare", "UHC-PLACEHOLDER-1", "GRP-7001", "prv_chen", "en"),
        ("pt_1002", "Jordan", "Smith", "1992-09-30", "+15555550102", "jordan.smith@example.com",
         "62308", "Cigna", "CIGNA-PLACEHOLDER-1", "GRP-3302", "prv_okafor", "en"),
        ("pt_1003", "Ana", "Lopez", "1975-01-22", "+15555550103", "ana.lopez@example.com",
         "60054", "Aetna", "AETNA-PLACEHOLDER-1", "GRP-5503", "prv_rivera", "es"),
    ]
    for p in patients:
        db.execute("INSERT INTO patients VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", p)

    zone = ZoneInfo(tz)
    day = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    added_days = 0
    while added_days < 10:
        day += timedelta(days=1)
        if day.weekday() >= 5:
            continue
        added_days += 1
        for prv, _, _, loc in providers:
            for hour in (9, 10, 11, 14, 15, 16):
                for minute in (0, 30):
                    n += 1
                    if n % 3 == 0:  # some slots already taken
                        continue
                    start = day.replace(hour=hour, minute=minute)
                    vt = "new_patient" if minute == 0 and hour in (9, 14) else "follow_up"
                    db.execute(
                        "INSERT INTO slots VALUES (?,?,?,?,?,?)",
                        (f"slot_{n:04d}", prv, start.isoformat(), vt, loc, "open"),
                    )

    db.execute("INSERT INTO balances VALUES (?,?,?,?)", ("pt_1001", 45.0, "2026-09-15", json.dumps(
        [{"date": "2026-08-28", "item": "Office visit copay", "amount": 30.0},
         {"date": "2026-08-28", "item": "Lab draw", "amount": 15.0}])))
    db.execute("INSERT INTO balances VALUES (?,?,?,?)", ("pt_1002", 0.0, "2026-09-15", "[]"))
    db.execute("INSERT INTO balances VALUES (?,?,?,?)", ("pt_1003", 120.0, "2026-09-15", json.dumps(
        [{"date": "2026-07-10", "item": "Annual physical - deductible", "amount": 120.0}])))

    journey = [
        ("pt_1001", "sms", "Asked by text about rescheduling his follow-up; no reply sent yet.", "neutral"),
        ("pt_1001", "portal", "Viewed lab results from Aug 28 in the patient portal.", "neutral"),
        ("pt_1001", "call", "Called about a $45 statement; said he'd call back to pay.", "neutral"),
        ("pt_1002", "web_chat", "Asked whether the clinic takes Cigna; told yes.", "positive"),
        ("pt_1002", "email", "Received new-patient forms; not completed yet.", "neutral"),
        ("pt_1003", "call", "Requested lisinopril refill last month; completed.", "positive"),
        ("pt_1003", "sms", "Prefers Spanish for phone calls.", "neutral"),
    ]
    for pid, ch, s, sent in journey:
        db.execute(
            "INSERT INTO journey_events(patient_id, channel, ts, summary, sentiment) VALUES (?,?,?,?,?)",
            (pid, ch, (datetime.now(timezone.utc) - timedelta(days=len(s) % 20 + 1)).isoformat(
                timespec="seconds"), s, sent))
