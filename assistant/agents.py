"""에이전트 오케스트라.

    디스코드 메시지
        │
        ▼
  ① 라우터 (Haiku)  ── 간단/복잡 분류
        │
        ├─ quick ─▶ ② 지휘자 (Haiku 실행자 + Opus 어드바이저, 조언 1회까지)
        └─ deep  ─▶ ② 지휘자 (Sonnet 실행자 + Opus 어드바이저, 조언 3회까지)
                        │  delegate(agent, task)
                        ▼
              ③ 전문 에이전트 (Haiku, 자기 도구만)
                 mail · schedule · dev · research(웹검색/코드실행) · life
                        │
                        ▼  대량·비긴급 작업
              ④ 배치 (Message Batches, 50% 할인) → 끝나면 디스코드로 결과

프롬프트 캐싱 (tools → system → messages 순서, 중단점 최대 4개):
  - 도구 정의 마지막 항목: 1시간 캐시
  - 시스템 프롬프트의 고정 부분: 1시간 캐시 (현재 시각 같은 변하는 부분은 그 뒤에 둠)
  - 대화: 최상위 cache_control 자동 캐싱 (5분)
"""
from __future__ import annotations

import asyncio
import time
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic

from .tools.base import REGISTRY, Context, Tool, tools_for

log = logging.getLogger("orchestra")
ADVISOR_BETA = "advisor-tool-2026-03-01"
CACHE_1H = {"type": "ephemeral", "ttl": "1h"}

# ───────────── 전문 에이전트 정의 ─────────────
WORKERS = {
    "mail": {
        "groups": ["mail"],
        "desc": "Gmail 받은편지함 검색·읽기·요약·답장 초안 (읽기 전용, 보내기 불가)",
        "prompt": "너는 메일 담당 에이전트다. Gmail은 읽기 전용이다. 검색 후 필요한 메일만 열어 읽고, "
                  "보낸 사람·날짜·핵심 내용·해야 할 일을 정리해 보고하라. 광고·뉴스레터는 요청이 없으면 생략. "
                  "답장을 써 달라면 mail_search로 uid를 찾아 mail_draft로 초안을 만들고, 결과(초안·링크)를 그대로 보고하라.",
    },
    "schedule": {
        "groups": ["calendar", "reminder"],
        "desc": "구글 캘린더 조회·일정 추가, 리마인더·할 일(갤럭시 리마인더 동기화) 등록·조회·완료·삭제",
        "prompt": "너는 일정 담당 에이전트다. 날짜·시간은 사용자의 시간대 기준으로 해석하라. "
                  "'내일', '다음 주 금요일' 같은 표현은 현재 시각을 기준으로 정확한 날짜로 바꿔 쓴다.",
    },
    "dev": {
        "groups": ["github", "notion"],
        "desc": "GitHub 커밋·diff·이슈 조회, 이슈 생성, 워크플로(개발 일지) 실행, Notion 개발 일지 검색·메모 작성",
        "prompt": "너는 개발 담당 에이전트다. 레포 조회 결과를 근거로만 답하고, 커밋은 짧은 해시와 함께 인용하라.",
    },
    "research": {
        "groups": ["research", "search"],
        "server_tools": True,
        "desc": "웹 검색·웹 페이지 읽기·코드 실행(계산, 데이터 분석), 구글 드라이브 문서 검색",
        "prompt": "너는 조사 담당 에이전트다. 최신 정보는 웹 검색으로 확인하고, 출처 URL을 함께 보고하라. "
                  "계산·표·통계는 코드 실행으로 정확히 구하라. 국내 후기·커뮤니티 의견·한국어 생활 정보는 먼저 daum_search(무료)로 찾고, "
                  "최신 뉴스·해외·공식 자료가 필요할 때 web_search를 쓴다.",
    },
    "life": {
        "groups": ["life", "search"],
        "desc": "날씨, 지금 나가도 되는지(부산 날씨·특보·파도·버스 도착), 카카오 장소 검색(맛집·가게), 카카오톡 나에게 보내기",
        "prompt": "너는 생활 담당 에이전트다. 장소는 거리·주소·카카오맵 링크와 함께 알려라. "
                  "맛집·가게 추천이나 '어때?' 질문은 kakao_place_search로 후보를 찾은 뒤 daum_search(blog)로 실제 후기를 2~3곳만 확인해 "
                  "장단점을 요약하고 후기 링크를 붙여라. 광고성 후기는 걸러서 판단하라.",
    },
}

SERVER_TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5},
    {"type": "code_execution_20260120", "name": "code_execution"},
]

ORCHESTRATOR_PROMPT = """너는 Kim의 개인 비서 '지휘자'다. 디스코드로 대화한다. 항상 한국어로 답한다.

## 일하는 방식
- 직접 할 수 있는 건(대화, 메모 기억, 시각 확인, 리마인더·할 일, 대량 작업 등록) 직접 한다.
- 리마인더·할 일은 갤럭시 삼성 리마인더와 같은 목록(Microsoft To Do)이다. "할 일 뭐 있어?"는 reminder_list,
  "~했어/완료"는 reminder_list로 번호를 확인한 뒤 reminder_done. 폰에서 만든 할 일도 목록에 나온다.
- 메일·일정·개발·조사·생활 정보가 필요하면 delegate 도구로 전문 에이전트에게 맡긴다.
  한 질문에 여러 분야가 필요하면 delegate를 여러 번 호출한다 (동시에 호출해도 된다).
- 맡길 때 task에는 필요한 맥락(날짜, 레포 이름, 찾는 조건)을 빠짐없이 적는다. 전문 에이전트는 이 대화를 보지 못한다.
- 수십 개 항목을 각각 처리하는 급하지 않은 작업(예: 커밋 100개 각각 요약)은 bulk_job으로 배치에 맡긴다. 끝나면 결과가 자동으로 온다.
- 메일 발송은 할 수 없다 (읽기 전용). 일정 추가·이슈 생성·워크플로 실행·카톡 보내기·메모 쓰기는 도구가 사용자 확인을 받은 뒤 실행된다.
- 사용자가 올린 문서(PDF·텍스트)는 '서재'에 있다. 표준·사양·문서 내용 질문이면 library_list로 문서를 확인하고 library_ask로 물어라.
  library_ask 결과는 쪽 번호·인용이 붙은 완성 답변이니 내용을 바꾸지 말고 그대로 전달한다.
- 비서 자신의 사용법·기능·명령(!로 시작)·설정·알림 시각·"뭐 할 수 있어?"·"○○ 왜 안 돼?" 질문은 기억으로 답하지 말고
  반드시 usage_guide로 문서를 찾아 그 내용대로 답한다. 문서에 없는 기능·명령은 없다고 말하고 지어내지 않는다.
  답은 질문한 것만 짧게, 명령은 `백틱`으로 적는다.
- 돈: "○○에 얼마 썼어"처럼 지출을 말하면 expense_add로 바로 기록하고(확인 질문 없이) 기록한 내용을 한 줄로 알려준다. 잘못 적었다고 하면 expense_undo.
  지출 질문은 expense_query (Notion 지출 DB, 영수증 스크린샷과 말로 쌓임). 기프티콘은 coupon_list / coupon_show(바코드 이미지 전송) /
  coupon_used. "○○ 근처에서 쓸 수 있는 쿠폰"은 coupon_list로 브랜드를 보고 life 에이전트에게 그 브랜드 매장 검색을 맡긴다.
- 읽을거리: "나중에 읽기" 목록은 readlater_list, "○○ 읽었어"는 readlater_done.
- 책: "○○ 읽기 시작했어 / 다 읽었어 / 읽고 싶어"는 book_log (Notion 독서 기록). 링크 글의 "읽었어"(readlater)와 구분한다.
- 약속 잡기: 장소가 정해졌으면 share_card로 카드 링크를 만들어 준다(사용자가 페이지의 버튼으로 카톡 공유). 여러 사람의 중간 지점은
  meet_midpoint. 기프티콘을 남에게 넘기려면 coupon_share. 만든 링크는 줄이거나 바꾸지 말고 그대로 한 줄에 적는다.
- "지금 나가도 돼?", 버스 도착, 부산 특보는 life 에이전트(outing_check)에게 맡긴다.
- 과거 일("지난번 ○○ 어디였지", "○○ 언제 했지", "전에 말한 ○○")은 먼저 recall로 찾는다. 키워드는 여러 형태로.
- 주기적으로 하는 일(정수기 필터·칫솔·보일러·엔진오일)은 cycle_add / cycle_done / cycle_list.
- 생일·기념일 질문은 birthdays_upcoming (구글 주소록).
- 큰 일을 쪼개 달라는 요청: 목표·마감을 확인하고, 5~15개 단계로 나눠 각 단계에 현실적인 날짜(알림 받을 시각)를 붙인 뒤
  reminder_add_many로 한 번에 등록한다(사용자가 확인 버튼을 누름). 마감을 모르면 먼저 물어본다. 어드바이저에게 계획 검토를 받아도 좋다.
- 메일 답장 초안은 mail 에이전트에게 맡긴다(mail_draft). Gmail은 읽기 전용이라 보내지는 못하고 초안만 쓴다.
- 사용자에 대해 오래 기억할 만한 사실이나 선호를 들으면 memory_write로 저장하고, 필요할 때 memory_read로 꺼내 쓴다.

## 답변 형식 (디스코드)
- 핵심부터. 짧은 문단과 목록. 1500자를 넘기지 않는다. 표는 꼭 필요할 때만.
- 도구 결과에 없는 내용을 지어내지 않는다. 모르면 모른다고 한다.

## advisor 도구
You have access to an `advisor` tool backed by a stronger reviewer model. It takes NO parameters — when you call advisor(), your entire conversation history is automatically forwarded.
Call advisor BEFORE substantive work on multi-step requests — before committing to an interpretation or a plan that involves several delegations. Orientation is not substantive work.
Also call advisor when stuck (tool errors recurring, results that don't fit) or when considering a change of approach.
On short reactive requests (a single lookup, small talk) do not call the advisor.
Give the advice serious weight, but if a tool result directly contradicts it, trust the evidence and say so.
"""

WORKER_SUFFIX = "\n\n작업을 마치면 결과만 간결하게 보고하라 (사용자에게 직접 말하는 게 아니라 지휘자에게 보고). 도구 결과에 없는 내용은 쓰지 마라."

ROUTE_TOOL = {
    "name": "route",
    "description": "메시지를 처리할 경로를 고른다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["quick", "deep"],
                     "description": "quick: 잡담, 단일 조회(메일 몇 개, 오늘 일정, 날씨), 일정+날씨처럼 단순 조회 2~3개를 묶어 보여주는 요청, 간단한 질문. "
                                    "deep: 여러 단계·여러 분야 조합, 분석·계획·작성(큰 일 쪼개기·일정 짜기·답장 쓰기 포함), 코드·설계 판단, 애매한 요청."},
        },
        "required": ["mode"],
    },
}


def to_dict(block) -> dict:
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else block


def now_text(tz: str) -> str:
    n = datetime.now(ZoneInfo(tz))
    return f"현재 시각: {n:%Y-%m-%d %H:%M} ({'월화수목금토일'[n.weekday()]}요일, {tz})"


class Orchestra:
    def __init__(self, cfg: dict, store, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        self.store = store
        self.client = client or anthropic.AsyncAnthropic()
        self.m = cfg["models"]

    # ───────── 공통 루프 ─────────
    def _tool_defs(self, tools: list[Tool], server: bool | list[dict] = False, extra: list[dict] | None = None) -> list[dict]:
        server_defs = server if isinstance(server, list) else (list(SERVER_TOOLS) if server else [])
        defs = server_defs + list(extra or []) + [t.definition() for t in tools]
        if defs:  # 마지막 도구에 1시간 캐시 중단점 → 도구 정의 전체가 캐시됨
            defs[-1] = {**defs[-1], "cache_control": CACHE_1H}
        return defs

    def _system(self, stable: str, extra: str = "") -> list[dict]:
        dyn = now_text(self.cfg["timezone"]) + (f"\n\n{extra}" if extra else "")
        return [
            {"type": "text", "text": stable, "cache_control": CACHE_1H},
            {"type": "text", "text": dyn},  # 변하는 부분(시각·프로젝트 맥락)은 캐시 중단점 뒤
        ]

    def _record(self, model: str, role: str, usage) -> None:
        u = to_dict(usage) if usage else {}
        its = u.get("iterations") or []
        if its:  # 어드바이저가 끼면 반복별로 모델·요금이 다름 → iterations로 집계
            for it in its:
                self.store.record_usage(it.get("model") or model,
                                        "advisor" if it.get("type") == "advisor_message" else role, it)
            n = (u.get("server_tool_use") or {}).get("web_search_requests") or 0
            if n:  # 검색 횟수는 반복별이 아니라 응답 전체에만 있음
                self.store.record_searches(role, n)
        else:
            self.store.record_usage(model, role, u)

    async def _create(self, *, model, system, tools, messages, max_tokens, advisor: bool):
        kw = dict(model=model, max_tokens=max_tokens, system=system, tools=tools, messages=messages,
                  cache_control={"type": "ephemeral"})  # 대화 자동 캐싱
        if advisor:
            return await self.client.beta.messages.create(betas=[ADVISOR_BETA], **kw)
        return await self.client.messages.create(**kw)

    async def loop(self, *, model: str, role: str, system: list[dict], tools: list[Tool], tool_defs: list[dict],
                   messages: list[dict], ctx: Context, max_rounds: int, advisor: bool = False,
                   special: dict | None = None, max_tokens: int = 4096) -> str:
        """도구 호출 루프. messages는 제자리에서 갱신된다. 마지막 답변 텍스트를 돌려준다."""
        by_name = {t.name: t for t in tools}
        special = special or {}
        for _ in range(max_rounds):
            resp = await self._create(model=model, system=system, tools=tool_defs, messages=messages,
                                      max_tokens=max_tokens, advisor=advisor)
            self._record(model, role, resp.usage)
            ctx.extras["api_calls"] = ctx.extras.get("api_calls", 0) + 1
            content = [to_dict(b) for b in resp.content]
            messages.append({"role": "assistant", "content": content})

            if resp.stop_reason == "pause_turn":  # 서버 도구(웹검색·어드바이저) 일시 중지 → 그대로 이어서 요청
                continue
            if resp.stop_reason != "tool_use":
                return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()

            calls = [b for b in content if b.get("type") == "tool_use"]

            async def run_one(b):
                name, args = b["name"], b.get("input") or {}
                if name in special:
                    out = await special[name](ctx, **args)
                elif name in by_name:
                    out = await by_name[name].run(ctx, args)
                else:
                    out = f"오류: 알 수 없는 도구 {name}"
                s_out = out if isinstance(out, str) else str(out)
                ctx.extras.setdefault("trace", []).append({   # 자기 개선 분석용 기록
                    "tool": name, "by": role, "err": s_out[:200] if s_out.startswith("오류") else "",
                    "cancel": s_out.startswith("사용자가 취소")})
                return {"type": "tool_result", "tool_use_id": b["id"], "content": out}

            # 확인이 필요한 도구는 버튼이 겹치지 않게 순서대로, 나머지는 동시에
            needs = {b["id"] for b in calls if b["name"] in by_name and by_name[b["name"]].confirm}
            results = {}
            parallel = [b for b in calls if b["id"] not in needs]
            for b, r in zip(parallel, await asyncio.gather(*(run_one(b) for b in parallel))):
                results[b["id"]] = r
            for b in calls:
                if b["id"] in needs:
                    results[b["id"]] = await run_one(b)
            messages.append({"role": "user", "content": [results[b["id"]] for b in calls]})
        return "(도구 호출 횟수 상한에 도달해 여기서 멈췄습니다. 범위를 좁혀 다시 요청해 주세요.)"

    # ───────── ① 라우터 ─────────
    async def route(self, text: str) -> str:
        if text.lstrip().startswith(("!deep", "/deep", "깊게:")):
            return "deep"
        try:
            kw = dict(model=self.m["router"], max_tokens=200, tools=[ROUTE_TOOL],
                      messages=[{"role": "user", "content": f"메시지: {text[:2000]}"}])
            try:
                resp = await self.client.messages.create(tool_choice={"type": "tool", "name": "route"}, **kw)
            except anthropic.BadRequestError:
                resp = await self.client.messages.create(tool_choice={"type": "auto"}, **kw)
            self._record(self.m["router"], "router", resp.usage)
            for b in resp.content:
                if b.type == "tool_use":
                    return b.input.get("mode", "quick")
        except Exception as e:
            log.warning("라우터 실패 → quick: %s", e)
        return "quick"

    # ───────── ③ 전문 에이전트 ─────────
    async def delegate(self, ctx: Context, agent: str, task: str) -> str:
        w = WORKERS.get(agent)
        if not w:
            return f"오류: 없는 에이전트 {agent}. 가능: {', '.join(WORKERS)}"
        await ctx.notify(f"🔧 {agent} 에이전트 작업 중…")
        tools = tools_for(*w["groups"])
        server = False
        if w.get("server_tools"):
            from . import costs
            left = costs.searches_left_today(self.store, self.cfg)   # 하루 웹 검색 상한
            server = []
            for t in SERVER_TOOLS:
                if t["name"] == "web_search" and left is not None:
                    if left <= 0:
                        continue
                    t = {**t, "max_uses": min(t["max_uses"], left)}
                server.append(t)
            if left is not None and left <= 0:
                await ctx.notify("🔎 오늘 웹 검색 한도에 도달해서 검색 없이 진행해요")
                task += "\n\n(오늘 웹 검색 한도에 도달해 web_search는 쓸 수 없다. web_fetch나 알고 있는 지식으로 답하되, 검색을 못 했다는 사실을 보고에 밝혀라.)"
        msgs = [{"role": "user", "content": task}]
        return await self.loop(
            model=self.m["worker"], role=f"worker:{agent}",
            system=self._system(w["prompt"] + WORKER_SUFFIX), tools=tools,
            tool_defs=self._tool_defs(tools, server=server),
            messages=msgs, ctx=ctx, max_rounds=self.cfg["conversation"]["worker_max_rounds"],
        ) or "(보고 내용 없음)"

    def _delegate_def(self) -> dict:
        return {
            "name": "delegate",
            "description": "전문 에이전트에게 작업을 맡기고 보고를 받는다. 에이전트 목록:\n"
                           + "\n".join(f"- {k}: {v['desc']}" for k, v in WORKERS.items()),
            "input_schema": {
                "type": "object",
                "properties": {
                    "agent": {"type": "string", "enum": list(WORKERS)},
                    "task": {"type": "string", "description": "맡길 일. 필요한 맥락을 모두 포함 (에이전트는 대화를 못 본다)."},
                },
                "required": ["agent", "task"],
            },
        }

    # ───────── ② 지휘자 ─────────
    async def handle(self, ctx: Context, user_content: list[dict], text_for_route: str) -> tuple[str, str]:
        t0 = time.time()
        ctx.extras["trace"], ctx.extras["api_calls"] = [], 0
        from . import costs
        saving = costs.save_mode(self.store, self.cfg)   # 월 예산 초과 → 빠른 길만, 어드바이저 끔
        mode = "quick" if saving else await self.route(text_for_route)
        model = self.m["deep" if mode == "deep" else "quick"]
        a = self.cfg["advisor"]
        advisor_def = {"type": "advisor_20260301", "name": "advisor", "model": self.m["advisor"],
                       "max_uses": a["deep_max_uses" if mode == "deep" else "quick_max_uses"],
                       "max_tokens": a["max_tokens"]}
        direct = tools_for("memory", "reminder", "batch", "library", "core", "money", "reading", "home", "recall", "share")
        tool_defs = self._tool_defs(direct, extra=([] if saving else [advisor_def]) + [self._delegate_def()])

        history = self.store.get_history(ctx.channel_id)
        messages = history + [{"role": "user", "content": user_content}]
        from . import projects, recall
        recall.archive(self.store, ctx.channel_id, "user", text_for_route)
        project_ctx = await projects.context_text(self.store, ctx.channel_id)
        answer = await self.loop(
            model=model, role=f"orchestrator:{mode}", system=self._system(ORCHESTRATOR_PROMPT, project_ctx),
            tools=direct, tool_defs=tool_defs, messages=messages, ctx=ctx,
            max_rounds=self.cfg["conversation"]["max_tool_rounds"], advisor=not saving,
            special={"delegate": self.delegate}, max_tokens=8000,
        )
        self.store.set_history(ctx.channel_id, compact_history(messages, self.cfg["conversation"]["keep_turns"]))
        recall.archive(self.store, ctx.channel_id, "assistant", answer or "")
        tok = self.store.usage_since(t0)
        self.store.add_trace(ctx.channel_id, {
            "mode": mode, "text": text_for_route[:300], "answer": (answer or "")[:300],
            "tools": ctx.extras["trace"], "api_calls": ctx.extras["api_calls"], "sec": round(time.time() - t0, 1),
            "in_tok": sum((r[3] or 0) + (r[4] or 0) + (r[5] or 0) for r in tok), "out_tok": sum(r[6] or 0 for r in tok),
            "hit_limit": (answer or "").startswith("(도구 호출 횟수 상한")})
        return answer or "(빈 응답)", mode


# ───────────── 대화 기록 정리 ─────────────
def _is_turn_start(m: dict) -> bool:
    return m["role"] == "user" and any(b.get("type") in ("text", "image") for b in m["content"]) \
        and not any(b.get("type") == "tool_result" for b in m["content"])


def compact_history(messages: list[dict], keep_turns: int) -> list[dict]:
    """최근 N턴만 남기고, 이미지는 텍스트 표시로 바꾸고, 어드바이저 블록은 뺀다.
    (어드바이저 블록을 남기면 다음 턴 모델이 달라질 때 호환 문제가 생길 수 있어 저장하지 않음)"""
    starts = [i for i, m in enumerate(messages) if _is_turn_start(m)]
    if len(starts) > keep_turns:
        messages = messages[starts[-keep_turns]:]
    out = []
    for m in messages:
        blocks = []
        for b in m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]:
            t = b.get("type")
            if t == "image":
                blocks.append({"type": "text", "text": "[이미지 첨부됨]"})
            elif t == "advisor_tool_result" or (t == "server_tool_use" and b.get("name") == "advisor"):
                continue
            else:
                blocks.append(b)
        if blocks:
            out.append({"role": m["role"], "content": blocks})
    # 연속된 같은 역할 메시지가 생기면 합침 (API는 user/assistant 교대만 허용)
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += m["content"]
        else:
            merged.append(m)
    return merged
