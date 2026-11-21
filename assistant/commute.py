"""출발 알림: 장소가 있는 캘린더 일정 → 대중교통·자동차 이동 시간 → 출발해야 할 시각 전에 알림.

- 장소 → 좌표: 카카오 로컬 (키워드 → 주소 순)
- 대중교통: 카카오맵 REST `GET dapi.kakao.com/v2/routing/publictraffic` (일 1,000건 무료, 카카오맵 API 활성화 필요)
- 자동차: 카카오모빌리티 `GET apis-navi.kakaomobility.com/v1/directions` (일 10,000건 무료)
- 도착지 강수: Open-Meteo (무료, 키 없음)
- 출발지: `!place 집 …` / `!place 회사 …`로 등록, 평일 근무시간이면 회사·아니면 집. `!origin`이나 알림 카드 버튼으로 바꿈
Claude는 쓰지 않는다 (비용 0).
"""
from __future__ import annotations

import asyncio
import math
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

LOCAL = "https://dapi.kakao.com/v2/local/search"
TRANSIT = "https://dapi.kakao.com/v2/routing/publictraffic"
CAR = "https://apis-navi.kakaomobility.com/v1/directions"
ONLINE = re.compile(r"https?://|zoom\.|meet\.google|teams\.|webex|온라인|화상", re.I)
PLACES = ("집", "회사")


def _cfg(cfg: dict) -> dict:
    return cfg.get("commute") or {}


def _headers() -> dict:
    key = os.environ.get("KAKAO_REST_API_KEY", "")
    if not key:
        raise RuntimeError("카카오 REST API 키가 없어요")
    return {"Authorization": f"KakaoAK {key}"}


async def geocode(query: str) -> dict | None:
    """{name, x, y} (x=경도, y=위도)"""
    q = query.split("\n")[0].strip()[:80]
    async with httpx.AsyncClient(timeout=15) as c:
        for kind in ("keyword", "address"):
            r = await c.get(f"{LOCAL}/{kind}.json", params={"query": q, "size": 1}, headers=_headers())
            docs = (r.json().get("documents") or []) if r.status_code == 200 else []
            if docs:
                d = docs[0]
                return {"name": d.get("place_name") or d.get("address_name") or q, "x": float(d["x"]), "y": float(d["y"])}
    return None


def distance_m(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(a["y"]), math.radians(b["y"])
    dlat, dlon = lat2 - lat1, math.radians(b["x"] - a["x"])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


async def transit(origin: dict, dest: dict) -> dict | None:
    params = {"start_x": origin["x"], "start_y": origin["y"], "end_x": dest["x"], "end_y": dest["y"],
              "s_name": origin.get("name", "출발")[:30], "e_name": dest.get("name", "도착")[:30]}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(TRANSIT, params=params, headers=_headers())
    if r.status_code != 200:
        raise RuntimeError(f"대중교통 경로 {r.status_code}: {r.text[:150]}")
    d = r.json()
    if d.get("status") != "OK" or not d.get("routes"):
        return None
    best = min(d["routes"], key=lambda rt: rt["properties"]["totalTime"])
    p = best["properties"]
    legs = []
    for st in best.get("steps", []):
        sp = st["properties"]
        if sp.get("type") in ("BUS", "SUBWAY"):
            names = "/".join(v.get("name", "") for v in sp.get("vehicles", [])[:2]) or sp.get("type")
            first = (sp.get("stops") or [{}])[0].get("name", "")
            legs.append(f"{'🚌' if sp['type'] == 'BUS' else '🚇'}{names}" + (f"({first})" if first else ""))
    return {"minutes": round(p["totalTime"] / 60), "transfers": p.get("transfers", 0),
            "fare": (p.get("fare") or {}).get("value"), "legs": legs}


async def car(origin: dict, dest: dict) -> dict | None:
    params = {"origin": f"{origin['x']},{origin['y']}", "destination": f"{dest['x']},{dest['y']}", "priority": "RECOMMEND"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(CAR, params=params, headers=_headers())
    if r.status_code != 200:
        raise RuntimeError(f"자동차 경로 {r.status_code}: {r.text[:150]}")
    routes = r.json().get("routes") or []
    if not routes or routes[0].get("result_code") != 0:
        return None
    s = routes[0]["summary"]
    return {"minutes": round(s["duration"] / 60), "km": round(s["distance"] / 1000, 1),
            "toll": (s.get("fare") or {}).get("toll", 0)}


async def rain_chance(dest: dict, when: datetime) -> int | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": dest["y"], "longitude": dest["x"], "hourly": "precipitation_probability",
                "timezone": str(when.tzinfo), "forecast_days": 3})
        h = r.json()["hourly"]
        key = when.strftime("%Y-%m-%dT%H:00")
        return h["precipitation_probability"][h["time"].index(key)] if key in h["time"] else None
    except Exception:
        return None


# ───────────── 출발지 ─────────────
def saved_place(store, name: str) -> dict | None:
    return store.get(f"place:{name}")


async def set_place(store, name: str, query: str) -> str:
    if name not in PLACES:
        return "`!place 집 주소` 또는 `!place 회사 주소` 형식으로 써주세요."
    g = await geocode(query)
    if not g:
        return f"'{query}'를 찾지 못했어요. 도로명 주소나 근처 건물 이름으로 다시 해보세요."
    store.put(f"place:{name}", g)
    return f"📍 {name} = {g['name']} 로 저장했어요."


def default_origin_name(cfg: dict, now: datetime) -> str:
    c = _cfg(cfg)
    days = c.get("work_days", [0, 1, 2, 3, 4])
    start, end = (c.get("work_hours") or "09:00-18:00").split("-")
    return "회사" if now.weekday() in days and start <= now.strftime("%H:%M") < end else "집"


async def origin_for(cfg: dict, store, now: datetime, prefer: str = "") -> dict | None:
    """우선순위: 지정(prefer) > 오늘 !origin > 시간 규칙."""
    over = store.get("origin_today") or {}
    name = prefer or (over.get("name") if over.get("date") == now.date().isoformat() else "") or default_origin_name(cfg, now)
    if name in PLACES:
        p = saved_place(store, name)
        return {**p, "label": name} if p else None
    if over.get("date") == now.date().isoformat() and over.get("place"):
        return {**over["place"], "label": over["place"]["name"]}
    return None


async def set_origin_today(store, now: datetime, text: str) -> str:
    text = text.strip()
    if not text:
        store.put("origin_today", {})
        return "오늘 출발지 지정을 지웠어요 (시간 규칙으로 돌아가요)."
    if text in PLACES:
        store.put("origin_today", {"date": now.date().isoformat(), "name": text})
        return f"오늘은 {text}에서 출발하는 걸로 계산할게요."
    g = await geocode(text)
    if not g:
        return f"'{text}'를 찾지 못했어요."
    store.put("origin_today", {"date": now.date().isoformat(), "place": g})
    return f"오늘은 {g['name']}에서 출발하는 걸로 계산할게요."


# ───────────── 계획·알림 ─────────────
def is_candidate(ev: dict) -> bool:
    loc = (ev.get("location") or "").strip()
    return bool(loc) and "T" in (ev.get("start") or "") and not ONLINE.search(loc)


async def plan(cfg: dict, store, ev: dict, now: datetime, prefer: str = "") -> dict | None:
    """일정 하나의 이동 계획. 출발지를 모르거나 목적지가 가까우면 None/skip."""
    tz = ZoneInfo(cfg["timezone"])
    start = datetime.fromisoformat(ev["start"]).astimezone(tz)
    origin = await origin_for(cfg, store, now, prefer)
    if not origin:
        return {"skip": "no_origin"}
    dest = await geocode(ev["location"])
    if not dest:
        return {"skip": "no_dest"}
    if distance_m(origin, dest) < _cfg(cfg).get("min_distance_m", 400):
        return {"skip": "near"}
    modes = _cfg(cfg).get("modes", ["transit", "car"])
    results = await asyncio.gather(*(transit(origin, dest) if m == "transit" else car(origin, dest) for m in modes),
                                   return_exceptions=True)
    routes = {m: (r if not isinstance(r, Exception) else {"error": str(r)[:120]}) for m, r in zip(modes, results) if r}
    usable = [r["minutes"] for r in routes.values() if r and "minutes" in r]
    if not usable:
        return {"skip": "no_route", "routes": routes}
    buffer = _cfg(cfg).get("buffer_min", 10)
    leave = {m: start - timedelta(minutes=r["minutes"] + buffer) for m, r in routes.items() if "minutes" in r}
    return {"event": ev, "start": start, "origin": origin, "dest": dest, "routes": routes, "leave": leave,
            "earliest_leave": min(leave.values()), "rain": await rain_chance(dest, start)}


def message(p: dict, now: datetime) -> str:
    ev, start = p["event"], p["start"]
    first = p["earliest_leave"]
    late = first < now
    head = (f"🚶 **{start:%H:%M} {ev['title']}** ({p['dest']['name']})\n"
            + ("⚠️ 지금 출발해도 빠듯해요" if late else f"**{first:%H:%M}**까지 출발하세요")
            + f" · 출발지: {p['origin']['label']}")
    lines = [head]
    t, c = p["routes"].get("transit"), p["routes"].get("car")
    if t and "minutes" in t:
        fare = f", {t['fare']:,}원" if t.get("fare") else ""
        lines.append(f"🚌 대중교통 {t['minutes']}분 (환승 {t['transfers']}{fare}) → {p['leave']['transit']:%H:%M} 출발"
                     + (f"\n   {' → '.join(t['legs'][:4])}" if t.get("legs") else ""))
    elif t and "error" in t:
        lines.append(f"🚌 대중교통 경로를 못 가져왔어요 ({t['error'][:60]})")
    if c and "minutes" in c:
        toll = f", 통행료 {c['toll']:,}원" if c.get("toll") else ""
        lines.append(f"🚗 자동차 {c['minutes']}분 ({c['km']}km{toll}) → {p['leave']['car']:%H:%M} 출발")
    if p.get("rain") is not None and p["rain"] >= 40:
        lines.append(f"☔ 도착 무렵 비 올 확률 {p['rain']}% — 우산 챙기세요")
    return "\n".join(lines)


async def due_alerts(cfg: dict, store, events: list[dict], now: datetime) -> list[dict]:
    """지금 보낼 출발 알림들. 일정마다 한 번만 (시간이 바뀐 일정은 다시)."""
    lead = timedelta(minutes=_cfg(cfg).get("lead_min", 15))
    out = []
    for ev in events:
        if not is_candidate(ev):
            continue
        key = f"commute:{ev.get('id') or ev['title']}:{ev['start']}"
        if store.get(key):
            continue
        cache_key = key + ":plan"
        cached = store.get(cache_key)
        if cached and now.timestamp() < cached["alert_at"]:
            continue     # 아직 알릴 때가 아님 (이동 시간은 알릴 때 다시 계산)
        p = await plan(cfg, store, ev, now)
        if not p or p.get("skip"):
            if p and p.get("skip") in ("near", "no_dest"):
                store.put(key, p["skip"])            # 다시 볼 필요 없음
            elif p and p.get("skip") == "no_origin" and not store.get("commute_no_origin_warned"):
                store.put("commute_no_origin_warned", True)
                out.append({"text": "📍 출발 알림을 쓰려면 집·회사를 등록해 주세요: `!place 집 주소`, `!place 회사 주소`"})
            continue
        alert_at = p["earliest_leave"] - lead
        if now < alert_at:
            store.put(cache_key, {"alert_at": alert_at.timestamp()})
            continue
        store.put(key, "sent")
        out.append({"text": message(p, now), "event": ev, "dest": p["dest"]})
    return out
