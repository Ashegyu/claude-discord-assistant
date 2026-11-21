"""Gmail (IMAP, 읽기 전용).

- 받은편지함을 readonly로 열고 BODY.PEEK로 읽어서 '읽음' 표시도 바꾸지 않는다.
- 검색은 Gmail 검색 문법(X-GM-RAW)을 그대로 쓴다: from:, subject:, newer_than:2d, has:attachment 등.
환경변수: GMAIL_ADDRESS, GMAIL_APP_PASSWORD (구글 계정 → 보안 → 앱 비밀번호)
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from .base import to_thread, tool

HOST = "imap.gmail.com"


def _decode(s: str | None) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return s


def _text_of(msg: email.message.Message, limit: int) -> str:
    plain, html = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        (plain if part.get_content_type() == "text/plain" else html if part.get_content_type() == "text/html" else []).append(text)
    body = "\n".join(plain) or re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", "", "\n".join(html)))
    body = re.sub(r"[ \t\r\f\v]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    return body[:limit]


def _attachments(msg) -> list[str]:
    return [_decode(p.get_filename()) for p in msg.walk() if p.get_filename()]


def summary_of(uid: str, raw: bytes, body_limit: int = 0) -> dict:
    msg = email.message_from_bytes(raw)
    name, addr = parseaddr(_decode(msg.get("From")))
    try:
        date = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        date = msg.get("Date", "")
    d = {"uid": uid, "from": f"{name} <{addr}>" if name else addr, "from_addr": addr.lower(),
         "subject": _decode(msg.get("Subject")), "date": date, "attachments": _attachments(msg)}
    text = _text_of(msg, body_limit or 300)
    d["body" if body_limit else "snippet"] = text
    return d


class Gmail:
    # 환경변수는 쓸 때마다 읽는다 (봇은 모듈을 불러온 뒤에 .env를 읽기 때문)
    @property
    def addr(self) -> str:
        return os.environ.get("GMAIL_ADDRESS", "").strip()

    @property
    def pw(self) -> str:
        return os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.addr and self.pw)

    def _conn(self) -> imaplib.IMAP4_SSL:
        c = imaplib.IMAP4_SSL(HOST, timeout=30)
        c.login(self.addr, self.pw)
        c.select("INBOX", readonly=True)
        return c

    def _fetch(self, c, uids: list[bytes], body_limit: int) -> list[dict]:
        out = []
        for u in uids:
            typ, data = c.uid("fetch", u, "(BODY.PEEK[])")
            if typ == "OK" and data and isinstance(data[0], tuple):
                out.append(summary_of(u.decode(), data[0][1], body_limit))
        return out

    def search(self, query: str, limit: int = 10) -> list[dict]:
        c = self._conn()
        try:
            q = query.replace('"', "")
            if q.isascii():
                typ, data = c.uid("search", None, "X-GM-RAW", f'"{q}"')
            else:  # 한글 검색어는 UTF-8 리터럴로 전송
                c.literal = q.encode("utf-8")
                typ, data = c.uid("search", "CHARSET", "UTF-8", "X-GM-RAW")
            uids = (data[0].split() if typ == "OK" and data and data[0] else [])[-limit:][::-1]
            return self._fetch(c, uids, 0)
        finally:
            c.logout()

    def read(self, uid: str, limit: int = 6000) -> dict:
        c = self._conn()
        try:
            got = self._fetch(c, [uid.encode()], limit)
            return got[0] if got else {"error": "메일을 찾지 못했습니다"}
        finally:
            c.logout()

    def new_since(self, last_uid: int, max_n: int = 30) -> tuple[list[dict], int]:
        """마지막으로 본 UID 이후의 새 메일. 처음이면 기준점만 잡고 빈 목록."""
        c = self._conn()
        try:
            if last_uid <= 0:
                typ, data = c.uid("search", None, "ALL")
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                return [], int(uids[-1]) if uids else 0
            typ, data = c.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [u for u in (data[0].split() if typ == "OK" and data and data[0] else []) if int(u) > last_uid]
            if not uids:
                return [], last_uid
            return self._fetch(c, uids[-max_n:], 1500), int(uids[-1])
        finally:
            c.logout()


GMAIL = Gmail()


def _need():
    if not GMAIL.enabled:
        raise RuntimeError("Gmail이 설정되지 않았습니다 (.env의 GMAIL_ADDRESS, GMAIL_APP_PASSWORD)")


@tool("mail_search", "Gmail 받은편지함을 Gmail 검색 문법으로 찾는다. 결과는 최신순 목록(uid, 보낸 사람, 제목, 날짜, 미리보기).",
      {"properties": {"query": {"type": "string", "description": "예: 'newer_than:1d', 'from:github.com is:unread', 'subject:결제 newer_than:7d'"},
                      "limit": {"type": "integer", "default": 10, "maximum": 30}},
       "required": ["query"]}, group="mail")
async def mail_search(ctx, query: str, limit: int = 10):
    _need()
    return await to_thread(GMAIL.search, query, min(limit, 30))


@tool("mail_read", "uid로 메일 한 통의 본문을 읽는다 (읽음 표시는 바꾸지 않음).",
      {"properties": {"uid": {"type": "string"}}, "required": ["uid"]}, group="mail")
async def mail_read(ctx, uid: str):
    _need()
    return await to_thread(GMAIL.read, uid)
