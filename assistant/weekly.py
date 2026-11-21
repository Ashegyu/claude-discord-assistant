"""나의 한 주 회고: 일요일 밤, 한 주를 숫자로 모으고(코드) Sonnet이 짧은 코멘트를 붙여 디스코드 + Notion에 남긴다.

모으는 것: 일정(지난주·다음 주), 끝낸 할 일·남은 마감, 지출, 나중에 읽기, 커밋, 기프티콘 만료, 비서 비용.
하나가 실패해도(설정 안 됨 등) 나머지로 만든다.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import costs, coupons, expenses, todo
from .tools import dev

log = logging.getLogger("weekly")

PROMPT = """아래는 Kim의 지난 한 주 기록(JSON)이다. 한국어로 짧게 써라.
1) 첫 줄: 한 주를 한 문장으로 (숫자 하나 포함)
2) "눈에 띄는 점" 2~3개: 지난주 대비 변화, 몰린 요일, 많이 쓴 곳, 끝낸 일 등 데이터에 있는 것만. 칭찬·훈계·추측 금지
3) "다음 주" 1~2개: 다음 주 일정·마감·만료에서 챙길 것
불릿은 '- '로. 데이터에 없는 사실을 지어내지 마라. 전체 12줄 이내.

{data}"""


async def _safe(name: str, coro):
    try:
        return await coro
    except Exception as e:
        log.info("주간 회고 %s 생략: %s", name, e)
        return None


async def collect(cfg: dict, store, now: datetime) -> dict:
    from .tools import google
    tz = ZoneInfo(cfg["timezone"])
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)   # 오늘 포함
    start = end - timedelta(days=7)
    nxt_end = end + timedelta(days=7)

    async def events(a, b):
        evs = await asyncio.to_thread(google.list_events, a, b)
        return [f"{e['start'][5:16].replace('T', ' ')} {e['title']}" for e in evs]

    async def done_tasks():
        if not todo.enabled():
            return None
        return [t["title"] for t in await todo.completed_since(cfg, store, start)]

    async def due_next():
        if not todo.enabled():
            return None
        out = []
        for t in await todo.open_tasks(cfg, store):
            d = todo.when_of(t, tz) or todo.due_of(t, tz)
            if d and d < nxt_end:
                out.append(f"{d:%m/%d} {t['title']}" + (" (밀림)" if d < end else ""))
        return sorted(out)

    async def spend():
        cur = expenses.summarize(await expenses.fetch(cfg, start.date(), end.date()))
        prev = expenses.summarize(await expenses.fetch(cfg, (start - timedelta(days=7)).date(), start.date()))
        return {"total": cur["total"], "count": cur["count"], "by_cat": cur["by_cat"], "prev_total": prev["total"],
                "top": [f"{t['title'] or t['store']} {int(t['amount'])}" for t in cur["top"]]}

    async def reading():
        db = (cfg.get("notion") or {}).get("readlater_database_id")
        if not db:
            return None
        res = await dev.notion("POST", f"/databases/{db}/query", {
            "filter": {"property": "저장일", "date": {"on_or_after": start.date().isoformat()}}, "page_size": 100})
        rows = res.get("results", [])
        return {"saved": len(rows), "read": sum(1 for p in rows if p["properties"]["읽음"]["checkbox"])}

    async def commits():
        out = {}
        for repo in (cfg.get("github") or {}).get("repos", []):
            full = repo if "/" in repo else f"{cfg['github'].get('owner', 'Ashegyu')}/{repo}"
            items = await dev.gh("GET", f"/repos/{full}/commits", params={"since": start.isoformat(), "per_page": 100})
            if items:
                out[full.split("/")[-1]] = len(items)
        return out

    (past, upcoming, done, due, money, read, cm) = await asyncio.gather(
        _safe("지난 일정", events(start, end)), _safe("다음 일정", events(end, nxt_end)),
        _safe("끝낸 할 일", done_tasks()), _safe("마감", due_next()), _safe("지출", spend()),
        _safe("읽기", reading()), _safe("커밋", commits()))
    soon = [c for c in coupons.active(store, now.date()) if c["days_left"] is not None and 0 <= c["days_left"] <= 14]
    cost, _ = costs.cost_since(store, cfg, start.timestamp())
    return {"period": f"{start:%m/%d}~{(end - timedelta(days=1)):%m/%d}",
            "events_this_week": past, "events_next_week": upcoming, "tasks_done": done, "tasks_due_next_week": due,
            "spending_won": money, "reading": read, "commits": cm,
            "coupons_expiring": [f"{c['item']} {c['expires']}" for c in soon],
            "assistant_cost_usd": round(cost, 2)}


def numbers(d: dict) -> list[str]:
    """숫자 블록은 코드가 직접 쓴다 (모델이 숫자를 바꾸지 못하게)."""
    out = []
    if d["events_this_week"] is not None:
        out.append(f"📅 일정 {len(d['events_this_week'])}개" + (f" · 다음 주 {len(d['events_next_week'])}개" if d["events_next_week"] is not None else ""))
    if d["tasks_done"] is not None:
        out.append(f"✅ 끝낸 할 일 {len(d['tasks_done'])}개" + (f" · 다음 주 마감 {len(d['tasks_due_next_week'] or [])}개" if d["tasks_due_next_week"] is not None else ""))
    m = d["spending_won"]
    if m is not None:
        diff = m["total"] - m["prev_total"]
        out.append(f"🧾 지출 {int(m['total']):,}원 ({m['count']}건" + (f", 전주 대비 {'+' if diff >= 0 else '-'}{abs(int(diff)):,}원)" if m["prev_total"] else ")"))
    if d["reading"] is not None:
        out.append(f"📚 저장 {d['reading']['saved']}개 · 읽음 {d['reading']['read']}개")
    if d["commits"]:
        out.append("💻 커밋 " + ", ".join(f"{k} {v}" for k, v in d["commits"].items()))
    if d["coupons_expiring"]:
        out.append("🎁 2주 안 만료: " + ", ".join(d["coupons_expiring"][:4]))
    out.append(f"🤖 비서 비용 ${d['assistant_cost_usd']:.2f}")
    return out


async def run(client, store, cfg: dict, now: datetime | None = None) -> str:
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    d = await collect(cfg, store, now)
    wc = cfg.get("weekly_review") or {}
    model = wc.get("model", cfg["models"]["deep"])
    resp = await client.messages.create(model=model, max_tokens=900, messages=[
        {"role": "user", "content": PROMPT.format(data=json.dumps(d, ensure_ascii=False, indent=1)[:30000])}])
    store.record_usage(model, "weekly_review", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    comment = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    text = "\n".join([f"🗓️ **나의 한 주** ({d['period']})", *numbers(d), "", comment])
    if wc.get("save_notion", True) and (cfg.get("notion") or {}).get("memo_parent_page_id"):
        try:
            class _C:
                pass
            c = _C()
            c.cfg = cfg
            res = await dev.notion_write_memo(c, f"주간 회고 {d['period']}", text.replace("**", ""))
            url = res.rsplit(" ", 1)[-1]
            text += f"\n📝 Notion: {url}"
        except Exception as e:
            text += f"\n(Notion 저장 실패: {str(e)[:80]})"
    return text[:1950]
