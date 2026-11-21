"""구글 캘린더·드라이브.

캘린더 읽기는 두 가지 중 하나:
  1) OAuth 토큰(data/google_token.json)이 있으면 Calendar API
  2) 없으면 캘린더의 '비공개 주소(iCal 형식)' URL (GOOGLE_CALENDAR_ICS_URLS, 쉼표로 여러 개) — 설정이 가장 간단
일정 추가·드라이브 검색은 OAuth 토큰이 있어야 한다 (deploy/google_auth.py로 발급).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .base import to_thread, tool

TOKEN = Path(os.environ.get("DATA_DIR", "data")) / "google_token.json"
YT_TOKEN = TOKEN.with_name("google_youtube_token.json")   # 유튜브 권한은 구글이 다른 권한과 함께 요청하지 못하게 해서 토큰을 따로 둔다
SCOPES = ["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/drive.file",          # drive.file: 봇이 만든 파일(백업·지출 시트)만 쓰기
          "https://www.googleapis.com/auth/contacts.readonly"]
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def _creds(path: Path | None = None):
    path = path or TOKEN
    if not path.exists():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(path))  # 토큰에 실제로 허락된 권한 그대로 (새 권한을 요청하면 갱신이 실패하므로)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        path.write_text(creds.to_json())
    return creds


def has_scope(scope: str, path: Path | None = None) -> bool:
    c = _creds(path)
    return bool(c and scope in (c.scopes or []))


def _service(name: str, ver: str, path: Path | None = None):
    creds = _creds(path)
    if not creds:
        raise RuntimeError("구글 OAuth 토큰이 없습니다. deploy/google_auth.py로 발급하세요.")
    from googleapiclient.discovery import build

    return build(name, ver, credentials=creds, cache_discovery=False)


def _parse_day(s: str, tz: ZoneInfo) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=tz)


# ───────────── 캘린더 조회 ─────────────
def _list_api(start: datetime, end: datetime) -> list[dict]:
    svc = _service("calendar", "v3")
    items = svc.events().list(calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
                              singleEvents=True, orderBy="startTime", maxResults=100).execute().get("items", [])
    return [{"id": e.get("id", ""), "title": e.get("summary", "(제목 없음)"), "start": e["start"].get("dateTime", e["start"].get("date")),
             "end": e["end"].get("dateTime", e["end"].get("date")), "location": e.get("location", ""),
             "description": (e.get("description") or "")[:300]} for e in items]


def _list_ics(start: datetime, end: datetime) -> list[dict]:
    import icalendar
    import recurring_ical_events

    out = []
    for url in [u.strip() for u in os.environ.get("GOOGLE_CALENDAR_ICS_URLS", "").split(",") if u.strip()]:
        cal = icalendar.Calendar.from_ical(httpx.get(url, timeout=20, follow_redirects=True).content)
        for ev in recurring_ical_events.of(cal).between(start, end):
            s, e = ev.get("DTSTART").dt, ev.get("DTEND").dt if ev.get("DTEND") else None
            out.append({"id": f"{ev.get('UID', '')}@{s}", "title": str(ev.get("SUMMARY", "(제목 없음)")),
                        "start": s.isoformat() if isinstance(s, (date, datetime)) else str(s),
                        "end": e.isoformat() if isinstance(e, (date, datetime)) else "",
                        "location": str(ev.get("LOCATION", "")), "description": str(ev.get("DESCRIPTION", ""))[:300]})
    return sorted(out, key=lambda x: x["start"])


def list_events(start: datetime, end: datetime) -> list[dict]:
    if TOKEN.exists():
        return _list_api(start, end)
    if os.environ.get("GOOGLE_CALENDAR_ICS_URLS"):
        return _list_ics(start, end)
    raise RuntimeError("캘린더가 설정되지 않았습니다 (GOOGLE_CALENDAR_ICS_URLS 또는 OAuth 토큰)")


@tool("calendar_list", "기간 내 구글 캘린더 일정을 조회한다. 날짜는 ISO 형식(예: 2026-11-20 또는 2026-11-20T09:00).",
      {"properties": {"start": {"type": "string"}, "end": {"type": "string", "description": "끝(포함 안 함). 하루면 다음 날 00:00"}},
       "required": ["start", "end"]}, group="calendar")
async def calendar_list(ctx, start: str, end: str):
    tz = ZoneInfo(ctx.cfg["timezone"])
    from .. import talkcal                      # 톡캘린더 읽기가 켜져 있으면 카톡 일정도 함께 (source: 톡캘린더)
    return await talkcal.all_events(ctx.cfg, ctx.store, _parse_day(start, tz), _parse_day(end, tz)) or "일정 없음"


def _create(title: str, start: str, end: str, location: str, description: str, tz: str) -> str:
    svc = _service("calendar", "v3")
    timed = "T" in start
    body = {"summary": title, "location": location, "description": description,
            "start": {"dateTime": start, "timeZone": tz} if timed else {"date": start},
            "end": {"dateTime": end, "timeZone": tz} if timed else {"date": end}}
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    return f"일정을 추가했습니다: {ev.get('htmlLink', '')}"


@tool("calendar_create", "구글 캘린더에 일정을 추가한다 (사용자 확인 후 실행). 종일 일정은 날짜만(YYYY-MM-DD), 시간 일정은 YYYY-MM-DDTHH:MM:SS.",
      {"properties": {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                      "location": {"type": "string", "default": ""}, "description": {"type": "string", "default": ""}},
       "required": ["title", "start", "end"]}, group="calendar", confirm=True,
      summarize=lambda a: f"📅 일정 추가: **{a.get('title')}**\n{a.get('start')} ~ {a.get('end')}"
                          + (f"\n📍 {a['location']}" if a.get("location") else ""))
async def calendar_create(ctx, title: str, start: str, end: str, location: str = "", description: str = ""):
    return await to_thread(_create, title, start, end, location, description, ctx.cfg["timezone"])


# ───────────── 드라이브 ─────────────
def _drive_search(query: str, limit: int) -> list[dict]:
    svc = _service("drive", "v3")
    q = f"fullText contains '{query.replace(chr(39), '')}' and trashed = false"
    files = svc.files().list(q=q, pageSize=limit, orderBy="modifiedTime desc",
                             fields="files(id,name,mimeType,modifiedTime,webViewLink)").execute().get("files", [])
    return files


def _drive_read(file_id: str, limit: int) -> str:
    svc = _service("drive", "v3")
    meta = svc.files().get(fileId=file_id, fields="name,mimeType").execute()
    if meta["mimeType"].startswith("application/vnd.google-apps."):
        data = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
    else:
        data = svc.files().get_media(fileId=file_id).execute()
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
    return f"# {meta['name']}\n\n{text[:limit]}"


@tool("drive_search", "구글 드라이브에서 문서를 전문 검색한다.",
      {"properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
      group="research")
async def drive_search(ctx, query: str, limit: int = 10):
    return await to_thread(_drive_search, query, min(limit, 25)) or "결과 없음"


@tool("drive_read", "드라이브 파일 내용을 텍스트로 읽는다 (구글 문서는 텍스트로 변환).",
      {"properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}, group="research")
async def drive_read(ctx, file_id: str):
    return await to_thread(_drive_read, file_id, 15000)
