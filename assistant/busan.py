"""부산 실시간 묶음: 버스 도착(부산 BIMS) · 기상 특보(기상청) · 비·바람(Open-Meteo) · 파도(Open-Meteo Marine).

"지금 나가도 돼?" → outing_check 도구. 부산에 새 특보가 뜨면 바로 알림.
.env: DATA_GO_KR_KEY (공공데이터포털 '일반 인증키(Decoding)'. 부산버스정보시스템·기상청_기상특보 조회서비스 활용신청 필요)
도시철도는 공개된 실시간 도착 API가 없어 넣지 않았다 (대중교통 경로는 출발 알림의 카카오 경로가 담당).
"""
from __future__ import annotations

import asyncio
import os
import re
import xml.etree.ElementTree as ET

import httpx

from . import commute
from .tools.base import tool

BIMS = "http://apis.data.go.kr/6260000/BusanBIMS"
KMA_WARN = "http://apis.data.go.kr/1360000/WthrWrnInfoService/getPwnStatus"


def _key() -> str:
    k = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not k:
        raise RuntimeError("DATA_GO_KR_KEY가 없어요 (공공데이터포털 인증키)")
    return k


def parse_items(xml_text: str) -> list[dict]:
    """공공데이터포털 XML → [{태그: 값}]. 오류 응답이면 예외."""
    root = ET.fromstring(xml_text)
    err = root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg")
    code = root.findtext(".//resultCode")
    if err or (code and code not in ("00", "0")):
        raise RuntimeError(f"공공데이터 오류: {err or root.findtext('.//resultMsg') or code}")
    return [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]


async def _get(url: str, **params) -> str:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, params={"serviceKey": _key(), **params})
    if r.status_code != 200:
        raise RuntimeError(f"공공데이터 {r.status_code}")
    return r.text


# ───────────── 버스 ─────────────
async def search_stops(name: str) -> list[dict]:
    items = parse_items(await _get(f"{BIMS}/busStopList", bstopnm=name, pageNo=1, numOfRows=10))
    return [{"id": i.get("bstopid", ""), "name": i.get("bstopnm", ""), "ars": i.get("arsno", "")} for i in items if i.get("bstopid")]


async def arrivals(stop_id: str) -> list[dict]:
    items = parse_items(await _get(f"{BIMS}/stopArrByBstopid", bstopid=stop_id))
    out = []
    for i in items:
        if i.get("min1"):
            out.append({"line": i.get("lineno", "?"), "min": int(i["min1"]), "left": i.get("station1", ""),
                        "next": int(i["min2"]) if i.get("min2") else None})
    return sorted(out, key=lambda a: a["min"])


def favorites(store) -> list[dict]:
    return store.get("bus_stops") or []


async def add_favorite(store, query: str) -> str:
    q = query.strip()
    stops = await search_stops(q) if not q.isdigit() else []
    if q.isdigit():       # 정류소번호(ARS 5자리)로 지정
        stops = [s for s in await _stops_by_ars(q)]
    if not stops:
        return f"'{q}' 정류소를 찾지 못했어요."
    if len(stops) > 1:
        return "여러 곳이 있어요. 정류소번호로 다시 골라 주세요:\n" + "\n".join(f"`!bus add {s['ars']}` {s['name']}" for s in stops[:8] if s["ars"])
    favs = [f for f in favorites(store) if f["id"] != stops[0]["id"]] + [stops[0]]
    store.put("bus_stops", favs[-6:])
    return f"🚌 {stops[0]['name']} ({stops[0]['ars']}) 을 즐겨찾기에 넣었어요."


async def _stops_by_ars(ars: str) -> list[dict]:
    items = parse_items(await _get(f"{BIMS}/busStopList", arsno=ars, pageNo=1, numOfRows=5))
    return [{"id": i.get("bstopid", ""), "name": i.get("bstopnm", ""), "ars": i.get("arsno", "")} for i in items if i.get("bstopid")]


def remove_favorite(store, name: str) -> str:
    favs = favorites(store)
    keep = [f for f in favs if name.strip() not in (f["name"], f["ars"])]
    store.put("bus_stops", keep)
    return "지웠어요." if len(keep) < len(favs) else "그 정류소가 즐겨찾기에 없어요."


async def bus_lines(store, limit: int = 6) -> list[str]:
    out = []
    for f in favorites(store):
        try:
            arr = await arrivals(f["id"])
        except Exception as e:
            out.append(f"🚏 {f['name']}: 조회 실패 ({str(e)[:40]})")
            continue
        body = " · ".join(f"**{a['line']}** {a['min']}분" + (f"({a['next']}분)" if a["next"] else "") for a in arr[:limit])
        out.append(f"🚏 {f['name']}: {body or '도착 예정 버스 없음'}")
    return out


# ───────────── 특보 · 날씨 · 바다 ─────────────
def region_warnings(texts: list[str], region: str) -> list[str]:
    """특보 현황 글에서 해당 지역이 들어간 줄만. 예: 'o 강풍주의보 : 부산, 울산' → '강풍주의보'"""
    out = []
    for t in texts:
        for line in re.split(r"[\r\n]+|(?=o )", t or ""):
            if region in line and ":" in line:
                name = line.split(":")[0].replace("o", "", 1).strip(" ·-")
                if name and "없음" not in name and name not in out:
                    out.append(name)
    return out


async def warnings(region: str = "부산") -> list[str]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(KMA_WARN, params={"serviceKey": _key(), "dataType": "JSON", "numOfRows": 10, "pageNo": 1})
    body = r.json()["response"]
    if body["header"]["resultCode"] not in ("00", "03"):       # 03 = 데이터 없음
        raise RuntimeError(f"기상청 오류: {body['header'].get('resultMsg')}")
    items = ((body.get("body") or {}).get("items") or {}).get("item") or []
    texts = [v for it in items for k, v in it.items() if k in ("t6", "t7") and isinstance(v, str)]
    return region_warnings(texts, region)


async def sky_now(lat: float, lon: float, tz: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as c:
        w, m = await asyncio.gather(
            c.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": lat, "longitude": lon, "timezone": tz, "forecast_days": 1,
                "current": "temperature_2m,apparent_temperature,precipitation,wind_speed_10m,wind_gusts_10m",
                "hourly": "precipitation_probability"}),
            c.get("https://marine-api.open-meteo.com/v1/marine", params={
                "latitude": lat, "longitude": lon, "timezone": tz, "current": "wave_height"}))
    d = w.json()
    cur, h = d["current"], d["hourly"]
    i = next((n for n, t in enumerate(h["time"]) if t >= cur["time"][:13]), 0)
    rain3 = max([p for p in h["precipitation_probability"][i:i + 3] if p is not None], default=None)
    wave = None
    try:
        wave = m.json()["current"]["wave_height"]
    except Exception:
        pass
    return {"temp": cur["temperature_2m"], "feels": cur["apparent_temperature"], "rain_now": cur["precipitation"],
            "rain3h": rain3, "wind": cur["wind_speed_10m"], "gust": cur.get("wind_gusts_10m"), "wave": wave}


def verdict(sky: dict, warns: list[str]) -> str:
    if warns:
        return "⚠️ 특보가 있어요 — 꼭 필요한 외출만"
    if sky["rain_now"] and sky["rain_now"] > 0:
        return "☔ 지금 비가 와요 — 우산 필수"
    if sky.get("rain3h") is not None and sky["rain3h"] >= 60:
        return "🌂 3시간 안에 비 올 가능성이 높아요 — 우산 챙기세요"
    if sky.get("gust") and sky["gust"] >= 50:
        return "💨 바람이 강해요"
    return "👍 나가기 괜찮아요"


async def check(cfg: dict, store) -> str:
    place = (cfg.get("busan") or {}).get("place") or "부산시청"
    g = await commute.geocode(place)
    lat, lon = (g["y"], g["x"]) if g else (35.1796, 129.0756)
    sky, warns, buses = await asyncio.gather(sky_now(lat, lon, cfg["timezone"]), _safe(warnings()), _safe(bus_lines(store)))
    warn_list = warns if isinstance(warns, list) else []
    lines = [f"**{verdict(sky, warn_list)}**",
             f"🌡️ {sky['temp']:.0f}°C (체감 {sky['feels']:.0f}°C) · 3시간 내 비 {sky['rain3h'] if sky['rain3h'] is not None else '?'}% · "
             f"바람 {sky['wind']:.0f}km/h" + (f" (돌풍 {sky['gust']:.0f})" if sky.get("gust") else "")
             + (f" · 파도 {sky['wave']:.1f}m" if sky.get("wave") is not None else "")]
    if warn_list:
        lines.append("🚨 부산 특보: " + ", ".join(warn_list))
    elif isinstance(warns, str):
        lines.append(f"(특보 조회 생략: {warns})")
    if isinstance(buses, list):
        lines += buses
    elif favorites(store):
        lines.append(f"(버스 조회 생략: {buses})")
    return "\n".join(lines)


async def _safe(coro):
    try:
        return await coro
    except Exception as e:
        return str(e)[:60]


async def new_warnings(store, region: str = "부산") -> str | None:
    """특보가 새로 생기거나 풀리면 알림 문장."""
    now_set = sorted(await warnings(region))
    old = store.get("busan_warn")
    store.put("busan_warn", now_set)
    if old is None or now_set == old:
        return None
    added, cleared = [w for w in now_set if w not in old], [w for w in old if w not in now_set]
    parts = ([f"🚨 **{region} {', '.join(added)}** 발효"] if added else []) + ([f"✅ {region} {', '.join(cleared)} 해제"] if cleared else [])
    return "\n".join(parts) or None


@tool("outing_check", "'지금 나가도 돼?', '밖에 어때?' — 부산의 지금 날씨·3시간 내 비·바람·파도·기상 특보와 즐겨찾는 버스 정류소 도착 정보를 한 번에.",
      {"properties": {}}, group="life")
async def outing_check(ctx):
    return await check(ctx.cfg, ctx.store)


