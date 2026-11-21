"""지휘자가 직접 쓰는 도구: 리마인더, 장기 기억(메모 파일), 대량 작업(배치)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import todo
from .base import tool

MEMORY_FILE = Path(os.environ.get("DATA_DIR", "data")) / "memory.md"


# ───────────── 리마인더 ─────────────
# Microsoft To Do가 연결돼 있으면 To Do(= 갤럭시 삼성 리마인더)에, 아니면 봇 자체(디스코드 알림)에 저장.
def _todo_on(ctx) -> bool:
    return todo.enabled() and (ctx.cfg.get("todo") or {}).get("enabled", True)


def _fmt_task(t: dict, tz: ZoneInfo) -> str:
    w = todo.when_of(t, tz)
    d = todo.due_of(t, tz)
    when = f"{w:%m/%d(%a) %H:%M}" if w else (f"{d:%m/%d(%a)} 마감" if d else "날짜 없음")
    rep = " [반복]" if t.get("recurrence") else ""
    star = " ★" if t.get("importance") == "high" else ""
    return f"{when} {t['title']}{rep}{star}"


@tool("reminder_add", "리마인더(할 일)를 등록한다. Microsoft To Do가 연결돼 있으면 갤럭시 삼성 리마인더에 동기화되어 폰 알람으로 울리고, "
      "아니면 그 시각에 디스코드로 알린다. when을 비우면 날짜 없는 할 일. repeat: ''(한 번), 'daily', 'weekdays', 'weekly'.",
      {"properties": {"when": {"type": "string", "description": "ISO 시각 (사용자 시간대 기준, 예: 2026-11-20T09:00). 날짜 없는 할 일이면 빈 문자열"},
                      "text": {"type": "string"}, "repeat": {"type": "string", "enum": ["", "daily", "weekdays", "weekly"], "default": ""},
                      "note": {"type": "string", "description": "메모 (선택)", "default": ""}},
       "required": ["when", "text"]}, group="reminder")
async def reminder_add(ctx, when: str, text: str, repeat: str = "", note: str = ""):
    tz = ZoneInfo(ctx.cfg["timezone"])
    dt = None
    if when:
        dt = datetime.fromisoformat(when)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=tz)
        if dt.timestamp() < datetime.now(tz).timestamp() - 60:
            return f"오류: 과거 시각입니다 ({dt:%Y-%m-%d %H:%M})"
    rep = f" (반복: {repeat})" if repeat else ""
    if _todo_on(ctx):
        await todo.add(ctx.cfg, ctx.store, text, dt, repeat, note)
        also = ""
        if dt and (ctx.cfg.get("todo") or {}).get("discord_too"):
            ctx.store.add_reminder(dt.timestamp(), text, ctx.channel_id, repeat)
            also = " + 디스코드 알림"
        when_s = f"{dt.astimezone(tz):%m/%d %H:%M} " if dt else ""
        return f"To Do(갤럭시 리마인더)에 등록: {when_s}{text}{rep}{also}"
    if not dt:
        return "오류: Microsoft To Do가 연결돼 있지 않아 날짜 없는 할 일은 저장할 수 없습니다. 시각을 정해 주세요."
    rid = ctx.store.add_reminder(dt.timestamp(), text, ctx.channel_id, repeat)
    return f"리마인더 D{rid} 등록 (디스코드 알림): {dt.astimezone(tz):%m/%d %H:%M} {text}{rep}"


def _many_summary(a: dict) -> str:
    prefix = f"[{a['prefix']}] " if a.get("prefix") else ""
    rows = [f"- {(i.get('when') or '날짜 없음')[:16].replace('T', ' ')}  {prefix}{i['text']}" for i in a.get("items", [])[:25]]
    return f"🗂️ 할 일 {len(a.get('items', []))}개 등록 (갤럭시 리마인더로 동기화)\n" + "\n".join(rows)


@tool("reminder_add_many", "큰 일을 쪼갠 단계들을 할 일 여러 개로 한 번에 등록한다 (사용자 확인 후). 각 항목 when은 ISO 시각 "
      "(그날 알림 받을 시각, 예: 2026-11-24T09:00) 또는 빈 문자열. prefix는 묶음 이름 (예: 'FA 0.2 출시').",
      {"properties": {"items": {"type": "array", "maxItems": 25, "items": {"type": "object", "properties": {
          "text": {"type": "string"}, "when": {"type": "string", "default": ""}, "note": {"type": "string", "default": ""}},
          "required": ["text"]}}, "prefix": {"type": "string", "default": ""}}, "required": ["items"]},
      group="reminder", confirm=True, summarize=_many_summary)
async def reminder_add_many(ctx, items: list[dict], prefix: str = ""):
    done, failed = [], []
    for it in items[:25]:
        title = (f"[{prefix}] " if prefix else "") + it["text"]
        r = await reminder_add(ctx, when=it.get("when") or "", text=title, note=it.get("note", ""))
        (failed if str(r).startswith("오류") else done).append(f"{title} → {r}" if str(r).startswith("오류") else title)
    return f"{len(done)}개 등록" + (f", 실패 {len(failed)}개: " + "; ".join(failed[:5]) if failed else "")


@tool("reminder_list", "등록된 리마인더·할 일 목록 (완료 안 된 것). 번호는 완료·삭제할 때 쓴다.", {"properties": {}}, group="reminder")
async def reminder_list(ctx):
    tz = ZoneInfo(ctx.cfg["timezone"])
    lines = []
    if _todo_on(ctx):
        tasks = sorted(await todo.open_tasks(ctx.cfg, ctx.store), key=lambda t: todo.sort_key(t, tz))
        ctx.store.put("todo_index", [t["id"] for t in tasks])
        lines += [f"{i}. {_fmt_task(t, tz)}" for i, t in enumerate(tasks, 1)]
    lines += [f"D{r[0]}. {datetime.fromtimestamp(r[1], tz):%m/%d(%a) %H:%M} {r[2]} (디스코드 알림)" + (f" [{r[3]}]" if r[3] else "")
              for r in ctx.store.list_reminders()]
    return lines or "없음"


async def _resolve(ctx, id: str):
    """'3' → To Do 목록 3번 (마지막 reminder_list 기준), 'D5' → 봇 리마인더 5번."""
    s = str(id).strip()
    if s[:1].upper() == "D" and s[1:].isdigit():
        return "local", int(s[1:])
    if s.isdigit() and _todo_on(ctx):
        idx = ctx.store.get("todo_index") or []
        n = int(s)
        if not idx:
            await reminder_list(ctx)
            idx = ctx.store.get("todo_index") or []
        if 1 <= n <= len(idx):
            return "todo", idx[n - 1]
        return None, None
    if s.isdigit():
        return "local", int(s)
    return None, None


@tool("reminder_done", "할 일을 완료 처리한다 (To Do·갤럭시에서도 완료로 바뀜). id는 reminder_list의 번호 (예: '3', 'D5').",
      {"properties": {"id": {"type": "string"}}, "required": ["id"]}, group="reminder")
async def reminder_done(ctx, id: str):
    kind, key = await _resolve(ctx, id)
    if kind == "todo":
        t = await todo.complete(ctx.cfg, ctx.store, key)
        return f"완료 처리했습니다: {t.get('title', '')}" + (" (반복 일정이라 다음 회차가 새로 생깁니다)" if t.get("recurrence") else "")
    if kind == "local":
        return "완료(삭제)했습니다." if ctx.store.delete_reminder(key) else "해당 리마인더가 없습니다."
    return "해당 번호가 없습니다. reminder_list로 번호를 다시 확인하세요."


@tool("reminder_delete", "리마인더·할 일을 삭제한다 (완료가 아니라 아예 지움). id는 reminder_list의 번호 (예: '3', 'D5').",
      {"properties": {"id": {"type": "string"}}, "required": ["id"]}, group="reminder")
async def reminder_delete(ctx, id: str):
    kind, key = await _resolve(ctx, id)
    if kind == "todo":
        await todo.delete(ctx.cfg, ctx.store, key)
        return "삭제했습니다."
    if kind == "local":
        return "삭제했습니다." if ctx.store.delete_reminder(key) else "해당 리마인더가 없습니다."
    return "해당 번호가 없습니다. reminder_list로 번호를 다시 확인하세요."


# ───────────── 장기 기억 ─────────────
@tool("memory_read", "사용자에 대해 저장해 둔 장기 기억(선호, 사실, 진행 중인 일)을 읽는다.", {"properties": {}}, group="memory")
async def memory_read(ctx):
    return MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else "(저장된 기억 없음)"


@tool("memory_write", "장기 기억에 한 줄을 추가하거나(op=add) 특정 문장을 지운다(op=remove). 사용자가 직접 말한 사실만 저장.",
      {"properties": {"op": {"type": "string", "enum": ["add", "remove"]}, "text": {"type": "string"}},
       "required": ["op", "text"]}, group="memory")
async def memory_write(ctx, op: str, text: str):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = MEMORY_FILE.read_text(encoding="utf-8").splitlines() if MEMORY_FILE.exists() else []
    if op == "add":
        if text in lines:
            return "이미 기억하고 있습니다."
        lines.append(f"- {text}" if not text.startswith("- ") else text)
    else:
        before = len(lines)
        lines = [l for l in lines if text not in l]
        if len(lines) == before:
            return "해당 내용이 없습니다."
    MEMORY_FILE.write_text("\n".join(lines[-300:]) + "\n", encoding="utf-8")
    return "기억을 갱신했습니다."


# ───────────── 대량 작업 (배치) ─────────────
@tool("bulk_job", "급하지 않은 대량 작업을 배치로 맡긴다 (표준가의 50%, 보통 1시간 이내 완료, 끝나면 결과 파일을 디스코드로 보냄). "
      "같은 지시를 여러 항목에 각각 적용할 때 쓴다. items가 커밋 목록이면 source='github_commits'로 자동 수집 가능.",
      {"properties": {
          "title": {"type": "string"},
          "instruction": {"type": "string", "description": "각 항목에 공통으로 적용할 지시"},
          "items": {"type": "array", "items": {"type": "string"}, "description": "처리할 항목들 (최대 500)"},
          "source": {"type": "object", "description": "items 대신 자동 수집: {type:'github_commits', repo, days} 또는 {type:'mail', query, limit}"},
          "combine": {"type": "boolean", "default": True, "description": "끝나면 결과를 한 번 더 종합 요약할지"}},
       "required": ["title", "instruction"]}, group="batch")
async def bulk_job(ctx, title: str, instruction: str, items: list[str] | None = None, source: dict | None = None,
                   combine: bool = True):
    from ..batch import submit_bulk

    return await submit_bulk(ctx, title, instruction, items or [], source, combine)
