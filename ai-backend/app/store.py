"""SQLite persistence: voice profiles, consent records, audit log, call history."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_name TEXT NOT NULL, owner_relation TEXT NOT NULL,
  sample_path TEXT, sample_sha256 TEXT, status TEXT NOT NULL, provider TEXT, provider_voice_id TEXT,
  error TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS consents(
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, text_version TEXT NOT NULL, owner_name TEXT NOT NULL,
  owner_relation TEXT NOT NULL, typed_name TEXT NOT NULL, permission_note TEXT, sample_sha256 TEXT,
  accepted_at REAL NOT NULL, client_ip TEXT, revoked_at REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL, profile_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, destination TEXT NOT NULL, profile_id TEXT,
  ai_voice INTEGER NOT NULL, status TEXT NOT NULL, end_reason TEXT);
"""


class Store:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def audit(self, event: str, profile_id: Optional[str] = None, detail: str = ""):
        self._q("INSERT INTO audit(ts,event,profile_id,detail) VALUES(?,?,?,?)", (time.time(), event, profile_id, detail))

    def audit_log(self, profile_id: Optional[str] = None) -> list[dict]:
        rows = self._q("SELECT * FROM audit WHERE (? IS NULL OR profile_id=?) ORDER BY id", (profile_id, profile_id))
        return [dict(r) for r in rows]

    # profiles
    def add_profile(self, **f: Any) -> dict:
        pid = str(uuid.uuid4())
        self._q("INSERT INTO profiles(id,name,owner_name,owner_relation,sample_path,sample_sha256,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (pid, f["name"], f["owner_name"], f["owner_relation"], f["sample_path"], f["sample_sha256"], f["status"], time.time()))
        return self.profile(pid)  # type: ignore[return-value]

    def profile(self, pid: str) -> Optional[dict]:
        r = self._q("SELECT * FROM profiles WHERE id=?", (pid,))
        return dict(r[0]) if r else None

    def profiles(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM profiles ORDER BY created_at DESC")]

    def update_profile(self, pid: str, **f: Any):
        cols = ",".join(f"{k}=?" for k in f)
        self._q(f"UPDATE profiles SET {cols} WHERE id=?", (*f.values(), pid))

    def delete_profile(self, pid: str):
        self._q("DELETE FROM profiles WHERE id=?", (pid,))

    # consent
    def add_consent(self, **f: Any) -> str:
        cid = str(uuid.uuid4())
        self._q("INSERT INTO consents(id,profile_id,text_version,owner_name,owner_relation,typed_name,permission_note,sample_sha256,accepted_at,client_ip) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cid, f["profile_id"], f["text_version"], f["owner_name"], f["owner_relation"], f["typed_name"], f.get("permission_note"), f.get("sample_sha256"), time.time(), f.get("client_ip")))
        return cid

    def active_consent(self, pid: str) -> Optional[dict]:
        r = self._q("SELECT * FROM consents WHERE profile_id=? AND revoked_at IS NULL ORDER BY accepted_at DESC LIMIT 1", (pid,))
        return dict(r[0]) if r else None

    def revoke_consents(self, pid: str):
        self._q("UPDATE consents SET revoked_at=? WHERE profile_id=? AND revoked_at IS NULL", (time.time(), pid))

    # calls
    def start_call(self, destination: str, profile_id: Optional[str], ai_voice: bool) -> str:
        cid = str(uuid.uuid4())
        self._q("INSERT INTO calls(id,started_at,destination,profile_id,ai_voice,status) VALUES(?,?,?,?,?,?)", (cid, time.time(), destination, profile_id, int(ai_voice), "started"))
        return cid

    def finish_call(self, cid: str, status: str, reason: str = ""):
        self._q("UPDATE calls SET ended_at=?, status=?, end_reason=? WHERE id=?", (time.time(), status, reason, cid))

    def calls(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM calls ORDER BY started_at DESC LIMIT ?", (limit,))]
