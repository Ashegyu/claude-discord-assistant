"""생활 도구: 날씨(Open-Meteo, 키 불필요), 카카오 장소 검색, 카카오톡 나에게 보내기.
환경변수: KAKAO_REST_API_KEY, KAKAO_REFRESH_TOKEN(최초 1회), KAKAO_CLIENT_SECRET(선택)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from .base import tool

KAKAO_TOKEN_FILE = Path(os.environ.get("DATA_DIR", "data")) / "kakao.json"
WMO = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개", 51: "이슬비", 53: "이슬비", 55: "이슬비",
       61: "비", 63: "비", 65: "강한 비", 71: "눈", 73: "눈", 75: "강한 눈", 80: "소나기", 81: "소나기", 82: "강한 소나기",
       95: "뇌우", 96: "뇌우", 99: "뇌우"}


async def _geocode(place: str) -> tuple[float, float, str]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://geocoding-api.open-meteo.com/v1/search",
                        params={"name": place, "count": 1, "language": "ko"})
    res = r.json().get("results") or []
    if not res:
        raise RuntimeError(f"장소를 찾지 못했습니다: {place}")
    return res[0]["latitude"], res[0]["longitude"], res[0].get("name", place)


@tool("weather", "현재 날씨와 앞으로 며칠 예보 (기온·강수확률·바람). 일부 소도시(예: 김해)는 지명 검색에 실패한다. "
      "'장소를 찾지 못했습니다' 오류가 나면 이름을 바꿔 재시도하지 말고 가까운 큰 도시(Busan)로 한 번만 조회한 뒤, 그 기준임을 보고에 밝혀라.",
      {"properties": {"place": {"type": "string", "description": "도시 이름 (영문이 더 정확: Busan, Seoul)"},
                      "days": {"type": "integer", "default": 3, "maximum": 7}}}, group="life")
async def weather(ctx, place: str = "", days: int = 3):
    lat, lon, name = await _geocode(place or ctx.cfg["weather"]["default_place"])
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon, "timezone": ctx.cfg["timezone"], "forecast_days": min(days, 7),
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"})
    d = r.json()
    cur, day = d["current"], d["daily"]
    return {"place": name,
            "now": f"{WMO.get(cur['weather_code'], cur['weather_code'])}, {cur['temperature_2m']}°C (체감 {cur['apparent_temperature']}°C), 바람 {cur['wind_speed_10m']}km/h",
            "daily": [{"date": day["time"][i], "sky": WMO.get(day["weather_code"][i], day["weather_code"][i]),
                       "min": day["temperature_2m_min"][i], "max": day["temperature_2m_max"][i],
                       "rain_prob": day["precipitation_probability_max"][i], "wind_max": day["wind_speed_10m_max"][i]}
                      for i in range(len(day["time"]))]}


@tool("kakao_place_search", "카카오맵 장소 검색 (맛집, 카페, 가게, 병원 등). 기준 위치 근처 검색 가능.",
      {"properties": {"query": {"type": "string", "description": "예: '서면 돼지국밥', '해운대 카페'"},
                      "near": {"type": "string", "description": "기준 위치 (주소나 장소명, 선택)", "default": ""},
                      "radius_m": {"type": "integer", "default": 1000}},
       "required": ["query"]}, group="life")
async def kakao_place_search(ctx, query: str, near: str = "", radius_m: int = 1000):
    key = os.environ.get("KAKAO_REST_API_KEY")
    if not key:
        return "카카오 REST API 키가 설정되지 않았습니다."
    h = {"Authorization": f"KakaoAK {key}"}
    params = {"query": query, "size": 10}
    async with httpx.AsyncClient(timeout=15) as c:
        if near:
            g = (await c.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=h, params={"query": near, "size": 1})).json()
            if g.get("documents"):
                params.update(x=g["documents"][0]["x"], y=g["documents"][0]["y"], radius=min(radius_m, 20000), sort="distance")
        r = await c.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=h, params=params)
    if r.status_code != 200:
        return f"카카오 검색 실패 ({r.status_code}): {r.text[:200]}"
    return [{"name": p["place_name"], "category": p["category_name"].split(" > ")[-1],
             "address": p.get("road_address_name") or p["address_name"], "distance_m": p.get("distance") or None,
             "phone": p.get("phone"), "url": p["place_url"].replace("http://", "https://")}
            for p in r.json().get("documents", [])] or "결과 없음"


# ───────────── 카카오톡 나에게 보내기 ─────────────
async def _kakao_access() -> str:
    saved = json.loads(KAKAO_TOKEN_FILE.read_text()) if KAKAO_TOKEN_FILE.exists() else {}
    refresh = saved.get("refresh_token") or os.environ.get("KAKAO_REFRESH_TOKEN")
    if not refresh:
        raise RuntimeError("카카오 리프레시 토큰이 없습니다.")
    data = {"grant_type": "refresh_token", "client_id": os.environ["KAKAO_REST_API_KEY"], "refresh_token": refresh}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    async with httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=3)) as c:
        r = await c.post("https://kauth.kakao.com/oauth/token", data=data)
    if r.status_code != 200:
        raise RuntimeError(f"카카오 토큰 갱신 실패: {r.text[:200]}")
    body = r.json()
    if body.get("refresh_token"):  # 만료 1개월 전부터 새 토큰 → 파일에 저장
        KAKAO_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        KAKAO_TOKEN_FILE.write_text(json.dumps({"refresh_token": body["refresh_token"]}))
    return body["access_token"]


async def kakao_send_text(text: str, url: str = "https://www.kakao.com") -> None:
    token = await _kakao_access()
    for chunk in [text[i:i + 190] for i in range(0, len(text), 190)][:5]:
        tmpl = {"object_type": "text", "text": chunk, "link": {"web_url": url, "mobile_web_url": url}}
        async with httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=3)) as c:
            r = await c.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                             headers={"Authorization": f"Bearer {token}"},
                             data={"template_object": json.dumps(tmpl, ensure_ascii=False)})
        if r.status_code != 200:
            raise RuntimeError(f"카톡 전송 실패: {r.text[:200]}")


async def kakao_send_card(text: str, url: str, button: str = "카드 열기") -> None:
    """나와의 채팅으로 '글 + 링크 버튼' 한 통. 카톡에서 길게 눌러 전달하면 친구에게 보낼 수 있다."""
    token = await _kakao_access()
    tmpl = {"object_type": "text", "text": text[:190], "link": {"web_url": url, "mobile_web_url": url}, "button_title": button}
    async with httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=3)) as c:
        r = await c.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                         headers={"Authorization": f"Bearer {token}"},
                         data={"template_object": json.dumps(tmpl, ensure_ascii=False)})
    if r.status_code != 200:
        raise RuntimeError(f"카톡 전송 실패: {r.text[:200]}")


@tool("kakao_send_me", "카카오톡 '나와의 채팅'으로 메시지를 보낸다 (사용자 확인 후). 긴 글은 190자씩 최대 5통.",
      {"properties": {"text": {"type": "string"}}, "required": ["text"]}, group="life", confirm=True,
      summarize=lambda a: f"💬 카톡 나에게 보내기:\n{(a.get('text') or '')[:400]}")
async def kakao_send_me(ctx, text: str):
    await kakao_send_text(text)
    return "카톡으로 보냈습니다."
