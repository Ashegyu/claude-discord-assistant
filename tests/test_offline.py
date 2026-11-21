"""오프라인 테스트: 실제 API 없이 가짜 Claude 클라이언트로 오케스트라 흐름 전체를 검증.

    python -m pytest -q tests/   (또는 python tests/test_offline.py)
"""
import asyncio
import email.message
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import yaml  # noqa: E402

from assistant import batch as batchmod  # noqa: E402
from assistant import mailwatch  # noqa: E402
from assistant.agents import WORKERS, Orchestra, compact_history  # noqa: E402
from assistant.store import Store  # noqa: E402
from assistant.tools import dev, google, life, mail, personal  # noqa: E402,F401
from assistant.tools.base import Context  # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
for _k in ("devlog_database_id", "memo_parent_page_id", "expense_database_id", "readlater_database_id", "books_database_id"):
    CFG["notion"][_k] = CFG["notion"].get(_k) or "test-" + _k   # 공개본처럼 ID가 비어 있어도 테스트가 돌게


class Block(NS):
    def model_dump(self, exclude_none=False):
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


def usage(**k):
    return Block(input_tokens=k.get("i", 10), output_tokens=5, cache_read_input_tokens=k.get("r", 0),
                 cache_creation_input_tokens=0, iterations=k.get("it"))


def resp(blocks, stop="end_turn", **u):
    return NS(content=[Block(**b) for b in blocks], stop_reason=stop, usage=usage(**u))


class FakeMessages:
    """script: 함수(kwargs) → 응답. 호출 기록을 남긴다."""
    def __init__(self, script):
        self.script, self.calls = script, []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.script(kw, len(self.calls))


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)
        self.beta = NS(messages=self.messages)


def ctx_for(store, confirm_answer=True, extras=None, channel_id="c1"):
    log = []

    async def confirm(d):
        log.append(("confirm", d))
        return confirm_answer

    async def notify(t):
        log.append(("notify", t))

    return Context(cfg=CFG, store=store, channel_id=channel_id, confirm=confirm, notify=notify, extras=extras or {}), log


# ───────────────────────── 테스트 ─────────────────────────
def test_orchestra_delegate_confirm_pause_and_caching():
    store = Store(Path(os.environ["DATA_DIR"]) / "t1.db")

    def script(kw, n):
        tools = [t.get("name") for t in kw.get("tools", [])]
        if tools == ["route"]:
            return resp([{"type": "tool_use", "id": "r", "name": "route", "input": {"mode": "deep"}}], "tool_use")
        if "delegate" in tools:  # 지휘자
            msgs = kw["messages"]
            if len(msgs) == 1:
                # 어드바이저 + 서버도구 일시중지 흉내
                return resp([{"type": "server_tool_use", "id": "a1", "name": "advisor", "input": {}},
                             {"type": "advisor_tool_result", "tool_use_id": "a1",
                              "content": {"type": "advisor_redacted_result", "encrypted_content": "xx"}}],
                            "pause_turn", it=[{"type": "message", "input_tokens": 5, "output_tokens": 1},
                                              {"type": "advisor_message", "model": "claude-opus-5-5", "input_tokens": 50, "output_tokens": 9}])
            if len(msgs) == 2:
                return resp([{"type": "tool_use", "id": "d1", "name": "delegate",
                              "input": {"agent": "dev", "task": "beast-hunter 이슈 만들어"}},
                             {"type": "tool_use", "id": "m1", "name": "memory_write", "input": {"op": "add", "text": "Kim은 부산에 산다"}}],
                            "tool_use")
            return resp([{"type": "text", "text": "완료했어요."}], r=100)
        if "github_create_issue" in tools:  # dev 워커
            if len(kw["messages"]) == 1:
                return resp([{"type": "tool_use", "id": "g1", "name": "github_create_issue",
                              "input": {"repo": "beast-hunter", "title": "버그"}}], "tool_use")
            return resp([{"type": "text", "text": "이슈 생성 취소됨 보고"}])
        raise AssertionError(f"예상 못한 호출 {tools}")

    client = FakeClient(script)
    orch = Orchestra(CFG, store, client)
    ctx, log = ctx_for(store, confirm_answer=False)
    answer, mode = asyncio.run(orch.handle(ctx, [{"type": "text", "text": "이슈 하나 만들어줘"}], "이슈 하나 만들어줘"))
    assert answer == "완료했어요." and mode == "deep"

    calls = client.messages.calls
    orch_calls = [c for c in calls if any(t.get("name") == "delegate" for t in c.get("tools", []))]
    first = orch_calls[0]
    # 지휘자는 deep 모델 + 어드바이저 베타 + 캐싱 구조
    assert first["model"] == CFG["models"]["deep"] and first["betas"] == ["advisor-tool-2026-03-01"]
    adv = next(t for t in first["tools"] if t.get("type") == "advisor_20260301")
    assert adv["model"] == CFG["models"]["advisor"] and adv["max_uses"] == CFG["advisor"]["deep_max_uses"] and adv["max_tokens"] == 2048
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}       # 도구 1h
    assert first["system"][0]["cache_control"]["ttl"] == "1h" and "cache_control" not in first["system"][1]  # 고정/변동 분리
    assert first["cache_control"] == {"type": "ephemeral"}                                   # 대화 자동 캐싱
    # pause_turn 뒤에 같은 요청을 이어감 (사용자 메시지 추가 없이)
    assert orch_calls[1]["messages"][-1]["role"] == "assistant"
    # 워커는 worker 모델, 어드바이저 없음
    worker = next(c for c in calls if any(t.get("name") == "github_create_issue" for t in c.get("tools", [])))
    assert worker["model"] == CFG["models"]["worker"] and "betas" not in worker
    # 확인 버튼이 떴고(취소), 진행 알림이 갔고, 기억이 저장됨
    assert any(k == "confirm" and "이슈 생성" in d for k, d in log)
    assert any(k == "notify" for k, _ in log)
    assert "부산" in personal.MEMORY_FILE.read_text(encoding="utf-8")
    # 저장된 기록에는 어드바이저 블록이 없음
    hist = store.get_history("c1")
    assert not any(b.get("type") in ("advisor_tool_result",) or b.get("name") == "advisor"
                   for m in hist for b in m["content"])
    # 사용량: 어드바이저 반복은 Opus로 따로 집계
    roles = {(r[0], r[1]) for r in store.usage_since(0)}
    assert ("claude-opus-5-5", "advisor") in roles and (CFG["models"]["worker"], "worker:dev") in roles


def test_compact_history_keeps_turns_and_strips_images():
    msgs = []
    for i in range(20):
        msgs += [{"role": "user", "content": [{"type": "image", "source": {}}, {"type": "text", "text": f"q{i}"}]},
                 {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]},
                 {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]}]
    out = compact_history(msgs, 3)
    assert out[0]["content"][1]["text"] == "q17" and len(out) == 12
    assert out[0]["content"][0] == {"type": "text", "text": "[이미지 첨부됨]"}
    roles = [m["role"] for m in out]
    assert all(a != b for a, b in zip(roles, roles[1:]))


def test_reminders_and_tool_errors():
    store = Store(Path(os.environ["DATA_DIR"]) / "t2.db")
    ctx, _ = ctx_for(store)
    r = asyncio.run(personal.reminder_add(ctx, when="2099-01-01T09:00", text="치과", repeat="weekly"))
    assert "등록" in r
    assert "치과" in asyncio.run(personal.reminder_list(ctx))[0]
    assert "과거" in asyncio.run(personal.reminder_add(ctx, when="2000-01-01T09:00", text="x"))
    # 등록부를 통한 실행: 잘못된 인자 → 오류 문자열 (예외로 죽지 않음)
    from assistant.tools.base import REGISTRY
    out = asyncio.run(REGISTRY["reminder_delete"].run(ctx, {"wrong": 1}))
    assert out.startswith("오류")


def test_ms_todo_reminders_sync():
    """MS To Do 연결 시: 등록(알림·반복) → 목록 번호 → 완료/삭제, 토큰 자동 갱신, 아침 브리핑 필터."""
    import time as _t
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    import httpx

    from assistant import todo

    tasks, seen = {}, []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path))
        if req.url.path.endswith("/token"):
            assert b"refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer new"
        if req.url.path == "/v1.0/me/todo/lists":
            return httpx.Response(200, json={"value": [{"id": "L0", "displayName": "작업", "wellknownListName": "defaultList"},
                                                       {"id": "L1", "displayName": "갤럭시"}]})
        assert "/lists/L0/tasks" in req.url.path
        tid = req.url.path.rsplit("/", 1)[-1]
        if req.method == "POST":
            body = json.loads(req.content)
            t = {"id": f"T{len(tasks) + 1}", "status": "notStarted", **body}
            tasks[t["id"]] = t
            return httpx.Response(201, json=t)
        if req.method == "GET":
            return httpx.Response(200, json={"value": list(tasks.values())})
        if req.method == "PATCH":
            tasks[tid] |= json.loads(req.content)
            return httpx.Response(200, json=tasks[tid])
        if req.method == "DELETE":
            tasks.pop(tid)
            return httpx.Response(204)
        return httpx.Response(404)

    todo.TRANSPORT = httpx.MockTransport(handler)
    os.environ["MS_CLIENT_ID"] = "cid"
    todo.token_path().write_text(json.dumps({"access_token": "old", "refresh_token": "r1", "expires_at": _t.time() - 10}))
    try:
        store = Store(Path(os.environ["DATA_DIR"]) / "todo.db")
        ctx, _ = ctx_for(store)
        r = asyncio.run(personal.reminder_add(ctx, when="2099-01-05T09:30", text="치과", repeat="weekdays"))
        assert "To Do" in r and "치과" in r
        t1 = tasks["T1"]
        assert t1["isReminderOn"] and t1["reminderDateTime"] == {"dateTime": "2099-01-05T00:30:00", "timeZone": "UTC"}
        assert t1["dueDateTime"]["dateTime"] == "2099-01-04T15:00:00"          # 현지 자정
        assert t1["recurrence"]["pattern"]["daysOfWeek"][-1] == "friday"
        assert json.loads(todo.token_path().read_text())["refresh_token"] == "r2"  # 회전된 토큰 저장
        asyncio.run(personal.reminder_add(ctx, when="", text="우유 사기"))            # 날짜 없는 할 일
        assert "reminderDateTime" not in tasks["T2"]
        lines = asyncio.run(personal.reminder_list(ctx))
        assert lines[0].startswith("1. 01/05") and "치과" in lines[0] and "[반복]" in lines[0]
        assert lines[1] == "2. 날짜 없음 우유 사기"
        assert "완료" in asyncio.run(personal.reminder_done(ctx, id="2")) and tasks["T2"]["status"] == "completed"
        assert len(asyncio.run(personal.reminder_list(ctx))) == 1                 # 완료된 건 목록에서 빠짐
        assert "없습니다" in asyncio.run(personal.reminder_done(ctx, id="9"))
        assert "삭제" in asyncio.run(personal.reminder_delete(ctx, id="1")) and "T1" not in tasks
        # 목록 id는 한 번만 조회해 저장
        assert seen.count(("GET", "/v1.0/me/todo/lists")) == 1
        # 아침 브리핑 필터: 오늘·밀린 것만
        tz = ZoneInfo("Asia/Seoul")
        now = _dt(2099, 1, 5, 7, 47, tzinfo=tz)
        mk = lambda s: {"isReminderOn": True, "reminderDateTime": {"dateTime": s, "timeZone": "UTC"}}
        assert todo.is_today_or_overdue(mk("2099-01-05T10:00:00"), tz, now)        # 오늘 19시
        assert todo.is_today_or_overdue(mk("2099-01-01T00:00:00"), tz, now)        # 밀림
        assert not todo.is_today_or_overdue(mk("2099-01-05T15:30:00"), tz, now)    # 내일 00:30
        assert not todo.is_today_or_overdue({"title": "x"}, tz, now)
    finally:
        todo.TRANSPORT = None
        os.environ.pop("MS_CLIENT_ID", None)
        todo.token_path().unlink(missing_ok=True)


def test_mail_parsing_and_triage():
    m = email.message.EmailMessage()
    m["From"] = "=?utf-8?b?7ZWc6rWt?= <boss@company.com>"
    m["Subject"] = "=?utf-8?b?6rKw7KCcIOyLpO2MqA==?="
    m["Date"] = "Thu, 19 Nov 2026 10:00:00 +0900"
    m.set_content("서버 결제가 실패했습니다. 오늘 안에 확인 부탁드립니다.")
    m.add_alternative("<html><style>x{}</style><b>HTML 본문</b></html>", subtype="html")
    d = mail.summary_of("42", m.as_bytes(), 500)
    assert d["from_addr"] == "boss@company.com" and "결제" in d["body"] and d["subject"]

    store = Store(Path(os.environ["DATA_DIR"]) / "t3.db")
    cfg = {**CFG, "mail": {**CFG["mail"], "vip_senders": ["boss@company.com"], "ignore_senders": ["ad@shop.com"]}}

    def script(kw, n):
        return resp([{"type": "tool_use", "id": "x", "name": "triage",
                      "input": {"mails": [{"uid": "42", "level": "normal", "summary": "결제 실패"}]}}], "tool_use")

    mails = [{**d, "body": d["body"]}, {"uid": "43", "from": "ad", "from_addr": "ad@shop.com", "subject": "세일", "snippet": ""}]
    out = asyncio.run(mailwatch.triage(FakeClient(script), store, cfg, mails))
    assert len(out) == 1 and out[0]["level"] == "important"   # VIP는 normal이어도 important로 올림


def test_batch_submit_and_poll():
    store = Store(Path(os.environ["DATA_DIR"]) / "t4.db")
    created = {}

    class Batches:
        async def create(self, requests):
            created["reqs"] = requests
            return NS(id="msgbatch_1")

        async def retrieve(self, bid):
            return NS(processing_status="ended")

        def results(self, bid):
            async def gen():
                for r in created["reqs"]:
                    yield NS(custom_id=r["custom_id"], result=NS(type="succeeded", message=NS(
                        model="claude-haiku-5-5", usage=usage(), content=[NS(type="text", text=f"결과 {r['custom_id']}")])))
            return gen()

    def script(kw, n):
        return resp([{"type": "text", "text": "종합 요약"}])

    client = FakeClient(script)
    client.messages.batches = Batches()
    ctx, _ = ctx_for(store, extras={"client": client})
    msg = asyncio.run(personal.bulk_job(ctx, title="커밋 요약", instruction="한 줄 요약", items=["a", "b", "c"]))
    assert "3건" in msg
    r0 = created["reqs"][0]
    assert r0["custom_id"] == "item-0000" and r0["params"]["system"][-1]["cache_control"]["ttl"] == "1h"

    sent = []

    async def send(cid, text, path):
        sent.append((cid, text, path))

    asyncio.run(batchmod.poll(client, store, CFG, send))
    assert sent and "배치 완료" in sent[0][1] and "종합 요약" in sent[0][1] and sent[0][2].exists()
    assert not store.open_batches()
    assert any(r[2] == 1 for r in store.usage_since(0))   # 배치 사용량 표시


def test_ics_calendar(monkeypatch=None):
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
DTSTART:20261120T010000Z
DTEND:20261120T020000Z
SUMMARY:Standup
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
END:VCALENDAR"""
    import httpx
    from datetime import datetime
    from zoneinfo import ZoneInfo

    orig = httpx.get
    httpx.get = lambda *a, **k: NS(content=ics)
    os.environ["GOOGLE_CALENDAR_ICS_URLS"] = "https://example.com/basic.ics"
    try:
        tz = ZoneInfo("Asia/Seoul")
        evs = google.list_events(datetime(2026, 11, 20, tzinfo=tz), datetime(2026, 11, 23, tzinfo=tz))
    finally:
        httpx.get = orig
        del os.environ["GOOGLE_CALENDAR_ICS_URLS"]
    assert [e["title"] for e in evs] == ["Standup"] * 3


def test_library_v2_page_search_cites_pages_and_visual_fallback():
    import io
    from pypdf import PdfWriter
    from assistant import library

    w = PdfWriter()
    for _ in range(40):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    pdf = buf.getvalue()

    # 쪽별 텍스트 (실제 표준 문서 흉내): 12쪽에 답, 13쪽에 표가 이어짐, 30쪽은 그림 위주
    fake_pages = [f"General text page {i}. presentation library overview." for i in range(1, 41)]
    fake_pages[11] = "10.3 Depth contours. DEPCNT02 is drawn when the depth contour value equals the SAFETY_CONTOUR. safety contour safety contour"
    fake_pages[12] = "Table 10-2 (continued) DEPCNT02 colour DEPSC"
    fake_pages[29] = "Fig 7"
    orig_extract = library.extract_pdf_pages
    library.extract_pdf_pages = lambda data: fake_pages

    store = Store(Path(os.environ["DATA_DIR"]) / "t5.db")
    library.init(store)
    uploads, deleted, calls = [], [], []

    class Files:
        async def upload(self, file, expires_in_seconds=None):
            uploads.append((file[0], expires_in_seconds))
            return NS(id=f"file_{len(uploads)}")

        async def delete(self, fid):
            deleted.append(fid)

    def script(kw, n):
        calls.append(kw)
        tool = (kw.get("tools") or [{}])[0].get("name")
        if tool == "describe":
            return resp([{"type": "tool_use", "id": "d", "name": "describe",
                          "input": {"summary": "S-52 표시 규칙", "keywords": "DEPCNT, safety contour"}}], "tool_use")
        if tool == "search_terms":
            return resp([{"type": "tool_use", "id": "t", "name": "search_terms",
                          "input": {"terms": ["safety contour", "DEPCNT02", "the"]}}], "tool_use")
        cit = NS(type="content_block_location", document_title="S-52", document_index=0, start_block_index=0,
                 end_block_index=1, cited_text="DEPCNT02 is drawn when the depth contour value equals the SAFETY_CONTOUR")
        return NS(content=[Block(type="text", text="안전 등심선 값과 같을 때 그립니다.", citations=[cit])],
                  stop_reason="end_turn", usage=usage())

    client = FakeClient(script)
    client.files = Files()
    try:
        d = asyncio.run(library.add_document(client, store, CFG, "S-52.pdf", pdf, "application/pdf", "S-52"))
    finally:
        library.extract_pdf_pages = orig_extract
    assert d["kind"] == "text" and d["pages"] == 40 and not uploads          # 업로드 시 Files API 사용 안 함
    summary_prompt = calls[0]["messages"][0]["content"]
    assert "[p.8]" in summary_prompt and "[p.9]" not in summary_prompt       # 요약은 앞 8쪽만

    answer = asyncio.run(library.ask(client, store, CFG, [d["id"]], "DEPCNT02는 언제 그려?"))
    final = calls[-1]["messages"][0]["content"]
    doc = final[0]
    pages_sent = [b["text"].split("]")[0] for b in doc["source"]["content"]]
    assert doc["source"]["type"] == "content" and doc["citations"] == {"enabled": True}
    assert pages_sent[:2] == ["[쪽 12", "[쪽 13"] and len(pages_sent) <= 9  # 관련 쪽 + 다음 쪽만
    assert "p.12" in answer and "[1]" in answer and "쪽 12, 13" in answer
    assert len(json.dumps(final, ensure_ascii=False)) < 20000                  # 문서 전체가 아니라 발췌만

    # 그림·표 질문 → 고른 쪽을 잘라 Files API(하루 만료)로 원본 PDF도 첨부, 같은 묶음은 재사용
    asyncio.run(library.ask(client, store, CFG, [d["id"]], "DEPCNT02 색상 표 보여줘"))
    vis = [b for b in calls[-1]["messages"][0]["content"] if b.get("source", {}).get("type") == "file"]
    assert vis and uploads[-1][1] == 86400
    n_up = len(uploads)
    asyncio.run(library.ask(client, store, CFG, [d["id"]], "DEPCNT02 색상 표 보여줘"))
    assert len(uploads) == n_up

    # 쪽 직접 지정
    def script2(kw, n):
        if (kw.get("tools") or [{}])[0].get("name") == "search_terms":
            return resp([{"type": "tool_use", "id": "t", "name": "search_terms", "input": {"terms": ["x"], "pages": [30]}}], "tool_use")
        return script(kw, n)
    client.messages.script = script2
    asyncio.run(library.ask(client, store, CFG, [d["id"]], "30쪽 그림 설명해줘"))
    sent = calls[-1]["messages"][0]["content"]
    assert sent[0]["source"]["content"][0]["text"].startswith("[쪽 30]")

    # 텍스트 파일: 구간으로 나눠 검색
    md = ("\n\n".join(f"## {i}절\n" + ("내용 " * 300) for i in range(5)) + "\n\n## 특별\nDEPCNT02 정의").encode()
    t = asyncio.run(library.add_document(client, store, CFG, "notes.md", md, "text/markdown"))
    assert t["mime"] == "text/plain" and len(library.page_texts(store, t["id"])) >= 2

    assert asyncio.run(library.delete_document(client, store, d["id"]))
    assert library.get_doc(store, d["id"]) is None and not library.page_texts(store, d["id"])
    assert set(deleted) >= {f"file_{i}" for i in range(1, n_up + 1)}         # 그림용 임시 파일도 정리


def test_library_v1_docs_still_answer():
    from assistant import library

    store = Store(Path(os.environ["DATA_DIR"]) / "t6.db")
    library.init(store)
    store._exec("INSERT INTO docs (title,filename,mime,size,pages,parts,summary,keywords,created,kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("old", "old.pdf", "application/pdf", 1, 150,
                 json.dumps([{"file_id": "f1", "pages": "1-100"}, {"file_id": "f2", "pages": "101-150"}]), "", "", 0, "files"))

    def script(kw, n):
        cit = NS(type="page_location", document_title="old", document_index=1, start_page_number=3, end_page_number=4, cited_text="x")
        return NS(content=[Block(type="text", text="답", citations=[cit])], stop_reason="end_turn", usage=usage())

    out = asyncio.run(library.ask(FakeClient(script), store, CFG, [1], "질문"))
    assert "p.103" in out   # 두 번째 부분의 3쪽 = 원본 103쪽

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("✔", name)


def test_docclean_finds_only_unused_files():
    import time as _t
    from assistant import library
    store = Store(Path(os.environ["DATA_DIR"]) / "clean.db")
    library.init(store)
    store._exec("INSERT INTO visual_cache VALUES (?,?,?)", ("1:3", "f_live", _t.time() + 3600))
    store._exec("INSERT INTO visual_cache VALUES (?,?,?)", ("1:4", "f_old", _t.time() - 10))
    files = [NS(id=i, filename=f"{i}.pdf", size_bytes=1000) for i in ("f_live", "f_old", "f_orphan")]
    deleted = []

    class Files:
        def list(self, limit=100):
            async def gen():
                for f in files:
                    yield f
            return gen()

        async def delete(self, fid):
            deleted.append(fid)

    client = NS(files=Files())
    orphans = asyncio.run(library.orphan_files(client, store))
    assert [f.id for f in orphans] == ["f_old", "f_orphan"]
    assert asyncio.run(library.delete_files(client, store, orphans)) == 2 and deleted == ["f_old", "f_orphan"]
    assert store._exec("SELECT key FROM visual_cache") == [("1:3",)]


def test_renewal_alerts_and_records():
    from datetime import date
    from assistant import renewals
    store = Store(Path(os.environ["DATA_DIR"]) / "renew.db")
    cfg = {"renewals": {"warn_days": [14, 7, 3, 1, 0], "items": [
        {"key": "k", "label": "카카오", "every_days": 55, "last": "2026-11-01", "how": "kakao_init.py"},
        {"key": "g", "label": "GH_PAT", "expires": ""}]}}
    # 미설정 항목은 한 번만 안내
    first = renewals.alerts(cfg, store, date(2026, 11, 20))
    assert len(first) == 1 and "!renewed g" in first[0]
    assert renewals.alerts(cfg, store, date(2026, 11, 21)) == []
    # 카카오: 11/01 + 55일 = 12/26 → D-14(12/12)에 알림, 같은 날 두 번은 안 보냄
    m = renewals.alerts(cfg, store, date(2026, 12, 12))
    assert len(m) == 1 and "14일 전" in m[0] and "kakao_init.py" in m[0]
    assert renewals.alerts(cfg, store, date(2026, 12, 12)) == []
    assert renewals.alerts(cfg, store, date(2026, 12, 13)) == []            # D-13은 알림 없음
    assert "지났어요" in renewals.alerts(cfg, store, date(2026, 12, 28))[0]  # 지난 뒤엔 매일
    # 재발급 기록 → 다음 날짜 갱신, 만료형은 날짜 필수
    assert "2027-02-21" in renewals.mark_renewed(cfg, store, "k", "", date(2026, 12, 28))
    assert "새 만료일" in renewals.mark_renewed(cfg, store, "g", "")
    assert "2027-11-01" in renewals.mark_renewed(cfg, store, "g", "2027-11-01")
    lines = renewals.status_lines(cfg, store, date(2027, 1, 1))
    assert "D-51" in lines[0] and "D-304" in lines[1]
    # 자동 감지된 GitHub 토큰도 목록에 포함
    store.put("renew_auto:github-assistant", "2027-01-08")
    assert any("7일 전" in x for x in renewals.alerts(cfg, store, date(2027, 1, 1)))


def test_screenshot_assistant_extract_and_actions(monkeypatch=None):
    from assistant import screenshot
    from assistant.tools import dev as devmod
    store = Store(Path(os.environ["DATA_DIR"]) / "snap.db")
    info_out = {"kind": "coupon", "title": "스타벅스 아메리카노", "summary": "기프티콘", "due": "2099-03-10", "code": "1234"}

    def script(kw, n):
        assert kw["messages"][0]["content"][0]["type"] == "image"
        return resp([{"type": "tool_use", "id": "t", "name": "extract", "input": info_out}], stop="tool_use")
    client = FakeClient(script)
    img = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]
    info = asyncio.run(screenshot.analyze(client, store, CFG, img))
    assert info["kind"] == "coupon"
    assert screenshot.actions_for(info)[0] == ("todo", "🎁 만료 알림")
    assert "1234" in screenshot.card(info)
    # To Do 미연결 → 로컬 리마인더로 만료 3일 전 09:00
    out = asyncio.run(screenshot.run_action("todo", CFG, store, info, "c1"))
    assert "03/07 09:00" in out and "만료 03/10" in out
    # 영수증 → Notion 지출 DB
    sent = {}

    async def fake_notion(method, path, body=None):
        sent.update(body)
        return {"url": "https://notion.so/x"}
    orig = devmod.notion
    devmod.notion = fake_notion
    try:
        r = {"kind": "receipt", "title": "점심", "store": "국밥집", "amount": 9000, "category": "식비", "date": "2026-11-19"}
        out = asyncio.run(screenshot.run_action("expense", CFG, store, r, "c1"))
    finally:
        devmod.notion = orig
    assert "9,000원" in out and sent["parent"]["database_id"] == CFG["notion"]["expense_database_id"]
    assert sent["properties"]["분류"]["select"]["name"] == "식비" and sent["properties"]["금액"]["number"] == 9000
    # 실패는 예외 대신 문장으로
    assert asyncio.run(screenshot.run_action("calendar", CFG, store, {"kind": "event", "title": "x"}, "c1")).startswith("날짜")


def test_self_improve_collect_and_validate():
    import time as _t
    from assistant import improve
    store = Store(Path(os.environ["DATA_DIR"]) / "imp.db")
    store.add_trace("c", {"mode": "quick", "tools": [{"tool": "weather", "err": "오류: x"}], "api_calls": 2})
    store.add_trace("c", {"mode": "deep", "tools": [{"tool": "calendar_create", "cancel": True}], "api_calls": 1})
    improve.note_user_message(store, "c", "아니 그게 아니라 내일이야")
    improve.note_user_message(store, "c", "고마워")
    stats, problems = improve.collect(store)
    assert stats["turns"] == 2 and stats["tool_errors"] == {"weather": 1} and stats["user_cancels"] == {"calendar_create": 1}
    assert stats["corrected_turns"] == 1 and len(problems) == 2
    files = {"assistant/agents.py": 'PROMPT = """안녕"""\nx = 1\n', "config.yaml": "a: 1\nmax_tokens: 2\n"}
    edits = [
        {"file": "assistant/agents.py", "old": "안녕", "new": "반가워", "reason": "r"},
        {"file": "config.yaml", "old": "max_tokens: 2", "new": "max_tokens: 3", "reason": "토큰 상한"},   # max_tokens는 허용
        {"file": ".env", "old": "a", "new": "b", "reason": "x"},
        {"file": "assistant/agents.py", "old": "x = 1", "new": "x = os.environ['ANTHROPIC_API_KEY']", "reason": "x"},
        {"file": "assistant/agents.py", "old": "없는 문자열", "new": "y", "reason": "x"},
        {"file": "config.yaml", "old": "a: 1", "new": "a: [", "reason": "깨진 yaml"},
    ]
    changed, applied, rejected = improve.validate_edits(edits, files)
    assert len(applied) == 2 and len(rejected) == 4
    assert "반가워" in changed["assistant/agents.py"] and "max_tokens: 3" in changed["config.yaml"]


def test_orchestra_records_trace():
    store = Store(Path(os.environ["DATA_DIR"]) / "trace.db")

    def script(kw, n):
        if kw.get("tool_choice", {}).get("name") == "route":
            return resp([{"type": "tool_use", "id": "r", "name": "route", "input": {"mode": "quick"}}], stop="tool_use")
        if n == 2:
            return resp([{"type": "tool_use", "id": "a", "name": "reminder_delete", "input": {"id": "D99"}}], stop="tool_use")
        return resp([{"type": "text", "text": "끝"}])
    orch = Orchestra(CFG, store, FakeClient(script))
    ctx, _ = ctx_for(store)
    asyncio.run(orch.handle(ctx, [{"type": "text", "text": "D99 지워"}], "D99 지워"))
    t = store.traces_since(0)[-1]
    assert t["mode"] == "quick" and t["tools"][0]["tool"] == "reminder_delete" and t["api_calls"] == 2


def test_playtest_trim_and_report():
    sys.path.insert(0, str(ROOT / "playtest"))
    import run as pt
    img = lambda: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "x"}}  # noqa: E731
    msgs = [{"role": "user", "content": [img()]}] + [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(i), "content": [{"type": "text", "text": "ok"}, img()]}]}
        for i in range(4)]
    pt.trim_images(msgs)
    kinds = [b["type"] for m in msgs for c in m["content"] for b in (c["content"] if c["type"] == "tool_result" else [c])]
    assert kinds.count("image") == pt.KEEP_IMAGES
    rep = pt.report("u", "g", [{"viewport": "desktop", "steps": 3, "load_sec": 1.2, "summary": "s", "console": ["e1", "e1"],
                                "notes": [{"kind": "good", "severity": "low", "text": "좋음"},
                                          {"kind": "bug", "severity": "high", "text": "멈춤"}]}], {"input_tokens": 10})
    assert rep.index("멈춤") < rep.index("좋음") and rep.count("e1") == 1


def test_costs_budget_warn_and_save_mode():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import costs
    store = Store(Path(os.environ["DATA_DIR"]) / "cost.db")
    cfg = {**CFG, "budget": {"monthly_usd": 1.0, "warn_ratio": 0.8, "save_mode": True}}
    # Sonnet 입력 30만 = $0.60 , Opus 출력 1만 = $0.20 → $0.80 (80%)
    store.record_usage("claude-sonnet-5-5", "orchestrator:deep", {"input_tokens": 300_000})
    store.record_usage("claude-opus-5-5", "advisor", {"output_tokens": 10_000})
    store.record_usage("claude-haiku-5-5", "batch", {"input_tokens": 1_000_000}, batch=True)   # $0.05
    total, by_role = costs.cost_since(store, cfg, 0)
    assert abs(total - 0.85) < 1e-9 and abs(by_role["batch (배치)"] - 0.05) < 1e-9
    now = _dt.now(ZoneInfo(cfg["timezone"]))
    m = costs.check(store, cfg, now)
    assert len(m) == 1 and "⚠️" in m[0] and not costs.save_mode(store, cfg)
    assert costs.check(store, cfg, now) == []                      # 경고는 달마다 한 번
    store.record_usage("claude-sonnet-5-5", "x", {"output_tokens": 20_000})   # +$0.20 → $1.05
    m = costs.check(store, cfg, now)
    assert "절약 모드" in m[0] and costs.save_mode(store, cfg)
    assert "껐어요" in costs.set_override(store, cfg, off=True) and not costs.save_mode(store, cfg)
    assert "$1.05" in costs.status(store, cfg)


def test_orchestra_save_mode_skips_router_and_advisor():
    from assistant import costs
    store = Store(Path(os.environ["DATA_DIR"]) / "save.db")
    costs.set_override(store, CFG, off=False)          # 절약 모드 켜기
    calls = []

    def script(kw, n):
        calls.append(kw)
        return resp([{"type": "text", "text": "ok"}])
    client = FakeClient(script)
    ctx, _ = ctx_for(store)
    reply, mode = asyncio.run(Orchestra(CFG, store, client).handle(ctx, [{"type": "text", "text": "!deep 분석"}], "!deep 분석"))
    assert mode == "quick" and len(calls) == 1
    assert calls[0]["model"] == CFG["models"]["quick"] and not any(t.get("type", "").startswith("advisor") for t in calls[0]["tools"])


def test_expense_summary_and_report():
    from datetime import date
    from assistant import expenses
    cur = expenses.summarize([{"title": "점심", "amount": 9000, "category": "식비", "store": "", "date": "2026-11-17"},
                              {"title": "노트북 가방", "amount": 50000, "category": "쇼핑", "store": "", "date": "2026-11-18"},
                              {"title": "커피", "amount": 30000, "category": "카페", "store": "", "date": "2026-11-18"}])
    prev = expenses.summarize([{"title": "x", "amount": 10000, "category": "카페", "store": "", "date": "2026-11-10"}])
    text = expenses.report_text("지난주", cur, prev)
    assert "89,000원" in text and "+79,000원" in text and "⬆️ 이전 10,000원" in text and "🆕" in text
    assert text.index("쇼핑") < text.index("카페") < text.index("식비")
    assert expenses.week_range(date(2026, 11, 19)) == (date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 2))
    assert expenses.month_range(date(2026, 1, 1)) == (date(2025, 12, 1), date(2026, 1, 1), date(2025, 11, 1))
    assert "없어요" in expenses.report_text("x", expenses.summarize([]), expenses.summarize([]))


def test_coupon_wallet():
    from datetime import date
    from assistant import coupons
    from assistant.tools.base import REGISTRY
    store = Store(Path(os.environ["DATA_DIR"]) / "coupon.db")
    c1, new = coupons.add(store, {"title": "스타벅스 아메리카노", "store": "스타벅스", "code": "1234-5678", "due": "2099-01-10"}, b"img", "png")
    assert new and Path(store._exec("SELECT image FROM coupons WHERE id=?", (c1,))[0][0]).read_bytes() == b"img"
    assert coupons.add(store, {"title": "중복", "code": "12345678"}, None) == (c1, False)       # 같은 번호는 중복 저장 안 함
    c2, _ = coupons.add(store, {"title": "배스킨 파인트", "due": "2099-01-03"}, None)
    lst = coupons.lines(store, date(2099, 1, 1))
    assert lst[0].startswith(f"`#{c2}`") and "D-2" in lst[0] and "D-9" in lst[1]
    sent = []

    async def send_file(path, cap):
        sent.append((path, cap))
    ctx, _ = ctx_for(store, extras={"send_file": send_file})
    assert "보냈어요" in asyncio.run(REGISTRY["coupon_show"].run(ctx, {"query": "스타벅스"})) and "1234-5678" in sent[0][1]
    assert "사용 처리" in asyncio.run(REGISTRY["coupon_used"].run(ctx, {"query": str(c1)}))
    assert len(coupons.active(store, date(2099, 1, 1))) == 1


def test_readlater_detect_extract_and_save():
    from assistant import readlater
    from assistant.tools import dev as devmod
    assert readlater.is_link_only("https://a.com/x") and readlater.is_link_only(" https://a.com\nhttps://b.com ")
    assert not readlater.is_link_only("이거 봐 https://a.com")
    html = ("<html><head><title>T</title><meta property='og:title' content='제목'></head><body><nav>메뉴</nav>"
            "<article><h1>큰 제목</h1><p>첫 문단 <b>강조</b> 내용.</p><script>x()</script><p>둘째 문단</p></article>"
            "<footer>저작권</footer></body></html>")
    page = readlater.extract(html)
    assert page["title"] == "제목" and "첫 문단 강조 내용." in page["text"] and "메뉴" not in page["text"]
    assert "저작권" not in page["text"] and "x()" not in page["text"]
    assert readlater.YT.search("https://youtu.be/dQw4w9WgXcQ?t=3").group(1) == "dQw4w9WgXcQ"

    store = Store(Path(os.environ["DATA_DIR"]) / "read.db")
    created = []

    async def fake_notion(method, path, body=None):
        if path.endswith("/query"):
            return {"results": []}
        created.append(body)
        return {"url": "u"}

    async def fake_read(url):
        return {"title": "기사", "description": "", "text": "본문 " * 300, "kind_hint": "글", "note": ""}

    def script(kw, n):
        return resp([{"type": "tool_use", "id": "t", "name": "save", "input": {
            "title": "기사 제목", "summary": "- 요점 하나", "tags": ["AI", "없는태그"], "kind": "글", "minutes": 4}}], stop="tool_use")
    orig_n, orig_r = devmod.notion, readlater.read
    devmod.notion, readlater.read = fake_notion, fake_read
    try:
        out = asyncio.run(readlater.save(FakeClient(script), store, CFG, "https://a.com/x"))
    finally:
        devmod.notion, readlater.read = orig_n, orig_r
    assert "기사 제목" in out and "약 4분" in out
    props = created[0]["properties"]
    assert props["URL"]["url"] == "https://a.com/x" and props["태그"]["multi_select"] == [{"name": "AI"}]


def test_mail_triage_task_gets_buttons():
    store = Store(Path(os.environ["DATA_DIR"]) / "mailtask.db")
    from assistant.tools import mail as mailmod
    mails = [{"uid": "5", "from": "관리사무소 <a@b.c>", "from_addr": "a@b.c", "subject": "11월 관리비", "body": "30일까지 납부"}]

    def script(kw, n):
        return resp([{"type": "tool_use", "id": "t", "name": "triage", "input": {"mails": [
            {"uid": "5", "level": "important", "summary": "관리비 고지", "task": {"kind": "todo", "title": "관리비 납부", "due": "2099-11-30"}}]}}],
            stop="tool_use")
    sent = []

    async def send(text, info=None, level="normal"):
        sent.append((text, info, level))

    class G:
        enabled = True

        def new_since(self, last):
            return mails, 5
    orig = mailwatch.GMAIL
    mailwatch.GMAIL = G()
    try:
        n = asyncio.run(mailwatch.check(FakeClient(script), store, CFG, send))
    finally:
        mailwatch.GMAIL = orig
    assert n == 1 and sent[0][1]["kind"] == "todo" and sent[0][1]["due"] == "2099-11-30" and "관리비 납부" in sent[0][0]
    assert sent[0][1]["_mail"]["uid"] == "5" and sent[0][2] == "normal"
    from assistant import screenshot
    assert screenshot.actions_for(sent[0][1])[0][0] == "todo"
    _ = mailmod


def test_backup_zip_and_restore(tmp_path=None):
    import tempfile as _tf
    import zipfile
    from assistant import backup
    root = Path(_tf.mkdtemp())
    data = root / "data"
    (data / "coupons").mkdir(parents=True)
    st = Store(data / "assistant.db")
    st.put("hello", "world")
    (data / "memory.md").write_text("- 기억")
    (data / "coupons" / "1.png").write_bytes(b"png")
    for secret in ("google_token.json", "ms_token.json", "kakao.json"):
        (data / secret).write_text("SECRET")
    (data / "library").mkdir()
    (data / "library" / "1.pdf").write_bytes(b"%PDF")
    z, names = backup.build_zip(root / "b.zip", data)
    inside = zipfile.ZipFile(z).namelist()
    assert set(names) == {"assistant.db", "memory.md", "coupons/1.png"} and "BACKUP_INFO.txt" in inside
    assert not any("token" in n or "kakao" in n or n.endswith(".pdf") for n in inside)
    # 복구: 기존 파일은 restore-backup-*/ 로 옮겨지고 백업 내용으로 바뀜
    (data / "memory.md").write_text("- 바뀐 기억")
    restored = backup.restore(z, data)
    assert (data / "memory.md").read_text() == "- 기억" and "coupons/1.png" in restored
    kept = list(data.glob("restore-backup-*/memory.md"))
    assert kept and kept[0].read_text() == "- 바뀐 기억"
    assert Store(data / "assistant.db").get("hello") == "world"


def test_web_search_counted_and_daily_cap():
    from assistant import costs
    store = Store(Path(os.environ["DATA_DIR"]) / "search.db")
    cfg = {**CFG, "budget": {**CFG["budget"], "daily_web_searches": 6, "monthly_usd": 100}}
    # 일반 응답과 어드바이저(iterations) 응답 모두에서 검색 횟수 기록
    store.record_usage("claude-haiku-5-5", "worker:research", {"input_tokens": 0, "server_tool_use": {"web_search_requests": 4}})
    orch = Orchestra(cfg, store, FakeClient(lambda kw, n: None))
    orch._record("claude-sonnet-5-5", "orchestrator:deep",
                 Block(iterations=[{"type": "message", "input_tokens": 0}], server_tool_use={"web_search_requests": 1}))
    assert store.searches_since(0) == 5 and costs.searches_left_today(store, cfg) == 1
    total, by_role = costs.cost_since(store, cfg, 0)
    assert abs(by_role["웹 검색"] - 0.05) < 1e-9 and "오늘 5회 / 6" in costs.status(store, cfg)

    seen = []

    def script(kw, n):
        seen.append(kw)
        return resp([{"type": "text", "text": "보고"}])
    orch = Orchestra(cfg, store, FakeClient(script))
    ctx, log = ctx_for(store)
    asyncio.run(orch.delegate(ctx, "research", "S-100 소식"))
    ws = [t for t in seen[-1]["tools"] if t.get("name") == "web_search"]
    assert ws and ws[0]["max_uses"] == 1                      # 남은 1회만 허용
    store.record_searches("worker:research", 1)
    asyncio.run(orch.delegate(ctx, "research", "S-100 소식"))
    assert not any(t.get("name") == "web_search" for t in seen[-1]["tools"])   # 한도 도달 → 검색 도구 제외
    assert any(t.get("name") == "web_fetch" for t in seen[-1]["tools"])
    assert "한도" in seen[-1]["messages"][0]["content"] and any("한도" in str(x) for x in log)


def test_commute_plan_and_alert_timing():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import commute
    tz = ZoneInfo("Asia/Seoul")
    store = Store(Path(os.environ["DATA_DIR"]) / "commute.db")
    home, work, dentist = {"name": "우리집", "x": 129.16, "y": 35.16}, {"name": "회사", "x": 129.06, "y": 35.10}, \
        {"name": "서면 치과", "x": 129.06, "y": 35.157}
    store.put("place:집", home)
    store.put("place:회사", work)

    async def geo(q):
        return dentist if "치과" in q else None

    async def tr(o, d):
        return {"minutes": 30, "transfers": 1, "fare": 1600, "legs": ["🚌1003(해운대)", "🚇1호선(서면)"]}

    async def cr(o, d):
        return {"minutes": 20, "km": 9.1, "toll": 0}

    async def rain(d, w):
        return 70
    orig = (commute.geocode, commute.transit, commute.car, commute.rain_chance)
    commute.geocode, commute.transit, commute.car, commute.rain_chance = geo, tr, cr, rain
    try:
        ev = {"id": "e1", "title": "치과", "start": "2026-11-21T14:00:00+09:00", "location": "서면 치과"}
        assert not commute.is_candidate({**ev, "location": "https://zoom.us/j/1"})
        assert not commute.is_candidate({**ev, "start": "2026-11-21"})
        sat = _dt(2026, 11, 21, 12, 0, tzinfo=tz)                       # 토요일 → 집에서
        assert commute.default_origin_name(CFG, sat) == "집"
        assert commute.default_origin_name(CFG, _dt(2026, 11, 20, 10, 0, tzinfo=tz)) == "회사"
        # 12:00엔 아직 (대중교통 30+여유10 → 13:20 출발, 15분 전 13:05 알림)
        assert asyncio.run(commute.due_alerts(CFG, store, [ev], sat)) == []
        out = asyncio.run(commute.due_alerts(CFG, store, [ev], _dt(2026, 11, 21, 13, 6, tzinfo=tz)))
        assert len(out) == 1 and "13:20" in out[0]["text"] and "13:30 출발" in out[0]["text"] and "☔" in out[0]["text"]
        assert "출발지: 집" in out[0]["text"] and "1,600원" in out[0]["text"]
        assert asyncio.run(commute.due_alerts(CFG, store, [ev], _dt(2026, 11, 21, 13, 10, tzinfo=tz))) == []   # 한 번만
        # 출발지 버튼: 회사에서 다시 계산
        p = asyncio.run(commute.plan(CFG, store, ev, sat, prefer="회사"))
        assert p["origin"]["label"] == "회사"
        # 오늘만 다른 출발지
        assert "회사" in asyncio.run(commute.set_origin_today(store, sat, "회사"))
        assert asyncio.run(commute.origin_for(CFG, store, sat))["label"] == "회사"
        # 출발지 미등록이면 한 번만 안내
        store2 = Store(Path(os.environ["DATA_DIR"]) / "commute2.db")
        o1 = asyncio.run(commute.due_alerts(CFG, store2, [ev], sat))
        assert "!place" in o1[0]["text"] and asyncio.run(commute.due_alerts(CFG, store2, [{**ev, "id": "e2"}], sat)) == []
    finally:
        commute.geocode, commute.transit, commute.car, commute.rain_chance = orig


def test_weekly_review_numbers_and_comment():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import weekly
    from assistant.tools import dev as devmod
    store = Store(Path(os.environ["DATA_DIR"]) / "weekly.db")
    data = {"period": "11/16~11/22", "events_this_week": ["11-17 10:00 회의"], "events_next_week": ["11-24 치과", "11-25 x"],
            "tasks_done": ["a", "b", "c"], "tasks_due_next_week": ["11/26 관리비"],
            "spending_won": {"total": 89000, "count": 3, "by_cat": {"식비": 89000}, "prev_total": 50000, "top": []},
            "reading": {"saved": 4, "read": 1}, "commits": {"football-academy": 12}, "coupons_expiring": [],
            "assistant_cost_usd": 1.234}
    nums = weekly.numbers(data)
    assert nums[0] == "📅 일정 1개 · 다음 주 2개" and "끝낸 할 일 3개" in nums[1] and "+39,000원" in nums[2]
    assert "football-academy 12" in nums[4] and nums[-1] == "🤖 비서 비용 $1.23"
    # 설정 안 된 항목(None)은 줄 자체가 빠짐
    assert len(weekly.numbers({**data, "tasks_done": None, "reading": None, "commits": {}})) == len(nums) - 3

    async def fake_collect(cfg, st, now):
        return data
    memo = []

    async def fake_memo(ctx, title, body):
        memo.append((title, body))
        return "메모를 만들었습니다: https://notion.so/w"
    orig = (weekly.collect, devmod.notion_write_memo)
    weekly.collect, devmod.notion_write_memo = fake_collect, fake_memo
    try:
        out = asyncio.run(weekly.run(FakeClient(lambda kw, n: resp([{"type": "text", "text": "- 한 주 요약"}])), store, CFG,
                                     _dt(2026, 11, 22, 21, 7, tzinfo=ZoneInfo("Asia/Seoul"))))
    finally:
        weekly.collect, devmod.notion_write_memo = orig
    assert "나의 한 주" in out and "- 한 주 요약" in out and "https://notion.so/w" in out and memo[0][0] == "주간 회고 11/16~11/22"


def test_heartbeat_payload():
    from assistant import heartbeat
    info = heartbeat.memory_info()
    assert "rss=" in info and "avail=" in info
    os.environ.pop("HEALTHCHECK_URL", None)
    assert asyncio.run(heartbeat.ping()) is False          # 설정 없으면 아무것도 안 함


def test_alerts_quiet_queue_and_digest():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import alerts
    tz = ZoneInfo("Asia/Seoul")
    cfg = {**CFG, "alerts": {"quiet": "23:00-07:00", "focus": ["09:30-11:30"]}}
    night, focus, day = _dt(2026, 11, 20, 2, 0, tzinfo=tz), _dt(2026, 11, 20, 10, 0, tzinfo=tz), _dt(2026, 11, 20, 14, 0, tzinfo=tz)
    assert alerts.in_range("23:00-07:00", night) and not alerts.in_range("23:00-07:00", day)
    assert alerts.decide(cfg, "urgent", night) == "send" and alerts.decide(cfg, "normal", night) == "queue"
    assert alerts.decide(cfg, "normal", focus) == "queue" and alerts.decide(cfg, "normal", day) == "send"
    assert alerts.decide(cfg, "low", day) == "queue"
    store = Store(Path(os.environ["DATA_DIR"]) / "alerts.db")
    alerts.enqueue(store, "🔑 토큰 점검 실패\n- GitHub: 만료", "normal")
    alerts.enqueue(store, "📧 관리비", "normal", {"_mail": {"uid": "1"}})
    alerts.enqueue(store, "💾 백업 완료", "low")
    assert alerts.pending_count(store) == 3
    text, separate = alerts.digest(alerts.drain(store), tz)
    assert "(2개)" in text and "토큰 점검 실패 - GitHub: 만료" in text and len(separate) == 1
    assert alerts.pending_count(store) == 0 and alerts.drain(store) == []


def test_cycles():
    from datetime import date
    from assistant import cycles
    from assistant.tools.base import REGISTRY
    store = Store(Path(os.environ["DATA_DIR"]) / "cycles.db")
    assert "다음 02/14" in cycles.add(store, "정수기필터", 90, date(2026, 11, 16))
    cycles.add(store, "칫솔", 60, date(2026, 9, 20))
    today = date(2026, 11, 19)
    lst = cycles.lines(store, today)
    assert lst[0].startswith("**칫솔**") and "오늘" in lst[0] and "D-87" in lst[1]
    assert [c["name"] for c in cycles.upcoming(store, today)] == ["칫솔"]
    a = cycles.due_alerts(store, today)
    assert len(a) == 1 and "!done 칫솔" in a[0] and cycles.due_alerts(store, today) == []   # 한 번만
    assert "다음 2027-01-18" in cycles.done(store, "칫 솔", today)                        # 띄어쓰기 무시
    ctx, _ = ctx_for(store)
    assert "D-" in str(asyncio.run(REGISTRY["cycle_list"].run(ctx, {})))
    assert "찾지 못했어요" in cycles.delete(store, "없는거")


def test_briefing_build():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import briefing, cycles
    store = Store(Path(os.environ["DATA_DIR"]) / "brief.db")
    cycles.add(store, "보일러 점검", 1, _dt(2026, 11, 18).date())
    assert briefing.grade(25, 10).startswith("좋음") and briefing.grade(90, 20).startswith("나쁨") and briefing.grade(None, 40).startswith("나쁨")

    async def w(cfg):
        return "🌤️ 부산 맑음 8~15°C"

    async def sch(cfg, st, now):
        return ["- 14:00 치과 📍서면"]
    orig = (briefing.weather_line, briefing.schedule_lines)
    briefing.weather_line, briefing.schedule_lines = w, sch
    try:
        text = asyncio.run(briefing.build(CFG, store, _dt(2026, 11, 19, 7, 47, tzinfo=ZoneInfo("Asia/Seoul")), "📥 밤사이 알림 (1개)"))
    finally:
        briefing.weather_line, briefing.schedule_lines = orig
    assert "아침 브리핑" in text and "부산 맑음" in text and "14:00 치과" in text and "보일러 점검 오늘" in text
    assert text.rstrip().endswith("📥 밤사이 알림 (1개)")


def test_recall_archive_and_tool():
    from assistant import recall
    from assistant.tools.base import REGISTRY
    store = Store(Path(os.environ["DATA_DIR"]) / "recall.db")
    recall.archive(store, "c", "user", "어제 서면 할매국밥 진짜 맛있더라")
    recall.archive(store, "c", "assistant", "다음엔 범내골 돼지국밥집도 가보세요")
    recall.archive(store, "c", "user", "치과 예약 바꿔야 해")
    hits = recall.search_archive(store, ["국밥", "돼지국밥"], 0)
    assert len(hits) == 2 and hits[0]["who"] == "비서" or hits[1]["who"] == "비서"
    assert recall.search_archive(store, ["x"], 0) == []           # 2글자 미만 무시

    orig = (recall._events, recall._expenses, recall._notion)
    recall._events = lambda k, a, b: [{"date": "2026-10-02T15:00", "title": "치과", "location": "서면"}]

    async def ex(cfg, k, s):
        return ["2026-10-05 점심 할매국밥 9,000원"]

    async def no(k):
        raise RuntimeError("notion off")
    recall._expenses, recall._notion = ex, no
    try:
        ctx, _ = ctx_for(store)
        out = asyncio.run(REGISTRY["recall"].handler(ctx, keywords=["국밥", "할매국밥"]))
    finally:
        recall._events, recall._expenses, recall._notion = orig
    assert "할매국밥" in out["대화"][0] or "할매국밥" in out["대화"][1]
    assert out["지출"] and out["지난 일정"][0]["title"] == "치과" and str(out["Notion"]).startswith("(못 찾음")


def test_mail_draft():
    from assistant import maildraft
    store = Store(Path(os.environ["DATA_DIR"]) / "draft.db")

    class G:
        def read(self, uid):
            assert uid == "77"
            return {"from": "김부장 <boss@corp.kr>", "from_addr": "boss@corp.kr", "subject": "회의 일정", "body": "다음 주 가능하신가요?"}
    seen = []

    def script(kw, n):
        seen.append(kw["messages"][0]["content"])
        return resp([{"type": "text", "text": "안녕하세요, [요일] 가능합니다."}])
    orig = maildraft.GMAIL
    maildraft.GMAIL = G()
    try:
        d = asyncio.run(maildraft.draft(FakeClient(script), store, CFG, "77", "화요일 가능"))
    finally:
        maildraft.GMAIL = orig
    assert d["to"] == "boss@corp.kr" and "화요일 가능" in seen[0] and "[요일]" in d["body"]
    assert d["url"].startswith("https://mail.google.com/mail/?view=cm") and "Re%3A" in d["url"] and len(d["url"]) <= 512
    assert "```" in maildraft.render(d)


def test_reminder_add_many_confirm_and_register():
    from assistant.tools.base import REGISTRY
    store = Store(Path(os.environ["DATA_DIR"]) / "many.db")
    t = REGISTRY["reminder_add_many"]
    assert t.confirm
    args = {"prefix": "FA 0.2", "items": [{"text": "빌드 점검", "when": "2099-01-05T09:00"},
                                          {"text": "릴리스 노트", "when": "2099-01-06T09:00"},
                                          {"text": "과거 일", "when": "2000-01-01T09:00"}]}
    summary = t.summarize(args)
    assert "3개" in summary and "[FA 0.2] 빌드 점검" in summary
    ctx, _ = ctx_for(store)
    out = asyncio.run(t.handler(ctx, **args))
    assert out.startswith("2개 등록") and "실패 1개" in out
    assert [r[2] for r in store.list_reminders()] == ["[FA 0.2] 빌드 점검", "[FA 0.2] 릴리스 노트"]


def test_project_thread_context_in_system():
    from assistant import projects
    from assistant.tools import dev as devmod
    store = Store(Path(os.environ["DATA_DIR"]) / "proj.db")
    cfg = {**CFG, "github": {"repos": ["me/football-academy", "me/beast-hunter"]}}
    p = projects.bind(store, "t1", cfg, "football-academy")
    assert p == {"name": "football-academy", "repo": "me/football-academy"}

    async def fake_gh(method, path, **kw):
        assert path == "/repos/me/football-academy/commits"
        return [{"commit": {"author": {"date": "2026-11-18T10:00:00Z"}, "message": "스태미나 계산 수정\n\n본문"}}]
    orig = devmod.gh
    devmod.gh = fake_gh
    seen = []

    def script(kw, n):
        if kw.get("tool_choice", {}).get("name") == "route":
            return resp([{"type": "tool_use", "id": "r", "name": "route", "input": {"mode": "quick"}}], stop="tool_use")
        seen.append(kw["system"])
        return resp([{"type": "text", "text": "확인했어요"}])
    try:
        ctx, _ = ctx_for(store, channel_id="t1")
        asyncio.run(Orchestra(cfg, store, FakeClient(script)).handle(ctx, [{"type": "text", "text": "최근 뭐 했지"}], "최근 뭐 했지"))
    finally:
        devmod.gh = orig
    dyn = seen[0][1]["text"]
    assert "football-academy" in dyn and "스태미나 계산 수정" in dyn and "cache_control" not in seen[0][1]
    from assistant import recall
    assert [h["who"] for h in recall.search_archive(store, ["최근", "확인했어요"], 0)] == ["비서", "나"]
    projects.unbind(store, "t1")
    assert not projects.get(store, "t1")


PCFG = {**CFG, "pages": {"base_url": "https://example.github.io/site", "repo": "o/r", "branch": "gh-pages"}}


def test_pages_card_urls_and_dashboard_encryption():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import pages
    from assistant.tools import dev as devmod
    os.environ["KAKAO_JS_KEY"] = "jskey"
    dest = {"name": "서면 치과", "x": 129.0591234567, "y": 35.1571234567}
    url = pages.go_url(PCFG, dest, "치과")
    d = pages.decode_card(url)
    assert url.startswith("https://example.github.io/site/card.html#d=") and len(url) <= 512
    assert d == {"k": "jskey", "t": "go", "title": "치과", "place": {"n": "서면 치과", "x": 129.059123, "y": 35.157123}}
    assert pages.kakaomap_to(dest).startswith("https://map.kakao.com/link/to/%EC%84%9C%EB%A9%B4")
    assert "+" not in url.split("#")[1] and "/" not in url.split("#d=")[1]
    # 암호화 왕복 + 올리기 (내용이 같으면 다시 올리지 않음)
    key = pages._b64(os.urandom(32))
    assert pages.decrypt(key, pages.encrypt(key, {"a": "가"})) == {"a": "가"}
    store = Store(Path(os.environ["DATA_DIR"]) / "pages.db")
    puts = []

    async def fake_gh(method, path, **kw):
        if method == "GET":
            raise RuntimeError("GitHub 404: Not Found")
        puts.append(kw["json"])
        return {}

    async def fake_data(cfg, st, now):
        return {"at": f"{now:%H:%M}", "events": ["14:00 치과"], "tasks": []}
    orig = (devmod.gh, pages.dash_data)
    devmod.gh, pages.dash_data = fake_gh, fake_data
    try:
        now = _dt(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        u1 = asyncio.run(pages.publish_dash(PCFG, store, now))
        u2 = asyncio.run(pages.publish_dash(PCFG, store, now.replace(hour=10)))     # 시각만 다름 → 안 올림
    finally:
        devmod.gh, pages.dash_data = orig
    assert u1 == u2 and "/dash.html#k=" in u1 and len(puts) == 1 and puts[0]["branch"] == "gh-pages"
    enc = json.loads(base64.b64decode(puts[0]["content"]))
    assert pages.decrypt(u1.split("#k=")[1], enc)["events"] == ["14:00 치과"] and "치과" not in puts[0]["content"]


def test_meet_midpoint_pick_and_tool():
    from assistant import commute, meet
    from assistant.tools.base import REGISTRY
    assert meet.pick({"A역": [30, 34], "B역": [20, 45], "C역": [30, None], "D역": [28, 34]}) == [("A역", 34, 4), ("D역", 34, 6), ("B역", 45, 25)]
    pts = {"해운대": {"name": "해운대역", "x": 129.16, "y": 35.16}, "사상": {"name": "사상역", "x": 128.98, "y": 35.16}}

    async def geo(q):
        return pts.get(q)

    async def stations(x, y, radius=5000, size=6):
        assert abs(x - 129.07) < 0.01
        return [{"name": "서면역", "x": 129.059, "y": 35.157}, {"name": "부전역", "x": 129.06, "y": 35.164}]

    async def tr(o, c):
        return {"minutes": {"해운대역": {"서면역": 30, "부전역": 33}, "사상역": {"서면역": 22, "부전역": 31}}[o["name"]][c["name"]]}

    async def shops(what, p, size=5):
        return [{"name": "○○고깃집", "x": 129.06, "y": 35.158, "category": "육류,고기"}]
    orig = (commute.geocode, meet.stations_near, commute.transit, meet.places_near)
    commute.geocode, meet.stations_near, commute.transit, meet.places_near = geo, stations, tr, shops
    try:
        ctx, _ = ctx_for(Store(Path(os.environ["DATA_DIR"]) / "meet.db"))
        ctx.cfg = PCFG
        out = asyncio.run(REGISTRY["meet_midpoint"].handler(ctx, origins=["해운대", "사상"], what="고깃집", title="송년회"))
        bad = asyncio.run(REGISTRY["meet_midpoint"].handler(ctx, origins=["해운대", "없는곳"]))
    finally:
        commute.geocode, meet.stations_near, commute.transit, meet.places_near = orig
    from assistant import pages
    assert out["중간 지점"] == "서면역" and out["사람별 대중교통(분)"] == {"해운대역": 30, "사상역": 22}
    card = pages.decode_card(out["공유 카드"])
    assert card["title"] == "송년회" and card["place"]["n"] == "서면역" and card["pts"][0]["n"] == "○○고깃집"
    assert "없는곳" in bad


def test_talkcal_convert_and_sync():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import talkcal
    from assistant.tools import life
    ev = {"id": "g1", "title": "치과", "start": "2026-11-21T14:02:00+09:00", "end": "2026-11-21T14:48:00+09:00", "location": "서면\n2층"}
    k = talkcal.to_kakao(ev, "Asia/Seoul")
    assert k["time"] == {"start_at": "2026-11-21T05:00:00Z", "end_at": "2026-11-21T05:50:00Z", "time_zone": "Asia/Seoul", "all_day": False}
    assert k["location"] == {"name": "서면"}
    allday = talkcal.to_kakao({"title": "휴가" * 40, "start": "2026-11-23", "end": "2026-11-24"}, "Asia/Seoul")
    assert allday["time"]["start_at"] == "2026-11-23T00:00:00Z" and allday["time"]["end_at"] == "2026-11-24T00:00:00Z"
    assert allday["time"]["all_day"] and len(allday["title"]) == 50

    store, calls = Store(Path(os.environ["DATA_DIR"]) / "talkcal.db"), []

    async def tok():
        return "t"

    async def create(token, event):
        calls.append(("create", event["title"]))
        return f"k{len(calls)}"

    async def delete(token, eid):
        calls.append(("delete", eid))
    orig = (life._kakao_access, talkcal.create, talkcal.delete)
    life._kakao_access, talkcal.create, talkcal.delete = tok, create, delete
    try:
        now = _dt(2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        ev2 = {"id": "g2", "title": "회식", "start": "2026-11-22T19:00:00+09:00", "end": "2026-11-22T21:00:00+09:00"}
        assert asyncio.run(talkcal.sync(CFG, store, [ev, ev2], now)) == {"created": 2, "deleted": 0}
        assert asyncio.run(talkcal.sync(CFG, store, [ev, ev2], now)) == {"created": 0, "deleted": 0}      # 그대로면 아무것도 안 함
        moved = {**ev, "start": "2026-11-21T16:00:00+09:00", "end": "2026-11-21T17:00:00+09:00"}
        assert asyncio.run(talkcal.sync(CFG, store, [moved], now)) == {"created": 1, "deleted": 2}        # 바뀐 것 재생성, 사라진 것 삭제
        later = _dt(2026, 11, 25, 9, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        assert asyncio.run(talkcal.sync(CFG, store, [], later)) == {"created": 0, "deleted": 0}           # 지난 일정은 톡캘린더에 남김
    finally:
        life._kakao_access, talkcal.create, talkcal.delete = orig
    assert calls[:2] == [("create", "치과"), ("create", "회식")] and store.get("talkcal_map") == {}


def test_busan_parse_warnings_and_verdict():
    from assistant import busan
    xml = ("<response><header><resultCode>00</resultCode></header><body><items>"
           "<item><bstopid>505780000</bstopid><lineno>1003</lineno><min1>4</min1><station1>2</station1><min2>15</min2></item>"
           "<item><lineno>31</lineno></item><item><lineno>100</lineno><min1>1</min1></item></items></body></response>")
    os.environ["DATA_GO_KR_KEY"] = "k"

    async def fake_get(url, **params):
        assert url.endswith("/stopArrByBstopid") and params == {"bstopid": "505780000"}
        return xml
    orig = busan._get
    busan._get = fake_get
    try:
        arr = asyncio.run(busan.arrivals("505780000"))
        store = Store(Path(os.environ["DATA_DIR"]) / "busan.db")
        store.put("bus_stops", [{"id": "505780000", "name": "서면역", "ars": "05713"}])
        lines = asyncio.run(busan.bus_lines(store))
    finally:
        busan._get = orig
        os.environ.pop("DATA_GO_KR_KEY", None)
    assert [a["line"] for a in arr] == ["100", "1003"] and arr[1]["next"] == 15
    assert lines == ["🚏 서면역: **100** 1분 · **1003** 4분(15분)"]
    try:
        busan.parse_items("<r><cmmMsgHeader><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg></cmmMsgHeader></r>")
        assert False
    except RuntimeError as e:
        assert "SERVICE_KEY" in str(e)
    text = "o 강풍주의보 : 부산, 울산, 경상남도(통영)\r\no 풍랑주의보 : 동해남부앞바다\r\no 건조주의보 : 부산"
    assert busan.region_warnings([text], "부산") == ["강풍주의보", "건조주의보"] and busan.region_warnings(["o 없음"], "부산") == []
    sky = {"rain_now": 0, "rain3h": 20, "gust": 20}
    assert "괜찮" in busan.verdict(sky, []) and "특보" in busan.verdict(sky, ["강풍주의보"])
    assert "우산" in busan.verdict({**sky, "rain3h": 70}, []) and "비가 와요" in busan.verdict({**sky, "rain_now": 1.2}, [])
    # 특보 변화 알림: 처음엔 기준만 잡고, 새로 생기면 알림, 풀리면 해제 알림
    store = Store(Path(os.environ["DATA_DIR"]) / "busan2.db")
    seq = iter([[], ["강풍주의보"], ["강풍주의보"], []])

    async def w(region="부산"):
        return next(seq)
    orig = busan.warnings
    busan.warnings = w
    try:
        res = [asyncio.run(busan.new_warnings(store)) for _ in range(4)]
    finally:
        busan.warnings = orig
    assert res[0] is None and "강풍주의보** 발효" in res[1] and res[2] is None and "해제" in res[3]


def test_commute_alert_carries_dest_for_link_buttons():
    import inspect
    from assistant import commute
    assert '"dest": p["dest"]' in inspect.getsource(commute.due_alerts)


def test_usage_guide_finds_right_sections_and_prompt_requires_it():
    from assistant import agents, guide
    from assistant.tools.base import REGISTRY
    secs = guide.sections()
    assert len(secs) >= 15 and all(s["keywords"] and s["body"] for s in secs)
    cases = {"기프티콘 어떻게 써?": "기프티콘 지갑", "밤에 알림 안 오게 하려면": "알림 시간표", "!dash 뭐야": "대시보드",
             "출발 알림 출발지 바꾸는 법": "출발 알림", "웹 검색 한도 있어?": "비용과 한도", "영수증 사진 보내면 어떻게 돼": "스크린샷 비서",
             "정수기 필터 교체 알림": "생활 주기", "답장 초안 써주는 기능": "메일", "버스 정류소 추가": "생활 (날씨·장소·부산 실시간)",
             "다른 채널에서 답이 없어": "기본: 대화하는 곳과 방법", "중간 지점 찾기": "약속 카드와 중간 지점"}
    for q, want in cases.items():
        hits = guide.find(q, 2, secs)
        assert hits and want in [h["title"] for h in hits], (q, [h["title"] for h in hits])
    assert guide.find("양자역학 설명해줘", 2, secs) == []
    assert "`!coupons`" in guide.help_text("쿠폰") and "주제:" in guide.help_text("zzzz")
    ctx, _ = ctx_for(Store(Path(os.environ["DATA_DIR"]) / "guide.db"))
    out = asyncio.run(REGISTRY["usage_guide"].handler(ctx, question="나중에 읽기 어떻게 써"))
    assert "링크만" in out
    assert "usage_guide" in agents.ORCHESTRATOR_PROMPT and REGISTRY["usage_guide"].group == "core"
    # 문서에 적힌 !명령은 모두 봇에 실제로 있어야 한다 (문서와 코드가 어긋나지 않게)
    import re
    bot_src = (ROOT / "assistant" / "bot.py").read_text(encoding="utf-8")
    for cmd in set(re.findall(r"`(![a-z]+)", (ROOT / "docs" / "usage.md").read_text(encoding="utf-8"))):
        assert f'"{cmd}' in bot_src or cmd == "!deep", cmd


def test_card_links_and_kakao_card_message():
    from assistant import pages
    from assistant.tools import life
    os.environ["KAKAO_JS_KEY"] = "jskey"
    url = pages.card_url(PCFG, {"t": "meet", "title": "송년회", "when": "11/21(토) 19:00", "place": {"n": "서면역", "x": 1, "y": 2}})
    reply = f"약속 카드를 만들었어요.\n{url}\n같은 링크 다시: {url} 그리고 다른 링크 https://example.com/x"
    assert pages.card_links(PCFG, reply) == [url] and pages.card_links(PCFG, "링크 없음") == []
    assert pages.card_summary(url) == "📍 송년회\n🕒 11/21(토) 19:00\n장소: 서면역"
    sent = {}

    async def tok():
        return "t"

    class FakeHTTP:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, u, headers=None, data=None):
            sent.update(url=u, tmpl=json.loads(data["template_object"]))
            return NS(status_code=200, text="")
    orig = (life._kakao_access, life.httpx.AsyncClient)
    life._kakao_access, life.httpx.AsyncClient = tok, FakeHTTP
    try:
        asyncio.run(life.kakao_send_card(pages.card_summary(url), url))
    finally:
        life._kakao_access, life.httpx.AsyncClient = orig
    assert sent["url"].endswith("/talk/memo/default/send") and sent["tmpl"]["link"]["mobile_web_url"] == url
    assert sent["tmpl"]["button_title"] == "카드 열기" and "송년회" in sent["tmpl"]["text"]


def test_daum_search_clean_and_book_log():
    from assistant import daum
    from assistant.tools import dev as devmod
    from assistant.tools.base import REGISTRY
    assert daum.clean("<b>서면</b> 국밥 &amp; 수육&#39;") == "서면 국밥 & 수육'"
    calls = []

    async def fake_get(path, **params):
        calls.append((path, params))
        if path.endswith("/book"):
            return {"documents": [{"title": "해상교통공학", "authors": ["김○○", "이○○"], "publisher": "해사출판", "isbn": "8912345678 9788912345678",
                                   "datetime": "2021-03-02T00:00:00.000+09:00", "thumbnail": "https://t/img", "url": "https://b/1", "contents": "…"}]}
        return {"documents": [{"title": "<b>서면</b> 국밥 후기", "contents": "국물이 <b>진하고</b> 좋았다", "blogname": "먹블로그",
                               "datetime": "2026-10-01T10:00:00.000+09:00", "url": "https://blog/1"}]}
    notion = []

    async def fake_notion(method, path, body=None):
        notion.append((method, path, body))
        if path.endswith("/query"):
            return {"results": [{"id": "pg1"}] if len([n for n in notion if n[1].endswith("/query")]) > 1 else []}
        return {"url": "https://notion/p"}
    orig = (daum._get, devmod.notion)
    daum._get, devmod.notion = fake_get, fake_notion
    try:
        ctx, _ = ctx_for(Store(Path(os.environ["DATA_DIR"]) / "daum.db"))
        ctx.cfg = {**CFG, "notion": {**CFG["notion"], "books_database_id": "bdb"}}
        res = asyncio.run(REGISTRY["daum_search"].handler(ctx, query="서면 국밥 후기", recent=True))
        first = asyncio.run(REGISTRY["book_log"].handler(ctx, title="해상교통공학", status="읽는 중"))
        second = asyncio.run(REGISTRY["book_log"].handler(ctx, title="해상교통공학", status="완독", note="좋았음"))
    finally:
        daum._get, devmod.notion = orig
    assert res == [{"title": "서면 국밥 후기", "text": "국물이 진하고 좋았다", "source": "먹블로그", "date": "2026-10-01", "url": "https://blog/1"}]
    assert calls[0] == ("/v2/search/blog", {"query": "서면 국밥 후기", "sort": "recency", "size": 8})
    created = [n for n in notion if n[0] == "POST" and n[1] == "/pages"][0][2]
    assert created["properties"]["ISBN"]["rich_text"][0]["text"]["content"] == "9788912345678" and created["cover"]["external"]["url"] == "https://t/img"
    assert "김○○, 이○○" in first and "완독" in second
    patch = [n for n in notion if n[0] == "PATCH"][0]
    assert patch[1] == "/pages/pg1" and patch[2]["properties"]["상태"]["select"]["name"] == "완독" and "제목" not in patch[2]["properties"]
    assert REGISTRY["daum_search"].group == "search" and "search" in WORKERS["life"]["groups"] and "search" in WORKERS["research"]["groups"]


def test_talkcal_read_merges_kakao_events():
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    from assistant import talkcal
    from assistant.tools import google, life
    tz = ZoneInfo("Asia/Seoul")
    e = talkcal.from_kakao({"id": "k9", "title": "동창 모임", "time": {"start_at": "2026-11-21T10:00:00Z", "end_at": "2026-11-21T12:00:00Z", "all_day": False},
                            "location": {"name": "서면 ○○"}}, tz)
    assert e == {"id": "kakao:k9", "title": "동창 모임", "start": "2026-11-21T19:00:00+09:00", "end": "2026-11-21T21:00:00+09:00",
                 "location": "서면 ○○", "description": "", "source": "톡캘린더"}
    assert talkcal.from_kakao({"id": "a", "time": {"start_at": "2026-11-23T00:00:00Z", "end_at": "2026-11-24T00:00:00Z", "all_day": True}}, tz)["start"] == "2026-11-23"

    store = Store(Path(os.environ["DATA_DIR"]) / "talkread.db")
    store.put("talkcal_map", {"g1": {"eid": "mine1", "sig": "x", "end": "2026-11-21"}})
    seen = []

    async def tok():
        return "t"

    async def call(method, path, token, **kw):
        seen.append((path, kw.get("params")))
        if path == "/events":
            return {"events": [{"id": "mine1", "time": {"start_at": "2026-11-21T05:00:00Z", "end_at": "2026-11-21T06:00:00Z"}},
                               {"id": "k9", "type": "USER", "time": {"start_at": "2026-11-21T10:00:00Z", "end_at": "2026-11-21T12:00:00Z"}},
                               {"id": "hol", "type": "PUBLIC", "title": "공휴일", "time": {"start_at": "2026-11-21T00:00:00Z", "end_at": "2026-11-22T00:00:00Z", "all_day": True}}]}
        return {"event": {"id": "k9", "title": "동창 모임", "location": {"name": "서면 ○○"}}}
    orig = (life._kakao_access, talkcal._call, google.list_events)
    life._kakao_access, talkcal._call = tok, call
    google.list_events = lambda a, b: [{"id": "g1", "title": "치과", "start": "2026-11-21T14:00:00+09:00", "end": "2026-11-21T15:00:00+09:00", "location": ""}]
    talkcal._detail_cache.clear()
    try:
        now = _dt(2026, 11, 21, 0, 0, tzinfo=tz)
        cfg_on = {**CFG, "talkcal": {"enabled": True, "read": True}}
        merged = asyncio.run(talkcal.all_events(cfg_on, store, now, now.replace(hour=23)))
        again = asyncio.run(talkcal.all_events(cfg_on, store, now, now.replace(hour=23)))
        off = asyncio.run(talkcal.all_events({**CFG, "talkcal": {"enabled": False}}, store, now, now.replace(hour=23)))
    finally:
        life._kakao_access, talkcal._call, google.list_events = orig
    assert [x["title"] for x in merged] == ["치과", "동창 모임"] and merged[1]["location"] == "서면 ○○"     # 내가 넣은 것·공휴일 제외
    assert seen[0][1]["from"] == "2026-11-20T15:00:00Z" and [p for p, _ in seen].count("/event") == 1 and again == merged   # 상세는 한 번만
    assert [x["title"] for x in off] == ["치과"]


def test_gextra_birthdays_youtube_sheet():
    from datetime import date, datetime as _dt, timezone as _tz
    from assistant import gextra
    from assistant.tools import google
    items = [{"name": "민수", "month": 11, "day": 20, "year": 1990, "kind": "생일"}, {"name": "지영", "month": 2, "day": 29, "year": None, "kind": "생일"},
             {"name": "부모님", "month": 12, "day": 5, "year": 1985, "kind": "기념일"}]
    up = gextra.upcoming(items, date(2026, 11, 20), 30)
    assert [(b["name"], b["left"]) for b in up] == [("민수", 0), ("부모님", 15)] and up[0]["age"] == " (만 36세)" and "41번째" in up[1]["age"]
    assert gextra.next_date(items[1], date(2027, 1, 1)) == date(2027, 2, 28)            # 평년의 2/29
    store = Store(Path(os.environ["DATA_DIR"]) / "gextra.db")
    store.put("birthdays", {"day": "2026-11-20", "items": items})
    assert asyncio.run(gextra.today_alert(store, date(2026, 11, 20))) == "🎂 오늘은 **민수** 생일이에요 (만 36세)"
    store.put("birthdays", {"day": "2026-11-21", "items": items})
    assert asyncio.run(gextra.today_alert(store, date(2026, 11, 21))) == ""
    assert gextra.duration_seconds("PT1H2M3S") == 3723 and gextra.duration_seconds("PT45S") == 45 and gextra._len(3723) == "1:02:03"

    # 유튜브: 구독 → 업로드 목록 → 길이 확인, 쇼츠·예정 방송·오래된 것 제외
    class Y:
        def subscriptions(self):
            return self

        def playlistItems(self):
            return self

        def videos(self):
            return self

        def list(self, **kw):
            self.kw = kw
            return self

        def execute(self):
            kw = self.kw
            if kw.get("mine"):
                return {"items": [{"snippet": {"title": "코딩채널", "resourceId": {"channelId": "UCabc"}}}]}
            if "playlistId" in kw:
                assert kw["playlistId"] == "UUabc"
                mk = lambda vid, t, pub: {"contentDetails": {"videoId": vid, "videoPublishedAt": pub}, "snippet": {"title": t, "description": "설명"}}  # noqa: E731
                return {"items": [mk("v1", "긴 영상", "2026-11-19T08:00:00Z"), mk("v2", "짧은 #Shorts", "2026-11-19T09:00:00Z"),
                                  mk("v3", "옛날 영상", "2026-11-10T09:00:00Z"), mk("v4", "예정 방송", "2026-11-19T09:30:00Z")]}
            return {"items": [{"id": "v1", "contentDetails": {"duration": "PT12M5S"}, "snippet": {}},
                              {"id": "v2", "contentDetails": {"duration": "PT40S"}, "snippet": {}},
                              {"id": "v4", "contentDetails": {"duration": "PT0S"}, "snippet": {"liveBroadcastContent": "upcoming"}}]}
    orig = (google._service, gextra._need)
    google._service, gextra._need = (lambda n, v, p=None: Y()), (lambda s, w: None)
    try:
        now = _dt(2026, 11, 19, 10, 0, tzinfo=_tz.utc)
        client = FakeClient(lambda kw, n: resp([{"type": "tool_use", "id": "t", "name": "lines", "input": {"items": [{"id": "v1", "line": "파이썬 비동기 설명"}]}}], stop="tool_use"))
        text = asyncio.run(gextra.youtube_digest(client, store, CFG, now))
        again = asyncio.run(gextra.youtube_digest(client, store, CFG, now))
    finally:
        google._service, gextra._need = orig
    assert "새 영상 1개" in text and "**코딩채널** · 긴 영상 (12:05)" in text and "파이썬 비동기 설명" in text and "<https://youtu.be/v1>" in text
    assert again == "" and store.get("yt_last") == now.timestamp()

    detail, summary = gextra.sheet_values([{"date": "2026-11-18", "title": "점심", "store": "국밥집", "category": "식비", "amount": 9000, "payment": "신한"},
                                           {"date": "2026-10-02", "title": "버스", "store": "", "category": "교통", "amount": 1600}])
    assert detail[0][-1] == "월" and detail[1] == ["2026-11-18", "점심", "국밥집", "식비", 9000, "신한", "2026-11"]
    assert summary[0] == ["월", *gextra.CATS, "합계"] and summary[1][0] == "'2026-11" and summary[2][0] == "'2026-10"
    assert summary[1][1] == "=SUMIFS('내역'!$E:$E,'내역'!$D:$D,B$1,'내역'!$G:$G,$A2)" and summary[2][-1] == "=SUM(B3:I3)"


def test_settings_local_override_and_set():
    import tempfile as _tf
    from assistant import settings
    root = Path(_tf.mkdtemp())
    (root / "config.yaml").write_text("talkcal:\n  enabled: false\n  days: 14\nbriefing:\n  time: '07:47'\nalerts:\n  digest: ['12:30']\n", encoding="utf-8")
    base = settings.load(root)
    local = root / "config.local.yaml"
    assert base["talkcal"] == {"enabled": False, "days": 14}
    assert "true 로 저장" not in settings.set_local("talkcal.enabeld", "true", base, local) and not local.exists()      # 오타 거절
    assert "형식이 달라요" in settings.set_local("talkcal.days", "많이", base, local)
    assert "묶음" in settings.set_local("talkcal", "true", base, local)
    assert "False → True" in settings.set_local("talkcal.enabled", "true", base, local)
    assert "'07:47' → '07:30'" in settings.set_local("briefing.time", '"07:30"', base, local)
    settings.set_local("alerts.digest", '["13:00", "19:00"]', base, local)
    merged = settings.load(root)
    assert merged["talkcal"] == {"enabled": True, "days": 14} and merged["briefing"]["time"] == "07:30" and merged["alerts"]["digest"] == ["13:00", "19:00"]
    assert "talkcal" in settings.show_local(local)
    assert "되돌렸어요" in settings.unset_local("talkcal.enabled", local) and settings.load(root)["talkcal"]["enabled"] is False
    assert "없어요" in settings.unset_local("talkcal.enabled", local)
    assert "config.local.yaml" in (ROOT / ".gitignore").read_text()


def test_expense_add_by_text_and_undo():
    from assistant import expenses
    from assistant.tools import dev as devmod
    from assistant.tools.base import REGISTRY
    assert expenses.parse_quick("9000 점심 식비") == {"amount": 9000.0, "title": "점심", "category": "식비"}
    assert expenses.parse_quick("택시 1.2만원") == {"amount": 12000.0, "title": "택시", "category": "기타"}
    assert expenses.parse_quick("커피 4,500원 카페")["amount"] == 4500.0 and expenses.parse_quick("3천 카페")["title"] == "카페"
    assert expenses.parse_quick("점심") is None
    calls = []

    async def fake_notion(method, path, body=None):
        calls.append((method, path, body))
        return {"id": "pg9", "results": []}
    orig = devmod.notion
    devmod.notion = fake_notion
    try:
        store = Store(Path(os.environ["DATA_DIR"]) / "expadd.db")
        ctx, _ = ctx_for(store)
        out = asyncio.run(REGISTRY["expense_add"].handler(ctx, title="택시", amount=12000, category="교통", date="2026-11-18", payment="신한카드"))
        bad = asyncio.run(REGISTRY["expense_add"].handler(ctx, title="뭔가", amount=0, category="기타"))
        undo = asyncio.run(REGISTRY["expense_undo"].handler(ctx))
        undo2 = asyncio.run(REGISTRY["expense_undo"].handler(ctx))
    finally:
        devmod.notion = orig
    assert out == "🧾 기록: 11/18 택시 **12,000원** (교통)" and "금액" in bad
    props = calls[0][2]["properties"]
    assert props["금액"]["number"] == 12000 and props["분류"]["select"]["name"] == "교통" and props["날짜"]["date"]["start"] == "2026-11-18"
    assert props["결제수단"]["rich_text"][0]["text"]["content"] == "신한카드"
    assert calls[-1] == ("PATCH", "/pages/pg9", {"archived": True}) and "택시 12,000원" in undo and "없어요" in undo2
    assert not REGISTRY["expense_add"].confirm


def test_paysms_parse_record_dedupe_cancel():
    from assistant import paysms
    from assistant.tools import dev as devmod
    P = paysms.parse
    a = P("[Web발신]\n신한카드(1234)승인 김*균 5,600원(일시불)11/20 14:30 스타벅스해운대점 누적1,234,567원")
    assert a == {"kind": "approve", "amount": 5600, "shop": "스타벅스해운대점", "card": "신한", "last4": "1234"}
    b = P("[Web발신]\nKB국민카드1234승인\n김*균님\n12,000원 일시불\n11/20 14:30\n카카오T택시\n누적1,234,567원")
    assert (b["amount"], b["shop"], b["card"]) == (12000, "카카오T택시", "KB국민")
    c = P("[Web발신]\n삼성1234취소 김*균\n45,000원 일시불\n11/20 15:30 (주)이마트 해운대\n누적1,189,567원")
    assert (c["kind"], c["amount"], c["shop"], c["card"]) == ("cancel", 45000, "이마트 해운대", "삼성")
    d = P("[Web발신]\n신한카드(1234)승인 김*균 1,200,000원(일시불)11/20 14:30 삼성전자판매 누적1,234,567원")
    assert (d["card"], d["shop"]) == ("신한", "삼성전자판매")           # 가맹점 이름 속 '삼성'을 카드사로 보지 않는다
    assert P("[Web발신] 신한카드 11/25 결제예정금액 1,234,567원") is None and P("(광고) 삼성카드 이벤트 10,000원 할인") is None
    assert P("인증번호 123456") is None and P("안녕하세요") is None and P("!restart") is None

    cfg = {**CFG, "paysms": {**CFG["paysms"], "enabled": True, "channel_id": 77, "webhook_id": 99}}
    assert paysms.is_source(cfg, 77, 99) and not paysms.is_source(cfg, 77, None) and not paysms.is_source(cfg, 78, 99)
    assert not paysms.is_source(cfg, 77, 98) and not paysms.is_source(CFG, 0, 0) and not paysms.is_source(CFG, 77, 99)

    calls, n = [], [0]

    async def fake_notion(method, path, body=None):
        calls.append((method, path, body))
        n[0] += 1
        return {"id": f"pg{n[0]}", "results": []}

    def script(kw, i):
        assert "tools" in kw and [t["name"] for t in kw["tools"]] == ["classify_payment"]   # 다른 도구는 주지 않는다
        return resp([{"type": "tool_use", "id": "t", "name": "classify_payment", "input": {"shop": "스타벅스 해운대점", "category": "카페"}}])
    client = FakeClient(script)
    orig = devmod.notion
    devmod.notion = fake_notion
    try:
        store = Store(Path(os.environ["DATA_DIR"]) / "paysms.db")
        sms = "[Web발신]\n신한카드(1234)승인 김*균 5,600원(일시불)11/20 14:30 스타벅스해운대점 누적1,234,567원"
        r1 = asyncio.run(paysms.handle(cfg, store, client, sms, now=1000.0))
        rep = asyncio.run(paysms.handle(cfg, store, client, sms, now=1050.0))                # 똑같은 글이 또 옴 → 조용히 넘김
        r2 = asyncio.run(paysms.handle(cfg, store, client, sms.replace("14:30", "14:31"), now=1100.0))   # 10분 안 같은 금액 → 묻기
        r3 = asyncio.run(paysms.handle(cfg, store, client, sms, now=1000.0 + 3600))          # 한 시간 뒤 → 새 결제
        ig = asyncio.run(paysms.handle(cfg, store, client, "[Web발신] 신한카드 결제예정금액 99,000원", now=5000.0))
        cancel = asyncio.run(paysms.handle(cfg, store, client, "[Web발신]\n신한카드(1234)승인취소 김*균 5,600원(일시불)11/20 16:00 스타벅스해운대점", now=6000.0))
        miss = asyncio.run(paysms.handle(cfg, store, client, "[Web발신]\n삼성1234취소 김*균\n77,000원\n11/20 16:00 어딘가", now=6100.0))
        asyncio.run(paysms.set_category(store, "pg1", "스타벅스 해운대점", "식비"))
    finally:
        devmod.notion = orig
    assert r1["action"] == "recorded" and r1["id"] == "pg1" and r1["category"] == "카페" and "5,600원" in r1["text"] and r1["text"].startswith("💳")
    props = calls[0][2]["properties"]
    assert props["금액"]["number"] == 5600 and props["분류"]["select"]["name"] == "카페" and props["결제수단"]["rich_text"][0]["text"]["content"] == "신한 1234"
    assert rep["action"] == "ignored" and rep.get("repeat")
    assert r2["action"] == "duplicate" and r2["p"]["amount"] == 5600 and r3["action"] == "recorded" and r3["id"] == "pg2"
    assert len(client.messages.calls) == 1                                    # 같은 가맹점은 분류를 기억해 모델을 다시 부르지 않는다
    assert ig["action"] == "ignored" and miss["action"] == "cancel_miss"
    assert cancel["action"] == "cancelled" and ("PATCH", "/pages/pg2", {"archived": True}) in calls   # 가장 최근 기록을 지움
    assert [r["id"] for r in store.get(paysms.RECENT_KEY)] == ["pg1"]
    assert store.get("paycat:스타벅스해운대점")["category"] == "식비" and store.get(paysms.RECENT_KEY)[0]["category"] == "식비"
    assert "5,600원" in paysms.preview(sms) and "보지 않았어요" in paysms.preview("안녕")
    assert "꺼져" in paysms.status(CFG, store, __import__("datetime").date(2026, 11, 20))


def test_fixed_remote():
    from assistant.settings import fixed_remote
    R = "me/assistant-private"
    assert fixed_remote("https://github.com/me/assistant.git", R) == "https://github.com/me/assistant-private.git"
    assert fixed_remote("https://tok@github.com/me/assistant", R) == "https://tok@github.com/me/assistant-private"
    assert fixed_remote("git@github.com:me/assistant.git", R) == "git@github.com:me/assistant-private.git"
    assert fixed_remote("https://github.com/me/assistant-private.git", R) is None   # 이미 맞음
    assert fixed_remote("https://github.com/someone/assistant.git", R) is None            # 다른 주인(포크)은 건드리지 않음
    assert fixed_remote("", R) is None and fixed_remote("https://github.com/a/b.git", "") is None
