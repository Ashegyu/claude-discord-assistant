"""지출 리포트: Notion '지출' DB(스크린샷 비서가 영수증을 기록하는 곳)를 집계한다.

숫자는 코드가 직접 계산한다 (모델이 합계를 지어낼 일이 없게). Claude는 쓰지 않는다.
- 주간: 매주 월요일, 지난주(월~일) / 월간: 매월 1일, 지난달
- 분류별 월 예산(config expenses.budgets)을 정하면 지출 기록 때 80%·100% 넘으면 알림
"""
from __future__ import annotations

from datetime import date, timedelta

from .tools import dev
from .tools.base import tool

ICON = {"식비": "🍚", "카페": "☕", "교통": "🚌", "쇼핑": "🛍️", "생활": "🏠", "의료": "💊", "문화": "🎬", "기타": "📦"}


def _db(cfg: dict) -> str:
    return (cfg.get("notion") or {}).get("expense_database_id", "")


def _plain(rich) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


async def fetch(cfg: dict, start: date, end: date) -> list[dict]:
    """[start, end) 기간의 지출 행."""
    db, rows, cursor = _db(cfg), [], None
    if not db:
        raise RuntimeError("지출 DB가 설정되지 않았어요 (notion.expense_database_id)")
    for _ in range(20):
        body = {"filter": {"and": [{"property": "날짜", "date": {"on_or_after": start.isoformat()}},
                                   {"property": "날짜", "date": {"before": end.isoformat()}}]},
                "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = await dev.notion("POST", f"/databases/{db}/query", body)
        for p in res.get("results", []):
            pr = p["properties"]
            rows.append({"title": _plain(pr["내용"]["title"]), "date": ((pr["날짜"]["date"] or {}).get("start") or "")[:10],
                         "amount": pr["금액"]["number"] or 0, "category": (pr["분류"]["select"] or {}).get("name", "기타"),
                         "store": _plain(pr["가게"]["rich_text"]),
                         "payment": _plain((pr.get("결제수단") or {}).get("rich_text"))})
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def summarize(rows: list[dict]) -> dict:
    by_cat: dict[str, float] = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + r["amount"]
    return {"total": sum(by_cat.values()), "count": len(rows), "by_cat": by_cat,
            "top": sorted(rows, key=lambda r: -r["amount"])[:3]}


def _won(x: float) -> str:
    return f"{int(round(x)):,}원"


def report_text(title: str, cur: dict, prev: dict) -> str:
    if not cur["count"]:
        return f"🧾 **{title}** — 기록된 지출이 없어요."
    diff = cur["total"] - prev["total"]
    head = f"🧾 **{title}** — 총 **{_won(cur['total'])}** ({cur['count']}건)"
    if prev["count"]:
        head += f" · 이전 대비 {'+' if diff >= 0 else '-'}{_won(abs(diff))}"
    lines = [head]
    for cat, amt in sorted(cur["by_cat"].items(), key=lambda x: -x[1]):
        before = prev["by_cat"].get(cat, 0)
        mark = ""
        if before and amt >= before * 1.5 and amt - before >= 10000:
            mark = f"  ⬆️ 이전 {_won(before)}"
        elif not before and prev["count"] and amt >= 20000:
            mark = "  🆕"
        lines.append(f"{ICON.get(cat, '•')} {cat} {_won(amt)} ({amt / cur['total']:.0%}){mark}")
    if cur["top"]:
        lines.append("큰 지출: " + " · ".join(f"{t['title'] or t['store']} {_won(t['amount'])}" for t in cur["top"]))
    return "\n".join(lines)


def week_range(today: date) -> tuple[date, date, date]:
    """지난주 월요일, 이번주 월요일, 지지난주 월요일."""
    this_mon = today - timedelta(days=today.weekday())
    return this_mon - timedelta(days=7), this_mon, this_mon - timedelta(days=14)


def month_range(today: date) -> tuple[date, date, date]:
    """지난달 1일, 이번달 1일, 지지난달 1일."""
    this1 = today.replace(day=1)
    last1 = (this1 - timedelta(days=1)).replace(day=1)
    prev1 = (last1 - timedelta(days=1)).replace(day=1)
    return last1, this1, prev1


async def weekly_report(cfg: dict, today: date) -> str:
    s, e, p = week_range(today)
    cur, prev = summarize(await fetch(cfg, s, e)), summarize(await fetch(cfg, p, s))
    return report_text(f"지난주 지출 ({s:%m/%d}~{e - timedelta(days=1):%m/%d})", cur, prev)


async def monthly_report(cfg: dict, today: date) -> str:
    s, e, p = month_range(today)
    cur, prev = summarize(await fetch(cfg, s, e)), summarize(await fetch(cfg, p, s))
    text = report_text(f"{s:%Y년 %m월} 지출", cur, prev)
    budgets = (cfg.get("expenses") or {}).get("budgets") or {}
    if budgets and cur["count"]:
        over = [f"{c} {_won(cur['by_cat'].get(c, 0))}/{_won(b)}" for c, b in budgets.items() if cur["by_cat"].get(c, 0) > b]
        if over:
            text += "\n예산 초과: " + ", ".join(over)
    return text


async def budget_alert(cfg: dict, store, category: str, today: date) -> str | None:
    """지출을 기록한 직후 호출: 그 분류의 이번 달 합계가 예산 80%·100%를 처음 넘으면 알림 문장."""
    budget = ((cfg.get("expenses") or {}).get("budgets") or {}).get(category)
    if not budget:
        return None
    rows = await fetch(cfg, today.replace(day=1), today + timedelta(days=1))
    spent = sum(r["amount"] for r in rows if r["category"] == category)
    for level in (1.0, 0.8):
        key = f"exp_budget:{today:%Y-%m}:{category}:{level}"
        if spent >= budget * level and not store.get(key):
            store.put(key, True)
            if level == 1.0:
                store.put(f"exp_budget:{today:%Y-%m}:{category}:0.8", True)
            return f"{'🛑' if level == 1.0 else '⚠️'} 이번 달 {category} {_won(spent)} / 예산 {_won(budget)} ({spent / budget:.0%})"
    return None


CATEGORIES = list(ICON)


async def add(cfg: dict, store, title: str, amount: float, category: str = "기타", day: str = "", shop: str = "",
              payment: str = "", memo: str = "") -> str:
    """지출 한 건을 Notion 지출 DB에 기록 (글로 말한 지출, !spend 금액 내용)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    db = _db(cfg)
    if not db:
        return "지출 DB가 설정되지 않았어요 (notion.expense_database_id)."
    if not amount or amount <= 0:
        return "금액을 알 수 없어요. 예: '점심 9천원 썼어'"
    today = datetime.now(ZoneInfo(cfg["timezone"])).date()
    d = date.fromisoformat(day[:10]) if day else today
    cat = category if category in CATEGORIES else "기타"
    txt = lambda v: {"rich_text": [{"text": {"content": (v or "")[:1900]}}]}  # noqa: E731
    props = {"내용": {"title": [{"text": {"content": (title or shop or "지출")[:200]}}]}, "날짜": {"date": {"start": d.isoformat()}},
             "금액": {"number": float(amount)}, "분류": {"select": {"name": cat}},
             "가게": txt(shop), "결제수단": txt(payment), "메모": txt(memo)}
    page = await dev.notion("POST", "/pages", {"parent": {"database_id": db}, "properties": props})
    store.put("expense_last", {"id": page.get("id", ""), "text": f"{title} {int(amount):,}원"})
    msg = f"🧾 기록: {d:%m/%d} {title or shop} **{_won(amount)}** ({cat})"
    try:
        alert = await budget_alert(cfg, store, cat, today)
        if alert:
            msg += "\n" + alert
    except Exception:
        pass
    return msg


async def undo_last(store) -> str:
    last = store.get("expense_last") or {}
    if not last.get("id"):
        return "취소할 최근 지출 기록이 없어요."
    await dev.notion("PATCH", f"/pages/{last['id']}", {"archived": True})
    store.put("expense_last", {})
    return f"🗑️ 방금 기록한 지출을 지웠어요: {last.get('text', '')}"


def parse_quick(arg: str) -> dict | None:
    """'9000 점심 식비' / '점심 9,000원' → {amount, title, category}. 금액을 못 찾으면 None."""
    import re
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(만\s*원?|천\s*원?|원)?", arg)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").replace(" ", "")
    n *= 10000 if unit.startswith("만") else 1000 if unit.startswith("천") else 1
    rest = (arg[:m.start()] + " " + arg[m.end():]).split()
    cat = next((w for w in rest if w in CATEGORIES), "기타")
    title = " ".join(w for w in rest if w != cat) or cat
    return {"amount": n, "title": title, "category": cat}


@tool("expense_add", "사용자가 말로 알려준 지출을 Notion 지출 DB에 기록한다 ('점심 9천원 썼어', '어제 택시 12,000원'). "
      "금액은 원 단위 숫자로 바꾼다 (9천원 → 9000, 1.2만 → 12000). 분류는 내용으로 골라라. 날짜를 말하지 않았으면 비운다(오늘). "
      "여러 건이면 건마다 한 번씩 호출한다. 금액이 불분명하면 기록하지 말고 물어본다.",
      {"properties": {"title": {"type": "string", "description": "무엇에 썼는지 (예: 점심, 택시)"},
                      "amount": {"type": "number"}, "category": {"type": "string", "enum": list(ICON)},
                      "date": {"type": "string", "default": "", "description": "YYYY-MM-DD, 비우면 오늘"},
                      "store": {"type": "string", "default": ""}, "payment": {"type": "string", "default": ""},
                      "memo": {"type": "string", "default": ""}},
       "required": ["title", "amount", "category"]}, group="money")
async def expense_add(ctx, title: str, amount: float, category: str, date: str = "", store: str = "", payment: str = "", memo: str = ""):
    return await add(ctx.cfg, ctx.store, title, amount, category, date, store, payment, memo)


@tool("expense_undo", "방금 기록한 지출 한 건을 지운다 ('방금 거 취소', '잘못 적었어').", {"properties": {}}, group="money")
async def expense_undo(ctx):
    return await undo_last(ctx.store)


@tool("expense_query", "Notion 지출 기록을 기간·분류로 조회해 합계와 내역을 돌려준다. 날짜는 YYYY-MM-DD, end는 포함 안 함.",
      {"properties": {"start": {"type": "string"}, "end": {"type": "string"},
                      "category": {"type": "string", "default": "", "description": "식비·카페·교통·쇼핑·생활·의료·문화·기타 중 하나, 비우면 전체"}},
       "required": ["start", "end"]}, group="money")
async def expense_query(ctx, start: str, end: str, category: str = ""):
    rows = await fetch(ctx.cfg, date.fromisoformat(start), date.fromisoformat(end))
    if category:
        rows = [r for r in rows if r["category"] == category]
    s = summarize(rows)
    return {"total_won": s["total"], "count": s["count"], "by_category": s["by_cat"],
            "items": [f"{r['date']} {r['title'] or r['store']} {int(r['amount']):,}원 ({r['category']})"
                      for r in sorted(rows, key=lambda r: r["date"])][:60]}
