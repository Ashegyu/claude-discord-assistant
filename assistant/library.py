"""문서 서재 v2 — 쪽 단위 텍스트 검색 + 필요한 쪽만 Claude에게.

업로드
  PDF → 쪽별 텍스트 추출(pypdf, 무료) → SQLite에 저장, 원본 PDF는 VM 디스크(data/library/)에 보관
  요약은 앞부분 몇 쪽 텍스트만으로 Haiku가 짧게 작성 (문서 전체를 읽지 않음)

질문
  ① Haiku가 검색어 확장: 한글 질문 → 원문 용어·약어·코드까지 (예: "안전 등심선" → safety contour, SAFCON, DEPCNT02)
  ② 쪽마다 점수(검색어 희소도 × 등장 횟수) → 상위 N쪽 + 바로 다음 쪽(표가 넘어가는 경우)
  ③ 고른 쪽 텍스트를 '쪽 = 블록'인 문서로 전달 + 인용(citations) → 인용이 정확한 쪽 번호로 이어짐
  ④ 그림·표 쪽(텍스트가 거의 없거나 질문에 그림/표/색 등이 있으면): 그 쪽만 잘라 Files API로 올려 PDF로 보여줌
     (같은 쪽 묶음은 하루 동안 file_id 재사용)

질문당 보통 수천~수만 토큰. 문서 전체를 읽던 v1보다 훨씬 저렴.
v1 방식으로 저장된 문서(Files API 부분 파일)는 그대로 v1 방식으로 답한다.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import time
from pathlib import Path

log = logging.getLogger("library")
LIB_DIR = Path(os.environ.get("DATA_DIR", "data")) / "library"
VISUAL_WORDS = re.compile(r"그림|도표|표\b|표에|표의|도식|다이어그램|색|컬러|심볼|기호|모양|레이아웃|figure|fig\.|table|diagram|colou?r|symbol|chart", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, filename TEXT, mime TEXT, size INTEGER,
  pages INTEGER, parts TEXT, summary TEXT, keywords TEXT, created REAL);
CREATE TABLE IF NOT EXISTS doc_pages (doc_id INTEGER, page INTEGER, text TEXT, PRIMARY KEY (doc_id, page));
CREATE TABLE IF NOT EXISTS visual_cache (key TEXT PRIMARY KEY, file_id TEXT, expires REAL);
"""


def init(store) -> None:
    with store.lock:
        store.db.executescript(SCHEMA)
        cols = {r[1] for r in store.db.execute("PRAGMA table_info(docs)")}
        if "kind" not in cols:  # v1 → v2 마이그레이션
            store.db.execute("ALTER TABLE docs ADD COLUMN kind TEXT DEFAULT 'files'")
        store.db.commit()


# ───────────── 저장소 ─────────────
def _row(r) -> dict:
    return {"id": r[0], "title": r[1], "filename": r[2], "mime": r[3], "size": r[4], "pages": r[5],
            "parts": json.loads(r[6] or "[]"), "summary": r[7] or "", "keywords": r[8] or "", "created": r[9],
            "kind": r[10] or "files"}


COLS = "id,title,filename,mime,size,pages,parts,summary,keywords,created,kind"


def get_doc(store, doc_id: int) -> dict | None:
    rows = store._exec(f"SELECT {COLS} FROM docs WHERE id=?", (doc_id,))
    return _row(rows[0]) if rows else None


def list_docs(store) -> list[dict]:
    return [_row(r) for r in store._exec(f"SELECT {COLS} FROM docs ORDER BY id DESC")]


def page_texts(store, doc_id: int) -> dict[int, str]:
    return {p: t for p, t in store._exec("SELECT page, text FROM doc_pages WHERE doc_id=? ORDER BY page", (doc_id,))}


# ───────────── 텍스트 추출 ─────────────
def _clean(t: str) -> str:
    t = t.replace("\x00", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


def extract_pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for p in reader.pages:
        try:
            out.append(_clean(p.extract_text() or ""))
        except Exception:
            out.append("")
    return out


def chunk_text(text: str, size: int) -> list[str]:
    """텍스트 파일은 문단 경계로 size자씩 '쪽'을 만든다."""
    chunks, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) > size:
            chunks.append(buf)
            buf = ""
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size:]
        buf = f"{buf}\n\n{para}" if buf else para
    if buf.strip():
        chunks.append(buf)
    return [_clean(c) for c in chunks] or [""]


# ───────────── 업로드 ─────────────
SUMMARY_TOOL = {
    "name": "describe",
    "description": "문서 내용을 기록한다.",
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "무엇에 대한 문서인지 2~3문장 (한국어)"},
        "keywords": {"type": "string", "description": "핵심어 15개 이내, 쉼표 구분 (원문 용어·약어 포함)"},
    }, "required": ["summary", "keywords"]},
}


async def add_document(client, store, cfg, filename: str, data: bytes, mime: str, title: str | None = None) -> dict:
    lcfg = cfg["library"]
    title = title or filename.rsplit(".", 1)[0]
    is_pdf = mime == "application/pdf" or filename.lower().endswith(".pdf")
    if is_pdf:
        pages = extract_pdf_pages(data)
        mime = "application/pdf"
    else:
        pages = chunk_text(data.decode("utf-8", "replace"), lcfg["text_chunk_chars"])
        mime = "text/plain"

    store._exec("INSERT INTO docs (title,filename,mime,size,pages,parts,summary,keywords,created,kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title, filename, mime, len(data), len(pages) if is_pdf else 0, "[]", "", "", time.time(), "text"))
    doc_id = store._exec("SELECT last_insert_rowid()")[0][0]
    with store.lock:
        store.db.executemany("INSERT INTO doc_pages VALUES (?,?,?)", [(doc_id, i + 1, t) for i, t in enumerate(pages)])
        store.db.commit()
    if is_pdf:  # 그림·표 쪽을 나중에 잘라 보여주기 위해 원본 보관
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        (LIB_DIR / f"{doc_id}.pdf").write_bytes(data)

    text_chars = sum(len(t) for t in pages)
    scanned = is_pdf and text_chars < 50 * max(1, len(pages))
    summary, keywords = "", ""
    if lcfg.get("auto_summary", True) and not scanned:
        from .claude_tools import acall_tool

        head = "\n\n".join(f"[p.{i + 1}]\n{t}" for i, t in enumerate(pages[:8]))[:12000]
        try:
            d = await acall_tool(client, store, lcfg["summary_model"],
                                 f"문서 '{title}'의 앞부분이다 (전체 {len(pages)}쪽). 요약과 키워드를 기록하라.\n\n{head}",
                                 SUMMARY_TOOL, "library:describe", 600)
            summary, keywords = d.get("summary", ""), d.get("keywords", "")
        except Exception as e:
            log.warning("요약 실패: %s", e)
    if scanned:
        summary = "⚠️ 텍스트가 거의 없는 PDF(스캔본으로 보임). 검색이 어려우니 '12~15쪽에서 ...'처럼 쪽을 지정해 물어보세요."
    store._exec("UPDATE docs SET summary=?, keywords=? WHERE id=?", (summary, keywords, doc_id))
    return get_doc(store, doc_id)


def referenced_file_ids(store) -> set[str]:
    """서재가 아직 쓰는 Files API 파일 (v1 문서 파일 + 만료 전 그림 캐시)."""
    ids = {p["file_id"] for d in list_docs(store) for p in (get_doc(store, d["id"]) or {}).get("parts", [])}
    ids |= {r[0] for r in store._exec("SELECT file_id FROM visual_cache WHERE expires > ?", (time.time(),))}
    return ids


async def orphan_files(client, store) -> list:
    """Files API에 남아 있지만 서재가 쓰지 않는 파일."""
    used = referenced_file_ids(store)
    out = []
    async for f in client.files.list(limit=100):
        if f.id not in used:
            out.append(f)
    return out


async def delete_files(client, store, files: list) -> int:
    n = 0
    for f in files:
        try:
            await client.files.delete(f.id)
            n += 1
        except Exception as e:
            log.warning("파일 삭제 실패 %s: %s", f.id, e)
    store._exec("DELETE FROM visual_cache WHERE expires <= ?", (time.time(),))
    return n


async def delete_document(client, store, doc_id: int) -> bool:
    d = get_doc(store, doc_id)
    if not d:
        return False
    for p in d["parts"]:  # v1 문서의 Files API 파일
        try:
            await client.files.delete(p["file_id"])
        except Exception as e:
            log.warning("파일 삭제 실패 %s: %s", p["file_id"], e)
    for key, fid in store._exec("SELECT key, file_id FROM visual_cache WHERE key LIKE ?", (f"{doc_id}:%",)):
        try:
            await client.files.delete(fid)
        except Exception:
            pass
    store._exec("DELETE FROM visual_cache WHERE key LIKE ?", (f"{doc_id}:%",))
    store._exec("DELETE FROM doc_pages WHERE doc_id=?", (doc_id,))
    store._exec("DELETE FROM docs WHERE id=?", (doc_id,))
    (LIB_DIR / f"{doc_id}.pdf").unlink(missing_ok=True)
    return True


# ───────────── 검색 ─────────────
TERMS_TOOL = {
    "name": "search_terms",
    "description": "문서에서 찾을 검색어를 제출한다.",
    "input_schema": {"type": "object", "properties": {
        "terms": {"type": "array", "items": {"type": "string"},
                  "description": "검색어 6~15개. 질문의 핵심어를 문서 원문 언어(대개 영어)로 옮긴 표현, 동의어, 약어, "
                                 "객체·속성 코드, 절 번호 등. 한국어 문서면 한국어 표현도. 너무 일반적인 단어(the, data)는 빼라."},
        "pages": {"type": "array", "items": {"type": "integer"}, "description": "질문에 쪽 번호가 명시돼 있으면 그 쪽들"},
    }, "required": ["terms"]},
}


async def expand_query(client, store, cfg, question: str, docs: list[dict]) -> dict:
    from .claude_tools import acall_tool

    about = "\n".join(f"- {d['title']}: {d['summary'][:200]} (키워드: {d['keywords'][:150]})" for d in docs)
    try:
        return await acall_tool(client, store, cfg["library"]["summary_model"],
                                f"다음 문서들에서 질문의 답이 있는 쪽을 찾으려 한다.\n문서:\n{about}\n\n질문: {question}",
                                TERMS_TOOL, "library:terms", 400)
    except Exception as e:
        log.warning("검색어 확장 실패: %s", e)
        return {"terms": [w for w in re.findall(r"[\w.-]{2,}", question)]}


def score_pages(pages: dict[int, str], terms: list[str]) -> list[tuple[float, int]]:
    """쪽 점수 = Σ idf(검색어) × log(1 + 등장 횟수). 대소문자 무시 부분 문자열 매칭."""
    terms = [t.strip().lower() for t in terms if len(t.strip()) >= 2]
    low = {p: t.lower() for p, t in pages.items()}
    n = max(1, len(low))
    scores = {p: 0.0 for p in low}
    for term in dict.fromkeys(terms):
        hits = {p: txt.count(term) for p, txt in low.items()}
        df = sum(1 for c in hits.values() if c)
        if not df:
            continue
        idf = math.log(1 + n / df)
        for p, c in hits.items():
            if c:
                scores[p] += idf * math.log(1 + c)
    return sorted(((s, p) for p, s in scores.items() if s > 0), reverse=True)


def select_pages(pages: dict[int, str], ranked: list[tuple[float, int]], top: int, neighbors: int, max_chars: int) -> list[int]:
    chosen: list[int] = []
    for i, (_, p) in enumerate(ranked[:top]):
        for q in [p] + ([p + k for k in range(1, neighbors + 1)] if i < 3 else []):  # 상위 3쪽은 다음 쪽까지 (표 이어짐)
            if q in pages and q not in chosen:
                chosen.append(q)
    out, total = [], 0
    for p in chosen:
        if total + len(pages[p]) > max_chars and out:
            break
        out.append(p)
        total += len(pages[p])
    return sorted(out)


# ───────────── 그림·표 쪽 (Files API) ─────────────
async def visual_file_id(client, store, doc_id: int, page_nums: list[int]) -> str | None:
    path = LIB_DIR / f"{doc_id}.pdf"
    if not path.exists() or not page_nums:
        return None
    key = f"{doc_id}:{','.join(map(str, page_nums))}"
    hit = store._exec("SELECT file_id, expires FROM visual_cache WHERE key=?", (key,))
    if hit and hit[0][1] > time.time() + 600:
        return hit[0][0]
    from pypdf import PdfReader, PdfWriter

    reader, w = PdfReader(str(path)), PdfWriter()
    for p in page_nums:
        if 1 <= p <= len(reader.pages):
            w.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    w.write(buf)
    meta = await client.files.upload(file=(f"doc{doc_id}_p{'-'.join(map(str, page_nums))}.pdf", buf.getvalue(), "application/pdf"),
                                     expires_in_seconds=86400)
    store._exec("INSERT OR REPLACE INTO visual_cache VALUES (?,?,?)", (key, meta.id, time.time() + 86400))
    return meta.id


# ───────────── 답변 ─────────────
def format_answer(resp_content: list, page_maps: dict[int, list[int]], max_quotes: int = 6) -> str:
    """page_maps: 문서 블록 번호 → [원본 쪽 번호…] (텍스트 문서는 블록 순서, 그림 PDF는 잘라낸 쪽 순서)"""
    text, notes, seen = [], [], {}
    for b in resp_content:
        if getattr(b, "type", None) != "text":
            continue
        text.append(b.text)
        for c in getattr(b, "citations", None) or []:
            title = getattr(c, "document_title", "") or "문서"
            pm = page_maps.get(getattr(c, "document_index", -1), [])
            ctype = getattr(c, "type", "")
            if ctype == "content_block_location":
                idx = getattr(c, "start_block_index", 0) or 0
                page = pm[idx] if idx < len(pm) else None
            elif ctype == "page_location":
                idx = (getattr(c, "start_page_number", 1) or 1) - 1
                page = pm[idx] if pm and idx < len(pm) else getattr(c, "start_page_number", None)
            else:
                page = None
            where = f"p.{page}" if page else ""
            quote = " ".join((getattr(c, "cited_text", "") or "").split())[:120]
            key = (title, where, quote[:40])
            if key not in seen and len(notes) < max_quotes:
                seen[key] = len(notes) + 1
                notes.append(f"[{seen[key]}] {title} {where} — “{quote}”")
            if key in seen:
                text.append(f"[{seen[key]}]")
    body = "".join(text).strip()
    return body + ("\n\n**출처**\n" + "\n".join(notes) if notes else "")


async def ask(client, store, cfg, doc_ids: list[int], question: str) -> str:
    lcfg = cfg["library"]
    docs = [get_doc(store, i) for i in doc_ids] if doc_ids else list_docs(store)
    docs = [d for d in docs if d][: lcfg["max_docs_per_question"]]
    if not docs:
        return "서재에 해당 문서가 없습니다."
    if all(d["kind"] == "files" for d in docs):
        return await ask_v1(client, store, cfg, [d["id"] for d in docs], question)

    q = await expand_query(client, store, cfg, question, docs)
    terms, forced = q.get("terms", []), [p for p in q.get("pages", []) if isinstance(p, int)]
    want_visual = bool(VISUAL_WORDS.search(question))

    blocks: list[dict] = []
    page_maps: dict[int, list[int]] = {}
    used: list[str] = []
    budget = lcfg["max_context_chars"]
    for d in docs:
        if d["kind"] != "text":
            continue
        pages = page_texts(store, d["id"])
        if forced:
            chosen = sorted(p for p in forced if p in pages)
        else:
            chosen = select_pages(pages, score_pages(pages, terms), lcfg["top_pages"], lcfg["neighbor_pages"],
                                  budget // max(1, len(docs)))
        if not chosen:
            continue
        label = "쪽" if d["mime"] == "application/pdf" else "구간"
        used.append(f"#{d['id']} {d['title']} ({label} {', '.join(map(str, chosen))})")
        page_maps[len(blocks)] = chosen
        blocks.append({"type": "document", "title": d["title"], "citations": {"enabled": True},
                       "source": {"type": "content", "content": [
                           {"type": "text", "text": f"[{label} {p}]\n{pages[p] or '(이 쪽은 텍스트가 거의 없음 — 그림·표 위주)'}"}
                           for p in chosen]}})
        # 그림·표 쪽은 원본 PDF로도 보여줌
        if d["mime"] == "application/pdf":
            sparse = [p for p in chosen if len(pages[p]) < lcfg["visual_min_chars"]]
            vis = (chosen if want_visual else sparse)[: lcfg["visual_max_pages"]]
            if vis:
                try:
                    fid = await visual_file_id(client, store, d["id"], vis)
                    if fid:
                        page_maps[len(blocks)] = vis
                        blocks.append({"type": "document", "title": f"{d['title']} (원본 {', '.join(map(str, vis))}쪽)",
                                       "citations": {"enabled": True}, "source": {"type": "file", "file_id": fid}})
                except Exception as e:
                    log.warning("그림 쪽 업로드 실패: %s", e)

    if not blocks:
        return (f"서재 문서에서 관련 내용을 찾지 못했습니다 (검색어: {', '.join(terms[:10])}). "
                "다른 표현으로 묻거나 쪽 번호를 지정해 주세요.")
    blocks.append({"type": "text", "text":
                   f"질문: {question}\n\n위 문서 발췌만 근거로 한국어로 답하라. 발췌에 없으면 '발췌한 쪽에는 없다'고 말하라. "
                   "용어·수치·조건은 원문 표현을 살려라. 디스코드용으로 1200자 이내."})
    resp = await client.messages.create(model=lcfg["answer_model"], max_tokens=3000,
                                        messages=[{"role": "user", "content": blocks}])
    store.record_usage(lcfg["answer_model"], "library:ask",
                       resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    return f"📚 {' · '.join(used)}\n\n" + format_answer(resp.content, page_maps)


# ───────────── v1 문서 (Files API 부분 파일) 호환 ─────────────
async def ask_v1(client, store, cfg, doc_ids: list[int], question: str) -> str:
    lcfg = cfg["library"]
    blocks, maps, used = [], {}, []
    for did in doc_ids:
        d = get_doc(store, did)
        for p in d["parts"][: lcfg.get("max_parts_per_question", 2)]:
            start, end = (int(x) for x in p["pages"].split("-")) if p.get("pages") else (1, 1)
            maps[len(blocks)] = list(range(start, end + 1))
            blocks.append({"type": "document", "source": {"type": "file", "file_id": p["file_id"]},
                           "title": d["title"], "citations": {"enabled": True}})
        used.append(f"#{d['id']} {d['title']}")
    blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    blocks.append({"type": "text", "text": f"질문: {question}\n\n문서 내용만 근거로 한국어로, 1200자 이내로 답하라."})
    resp = await client.messages.create(model=lcfg["answer_model"], max_tokens=3000,
                                        messages=[{"role": "user", "content": blocks}])
    store.record_usage(lcfg["answer_model"], "library:ask", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    return f"📚 {' · '.join(used)}\n\n" + format_answer(resp.content, maps)
