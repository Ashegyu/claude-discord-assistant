"""웹 카드·대시보드 (GitHub Pages + 카카오 JavaScript SDK).

- 카드(약속·내비·쿠폰·오늘 동선): 내용은 링크의 # 뒤에만 들어간다. 공개 사이트에는 빈 틀(site/card.html)만 있다.
  페이지 버튼이 카카오내비 실행(Kakao.Navi), 카카오톡 공유(Kakao.Share), 카카오맵 길찾기를 연다.
- 대시보드: 봇이 AES-GCM으로 암호화한 dash.json을 gh-pages 브랜치에 올리고, 열쇠는 링크의 # 뒤에만 있다.
  (정적 사이트는 로그인으로 파일을 숨길 수 없어서, 로그인 대신 암호화를 쓴다.)
.env: KAKAO_JS_KEY (카카오 JavaScript 키 — 공개돼도 되는 키, 도메인으로 제한됨)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote

from . import commute, costs, coupons, cycles, todo
from .tools import dev
from .tools.base import tool


def _cfg(cfg: dict) -> dict:
    return cfg.get("pages") or {}


def base_url(cfg: dict) -> str:
    return (_cfg(cfg).get("base_url") or "").rstrip("/")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def card_url(cfg: dict, data: dict) -> str:
    if not base_url(cfg):
        raise RuntimeError("config.yaml의 pages.base_url이 비어 있어요")
    d = {"k": os.environ.get("KAKAO_JS_KEY", "").strip(), **{k: v for k, v in data.items() if v not in (None, "", [])}}
    return f"{base_url(cfg)}/card.html#d={_b64(json.dumps(d, ensure_ascii=False, separators=(',', ':')).encode())}"


def decode_card(url: str) -> dict:
    s = url.split("#d=")[1]
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))


def _pt(g: dict, **extra) -> dict:
    return {"n": g["name"][:40], "x": round(g["x"], 6), "y": round(g["y"], 6), **extra}


def go_url(cfg: dict, dest: dict, title: str = "") -> str:
    """출발 알림용: 카카오내비 버튼이 있는 작은 카드."""
    return card_url(cfg, {"t": "go", "title": title or dest["name"], "place": _pt(dest)})


def kakaomap_to(dest: dict) -> str:
    return f"https://map.kakao.com/link/to/{quote(dest['name'])},{dest['y']},{dest['x']}"


# ───────────── 도구: 약속 카드 · 쿠폰 카드 ─────────────
@tool("share_card", "약속·모임 카드 링크를 만든다. 페이지에 지도, 카카오내비·길찾기 버튼, [카카오톡으로 공유] 버튼이 있어 "
      "사용자가 친구·단톡방에 직접 보낼 수 있다. place는 장소 이름이나 주소. 결과 링크를 그대로 사용자에게 전달하라.",
      {"properties": {"title": {"type": "string"}, "when": {"type": "string", "description": "사람이 읽는 형태 (예: 11/21(토) 19:00)"},
                      "place": {"type": "string"}, "note": {"type": "string", "default": ""}},
       "required": ["title", "place"]}, group="share")
async def share_card(ctx, title: str, place: str, when: str = "", note: str = ""):
    g = await commute.geocode(place)
    if not g:
        return f"'{place}'를 지도에서 찾지 못했어요. 더 정확한 이름이나 주소로 다시 해주세요."
    return f"약속 카드: {card_url(ctx.cfg, {'t': 'meet', 'title': title, 'when': when, 'place': _pt(g), 'note': note[:200]})}"


@tool("coupon_share", "기프티콘을 다른 사람에게 넘길 카드 링크를 만든다 (상품·만료일·쿠폰 번호와 번호로 그린 바코드, 카톡 공유 버튼). "
      "링크를 받은 사람은 누구나 쿠폰 번호를 볼 수 있으니 사용자가 넘기려는 게 맞을 때만 쓴다.",
      {"properties": {"query": {"type": "string", "description": "쿠폰 번호(#3)나 이름 일부"}}, "required": ["query"]},
      group="share", confirm=True, summarize=lambda a: f"🎁 쿠폰 공유 카드 만들기: {a.get('query')}\n(링크를 가진 사람은 쿠폰 번호를 볼 수 있어요)")
async def coupon_share(ctx, query: str):
    hits = coupons.find(ctx.store, datetime.now().date(), query)
    if len(hits) != 1:
        return "맞는 쿠폰이 없어요." if not hits else "여러 개가 맞아요: " + "; ".join(f"#{c['id']} {c['item']}" for c in hits)
    c = hits[0]
    if not c["code"]:
        return "이 쿠폰은 번호를 읽지 못해서 카드로 만들 수 없어요. coupon_show로 원본 이미지를 받아 직접 공유하세요."
    return f"쿠폰 카드: {card_url(ctx.cfg, {'t': 'coupon', 'title': c['item'] or c['brand'], 'exp': c['expires'], 'code': c['code']})}"


# ───────────── 오늘 동선 지도 ─────────────
async def day_map_url(cfg: dict, events: list[dict], tz) -> str:
    pts = []
    for e in events:
        if not commute.is_candidate(e):
            continue
        g = await commute.geocode(e["location"])
        if g:
            hm = datetime.fromisoformat(e["start"]).astimezone(tz).strftime("%H:%M")
            pts.append(_pt(g, t=hm, m=e["title"][:30]))
    if not pts:
        return ""
    return card_url(cfg, {"t": "map", "title": "오늘 동선", "pts": pts[:8]})


# ───────────── 대시보드 ─────────────
def dash_key(store, create: bool = False) -> str:
    k = store.get("dash_key")
    if not k and create:
        k = _b64(os.urandom(32))
        store.put("dash_key", k)
    return k or ""


def encrypt(key_b64: str, data: dict) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(data, ensure_ascii=False).encode(), None)
    return {"iv": _b64(iv), "ct": _b64(ct)}


def decrypt(key_b64: str, enc: dict) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    return json.loads(AESGCM(pad(key_b64)).decrypt(pad(enc["iv"]), pad(enc["ct"]), None))


async def dash_data(cfg: dict, store, now: datetime) -> dict:
    from . import briefing, expenses
    tz = now.tzinfo

    async def safe(coro, default=None):
        try:
            return await coro
        except Exception:
            return default

    async def tasks():
        if not todo.enabled():
            return None
        ts = sorted(await todo.open_tasks(cfg, store), key=lambda t: todo.sort_key(t, tz))[:15]
        out = []
        for t in ts:
            d = todo.when_of(t, tz) or todo.due_of(t, tz)
            out.append((f"{d:%m/%d %H:%M} " if d else "") + t["title"])
        return out

    async def spend():
        s = expenses.summarize(await expenses.fetch(cfg, now.date().replace(day=1), now.date() + timedelta(days=1)))
        return s

    weather, sched, tk, sp = await asyncio.gather(safe(briefing.weather_line(cfg), ""), safe(briefing.schedule_lines(cfg, store, now), []),
                                                  safe(tasks()), safe(spend()))
    month_cost, _ = costs.cost_since(store, cfg, costs.month_start(tz, now))
    d = {"at": f"{now:%m/%d %H:%M}", "weather": weather.replace("🌤️ ", "") if weather else "",
         "events": [l.lstrip("- ").strip() for l in sched or []], "tasks": tk or [],
         "coupons": [l.replace("`", "") for l in coupons.lines(store, now.date())][:8],
         "cycles": [l.replace("*", "") for l in cycles.lines(store, now.date())][:8],
         "cost": f"${month_cost:.2f}"}
    if sp:
        d["spend"] = f"{int(sp['total']):,}원 ({sp['count']}건)"
        d["spend_cats"] = [f"{k} {int(v):,}원" for k, v in sorted(sp["by_cat"].items(), key=lambda x: -x[1])]
    return d


async def publish_dash(cfg: dict, store, now: datetime, force: bool = False) -> str:
    """암호화한 대시보드를 gh-pages에 올리고 링크를 돌려준다. 내용이 그대로면 올리지 않는다."""
    repo, branch = _cfg(cfg).get("repo", "Ashegyu/claude-discord-assistant"), _cfg(cfg).get("branch", "gh-pages")
    key = dash_key(store, create=True)
    data = await dash_data(cfg, store, now)
    sig = hashlib.sha256(json.dumps({**data, "at": ""}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    url = f"{base_url(cfg)}/dash.html#k={key}"
    if not force and store.get("dash_sig") == sig:
        return url
    body = {"message": "dashboard", "branch": branch,
            "content": base64.b64encode(json.dumps(encrypt(key, data)).encode()).decode()}
    try:
        cur = await dev.gh("GET", f"/repos/{repo}/contents/dash.json", params={"ref": branch})
        body["sha"] = cur["sha"]
    except RuntimeError as e:
        if "404" not in str(e):
            raise
    await dev.gh("PUT", f"/repos/{repo}/contents/dash.json", json=body)
    store.put("dash_sig", sig)
    return url


# ───────────── 답변 속 카드 링크 → [내 카톡으로 보내기] ─────────────
def card_links(cfg: dict, text: str) -> list[str]:
    import re
    base = base_url(cfg)
    if not base:
        return []
    found = re.findall(re.escape(base) + r"/card\.html#d=[A-Za-z0-9_-]+", text or "")
    return list(dict.fromkeys(found))[:3]


def card_summary(url: str) -> str:
    """카톡 메시지에 넣을 짧은 글 (카드 종류에 맞게)."""
    try:
        d = decode_card(url)
    except Exception:
        return "카드"
    lines = [("🎁 " if d.get("t") == "coupon" else "📍 " if d.get("t") in ("meet", "go") else "🗺️ ") + d.get("title", "카드")]
    if d.get("when"):
        lines.append(f"🕒 {d['when']}")
    if d.get("place"):
        lines.append(f"장소: {d['place']['n']}")
    if d.get("exp"):
        lines.append(f"⏳ {d['exp']} 까지")
    return "\n".join(lines)
