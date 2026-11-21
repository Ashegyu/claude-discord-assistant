"""다음 검색 (카카오 REST 키): 블로그·카페·웹문서·동영상 후기 검색, 책 검색 → Notion 독서 기록.

국내 맛집·후기·생활 정보는 블로그·카페 검색이 잘 맞고, Claude 웹 검색(1회 $0.01)을 쓰지 않아도 된다.
GET dapi.kakao.com/v2/search/{web|blog|cafe|vclip}, /v3/search/book — Authorization: KakaoAK <REST 키>
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .tools import dev
from .tools.base import tool

BASE = "https://dapi.kakao.com"
KINDS = {"blog": "/v2/search/blog", "cafe": "/v2/search/cafe", "web": "/v2/search/web", "video": "/v2/search/vclip"}
STATUSES = ["읽고 싶음", "읽는 중", "완독", "중단"]


def clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


async def _get(path: str, **params) -> dict:
    key = os.environ.get("KAKAO_REST_API_KEY", "")
    if not key:
        raise RuntimeError("카카오 REST API 키가 없어요")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(BASE + path, params=params, headers={"Authorization": f"KakaoAK {key}"})
    if r.status_code != 200:
        raise RuntimeError(f"다음 검색 {r.status_code}: {r.text[:150]}")
    return r.json()


async def search(query: str, kind: str = "blog", recent: bool = False, size: int = 8) -> list[dict]:
    d = await _get(KINDS.get(kind, KINDS["blog"]), query=query, sort="recency" if recent else "accuracy", size=min(size, 15))
    out = []
    for x in d.get("documents", []):
        out.append({"title": clean(x.get("title")), "text": clean(x.get("contents"))[:220],
                    "source": x.get("blogname") or x.get("cafename") or x.get("author") or "",
                    "date": (x.get("datetime") or "")[:10], "url": x.get("url", "")})
    return out


async def books(query: str, size: int = 5) -> list[dict]:
    d = await _get("/v3/search/book", query=query, size=size)
    return [{"title": x.get("title", ""), "authors": ", ".join(x.get("authors") or []), "publisher": x.get("publisher", ""),
             "isbn": (x.get("isbn") or "").split()[-1] if x.get("isbn") else "", "year": (x.get("datetime") or "")[:4],
             "thumbnail": x.get("thumbnail", ""), "url": x.get("url", ""), "about": clean(x.get("contents"))[:200]}
            for x in d.get("documents", [])]


@tool("daum_search", "다음 블로그·카페·웹문서·동영상 검색. 국내 맛집·가게·제품 후기, 여행지, 생활 정보처럼 한국어 후기가 중요한 질문에 쓴다 "
      "(무료). kind: blog(후기 기본), cafe(커뮤니티 의견), web, video. recent=true면 최신순. 결과의 url을 출처로 함께 알려라.",
      {"properties": {"query": {"type": "string", "description": "예: '서면 ○○국밥 후기'"},
                      "kind": {"type": "string", "enum": list(KINDS), "default": "blog"},
                      "recent": {"type": "boolean", "default": False}}, "required": ["query"]}, group="search")
async def daum_search(ctx, query: str, kind: str = "blog", recent: bool = False):
    return await search(query, kind, recent) or "결과 없음"


@tool("book_search", "책 검색 (제목·저자·ISBN). 독서 기록에 넣기 전에 어떤 책인지 확인할 때 쓴다.",
      {"properties": {"query": {"type": "string"}}, "required": ["query"]}, group="reading")
async def book_search(ctx, query: str):
    return await books(query) or "결과 없음"


@tool("book_log", "독서 기록(Notion '독서')에 책을 넣거나 상태를 바꾼다. 제목으로 책 정보를 찾아 저자·출판사·표지를 채운다. "
      "status: 읽고 싶음 / 읽는 중 / 완독 / 중단. 같은 책이 이미 있으면 상태·메모만 고친다. 제목이 애매하면 먼저 book_search로 확인.",
      {"properties": {"title": {"type": "string"}, "status": {"type": "string", "enum": STATUSES, "default": "읽는 중"},
                      "note": {"type": "string", "default": ""}}, "required": ["title"]}, group="reading")
async def book_log(ctx, title: str, status: str = "읽는 중", note: str = ""):
    db = (ctx.cfg.get("notion") or {}).get("books_database_id")
    if not db:
        return "독서 DB가 설정되지 않았어요 (config.yaml notion.books_database_id)."
    found = await books(title, 1)
    b = found[0] if found else {"title": title, "authors": "", "publisher": "", "isbn": "", "thumbnail": "", "url": ""}
    today = datetime.now(ZoneInfo(ctx.cfg["timezone"])).date().isoformat()
    txt = lambda s: {"rich_text": [{"text": {"content": (s or "")[:1900]}}]}  # noqa: E731
    props = {"상태": {"select": {"name": status if status in STATUSES else "읽는 중"}}, "기록일": {"date": {"start": today}}}
    if note:
        props["메모"] = txt(note)
    flt = ({"property": "ISBN", "rich_text": {"equals": b["isbn"]}} if b["isbn"]
           else {"property": "제목", "title": {"equals": b["title"]}})
    hit = (await dev.notion("POST", f"/databases/{db}/query", {"filter": flt, "page_size": 1})).get("results") or []
    if hit:
        await dev.notion("PATCH", f"/pages/{hit[0]['id']}", {"properties": props})
        return f"📖 '{b['title']}' 상태를 {status}(으)로 바꿨어요."
    props |= {"제목": {"title": [{"text": {"content": b["title"][:200]}}]}, "저자": txt(b["authors"]), "출판사": txt(b["publisher"]),
              "ISBN": txt(b["isbn"])}
    if b["thumbnail"]:
        props["표지"] = {"url": b["thumbnail"]}
    if b["url"]:
        props["링크"] = {"url": b["url"]}
    body = {"parent": {"database_id": db}, "properties": props}
    if b["thumbnail"]:
        body["cover"] = {"type": "external", "external": {"url": b["thumbnail"]}}
    p = await dev.notion("POST", "/pages", body)
    who = f" ({b['authors']})" if b["authors"] else (" (책 정보를 못 찾아 제목만 넣었어요)" if not found else "")
    return f"📖 '{b['title']}'{who} — {status}로 기록했어요. {p.get('url', '')}"
