"""스크린샷 비서: 사진만 보내면 무엇인지 알아보고, 버튼 하나로 캘린더·할 일·메모·지출에 넣는다.

Haiku 비전으로 종류를 나누고 필요한 값만 뽑는다 (장당 수천 토큰 이하).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import todo
from .claude_tools import acall_tool

KINDS = {"event": "📅 일정", "todo": "✅ 할 일", "coupon": "🎁 기프티콘·쿠폰", "parcel": "📦 택배",
         "receipt": "🧾 영수증", "memo": "📝 메모", "other": "🖼️ 기타"}
CATEGORIES = ["식비", "카페", "교통", "쇼핑", "생활", "의료", "문화", "기타"]

EXTRACT = {
    "name": "extract",
    "description": "스크린샷에서 알아낸 내용을 제출한다. 보이지 않는 값은 지어내지 말고 빈 문자열로 둔다.",
    "input_schema": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": list(KINDS), "description":
                 "event=날짜·시간이 있는 약속·예매·예약·청첩장·공지, todo=마감·제출·해야 할 일, coupon=기프티콘·쿠폰(유효기간),"
                 " parcel=택배·배송 조회, receipt=영수증·결제 내역, memo=나중에 볼 정보·글, other=그 외"},
        "title": {"type": "string", "description": "짧은 제목 (예: '아이유 콘서트', '치과 예약', '스타벅스 아메리카노')"},
        "summary": {"type": "string", "description": "핵심 내용 1~3줄"},
        "start": {"type": "string", "description": "일정 시작 YYYY-MM-DDTHH:MM (시간 모르면 YYYY-MM-DD)"},
        "end": {"type": "string", "description": "일정 끝 (모르면 빈 문자열)"},
        "location": {"type": "string"},
        "due": {"type": "string", "description": "마감·유효기간 YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM"},
        "code": {"type": "string", "description": "쿠폰 번호·예매 번호·송장 번호"},
        "carrier": {"type": "string", "description": "택배사"},
        "amount": {"type": "number", "description": "결제 총액 (원 단위 숫자)"},
        "store": {"type": "string", "description": "가게·판매처"},
        "category": {"type": "string", "enum": CATEGORIES + [""]},
        "payment": {"type": "string", "description": "결제수단 (예: 신한카드, 카카오페이)"},
        "date": {"type": "string", "description": "결제일 YYYY-MM-DD"},
        "text": {"type": "string", "description": "memo/other일 때 옮겨 적을 본문 (중요한 부분만, 1500자 이내)"},
    }, "required": ["kind", "title", "summary"]},
}


async def analyze(client, store, cfg: dict, images: list[dict], hint: str = "") -> dict:
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    prompt = [*images, {"type": "text", "text":
              f"지금은 {now:%Y-%m-%d(%a) %H:%M} (한국 시간)이다. 연도가 없는 날짜는 지금 이후 가장 가까운 날짜로 해석하라.\n"
              "이 스크린샷이 무엇인지 판단하고 extract 도구로 제출하라." + (f"\n사용자 메모: {hint}" if hint else "")}]
    model = (cfg.get("screenshot") or {}).get("model", cfg["models"]["quick"])
    info = await acall_tool(client, store, model, prompt, EXTRACT, role="screenshot", max_tokens=1200)
    info.setdefault("kind", "other")
    return info


def actions_for(info: dict) -> list[tuple[str, str]]:
    """(버튼 키, 라벨) — 첫 번째가 추천 동작."""
    k = info.get("kind")
    first = {"event": [("calendar", "📅 캘린더")], "todo": [("todo", "✅ 할 일")], "coupon": [("todo", "🎁 만료 알림")],
             "parcel": [("todo", "📦 할 일에 기록")], "receipt": [("expense", "🧾 지출 기록")]}.get(k, [])
    rest = [("calendar", "📅 캘린더"), ("todo", "✅ 할 일"), ("memo", "📝 메모"), ("expense", "🧾 지출")]
    keys = {a for a, _ in first}
    return first + [r for r in rest if r[0] not in keys]


def card(info: dict) -> str:
    lines = [f"{KINDS.get(info.get('kind'), '🖼️')} **{info.get('title', '')}**"]
    if info.get("summary"):
        lines.append(info["summary"])
    fields = [("start", "🕒"), ("end", "~"), ("location", "📍"), ("due", "⏳ 기한"), ("code", "🔢"),
              ("carrier", "🚚"), ("store", "🏪"), ("payment", "💳"), ("category", "🏷️")]
    lines += [f"{icon} {info[k]}" for k, icon in fields if info.get(k)]
    if info.get("amount"):
        lines.append(f"💰 {int(info['amount']):,}원")
    return "\n".join(lines)[:1800]


# ───────────── 동작 ─────────────
def _dt(s: str, tz: ZoneInfo) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=tz)


async def do_calendar(cfg: dict, info: dict) -> str:
    from .tools import google

    start = info.get("start") or info.get("due") or ""
    if not start:
        return "날짜를 찾지 못해 캘린더에 넣을 수 없어요."
    if "T" in start:
        s = datetime.fromisoformat(start)
        end = info.get("end") if "T" in (info.get("end") or "") else (s + timedelta(hours=1)).isoformat(timespec="minutes")
        start_s, end_s = s.isoformat(timespec="seconds"), datetime.fromisoformat(end).isoformat(timespec="seconds")
    else:
        d = datetime.fromisoformat(start[:10]).date()
        start_s, end_s = d.isoformat(), (d + timedelta(days=1)).isoformat()
    desc = "\n".join(x for x in [info.get("summary", ""), info.get("code", "")] if x)
    return await asyncio.to_thread(google._create, info.get("title", "일정"), start_s, end_s,
                                   info.get("location", ""), desc, cfg["timezone"])


async def do_todo(cfg: dict, store, info: dict, channel_id: str) -> str:
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    title, when = info.get("title", "할 일"), None
    note = "\n".join(x for x in [info.get("summary", ""), info.get("code", "")] if x)
    if info.get("kind") == "coupon" and info.get("due"):
        exp = _dt(info["due"][:10] + "T09:00", tz)
        title = f"🎁 {title} (만료 {exp:%m/%d})"
        days = (cfg.get("screenshot") or {}).get("coupon_notice_days", 3)
        for cand in (exp - timedelta(days=days), exp):
            if cand > now:
                when = cand
                break
    elif info.get("kind") == "parcel":
        title = f"📦 {title}" + (f" ({info.get('carrier', '')} {info.get('code', '')})".rstrip() if info.get("code") else "")
    else:
        d = _dt(info.get("due") or info.get("start") or "", tz)
        if d and "T" not in (info.get("due") or info.get("start") or ""):
            d = d.replace(hour=9)               # 날짜만 있으면 그날 아침 9시 알림
        when = d if d and d > now else None
    if todo.enabled():
        task = await todo.add(cfg, store, title, when, "", note)
        if info.get("_coupon_id") and task.get("id"):      # 쿠폰을 썼다고 하면 이 알림도 완료 처리하려고 연결
            from . import coupons
            coupons.link_todo(store, info["_coupon_id"], task["id"])
        return f"To Do에 넣었어요: {title}" + (f" — {when:%m/%d %H:%M} 알림" if when else "")
    if not when:
        return "Microsoft To Do가 연결돼 있지 않아 날짜 없는 할 일은 넣을 수 없어요."
    rid = store.add_reminder(when.timestamp(), title, channel_id, "")
    return f"리마인더 D{rid} 등록: {when:%m/%d %H:%M} {title}"


async def do_memo(cfg: dict, info: dict) -> str:
    from .tools import dev

    body = "\n".join(x for x in [info.get("summary", ""), info.get("text", ""), info.get("code", "")] if x) or "(내용 없음)"
    return await dev.notion_write_memo(_Ctx(cfg), info.get("title", "스크린샷 메모"), body)


async def do_expense(cfg: dict, info: dict, store=None) -> str:
    from .tools import dev

    db = (cfg.get("notion") or {}).get("expense_database_id")
    if not db:
        return "지출 데이터베이스가 설정되지 않았어요 (config.yaml notion.expense_database_id)."
    tz = ZoneInfo(cfg["timezone"])
    day = (info.get("date") or info.get("start") or datetime.now(tz).date().isoformat())[:10]
    txt = lambda s: {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}  # noqa: E731
    props = {"내용": {"title": [{"text": {"content": (info.get("title") or info.get("store") or "지출")[:200]}}]},
             "날짜": {"date": {"start": day}},
             "가게": txt(info.get("store")), "결제수단": txt(info.get("payment")), "메모": txt(info.get("summary"))}
    if info.get("amount"):
        props["금액"] = {"number": float(info["amount"])}
    cat = info.get("category") if info.get("category") in CATEGORIES else "기타"
    props["분류"] = {"select": {"name": cat}}
    p = await dev.notion("POST", "/pages", {"parent": {"database_id": db}, "properties": props})
    amt = f" {int(info['amount']):,}원" if info.get("amount") else ""
    msg = f"지출에 기록했어요:{amt} ({cat}) {p.get('url', '')}"
    if store is not None:
        from . import expenses
        try:
            alert = await expenses.budget_alert(cfg, store, cat, datetime.now(tz).date())
            if alert:
                msg += "\n" + alert
        except Exception:
            pass
    return msg


class _Ctx:
    def __init__(self, cfg):
        self.cfg = cfg


async def run_action(key: str, cfg: dict, store, info: dict, channel_id: str) -> str:
    try:
        if key == "calendar":
            return await do_calendar(cfg, info)
        if key == "todo":
            return await do_todo(cfg, store, info, channel_id)
        if key == "memo":
            return await do_memo(cfg, info)
        if key == "expense":
            return await do_expense(cfg, info, store)
    except Exception as e:
        return f"실패: {type(e).__name__}: {str(e)[:200]}"
    return "알 수 없는 동작"
