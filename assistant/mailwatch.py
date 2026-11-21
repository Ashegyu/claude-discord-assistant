"""새 메일 감시: 주기적으로 새 메일을 확인하고, 중요한 것만 디스코드로 알린다.

규칙이 먼저 (VIP 발신자는 항상 알림, 무시 목록은 항상 무시), 나머지는 Haiku가 한 번에 분류.
"""
from __future__ import annotations

import asyncio
import logging

from .tools.mail import GMAIL

log = logging.getLogger("mailwatch")
LEVEL = {"urgent": 3, "important": 2, "normal": 1, "promo": 0}
EMOJI = {"urgent": "🚨", "important": "📌", "normal": "✉️", "promo": "🏷️"}

TRIAGE_TOOL = {
    "name": "triage",
    "description": "메일들의 중요도를 판정한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mails": {"type": "array", "items": {"type": "object", "properties": {
                "uid": {"type": "string"},
                "level": {"type": "string", "enum": ["urgent", "important", "normal", "promo"],
                          "description": "urgent: 오늘 안에 대응 필요(장애·보안·마감·결제 실패). important: 사람이 직접 보낸 업무·개인 메일, 확인 필요한 결제·계약. normal: 자동 알림. promo: 광고·뉴스레터"},
                "summary": {"type": "string", "description": "한 문장 요약 (60자 이내)"},
                "action": {"type": "string", "description": "해야 할 일 (없으면 빈 문자열)"},
                "task": {"type": "object", "description": "urgent/important 메일에 날짜가 있는 할 일·일정이 있을 때만. 없으면 생략",
                         "properties": {
                             "kind": {"type": "string", "enum": ["todo", "event"],
                                      "description": "todo=기한까지 할 일(납부·제출·회신), event=그 시각에 있는 일(회의·면접·약속)"},
                             "title": {"type": "string", "description": "짧게 (예: '관리비 납부', '1차 면접')"},
                             "due": {"type": "string", "description": "todo의 기한 YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM"},
                             "start": {"type": "string", "description": "event 시작 YYYY-MM-DDTHH:MM"},
                             "end": {"type": "string"}, "location": {"type": "string"}},
                         "required": ["kind", "title"]},
            }, "required": ["uid", "level", "summary"]}},
        },
        "required": ["mails"],
    },
}


async def triage(client, store, cfg, mails: list[dict]) -> list[dict]:
    vip = {s.lower() for s in cfg["mail"].get("vip_senders", [])}
    ignore = {s.lower() for s in cfg["mail"].get("ignore_senders", [])}
    mails = [m for m in mails if m["from_addr"] not in ignore]
    if not mails:
        return []
    brief = "\n\n".join(f"uid={m['uid']}\n보낸 사람: {m['from']}\n제목: {m['subject']}\n본문: {m.get('body', m.get('snippet', ''))[:800]}"
                        for m in mails)
    model = cfg["models"]["worker"]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo(cfg["timezone"])).strftime("%Y-%m-%d(%a)")
    kw = dict(model=model, max_tokens=2000, tools=[TRIAGE_TOOL],
              messages=[{"role": "user", "content": f"오늘은 {today}. 새로 온 메일들의 중요도를 판정하라. "
                                                    f"날짜가 있는 할 일·일정은 task로 뽑되, 연도가 없으면 가장 가까운 미래로.\n\n{brief}"}])
    try:
        try:
            resp = await client.messages.create(tool_choice={"type": "tool", "name": "triage"}, **kw)
        except Exception:
            resp = await client.messages.create(tool_choice={"type": "auto"}, **kw)
        store.record_usage(model, "mail_triage", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
        verdict = next((b.input for b in resp.content if b.type == "tool_use"), {"mails": []})
        by_uid = {str(v["uid"]): v for v in verdict.get("mails", [])}
    except Exception as e:
        log.warning("분류 실패: %s", e)
        by_uid = {}
    out = []
    for m in mails:
        v = by_uid.get(m["uid"], {"level": "normal", "summary": m["subject"], "action": ""})
        if m["from_addr"] in vip:
            v = {**v, "level": "urgent" if v["level"] == "urgent" else "important"}
        out.append({**m, **v})
    return out


async def check(client, store, cfg, send) -> int:
    """새 메일 확인 → 기준 이상만 알림. 알린 개수 반환."""
    if not GMAIL.enabled:
        return 0
    last = int(store.get("mail_last_uid", 0) or 0)
    mails, new_last = await asyncio.to_thread(GMAIL.new_since, last)
    if new_last != last:
        store.put("mail_last_uid", new_last)
    if not mails:
        return 0
    judged = await triage(client, store, cfg, mails)
    threshold = LEVEL[cfg["mail"].get("alert_min", "important")]
    hits = [m for m in judged if LEVEL.get(m["level"], 1) >= threshold]
    for m in hits:
        lines = [f"{EMOJI[m['level']]} **{m['subject'] or '(제목 없음)'}**", f"from {m['from']}", m["summary"]]
        if m.get("action"):
            lines.append(f"→ {m['action']}")
        if m.get("attachments"):
            lines.append("📎 " + ", ".join(m["attachments"][:3]))
        task = m.get("task") or {}
        info = {"_mail": {"uid": m["uid"], "subject": m["subject"], "from": m["from"]}}
        if task.get("title") and (task.get("due") or task.get("start")):
            info.update({**task, "summary": f"메일: {m['subject']} ({m['from']})"})
            lines.append(f"{'📅' if task.get('kind') == 'event' else '✅'} {task['title']} — {task.get('start') or task.get('due')}")
        await send("\n".join(lines), info, "urgent" if m["level"] == "urgent" else "normal")
    log.info("새 메일 %d통, 알림 %d통", len(mails), len(hits))
    return len(hits)
