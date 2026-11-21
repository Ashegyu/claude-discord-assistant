"""통합 아침 브리핑: 날씨·미세먼지·오늘 일정과 첫 출발·할 일·교체 주기·만료 쿠폰·밤사이 모아둔 알림을 한 장으로.

미세먼지는 Open-Meteo 대기질(모델 예보, 키 없음). 등급은 환경부 기준(PM10 30/80/150, PM2.5 15/35/75).
Claude는 쓰지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from . import commute, coupons, cycles, todo
from .tools import life

log = logging.getLogger("briefing")


def grade(pm10: float | None, pm25: float | None) -> str:
    def g(v, cuts):
        if v is None:
            return None
        return next((i for i, c in enumerate(cuts) if v <= c), 3)
    lv = max(x for x in (g(pm10, (30, 80, 150)), g(pm25, (15, 35, 75)), 0) if x is not None)
    return ["좋음 😊", "보통 🙂", "나쁨 😷", "매우 나쁨 🚫"][lv]


async def weather_line(cfg: dict) -> str:
    place = cfg["weather"]["default_place"]
    lat, lon, name = await life._geocode(place)
    async with httpx.AsyncClient(timeout=15) as c:
        w, a = await asyncio.gather(
            c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "timezone": cfg["timezone"], "forecast_days": 1,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"}),
            c.get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
                "latitude": lat, "longitude": lon, "timezone": cfg["timezone"], "forecast_days": 1,
                "hourly": "pm10,pm2_5"}))
    d = w.json()["daily"]
    sky = life.WMO.get(d["weather_code"][0], "")
    line = f"🌤️ {name} {sky} {d['temperature_2m_min'][0]:.0f}~{d['temperature_2m_max'][0]:.0f}°C · 비 {d['precipitation_probability_max'][0]}%"
    try:
        h = a.json()["hourly"]
        day = [i for i, t in enumerate(h["time"]) if "T07" <= t[10:13] <= "T21"]
        pm10 = max((h["pm10"][i] for i in day if h["pm10"][i] is not None), default=None)
        pm25 = max((h["pm2_5"][i] for i in day if h["pm2_5"][i] is not None), default=None)
        if pm10 is not None or pm25 is not None:
            line += f" · 미세먼지 {grade(pm10, pm25)} (PM10 {pm10:.0f} / PM2.5 {pm25:.0f})"
    except Exception:
        pass
    return line


async def schedule_lines(cfg: dict, store, now: datetime) -> list[str]:
    from . import talkcal
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await talkcal.all_events(cfg, store, start, start + timedelta(days=1))
    out = []
    for e in events:
        st = e.get("start", "")
        hm = datetime.fromisoformat(st).astimezone(now.tzinfo).strftime("%H:%M") if "T" in st else "종일"
        out.append(f"- {hm} {e.get('title', '')}" + (f" 📍{e['location'][:20]}" if e.get("location") else "")
                   + (" 💬" if e.get("source") == "톡캘린더" else ""))
    first = next((e for e in events if commute.is_candidate(e) and datetime.fromisoformat(e["start"]) > now), None)
    if first:
        try:
            p = await commute.plan(cfg, store, first, now)
            if p and not p.get("skip"):
                ways = " / ".join(f"{'🚌' if m == 'transit' else '🚗'} {t:%H:%M}" for m, t in p["leave"].items())
                out.append(f"  ↳ 첫 이동 '{first['title']}': {ways} 출발 (출발지 {p['origin']['label']})")
        except Exception as e:
            log.info("브리핑 출발 계산 생략: %s", e)
    return out


async def _day_map(cfg: dict, store, now: datetime) -> str:
    from . import pages, talkcal
    if not pages.base_url(cfg):
        return ""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await talkcal.all_events(cfg, store, start, start + timedelta(days=1))
    return await pages.day_map_url(cfg, events, now.tzinfo)


async def build(cfg: dict, store, now: datetime, queued: str = "") -> str:
    tz = now.tzinfo
    lines = [f"☀️ **{now:%m/%d(%a)} 아침 브리핑**"]

    async def safe(coro, default=None):
        try:
            return await coro
        except Exception as e:
            log.info("브리핑 일부 생략: %s", e)
            return default

    weather, sched = await asyncio.gather(safe(weather_line(cfg), ""), safe(schedule_lines(cfg, store, now), None))
    if weather:
        lines.append(weather)
    if sched is not None:
        lines.append("📅 **오늘 일정**" + ("" if sched else " 없음"))
        lines += sched
        day_map = await safe(_day_map(cfg, store, now), "")
        if day_map:
            lines.append(f"🗺️ [오늘 동선 지도](<{day_map}>)")
    if todo.enabled():
        tasks = await safe(todo.open_tasks(cfg, store), [])
        today = [t for t in tasks if todo.is_today_or_overdue(t, tz, now)]
        today.sort(key=lambda t: todo.sort_key(t, tz))
        lines.append("✅ **할 일**" + ("" if today else " 없음"))
        for t in today[:10]:
            d = todo.when_of(t, tz) or todo.due_of(t, tz)
            late = " ⚠️밀림" if d.date() < now.date() else ""
            hm = f"{d:%H:%M} " if todo.when_of(t, tz) and d.date() == now.date() else ""
            lines.append(f"- {hm}{t['title']}{late}")
    soon = cycles.upcoming(store, now.date(), 3)
    if soon:
        def when(c):
            return "오늘" if c["left"] == 0 else ("지남" if c["left"] < 0 else f"D-{c['left']}")
        lines.append("🔁 " + " · ".join(f"{c['name']} {when(c)}" for c in soon))
    exp = [c for c in coupons.active(store, now.date()) if c["days_left"] is not None and 0 <= c["days_left"] <= 3]
    if exp:
        lines.append("🎁 곧 만료: " + ", ".join(f"{c['item']} D-{c['days_left']}" for c in exp))
    if queued:
        lines += ["", queued]
    return "\n".join(lines)
