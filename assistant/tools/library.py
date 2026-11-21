"""문서 서재 도구 (지휘자가 직접 사용)."""
from __future__ import annotations

from datetime import datetime

from .. import library
from .base import tool


@tool("library_list", "서재에 저장된 문서 목록 (번호, 제목, 쪽수, 요약, 키워드). 어떤 문서에 물어볼지 고를 때 먼저 본다.",
      {"properties": {}}, group="library")
async def library_list(ctx):
    docs = library.list_docs(ctx.store)
    if not docs:
        return "서재가 비어 있습니다. 디스코드에 PDF나 텍스트 파일을 첨부하면 저장됩니다."
    return [{"id": d["id"], "title": d["title"], "pages": d["pages"] or None,
             "summary": d["summary"][:300], "keywords": d["keywords"][:200],
             "added": datetime.fromtimestamp(d["created"]).strftime("%Y-%m-%d")} for d in docs]


@tool("library_ask", "서재 문서에서 관련 쪽을 검색해 그 쪽만 읽고 답한다 (쪽 번호·원문 인용 포함). 답변은 이미 사용자용 형식이므로 그대로 전달한다. "
      "쪽 번호가 언급되면 question에 그대로 넣어라 (예: '12~15쪽에서 …').",
      {"properties": {"doc_ids": {"type": "array", "items": {"type": "integer"}, "description": "물어볼 문서 번호들 (최대 3). 비우면 서재 전체에서 찾음"},
                      "question": {"type": "string", "description": "질문 (대화 맥락을 풀어서 완결된 문장으로)"}},
       "required": ["question"]}, group="library")
async def library_ask(ctx, question: str, doc_ids: list[int] | None = None):
    await ctx.notify("📚 문서 읽는 중…")
    return await library.ask(ctx.extras["client"], ctx.store, ctx.cfg, doc_ids or [], question)


@tool("library_delete", "서재에서 문서를 지운다 (Files API에서도 삭제, 사용자 확인 후).",
      {"properties": {"doc_id": {"type": "integer"}}, "required": ["doc_id"]}, group="library", confirm=True,
      summarize=lambda a: f"🗑️ 서재 문서 #{a.get('doc_id')} 삭제 (되돌릴 수 없음)")
async def library_delete(ctx, doc_id: int):
    ok = await library.delete_document(ctx.extras["client"], ctx.store, doc_id)
    return "삭제했습니다." if ok else "해당 문서가 없습니다."
