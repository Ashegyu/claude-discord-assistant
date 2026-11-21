"""Microsoft To Do 연동 (Microsoft Graph).

갤럭시 '삼성 리마인더'는 Microsoft To Do 목록 하나와 양방향 동기화된다.
봇이 같은 목록에 할 일을 쓰고 읽으면 → 폰 기본 알람으로 울리고, 폰에서 만든 것도 봇이 안다.

토큰: deploy/ms_auth.py (기기 코드 로그인) 로 data/ms_token.json 을 한 번 만들면 이후 자동 갱신.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "Tasks.ReadWrite offline_access"
TRANSPORT: httpx.AsyncBaseTransport | None = None   # 테스트용 가짜 전송 계층
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def token_path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "ms_token.json"


def client_id() -> str:
    return os.environ.get("MS_CLIENT_ID", "").strip()


def authority() -> str:
    # 개인 Microsoft 계정(outlook.com·hotmail 등)은 consumers
    return f"https://login.microsoftonline.com/{os.environ.get('MS_TENANT', 'consumers').strip() or 'consumers'}/oauth2/v2.0"


def enabled() -> bool:
    return bool(client_id()) and token_path().exists()


class TodoError(RuntimeError):
    pass


def _http(**kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, transport=TRANSPORT, **kw)


async def _access_token() -> str:
    tok = json.loads(token_path().read_text(encoding="utf-8"))
    if tok.get("expires_at", 0) > time.time() + 120:
        return tok["access_token"]
    async with _http() as c:
        r = await c.post(f"{authority()}/token", data={
            "client_id": client_id(), "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"], "scope": SCOPES})
    if r.status_code != 200:
        raise TodoError(f"Microsoft 토큰 갱신 실패 ({r.status_code}). deploy/ms_auth.py 로 다시 로그인하세요.")
    save_token(r.json())
    return r.json()["access_token"]


def save_token(data: dict) -> None:
    old = json.loads(token_path().read_text(encoding="utf-8")) if token_path().exists() else {}
    token_path().parent.mkdir(parents=True, exist_ok=True)
    token_path().write_text(json.dumps({
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or old.get("refresh_token", ""),   # 회전되면 새 것으로
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
    }), encoding="utf-8")
    os.chmod(token_path(), 0o600)


async def graph(method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {await _access_token()}"}
    async with _http() as c:
        r = await c.request(method, f"{GRAPH}{path}", json=body, params=params, headers=headers)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = r.text[:200]
        raise TodoError(f"Microsoft To Do 오류 {r.status_code}: {msg}")
    return r.json() if r.content else {}


# ───────────── 목록 ─────────────
async def lists() -> list[dict]:
    return (await graph("GET", "/me/todo/lists"))["value"]


async def list_id(cfg: dict, store) -> str:
    """config의 todo.list_name 목록 (비우면 기본 '작업' 목록). 찾은 id는 저장해 둔다."""
    name = (cfg.get("todo") or {}).get("list_name", "").strip()
    key = f"todo_list:{name}"
    if cached := store.get(key):
        return cached
    ls = await lists()
    hit = next((l for l in ls if name and l["displayName"] == name), None) if name else \
        next((l for l in ls if l.get("wellknownListName") == "defaultList"), None)
    if not hit:
        raise TodoError(f"To Do 목록 '{name}'을 찾지 못했습니다. 있는 목록: {', '.join(l['displayName'] for l in ls)}")
    store.put(key, hit["id"])
    return hit["id"]


# ───────────── 시각 변환 ─────────────
def _dtz(dt: datetime) -> dict:
    u = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return {"dateTime": u.isoformat(timespec="seconds"), "timeZone": "UTC"}


def parse_dtz(v: dict | None, tz: ZoneInfo) -> datetime | None:
    if not v or not v.get("dateTime"):
        return None
    s = v["dateTime"].split(".")[0]
    d = datetime.fromisoformat(s)
    src = timezone.utc if v.get("timeZone", "UTC") in ("UTC", "tzone://Microsoft/Utc") else tz
    return d.replace(tzinfo=src).astimezone(tz)


def recurrence(repeat: str, start: datetime) -> dict | None:
    if not repeat:
        return None
    if repeat == "daily":
        pattern = {"type": "daily", "interval": 1}
    elif repeat == "weekdays":
        pattern = {"type": "weekly", "interval": 1, "daysOfWeek": WEEKDAYS[:5], "firstDayOfWeek": "monday"}
    else:
        pattern = {"type": "weekly", "interval": 1, "daysOfWeek": [WEEKDAYS[start.weekday()]], "firstDayOfWeek": "monday"}
    return {"pattern": pattern, "range": {"type": "noEnd", "startDate": start.date().isoformat()}}


# ───────────── 할 일 ─────────────
async def add(cfg: dict, store, title: str, when: datetime | None, repeat: str = "", note: str = "") -> dict:
    tz = ZoneInfo(cfg["timezone"])
    body: dict = {"title": title}
    if note:
        body["body"] = {"content": note, "contentType": "text"}
    if when:
        local = when.astimezone(tz)
        body |= {"isReminderOn": True, "reminderDateTime": _dtz(local),
                 # 마감일은 '날짜'만 의미가 있어 그날 자정(현지)으로
                 "dueDateTime": _dtz(local.replace(hour=0, minute=0, second=0, microsecond=0))}
        if rec := recurrence(repeat, local):
            body["recurrence"] = rec
    return await graph("POST", f"/me/todo/lists/{await list_id(cfg, store)}/tasks", body)


async def open_tasks(cfg: dict, store) -> list[dict]:
    lid = await list_id(cfg, store)
    out, path, params = [], f"/me/todo/lists/{lid}/tasks", {"$top": "100"}
    for _ in range(5):
        page = await graph("GET", path, params=params)
        out += [t for t in page.get("value", []) if t.get("status") != "completed"]
        nxt = page.get("@odata.nextLink")
        if not nxt:
            break
        path, params = nxt.replace(GRAPH, ""), None
    return out


async def completed_since(cfg: dict, store, since: datetime) -> list[dict]:
    """since 이후 완료한 할 일 (주간 회고용)."""
    lid = await list_id(cfg, store)
    tz = ZoneInfo(cfg["timezone"])
    path, params, out = f"/me/todo/lists/{lid}/tasks", {"$top": "100", "$filter": "status eq 'completed'"}, []
    for _ in range(5):
        try:
            page = await graph("GET", path, params=params)
        except TodoError:
            if params and "$filter" in params:      # 필터를 못 쓰면 전부 받아서 거른다
                params = {"$top": "100"}
                continue
            raise
        for t in page.get("value", []):
            done = parse_dtz(t.get("completedDateTime"), tz)
            if t.get("status") == "completed" and done and done >= since:
                out.append(t)
        nxt = page.get("@odata.nextLink")
        if not nxt:
            break
        path, params = nxt.replace(GRAPH, ""), None
    return out


def when_of(t: dict, tz: ZoneInfo) -> datetime | None:
    return parse_dtz(t.get("reminderDateTime"), tz) if t.get("isReminderOn") else None


def due_of(t: dict, tz: ZoneInfo) -> datetime | None:
    return parse_dtz(t.get("dueDateTime"), tz)


def sort_key(t: dict, tz: ZoneInfo) -> float:
    d = when_of(t, tz) or due_of(t, tz)
    return d.timestamp() if d else float("inf")


async def complete(cfg: dict, store, task_id: str) -> dict:
    return await graph("PATCH", f"/me/todo/lists/{await list_id(cfg, store)}/tasks/{task_id}", {"status": "completed"})


async def delete(cfg: dict, store, task_id: str) -> None:
    await graph("DELETE", f"/me/todo/lists/{await list_id(cfg, store)}/tasks/{task_id}")


def is_today_or_overdue(t: dict, tz: ZoneInfo, now: datetime) -> bool:
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    d = when_of(t, tz) or due_of(t, tz)
    return bool(d and d < end)
