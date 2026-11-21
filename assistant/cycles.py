"""생활 주기 관리: 정수기 필터·칫솔·보일러 점검·엔진오일처럼 주기적으로 하는 일.

`!cycle add 정수기필터 90` (마지막 날짜를 붙이면 그날부터) → 때가 되면 알림 → `!done 정수기필터` 로 다시 시작.
"""
from __future__ import annotations

from datetime import date, timedelta

from .tools.base import tool

SCHEMA = """CREATE TABLE IF NOT EXISTS cycles (name TEXT PRIMARY KEY, every_days INTEGER, last TEXT, note TEXT,
  notified TEXT DEFAULT '')"""


def init(store) -> None:
    store._exec(SCHEMA)


def add(store, name: str, every_days: int, last: date, note: str = "") -> str:
    init(store)
    name = name.strip()
    store._exec("INSERT OR REPLACE INTO cycles (name, every_days, last, note, notified) VALUES (?,?,?,?, '')",
                (name, int(every_days), last.isoformat(), note))
    return f"🔁 {name}: {every_days}일마다 · 다음 {last + timedelta(days=int(every_days)):%m/%d}"


def _find(store, name: str) -> str | None:
    init(store)
    names = [r[0] for r in store._exec("SELECT name FROM cycles")]
    q = name.strip().replace(" ", "")
    exact = [n for n in names if n.replace(" ", "") == q]
    part = [n for n in names if q and q in n.replace(" ", "")]
    hits = exact or part
    return hits[0] if len(hits) == 1 else None


def done(store, name: str, when: date) -> str:
    n = _find(store, name)
    if not n:
        return f"'{name}' 항목을 찾지 못했어요. `!cycle`로 이름을 확인하세요."
    store._exec("UPDATE cycles SET last=?, notified='' WHERE name=?", (when.isoformat(), n))
    every = store._exec("SELECT every_days FROM cycles WHERE name=?", (n,))[0][0]
    return f"✅ {n} 기록했어요 · 다음 {when + timedelta(days=every):%Y-%m-%d}"


def delete(store, name: str) -> str:
    n = _find(store, name)
    if not n:
        return f"'{name}' 항목을 찾지 못했어요."
    store._exec("DELETE FROM cycles WHERE name=?", (n,))
    return f"🗑️ {n} 삭제했어요."


def items(store, today: date) -> list[dict]:
    init(store)
    out = []
    for name, every, last, note, notified in store._exec("SELECT name, every_days, last, note, notified FROM cycles"):
        due = date.fromisoformat(last) + timedelta(days=every)
        out.append({"name": name, "every": every, "last": last, "due": due, "left": (due - today).days,
                    "note": note, "notified": notified})
    return sorted(out, key=lambda x: x["left"])


def lines(store, today: date) -> list[str]:
    out = []
    for c in items(store, today):
        when = "오늘" if c["left"] == 0 else (f"{-c['left']}일 지남" if c["left"] < 0 else f"D-{c['left']}")
        out.append(f"**{c['name']}** — {c['due']:%m/%d} ({when}) · {c['every']}일마다" + (f" · {c['note']}" if c["note"] else ""))
    return out


def upcoming(store, today: date, within: int = 3) -> list[dict]:
    return [c for c in items(store, today) if c["left"] <= within]


def due_alerts(store, today: date) -> list[str]:
    """당일·지난 항목을 주기마다 한 번만 알림."""
    out = []
    for c in items(store, today):
        if c["left"] <= 0 and c["notified"] != c["due"].isoformat():
            store._exec("UPDATE cycles SET notified=? WHERE name=?", (c["due"].isoformat(), c["name"]))
            when = "오늘" if c["left"] == 0 else f"{-c['left']}일 지남"
            out.append(f"🔁 **{c['name']}** {when} — 하고 나면 `!done {c['name']}`")
    return out


@tool("cycle_add", "주기적으로 하는 일을 등록한다 (정수기 필터 90일, 칫솔 90일, 엔진오일 180일 등). last는 마지막으로 한 날짜(모르면 오늘).",
      {"properties": {"name": {"type": "string"}, "every_days": {"type": "integer"},
                      "last": {"type": "string", "description": "YYYY-MM-DD, 비우면 오늘", "default": ""},
                      "note": {"type": "string", "default": ""}}, "required": ["name", "every_days"]}, group="home")
async def cycle_add(ctx, name: str, every_days: int, last: str = "", note: str = ""):
    return add(ctx.store, name, every_days, date.fromisoformat(last) if last else date.today(), note)


@tool("cycle_done", "주기 항목을 했다고 기록한다 (다음 주기가 오늘부터 다시 시작).",
      {"properties": {"name": {"type": "string"}, "when": {"type": "string", "default": "", "description": "YYYY-MM-DD, 비우면 오늘"}},
       "required": ["name"]}, group="home")
async def cycle_done(ctx, name: str, when: str = ""):
    return done(ctx.store, name, date.fromisoformat(when) if when else date.today())


@tool("cycle_list", "주기 항목 목록과 다음 날짜.", {"properties": {}}, group="home")
async def cycle_list(ctx):
    return lines(ctx.store, date.today()) or "등록된 주기 항목이 없어요."
