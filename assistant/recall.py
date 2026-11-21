"""기억 검색: "지난달 그 국밥집 어디였지?", "치과 마지막으로 언제 갔지?"

한 번에 찾는 곳: 지난 대화(보관 1년), 지난 캘린더 일정, 지출(Notion), Notion 메모·나중에 읽기·개발 일지(Notion 검색).
한국어는 조사가 붙어 단어 검색이 어긋나므로, 지휘자가 키워드를 여러 형태로(국밥, 국밥집) 넘기게 한다.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .tools import dev
from .tools.base import tool

SCHEMA = "CREATE TABLE IF NOT EXISTS archive (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel_id TEXT, role TEXT, text TEXT)"
KEEP_DAYS = 365


def init(store) -> None:
    store._exec(SCHEMA)


def archive(store, channel_id: str, role: str, text: str) -> None:
    if not text or not text.strip():
        return
    init(store)
    store._exec("INSERT INTO archive (ts, channel_id, role, text) VALUES (?,?,?,?)",
                (time.time(), channel_id, role, text[:4000]))
    if int(time.time()) % 50 == 0:        # 가끔 오래된 것 정리
        store._exec("DELETE FROM archive WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))


def search_archive(store, keywords: list[str], since: float, limit: int = 10) -> list[dict]:
    init(store)
    kws = [k.strip() for k in keywords if k and len(k.strip()) >= 2][:6]
    if not kws:
        return []
    where = " OR ".join("text LIKE ?" for _ in kws)
    rows = store._exec(f"SELECT ts, role, text FROM archive WHERE ts >= ? AND ({where}) ORDER BY ts DESC LIMIT 200",
                       (since, *[f"%{k}%" for k in kws]))
    scored = sorted(rows, key=lambda r: (-sum(r[2].count(k) for k in kws), -r[0]))[:limit]
    out = []
    for ts, role, text in sorted(scored, key=lambda r: -r[0]):
        i = min((text.find(k) for k in kws if k in text), default=0)
        snippet = text[max(0, i - 80): i + 160].replace("\n", " ")
        out.append({"ts": ts, "who": "나" if role == "user" else "비서", "text": snippet})
    return out


def _events(keywords: list[str], start: datetime, end: datetime) -> list[dict]:
    from .tools import google
    if google.TOKEN.exists():
        svc = google._service("calendar", "v3")
        seen, out = set(), []
        for k in keywords[:3]:
            items = svc.events().list(calendarId="primary", q=k, timeMin=start.isoformat(), timeMax=end.isoformat(),
                                      singleEvents=True, orderBy="startTime", maxResults=20).execute().get("items", [])
            for e in items:
                if e["id"] in seen:
                    continue
                seen.add(e["id"])
                out.append({"date": e["start"].get("dateTime", e["start"].get("date", ""))[:16],
                            "title": e.get("summary", ""), "location": e.get("location", "")})
        return sorted(out, key=lambda x: x["date"], reverse=True)[:15]
    evs = google.list_events(start, end)
    return [{"date": e["start"][:16], "title": e["title"], "location": e.get("location", "")}
            for e in evs if any(k in (e["title"] + e.get("location", "") + e.get("description", "")) for k in keywords)][-15:]


async def _expenses(cfg: dict, keywords: list[str], start: datetime) -> list[str]:
    db = (cfg.get("notion") or {}).get("expense_database_id")
    if not db:
        return []
    ors = [{"property": p, ("title" if p == "내용" else "rich_text"): {"contains": k}} for k in keywords[:3] for p in ("내용", "가게")]
    res = await dev.notion("POST", f"/databases/{db}/query", {
        "filter": {"and": [{"property": "날짜", "date": {"on_or_after": start.date().isoformat()}}, {"or": ors}]},
        "sorts": [{"property": "날짜", "direction": "descending"}], "page_size": 15})
    out = []
    for p in res.get("results", []):
        pr = p["properties"]
        out.append(f"{(pr['날짜']['date'] or {}).get('start', '')} {dev._plain(pr['내용']['title'])} "
                   f"{dev._plain(pr['가게']['rich_text'])} {int(pr['금액']['number'] or 0):,}원")
    return out


async def _notion(keywords: list[str]) -> list[str]:
    seen, out = set(), []
    for k in keywords[:3]:
        res = await dev.notion("POST", "/search", {"query": k, "page_size": 8,
                                                  "sort": {"direction": "descending", "timestamp": "last_edited_time"}})
        for p in res.get("results", []):
            if p["id"] in seen or p.get("object") != "page":
                continue
            seen.add(p["id"])
            title = ""
            for v in (p.get("properties") or {}).values():
                if v.get("type") == "title":
                    title = dev._plain(v["title"])
            out.append(f"{p.get('last_edited_time', '')[:10]} {title or '(제목 없음)'} {p.get('url', '')}")
    return out[:10]


@tool("recall", "과거 기록을 한 번에 찾는다: 지난 대화, 지난 캘린더 일정, 지출, Notion(메모·나중에 읽기·개발 일지). "
      "'지난번 ○○ 어디였지', '○○ 언제 했지', '전에 말한 ○○' 같은 질문에 쓴다. 한국어는 조사 때문에 어긋나니 "
      "keywords에 여러 형태를 넣는다 (예: ['국밥', '국밥집', '돼지국밥']).",
      {"properties": {"keywords": {"type": "array", "items": {"type": "string"}, "description": "2~6개, 각 2글자 이상"},
                      "days": {"type": "integer", "default": 365, "description": "며칠 전까지 찾을지"}},
       "required": ["keywords"]}, group="recall")
async def recall(ctx, keywords: list[str], days: int = 365):
    tz = ZoneInfo(ctx.cfg["timezone"])
    now = datetime.now(tz)
    start = now - timedelta(days=min(days, KEEP_DAYS))
    kws = [k for k in keywords if k and len(k.strip()) >= 2]

    async def safe(coro):
        try:
            return await coro
        except Exception as e:
            return f"(못 찾음: {type(e).__name__})"

    conv = search_archive(ctx.store, kws, start.timestamp())
    ev, money, notes = await asyncio.gather(safe(asyncio.to_thread(_events, kws, start, now)),
                                            safe(_expenses(ctx.cfg, kws, start)), safe(_notion(kws)))
    return {"대화": [f"{datetime.fromtimestamp(c['ts'], tz):%Y-%m-%d} {c['who']}: {c['text']}" for c in conv],
            "지난 일정": ev, "지출": money, "Notion": notes}
