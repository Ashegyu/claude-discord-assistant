"""사용법 안내: docs/usage.md를 주제(## 제목)별로 읽어, 비서가 지어내지 않고 문서대로 답하게 한다.

- 지휘자 도구 `usage_guide(question)`: 관련 섹션을 찾아 돌려줌
- `!help 주제`: Claude를 거치지 않고 섹션을 바로 보여줌 (비용 0)
"""
from __future__ import annotations

import re
from pathlib import Path

from .tools.base import tool

PATH = Path(__file__).resolve().parent.parent / "docs" / "usage.md"


def sections(text: str | None = None) -> list[dict]:
    text = PATH.read_text(encoding="utf-8") if text is None else text
    out = []
    for block in re.split(r"\n(?=## )", text):
        if not block.startswith("## "):
            continue
        title, _, body = block.partition("\n")
        kw = re.search(r"^키워드:\s*(.+)$", body, re.M)
        keywords = [k.strip() for k in kw.group(1).split(",")] if kw else []
        out.append({"title": title[3:].strip(), "keywords": keywords,
                    "body": re.sub(r"^키워드:.*\n?", "", body, flags=re.M).strip()})
    return out


def _score(sec: dict, query: str) -> float:
    q = query.lower()
    words = [w for w in re.split(r"[\s,?!.·]+", q) if len(w) >= 2]
    s = 0.0
    for k in sec["keywords"]:
        if k.lower() in q:
            s += 3 + len(k) * 0.2            # 키워드가 질문에 그대로 들어 있음
    title, body = sec["title"].lower(), sec["body"].lower()
    for w in words:
        stem = w[:2] if len(w) > 2 else w     # 조사가 붙어도 앞 두 글자로 느슨하게
        if w in title or any(w in k.lower() or k.lower() in w for k in sec["keywords"]):
            s += 2
        elif w in body:
            s += 1
        elif stem in title:
            s += 0.5
    for cmd in re.findall(r"![a-z]+", q):     # '!dash 뭐야' 처럼 명령을 물을 때
        if cmd in body:
            s += 4
    return s


def find(query: str, limit: int = 2, secs: list[dict] | None = None) -> list[dict]:
    secs = sections() if secs is None else secs
    ranked = sorted(((_score(s, query), i, s) for i, s in enumerate(secs)), key=lambda x: (-x[0], x[1]))
    return [s for sc, _, s in ranked[:limit] if sc >= 2]


def topics() -> list[str]:
    return [s["title"] for s in sections()]


def render(secs: list[dict]) -> str:
    return "\n\n".join(f"**{s['title']}**\n{s['body']}" for s in secs)


def help_text(query: str) -> str:
    """!help 주제"""
    try:
        hits = find(query, 1)
    except OSError:
        return "사용법 문서(docs/usage.md)를 찾지 못했어요. `!update`로 최신 코드를 받아 주세요."
    if not hits:
        return f"'{query}'에 맞는 항목을 못 찾았어요. 주제: " + " · ".join(f"`{t.split(':')[0].split(' (')[0]}`" for t in topics())
    return render(hits)[:1900]


@tool("usage_guide", "이 비서의 사용법 문서에서 관련 항목을 찾는다. 사용자가 비서의 기능·명령·설정·알림 시각·'어떻게 써?'·"
      "'뭐 할 수 있어?'·'○○ 왜 안 돼?'를 물으면 반드시 먼저 이 도구로 확인하고, 문서에 있는 내용만 답한다.",
      {"properties": {"question": {"type": "string", "description": "사용자의 질문 그대로 (핵심 단어 포함)"}},
       "required": ["question"]}, group="core")
async def usage_guide(ctx, question: str):
    try:
        hits = find(question, 3)
    except OSError:
        return "사용법 문서를 읽지 못했습니다 (docs/usage.md 없음). 사용자에게 !update를 안내하세요."
    if not hits:
        return {"결과": "맞는 항목 없음", "전체 주제": topics(),
                "안내": "주제 목록을 보여주고 어떤 것이 궁금한지 되묻거나, 다른 단어로 다시 찾으세요. 문서에 없는 기능을 지어내지 마세요."}
    return render(hits)
