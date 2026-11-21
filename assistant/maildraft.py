"""메일 답장 초안: Gmail은 읽기 전용 그대로 두고, 초안을 디스코드에 써준다 (복사해서 보내기).

메일 알림의 [✉️ 답장 초안] 버튼, 또는 "그 메일 답장 써줘. 다음 주 화요일 가능하다고" (mail_draft 도구).
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from urllib.parse import quote

from .tools.base import tool
from .tools.mail import GMAIL

PROMPT = """아래 메일에 대한 답장 초안을 써라.
- 받은 메일의 언어와 격식에 맞춘다 (한국어 업무 메일이면 존댓말, 영어면 영어)
- 사용자가 준 지시가 있으면 그대로 반영하고, 없으면 받았음·확인·다음 행동 정도로 짧게
- 날짜·금액·약속처럼 사용자가 정하지 않은 사실은 지어내지 말고 [ ]로 빈칸을 남긴다
- 서명은 사용자 정보에 이름이 있으면 그 이름, 없으면 [이름]
- 제목 줄 없이 본문만

사용자 정보:
{memory}

사용자 지시: {instructions}

받은 메일
보낸 사람: {sender}
제목: {subject}
본문:
{body}"""


def _memory() -> str:
    import os
    p = Path(os.environ.get("DATA_DIR", "data")) / "memory.md"
    return p.read_text(encoding="utf-8")[:1500] if p.exists() else "(없음)"


def compose_url(to: str, subject: str) -> str:
    """Gmail 새 메일 창 (받는 사람·제목만 — 디스코드 링크 버튼은 512자 제한)."""
    subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    url = f"https://mail.google.com/mail/?view=cm&fs=1&to={quote(to)}&su={quote(subj)}"
    return url[:512]


def address_of(sender: str) -> str:
    m = re.search(r"<([^>]+)>", sender or "")
    return m.group(1) if m else (sender or "").strip()


async def draft(client, store, cfg: dict, uid: str, instructions: str = "") -> dict:
    mail = await asyncio.to_thread(GMAIL.read, str(uid))
    model = (cfg.get("mail") or {}).get("draft_model", cfg["models"]["deep"])
    resp = await client.messages.create(model=model, max_tokens=1200, messages=[{"role": "user", "content": PROMPT.format(
        memory=_memory(), instructions=instructions or "(없음)", sender=mail.get("from", ""),
        subject=mail.get("subject", ""), body=(mail.get("body") or "")[:8000])}])
    store.record_usage(model, "mail_draft", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    body = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    to = address_of(mail.get("from", ""))
    return {"to": to, "subject": mail.get("subject", ""), "body": body, "url": compose_url(to, mail.get("subject", ""))}


def render(d: dict) -> str:
    return (f"✉️ **답장 초안** → {d['to']}\n제목: Re: {d['subject']}\n```\n{d['body'][:1600]}\n```"
            "아래 버튼으로 Gmail 새 메일 창을 열고 본문을 붙여 넣으세요.")


@tool("mail_draft", "메일 답장 초안을 쓴다 (보내지 않음, Gmail은 읽기 전용). uid는 mail_search 결과의 uid. "
      "instructions에 사용자가 원하는 내용(가능한 날짜, 수락/거절 등)을 넣는다.",
      {"properties": {"uid": {"type": "string"}, "instructions": {"type": "string", "default": ""}}, "required": ["uid"]},
      group="mail")
async def mail_draft(ctx, uid: str, instructions: str = ""):
    d = await draft(ctx.extras["client"], ctx.store, ctx.cfg, uid, instructions)
    return render(d) + f"\n{d['url']}"
