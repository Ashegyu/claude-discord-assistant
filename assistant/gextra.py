"""구글 추가 연동: 연락처 생일(People API) · 유튜브 구독 새 영상(YouTube Data API) · 지출 시트(Sheets API).

권한: contacts.readonly 는 deploy/google_auth.py 를 다시 실행해 추가.
유튜브(youtube.readonly)는 구글이 drive.file 등과 함께 요청하지 못하게 해서 별도 토큰: deploy/google_auth.py --youtube
시트는 봇이 만든 파일만 다루므로 기존 drive.file 권한으로 동작한다.
Cloud 콘솔에서 People API, YouTube Data API v3, Google Sheets API 사용 설정 필요.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta, timezone

from . import expenses
from .claude_tools import acall_tool
from .tools import google
from .tools.base import tool

CONTACTS = "https://www.googleapis.com/auth/contacts.readonly"
YOUTUBE = "https://www.googleapis.com/auth/youtube.readonly"


def _need(scope: str, what: str) -> None:
    yt = scope == YOUTUBE
    path = google.YT_TOKEN if yt else google.TOKEN
    cmd = "deploy/google_auth.py --youtube" if yt else "deploy/google_auth.py"
    if not path.exists() or not google.has_scope(scope, path):
        raise RuntimeError(f"구글 {what} 권한이 없어요 → SSH 터널로 `{cmd}` 를 실행하세요")


# ───────────── 생일 ─────────────
def fetch_birthdays() -> list[dict]:
    """주소록에서 생일·기념일이 있는 사람만: [{name, month, day, year, kind}] (동기 함수)."""
    _need(CONTACTS, "연락처 읽기")
    svc, out, token = google._service("people", "v1"), [], None
    for _ in range(10):
        r = svc.people().connections().list(resourceName="people/me", personFields="names,birthdays,events",
                                            pageSize=1000, pageToken=token).execute()
        for p in r.get("connections", []):
            name = (p.get("names") or [{}])[0].get("displayName", "")
            if not name:
                continue
            for b in p.get("birthdays", []):
                d = b.get("date") or {}
                if d.get("month") and d.get("day"):
                    out.append({"name": name, "month": d["month"], "day": d["day"], "year": d.get("year"), "kind": "생일"})
                    break
            for e in p.get("events", []):
                d = e.get("date") or {}
                if d.get("month") and d.get("day"):
                    out.append({"name": name, "month": d["month"], "day": d["day"], "year": d.get("year"),
                                "kind": e.get("formattedType") or "기념일"})
        token = r.get("nextPageToken")
        if not token:
            break
    return out


def next_date(b: dict, today: date) -> date:
    for y in (today.year, today.year + 1):
        try:
            d = date(y, b["month"], b["day"])
        except ValueError:                 # 2월 29일 → 평년엔 28일
            d = date(y, 2, 28)
        if d >= today:
            return d
    return today


def upcoming(items: list[dict], today: date, days: int = 30) -> list[dict]:
    out = []
    for b in items:
        d = next_date(b, today)
        if (d - today).days <= days:
            age = f" ({d.year - b['year']}번째)" if b.get("year") and b["kind"] != "생일" else (f" (만 {d.year - b['year']}세)" if b.get("year") else "")
            out.append({**b, "date": d, "left": (d - today).days, "age": age})
    return sorted(out, key=lambda x: x["left"])


async def birthdays(store, today: date, refresh: bool = False) -> list[dict]:
    """하루 한 번만 주소록을 읽고 VM DB에 이름·날짜만 보관."""
    cached = store.get("birthdays") or {}
    if refresh or cached.get("day") != today.isoformat():
        items = await asyncio.to_thread(fetch_birthdays)
        store.put("birthdays", {"day": today.isoformat(), "items": items})
        return items
    return cached.get("items", [])


async def today_alert(store, today: date) -> str:
    hits = [b for b in upcoming(await birthdays(store, today), today, 0)]
    if not hits:
        return ""
    return "\n".join(f"🎂 오늘은 **{b['name']}** {b['kind']}이에요{b['age']}" for b in hits)


@tool("birthdays_upcoming", "구글 주소록의 다가오는 생일·기념일. '다음 달 생일 누구 있어?', '○○ 생일 언제야?'에 쓴다.",
      {"properties": {"days": {"type": "integer", "default": 30, "description": "며칠 안의 것을 볼지 (전체는 366)"}}}, group="home")
async def birthdays_upcoming(ctx, days: int = 30):
    today = date.today()
    items = upcoming(await birthdays(ctx.store, today), today, min(days, 366))
    return [f"{b['date']:%m/%d} {b['name']} {b['kind']}{b['age']} (D-{b['left']})" for b in items] or f"{days}일 안에 생일·기념일이 없어요."


# ───────────── 유튜브 ─────────────
def duration_seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def fetch_new_videos(since: datetime, skip_shorts: bool = True, per_channel: int = 5) -> list[dict]:
    """구독 채널의 since 이후 새 영상 (동기 함수). 채널당 최근 per_channel개만 본다."""
    _need(YOUTUBE, "유튜브 읽기")
    yt = google._service("youtube", "v3", google.YT_TOKEN)
    channels, token = [], None
    for _ in range(20):
        r = yt.subscriptions().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
        channels += [(i["snippet"]["resourceId"]["channelId"], i["snippet"]["title"]) for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    found = []
    for cid, cname in channels:
        try:
            r = yt.playlistItems().list(part="snippet,contentDetails", playlistId="UU" + cid[2:], maxResults=per_channel).execute()
        except Exception:
            continue                       # 업로드가 없거나 비공개인 채널
        for it in r.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt") or it["snippet"].get("publishedAt", "")
            if pub and datetime.fromisoformat(pub.replace("Z", "+00:00")) > since:
                found.append({"id": it["contentDetails"]["videoId"], "channel": cname, "title": it["snippet"]["title"],
                              "description": (it["snippet"].get("description") or "")[:600], "published": pub})
    out = []
    for i in range(0, len(found), 50):
        chunk = found[i:i + 50]
        r = yt.videos().list(part="contentDetails,snippet", id=",".join(v["id"] for v in chunk)).execute()
        info = {x["id"]: x for x in r.get("items", [])}
        for v in chunk:
            x = info.get(v["id"])
            if not x or x["snippet"].get("liveBroadcastContent") in ("upcoming", "live"):
                continue
            sec = duration_seconds(x["contentDetails"].get("duration", ""))
            if skip_shorts and (sec <= 60 or "#shorts" in v["title"].lower()):
                continue
            out.append({**v, "seconds": sec})
    return sorted(out, key=lambda v: v["published"], reverse=True)


SUMMARY_TOOL = {"name": "lines", "description": "영상마다 한 줄 요약을 제출한다.", "input_schema": {
    "type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "line": {"type": "string", "description": "무슨 내용인지 한국어 한 줄 (40자 안팎). 설명에 없으면 제목으로만"}},
        "required": ["id", "line"]}}}, "required": ["items"]}}


def _len(sec: int) -> str:
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


async def youtube_digest(client, store, cfg: dict, now: datetime) -> str:
    yc = cfg.get("youtube") or {}
    last = store.get("yt_last")
    since = datetime.fromtimestamp(last, timezone.utc) if last else now.astimezone(timezone.utc) - timedelta(hours=24)
    videos = await asyncio.to_thread(fetch_new_videos, since, yc.get("skip_shorts", True))
    store.put("yt_last", now.timestamp())
    if not videos:
        return ""
    cap = yc.get("max_videos", 25)
    shown, lines_by_id = videos[:cap], {}
    if yc.get("summary", True):
        try:
            prompt = "유튜브 새 영상들이다. 각 영상이 무슨 내용인지 한 줄로 써라. 과장·낚시 표현은 빼고 사실만.\n\n" + "\n\n".join(
                f"id: {v['id']}\n채널: {v['channel']}\n제목: {v['title']}\n설명: {v['description'][:400]}" for v in shown)
            res = await acall_tool(client, store, yc.get("model", cfg["models"]["quick"]), prompt, SUMMARY_TOOL,
                                   role="youtube", max_tokens=2500)
            lines_by_id = {i["id"]: i["line"] for i in res.get("items", [])}
        except Exception:
            pass
    out = [f"📺 **구독 채널 새 영상 {len(videos)}개**"]
    for v in shown:
        line = f"\n   {lines_by_id[v['id']]}" if lines_by_id.get(v["id"]) else ""
        out.append(f"**{v['channel']}** · {v['title'][:70]} ({_len(v['seconds'])}){line}\n   <https://youtu.be/{v['id']}>")
    if len(videos) > cap:
        out.append(f"…외 {len(videos) - cap}개")
    return "\n".join(out)


# ───────────── 지출 시트 ─────────────
CATS = ["식비", "카페", "교통", "쇼핑", "생활", "의료", "문화", "기타"]


def sheet_values(rows: list[dict]) -> tuple[list[list], list[list]]:
    """(내역 탭, 월별 요약 탭) — 요약은 SUMIFS 수식이라 시트에서 내역을 고치면 바로 다시 계산된다."""
    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    detail = [["날짜", "내용", "가게", "분류", "금액", "결제수단", "월"]] + [
        [r["date"], r["title"], r.get("store", ""), r["category"], r["amount"], r.get("payment", ""), r["date"][:7]] for r in rows]
    months = sorted({r["date"][:7] for r in rows if r["date"]}, reverse=True)
    summary = [["월", *CATS, "합계"]]
    for i, m in enumerate(months, 2):
        cells = [f"=SUMIFS('내역'!$E:$E,'내역'!$D:$D,{chr(66 + j)}$1,'내역'!$G:$G,$A{i})" for j in range(len(CATS))]
        summary.append([f"'{m}", *cells, f"=SUM(B{i}:{chr(65 + len(CATS))}{i})"])
    return detail, summary


def _sync_sheet(store, detail: list[list], summary: list[list]) -> str:
    svc = google._service("sheets", "v4")
    sid = store.get("expense_sheet_id")
    if sid:
        try:
            svc.spreadsheets().get(spreadsheetId=sid, fields="spreadsheetId").execute()
        except Exception:
            sid = None                     # 사용자가 지웠으면 새로 만든다
    if not sid:
        made = svc.spreadsheets().create(body={"properties": {"title": "지출 (비서)"},
                                               "sheets": [{"properties": {"title": "내역"}}, {"properties": {"title": "월별 요약"}}]},
                                         fields="spreadsheetId").execute()
        sid = made["spreadsheetId"]
        store.put("expense_sheet_id", sid)
    v = svc.spreadsheets().values()
    v.batchClear(spreadsheetId=sid, body={"ranges": ["'내역'!A:G", "'월별 요약'!A:J"]}).execute()
    v.batchUpdate(spreadsheetId=sid, body={"valueInputOption": "USER_ENTERED", "data": [
        {"range": "'내역'!A1", "values": detail}, {"range": "'월별 요약'!A1", "values": summary}]}).execute()
    return f"https://docs.google.com/spreadsheets/d/{sid}"


async def sync_expense_sheet(cfg: dict, store, today: date) -> str:
    if not google.TOKEN.exists() or not google.has_scope("https://www.googleapis.com/auth/drive.file"):
        raise RuntimeError("구글 토큰에 drive.file 권한이 없어요 → deploy/google_auth.py 다시 실행")
    rows = await expenses.fetch(cfg, date(2000, 1, 1), today + timedelta(days=1))
    detail, summary = sheet_values(rows)
    url = await asyncio.to_thread(_sync_sheet, store, detail, summary)
    return f"📊 지출 시트 동기화: {len(rows)}건 → <{url}>"
