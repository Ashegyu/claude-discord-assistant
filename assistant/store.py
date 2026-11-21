"""SQLite 저장소: 대화 기록, 리마인더, 메일 상태, 배치 작업, 토큰 사용량."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (channel_id TEXT PRIMARY KEY, messages TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, due REAL NOT NULL, text TEXT NOT NULL,
  channel_id TEXT NOT NULL, repeat TEXT DEFAULT '', done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, kind TEXT, title TEXT, channel_id TEXT, meta TEXT,
  created REAL, status TEXT DEFAULT 'in_progress');
CREATE TABLE IF NOT EXISTS traces (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel_id TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS searches (ts REAL, role TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS usage (
  ts REAL, model TEXT, role TEXT, input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER, batch INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.fetchall()

    def insert(self, sql: str, args: tuple = ()) -> int:
        """INSERT 하고 새 행 id를 돌려준다."""
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.lastrowid

    # ── 대화 기록 ──
    def get_history(self, channel_id: str) -> list[dict]:
        rows = self._exec("SELECT messages FROM history WHERE channel_id=?", (channel_id,))
        return json.loads(rows[0][0]) if rows else []

    def set_history(self, channel_id: str, messages: list[dict]) -> None:
        self._exec("INSERT OR REPLACE INTO history VALUES (?,?,?)",
                   (channel_id, json.dumps(messages, ensure_ascii=False), time.time()))

    def clear_history(self, channel_id: str) -> None:
        self._exec("DELETE FROM history WHERE channel_id=?", (channel_id,))

    # ── key-value ──
    def get(self, k: str, default=None):
        rows = self._exec("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(rows[0][0]) if rows else default

    def put(self, k: str, v) -> None:
        self._exec("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v, ensure_ascii=False)))

    # ── 리마인더 ──
    def add_reminder(self, due: float, text: str, channel_id: str, repeat: str = "") -> int:
        self._exec("INSERT INTO reminders (due,text,channel_id,repeat) VALUES (?,?,?,?)", (due, text, channel_id, repeat))
        return self._exec("SELECT last_insert_rowid()")[0][0]

    def due_reminders(self, now: float) -> list[tuple]:
        return self._exec("SELECT id,due,text,channel_id,repeat FROM reminders WHERE done=0 AND due<=?", (now,))

    def list_reminders(self) -> list[tuple]:
        return self._exec("SELECT id,due,text,repeat FROM reminders WHERE done=0 ORDER BY due")

    def finish_reminder(self, rid: int, next_due: float | None) -> None:
        if next_due:
            self._exec("UPDATE reminders SET due=? WHERE id=?", (next_due, rid))
        else:
            self._exec("UPDATE reminders SET done=1 WHERE id=?", (rid,))

    def delete_reminder(self, rid: int) -> bool:
        before = len(self.list_reminders())
        self._exec("UPDATE reminders SET done=1 WHERE id=?", (rid,))
        return len(self.list_reminders()) < before

    # ── 배치 ──
    def add_batch(self, bid: str, kind: str, title: str, channel_id: str, meta: dict) -> None:
        self._exec("INSERT INTO batches (id,kind,title,channel_id,meta,created) VALUES (?,?,?,?,?,?)",
                   (bid, kind, title, channel_id, json.dumps(meta, ensure_ascii=False), time.time()))

    def open_batches(self) -> list[tuple]:
        return self._exec("SELECT id,kind,title,channel_id,meta FROM batches WHERE status='in_progress'")

    def close_batch(self, bid: str, status: str = "ended") -> None:
        self._exec("UPDATE batches SET status=? WHERE id=?", (status, bid))

    # ── 사용량 ──
    def record_usage(self, model: str, role: str, u: dict, batch: bool = False) -> None:
        self._exec("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?)", (
            time.time(), model, role, u.get("input_tokens", 0) or 0, u.get("cache_read_input_tokens", 0) or 0,
            u.get("cache_creation_input_tokens", 0) or 0, u.get("output_tokens", 0) or 0, int(batch)))
        n = ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        if n:
            self.record_searches(role, n)

    # ── 웹 검색 횟수 (검색 1회당 요금이 따로 붙음) ──
    def record_searches(self, role: str, n: int) -> None:
        self._exec("INSERT INTO searches VALUES (?,?,?)", (time.time(), role, int(n)))

    def searches_since(self, since: float) -> int:
        return self._exec("SELECT COALESCE(SUM(n),0) FROM searches WHERE ts>=?", (since,))[0][0]

    # ── 턴 기록 (자기 개선 분석용, 30일 보관) ──
    def add_trace(self, channel_id: str, data: dict) -> None:
        self._exec("INSERT INTO traces (ts, channel_id, data) VALUES (?,?,?)",
                   (time.time(), channel_id, json.dumps(data, ensure_ascii=False)))
        self._exec("DELETE FROM traces WHERE ts < ?", (time.time() - 30 * 86400,))

    def flag_last_trace(self, channel_id: str, key: str, value) -> None:
        rows = self._exec("SELECT id, data FROM traces WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,))
        if rows:
            d = json.loads(rows[0][1])
            d[key] = value
            self._exec("UPDATE traces SET data=? WHERE id=?", (json.dumps(d, ensure_ascii=False), rows[0][0]))

    def traces_since(self, since: float) -> list[dict]:
        return [{"ts": ts, **json.loads(d)} for ts, d in
                self._exec("SELECT ts, data FROM traces WHERE ts>=? ORDER BY id", (since,))]

    def usage_since(self, since: float) -> list[tuple]:
        return self._exec(
            "SELECT model, role, batch, SUM(input), SUM(cache_read), SUM(cache_write), SUM(output), COUNT(*) "
            "FROM usage WHERE ts>=? GROUP BY model, role, batch ORDER BY model", (since,))
