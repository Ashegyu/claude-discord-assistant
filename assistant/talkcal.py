"""톡캘린더 연동: 구글 캘린더의 앞으로 2주 일정을 카카오 톡캘린더에도 넣어 카톡 알림으로 받는다 (한 방향).

API: POST kapi.kakao.com/v2/api/calendar/create/event (폼 필드 event=JSON), DELETE …/delete/event?event_id=
- 시각은 UTC, 5분 단위. 종일 일정은 T00:00:00Z. 제목 50자.
- 동의항목 talk_calendar 필요 → deploy/kakao_auth.py 로 다시 로그인.
바뀐 일정은 지우고 다시 만들고, 구글에서 사라진 일정은 톡캘린더에서도 지운다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx

from .tools import life

API = "https://kapi.kakao.com/v2/api/calendar"


def to_kakao(ev: dict, tz_name: str) -> dict:
    start, end = ev["start"], ev.get("end") or ev["start"]
    if "T" not in start:                                   # 종일
        s = datetime.fromisoformat(start[:10])
        e = datetime.fromisoformat(end[:10]) if end[:10] > start[:10] else s + timedelta(days=1)
        time = {"start_at": f"{s:%Y-%m-%d}T00:00:00Z", "end_at": f"{e:%Y-%m-%d}T00:00:00Z", "time_zone": tz_name, "all_day": True}
    else:
        s = datetime.fromisoformat(start).astimezone(timezone.utc).replace(second=0, microsecond=0)
        e = datetime.fromisoformat(end).astimezone(timezone.utc).replace(second=0, microsecond=0)
        s -= timedelta(minutes=s.minute % 5)
        e += timedelta(minutes=(-e.minute) % 5)
        if e <= s:
            e = s + timedelta(minutes=30)
        time = {"start_at": f"{s:%Y-%m-%dT%H:%M}:00Z", "end_at": f"{e:%Y-%m-%dT%H:%M}:00Z", "time_zone": tz_name, "all_day": False}
    out = {"title": (ev.get("title") or "(제목 없음)")[:50], "time": time}
    if ev.get("location"):
        out["location"] = {"name": ev["location"].split("\n")[0][:100]}
    if ev.get("description"):
        out["description"] = ev["description"][:1000]
    return out


def sig(ev: dict) -> str:
    return hashlib.sha1(json.dumps([ev.get("title"), ev.get("start"), ev.get("end"), ev.get("location")],
                                   ensure_ascii=False).encode()).hexdigest()[:16]


async def _call(method: str, path: str, token: str, **kw) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"톡캘린더 {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else {}


async def create(token: str, event: dict) -> str:
    r = await _call("POST", "/create/event", token, data={"calendar_id": "primary", "event": json.dumps(event, ensure_ascii=False)})
    return r["event_id"]


async def delete(token: str, event_id: str) -> None:
    await _call("DELETE", "/delete/event", token, params={"event_id": event_id})


async def sync(cfg: dict, store, events: list[dict], now: datetime, limit: int = 20) -> dict:
    """events: 구글 일정(id 포함). 돌려주는 값: {created, deleted}"""
    token = await life._kakao_access()
    state: dict = store.get("talkcal_map") or {}
    current = {e["id"]: e for e in events if e.get("id")}
    made = gone = 0
    for gid, st in list(state.items()):
        ended = st.get("end", "") < now.isoformat()
        if gid not in current or st["sig"] != sig(current[gid]):
            if not ended or gid in current:
                try:
                    await delete(token, st["eid"])
                    gone += 1
                except RuntimeError:
                    pass                     # 사용자가 톡캘린더에서 이미 지운 경우
            del state[gid]
    for gid, ev in current.items():
        if gid in state or made >= limit:
            continue
        eid = await create(token, to_kakao(ev, cfg["timezone"]))
        state[gid] = {"eid": eid, "sig": sig(ev), "end": ev.get("end") or ev["start"]}
        made += 1
        store.put("talkcal_map", state)      # 중간에 실패해도 중복 생성하지 않게 바로 저장
    store.put("talkcal_map", state)
    return {"created": made, "deleted": gone}


# ───────────── 읽기: 카톡에서 만든 일정을 비서가 본다 ─────────────
_detail_cache: dict[str, dict] = {}


def from_kakao(e: dict, tz) -> dict:
    """톡캘린더 일정 → 구글 일정과 같은 모양 {id,title,start,end,location,description}."""
    t = e.get("time") or {}
    if t.get("all_day"):
        start, end = t.get("start_at", "")[:10], t.get("end_at", "")[:10]
    else:
        conv = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz).isoformat(timespec="seconds")  # noqa: E731
        start, end = conv(t["start_at"]), conv(t["end_at"])
    loc = e.get("location") or {}
    return {"id": f"kakao:{e.get('id', '')}", "title": e.get("title") or "(카톡 일정)", "start": start, "end": end,
            "location": loc.get("name") or loc.get("address") or "", "description": (e.get("description") or "")[:300],
            "source": "톡캘린더"}


async def kakao_events(cfg: dict, store, start: datetime, end: datetime) -> list[dict]:
    """카톡 톡캘린더의 일정 (비서가 구글에서 복사해 넣은 것은 빼고). 한 번에 31일까지."""
    token = await life._kakao_access()
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    end = min(end, start + timedelta(days=31))
    r = await _call("GET", "/events", token, params={"from": utc(start), "to": utc(end), "limit": 100, "time_zone": cfg["timezone"]})
    mine = {st["eid"] for st in (store.get("talkcal_map") or {}).values()}
    tz, out = start.tzinfo, []
    for e in r.get("events", []):
        eid = e.get("id", "")
        if eid.split("_")[0] in mine or e.get("type") in ("PUBLIC", "SUBSCRIBE"):
            continue
        key = f"{eid}:{(e.get('time') or {}).get('start_at')}"
        if key not in _detail_cache:               # 목록에는 장소·설명이 없어서 상세를 한 번 읽어 둔다
            try:
                _detail_cache[key] = (await _call("GET", "/event", token, params={"event_id": eid})).get("event") or e
            except RuntimeError:
                _detail_cache[key] = e
            if len(_detail_cache) > 500:
                _detail_cache.pop(next(iter(_detail_cache)))
        d = {**e, **{k: v for k, v in _detail_cache[key].items() if v}}
        if d.get("time"):
            out.append(from_kakao(d, tz))
    return out


async def all_events(cfg: dict, store, start: datetime, end: datetime) -> list[dict]:
    """구글 캘린더 + (켜져 있으면) 카톡 톡캘린더 일정. 브리핑·출발 알림·일정 조회가 함께 쓴다."""
    import asyncio

    from .tools import google
    tc = cfg.get("talkcal") or {}
    try:
        events = await asyncio.to_thread(google.list_events, start, end)
    except RuntimeError:
        if not (tc.get("enabled") and tc.get("read", True)):
            raise
        events = []
    if tc.get("enabled") and tc.get("read", True) and not store.get("talkcal_paused"):
        try:
            events = events + await kakao_events(cfg, store, start, end)
        except Exception:
            pass                                    # 카톡 쪽이 실패해도 구글 일정은 보여준다
    return sorted(events, key=lambda e: e["start"])
