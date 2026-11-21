"""중간 지점 찾기: 여러 출발지에서 대중교통 시간이 비슷한 곳(지하철역)을 고르고 근처 가게 후보를 붙인다.

방법: 출발지들의 무게중심 근처 지하철역 후보 → 각 출발지에서 후보까지 대중교통 시간(카카오) →
'가장 오래 걸리는 사람의 시간'이 가장 짧은 역 (같으면 시간 차이가 작은 역).
"""
from __future__ import annotations

import asyncio

import httpx

from . import commute, pages
from .tools.base import tool


async def stations_near(x: float, y: float, radius: int = 5000, size: int = 6) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{commute.LOCAL}/category.json", headers=commute._headers(),
                        params={"category_group_code": "SW8", "x": x, "y": y, "radius": radius, "sort": "distance", "size": size})
    seen, out = set(), []
    for d in (r.json().get("documents") or []) if r.status_code == 200 else []:
        name = d["place_name"].split()[0]          # '서면역 부산1호선' → '서면역'
        if name not in seen:
            seen.add(name)
            out.append({"name": name, "x": float(d["x"]), "y": float(d["y"])})
    return out


async def places_near(what: str, p: dict, size: int = 5) -> list[dict]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{commute.LOCAL}/keyword.json", headers=commute._headers(),
                        params={"query": what, "x": p["x"], "y": p["y"], "radius": 700, "sort": "accuracy", "size": size})
    return [{"name": d["place_name"], "x": float(d["x"]), "y": float(d["y"]),
             "category": d.get("category_name", "").split(" > ")[-1]}
            for d in (r.json().get("documents") or [])] if r.status_code == 200 else []


def pick(times: dict[str, list[int | None]]) -> list[tuple[str, int, int]]:
    """{후보: [사람별 분]} → [(후보, 최대, 차이)] 좋은 순. 한 명이라도 경로가 없으면 제외."""
    ranked = [(name, max(ts), max(ts) - min(ts)) for name, ts in times.items() if ts and all(t is not None for t in ts)]
    return sorted(ranked, key=lambda r: (r[1], r[2]))


async def find(origins: list[str]) -> dict:
    geos = await asyncio.gather(*(commute.geocode(o) for o in origins))
    missing = [o for o, g in zip(origins, geos) if not g]
    if missing:
        return {"error": f"찾지 못한 출발지: {', '.join(missing)}"}
    cx, cy = sum(g["x"] for g in geos) / len(geos), sum(g["y"] for g in geos) / len(geos)
    cands = await stations_near(cx, cy)
    if not cands:
        return {"error": "중간 지점 근처에서 지하철역을 찾지 못했어요 (지하철이 없는 지역일 수 있어요)."}

    async def minutes(o, c):
        try:
            if commute.distance_m(o, c) < 400:
                return 5
            t = await commute.transit(o, c)
            return t["minutes"] if t else None
        except Exception:
            return None
    grid = await asyncio.gather(*(minutes(o, c) for c in cands for o in geos))
    times = {c["name"]: list(grid[i * len(geos):(i + 1) * len(geos)]) for i, c in enumerate(cands)}
    ranked = pick(times)
    if not ranked:
        return {"error": "대중교통 경로를 계산하지 못했어요 (카카오맵 API 활성화·쿼터를 확인하세요)."}
    best = next(c for c in cands if c["name"] == ranked[0][0])
    return {"origins": [g["name"] for g in geos], "best": best, "times": times, "ranked": ranked[:3]}


@tool("meet_midpoint", "여러 사람의 출발지에서 대중교통 시간이 비슷한 중간 지점(지하철역)을 찾고, 그 근처 가게 후보와 "
      "공유용 약속 카드 링크를 만든다. origins는 2~6개 (역·동 이름·주소). what은 찾을 가게 종류 (예: 고깃집, 카페).",
      {"properties": {"origins": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
                      "what": {"type": "string", "default": "맛집"}, "title": {"type": "string", "default": "약속"},
                      "when": {"type": "string", "default": ""}}, "required": ["origins"]}, group="share")
async def meet_midpoint(ctx, origins: list[str], what: str = "맛집", title: str = "약속", when: str = ""):
    r = await find(origins[:6])
    if r.get("error"):
        return r["error"]
    best = r["best"]
    shops = await places_near(what, best)
    card = pages.card_url(ctx.cfg, {"t": "meet", "title": title, "when": when, "place": pages._pt(best),
                                    "pts": [pages._pt(s, m=s["category"]) for s in shops[:5]]})
    return {"중간 지점": best["name"],
            "사람별 대중교통(분)": dict(zip(r["origins"], r["times"][best["name"]])),
            "다른 후보": [f"{n}: 최대 {mx}분, 차이 {gap}분" for n, mx, gap in r["ranked"][1:]],
            f"근처 {what}": [f"{s['name']} ({s['category']})" for s in shops[:5]],
            "공유 카드": card}
