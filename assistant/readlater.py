"""나중에 읽기: 링크만 보내면 본문(유튜브는 자막)을 읽어 3~5줄로 요약하고 Notion '나중에 읽기' DB에 저장.

- 본문 추출은 표준 라이브러리 HTMLParser로 (script·nav·footer 등 제외, p·h·li 위주)
- 유튜브는 youtube-transcript-api(비공식). 막히거나 자막이 없으면 제목·설명만으로 요약
- 매주 토요일 아침 안 읽은 것 목록, "그거 읽었어" → 읽음 처리
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx

from .claude_tools import acall_tool
from .tools import dev
from .tools.base import tool

URL_ONLY = re.compile(r"^\s*(https?://\S+\s*)+$")
URL = re.compile(r"https?://\S+")
YT = re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)|youtu\.be/)([A-Za-z0-9_-]{11})")
TAGS = ["개발", "ECDIS", "게임", "AI", "뉴스"]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

SUMMARY_TOOL = {
    "name": "save",
    "description": "읽을거리를 요약해 제출한다.",
    "input_schema": {"type": "object", "properties": {
        "title": {"type": "string", "description": "원래 제목 (없으면 내용으로 짓기)"},
        "summary": {"type": "string", "description": "핵심 3~5줄, 한국어. 숫자·결론 위주. 줄마다 '- '로 시작"},
        "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}, "description": "해당하는 것만 (없으면 빈 배열)"},
        "kind": {"type": "string", "enum": ["글", "영상", "문서", "기타"]},
        "minutes": {"type": "integer", "description": "읽기/보기 예상 시간(분)"},
    }, "required": ["title", "summary", "kind"]},
}


def is_link_only(text: str) -> bool:
    return bool(text) and bool(URL_ONLY.match(text))


def urls_in(text: str) -> list[str]:
    return [u.rstrip(").,>") for u in URL.findall(text)][:5]


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "button", "iframe"}
    KEEP = {"p", "h1", "h2", "h3", "li", "blockquote", "pre", "td", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.stack, self.parts, self.meta, self.title, self._in_title = 0, [], [], {}, "", False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta" and a.get("content"):
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("og:title", "og:description", "description", "twitter:title"):
                self.meta.setdefault(key, a["content"])
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.KEEP:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.stack and self.stack[-1] in self.KEEP | {"a", "strong", "em", "span", "b", "code"}:
            self.parts.append(data)


def extract(html: str) -> dict:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        pass
    text = re.sub(r"[ \t]+", " ", "".join(p.parts))
    text = "\n".join(line.strip() for line in text.splitlines() if len(line.strip()) > 1)
    return {"title": (p.meta.get("og:title") or p.title or "").strip(),
            "description": (p.meta.get("og:description") or p.meta.get("description") or "").strip(), "text": text}


def youtube_transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return ""
    try:
        try:
            got = YouTubeTranscriptApi().fetch(video_id, languages=["ko", "en"])      # 1.x
            return " ".join(s.text for s in got)
        except AttributeError:
            got = YouTubeTranscriptApi.get_transcript(video_id, languages=["ko", "en"])  # 0.6.x
            return " ".join(s["text"] for s in got)
    except Exception:
        return ""   # 자막 없음·클라우드 IP 차단 등


async def read(url: str) -> dict:
    """{title, description, text, kind_hint, note}"""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"}) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype:
        return {"title": url.rsplit("/", 1)[-1], "description": "", "text": "", "kind_hint": "문서",
                "note": "PDF는 본문을 읽지 않았어요 (서재에 올리면 읽어요)"}
    page = extract(r.text)
    m = YT.search(url)
    if m:
        tr = await asyncio.to_thread(youtube_transcript, m.group(1))
        return {**page, "text": tr, "kind_hint": "영상", "note": "" if tr else "자막을 못 가져와 제목·설명으로만 요약"}
    note = "" if len(page["text"]) >= 400 else "본문을 거의 못 읽어 제목·설명 위주로 요약"
    return {**page, "kind_hint": "글", "note": note}


def _db(cfg: dict) -> str:
    return (cfg.get("notion") or {}).get("readlater_database_id", "")


async def find_existing(cfg: dict, url: str) -> dict | None:
    res = await dev.notion("POST", f"/databases/{_db(cfg)}/query",
                           {"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 1})
    return (res.get("results") or [None])[0]


async def save(client, store, cfg: dict, url: str) -> str:
    if not _db(cfg):
        return "나중에 읽기 DB가 설정되지 않았어요 (notion.readlater_database_id)."
    if await find_existing(cfg, url):
        return f"이미 저장된 링크예요: {url}"
    try:
        page = await read(url)
    except Exception as e:
        page = {"title": "", "description": "", "text": "", "kind_hint": "기타", "note": f"페이지를 못 열었어요 ({type(e).__name__})"}
    prompt = (f"URL: {url}\n제목: {page['title']}\n설명: {page['description']}\n종류 힌트: {page['kind_hint']}\n"
              f"본문:\n{page['text'][:24000] or '(없음)'}\n\n이 읽을거리를 save 도구로 요약하라. 본문이 없으면 제목·설명으로만, 지어내지 말 것.")
    model = (cfg.get("readlater") or {}).get("model", cfg["models"]["quick"])
    s = await acall_tool(client, store, model, prompt, SUMMARY_TOOL, role="readlater", max_tokens=800)
    tz = ZoneInfo(cfg["timezone"])
    summary = s.get("summary", "") + (f"\n({page['note']})" if page.get("note") else "")
    props = {"제목": {"title": [{"text": {"content": (s.get("title") or page["title"] or url)[:200]}}]},
             "URL": {"url": url}, "요약": {"rich_text": [{"text": {"content": summary[:1900]}}]},
             "종류": {"select": {"name": s.get("kind") or page["kind_hint"]}},
             "태그": {"multi_select": [{"name": t} for t in (s.get("tags") or []) if t in TAGS]},
             "읽음": {"checkbox": False}, "저장일": {"date": {"start": datetime.now(tz).date().isoformat()}}}
    await dev.notion("POST", "/pages", {"parent": {"database_id": _db(cfg)}, "properties": props})
    mins = f" · 약 {s['minutes']}분" if s.get("minutes") else ""
    return f"📚 **{s.get('title') or page['title'] or url}**{mins}\n{summary}\n→ 나중에 읽기에 저장했어요"


async def unread(cfg: dict, limit: int = 30) -> list[dict]:
    res = await dev.notion("POST", f"/databases/{_db(cfg)}/query", {
        "filter": {"property": "읽음", "checkbox": {"equals": False}},
        "sorts": [{"property": "저장일", "direction": "descending"}], "page_size": limit})
    out = []
    for p in res.get("results", []):
        pr = p["properties"]
        out.append({"id": p["id"], "title": dev._plain(pr["제목"]["title"]), "url": pr["URL"]["url"] or "",
                    "summary": dev._plain(pr["요약"]["rich_text"]), "saved": ((pr["저장일"]["date"] or {}).get("start") or "")})
    return out


async def weekly_digest(cfg: dict) -> str:
    items = await unread(cfg, 15)
    if not items:
        return ""
    lines = [f"📚 **안 읽은 읽을거리 {len(items)}개**"]
    for i, it in enumerate(items, 1):
        first = next((l.lstrip("- ").strip() for l in it["summary"].splitlines() if l.strip()), "")
        lines.append(f"{i}. **{it['title'][:60]}** — {first[:80]}\n   <{it['url']}>")
    lines.append("읽은 건 \"○○ 읽었어\"라고 말해 주세요.")
    return "\n".join(lines)[:1900]


@tool("readlater_list", "나중에 읽기 목록에서 안 읽은 것을 보여준다.", {"properties": {}}, group="reading")
async def readlater_list(ctx):
    items = await unread(ctx.cfg)
    ctx.store.put("readlater_index", [it["id"] for it in items])
    return [f"{i}. {it['title']} ({it['saved']}) {it['url']}" for i, it in enumerate(items, 1)] or "안 읽은 것이 없어요."


@tool("readlater_done", "나중에 읽기 항목을 읽음 처리한다. query는 readlater_list의 번호나 제목 일부.",
      {"properties": {"query": {"type": "string"}}, "required": ["query"]}, group="reading")
async def readlater_done(ctx, query: str):
    items = await unread(ctx.cfg)
    q = str(query).strip()
    if q.isdigit():
        idx = ctx.store.get("readlater_index") or [it["id"] for it in items]
        hits = [it for it in items if 1 <= int(q) <= len(idx) and it["id"] == idx[int(q) - 1]]
    else:
        hits = [it for it in items if q.lower() in it["title"].lower()]
    if len(hits) != 1:
        return "맞는 항목이 없어요." if not hits else "여러 개가 맞아요: " + "; ".join(h["title"] for h in hits[:5])
    await dev.notion("PATCH", f"/pages/{hits[0]['id']}", {"properties": {"읽음": {"checkbox": True}}})
    return f"읽음 처리했어요: {hits[0]['title']}"
