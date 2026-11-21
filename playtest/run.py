"""AI 플레이테스터 — GitHub Actions에서 실행 (VM은 메모리가 작아 브라우저를 띄우지 않음).

헤드리스 크롬(Playwright)으로 게임을 열고, Claude가 화면을 보며 클릭·키 입력으로 플레이한다.
콘솔 오류·프레임·로딩 시간을 같이 기록하고, 발견 사항을 스크린샷과 함께 디스코드 웹후크로 보낸다.

환경변수: ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL(선택), PT_URL, PT_GOAL, PT_STEPS, PT_VIEWPORTS, PT_MODEL
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import anthropic
import httpx

OUT = Path(os.environ.get("PT_OUT", "playtest-out"))
MODEL = os.environ.get("PT_MODEL") or "claude-sonnet-5-5"
KEEP_IMAGES = 3
VIEWPORTS = {
    "desktop": {"viewport": {"width": 1280, "height": 720}},
    "mobile": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True, "device_scale_factor": 1},
}

FPS_SCRIPT = """
window.__pt = {frames: [], errors: []};
(function loop(t){ const f = window.__pt.frames; f.push(t); while (f.length && t - f[0] > 2000) f.shift();
  requestAnimationFrame(loop); })(performance.now());
"""

TOOLS = [
    {"name": "click", "description": "화면 좌표를 클릭한다 (모바일은 탭). 좌표는 스크린샷 픽셀 기준.",
     "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}},
    {"name": "key", "description": "키를 누른다. hold_ms만큼 누르고 있을 수 있다. 예: ArrowLeft, ArrowUp, Space, Enter, KeyW, Escape",
     "input_schema": {"type": "object", "properties": {"key": {"type": "string"}, "hold_ms": {"type": "integer", "default": 0}},
                      "required": ["key"]}},
    {"name": "drag", "description": "끌기 (스와이프·조이스틱).",
     "input_schema": {"type": "object", "properties": {"x1": {"type": "integer"}, "y1": {"type": "integer"},
                                                       "x2": {"type": "integer"}, "y2": {"type": "integer"},
                                                       "ms": {"type": "integer", "default": 300}},
                      "required": ["x1", "y1", "x2", "y2"]}},
    {"name": "type", "description": "글자를 입력한다.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "wait", "description": "기다린다 (애니메이션·로딩).",
     "input_schema": {"type": "object", "properties": {"ms": {"type": "integer"}}, "required": ["ms"]}},
    {"name": "note", "description": "발견 사항을 기록한다. 지금 화면이 증거로 저장된다.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["bug", "ux", "balance", "perf", "good"]},
         "severity": {"type": "string", "enum": ["high", "mid", "low"]},
         "text": {"type": "string"}}, "required": ["kind", "severity", "text"]}},
    {"name": "finish", "description": "플레이를 끝내고 전체 소감을 제출한다.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
]

SYSTEM = """너는 꼼꼼한 게임 QA 플레이테스터다. 브라우저 게임을 실제로 플레이하며 문제를 찾는다.
- 매 턴 스크린샷과 상태(콘솔 오류, FPS)를 받는다. 화면을 보고 다음 행동을 도구로 한다 (한 번에 1~3개).
- 목표를 향해 진행하되, 이상한 점(버그·겹친 UI·읽기 힘든 글자·막히는 흐름·너무 쉽거나 어려운 구간·느려짐)은 즉시 note로 남긴다.
- 같은 행동을 3번 해도 화면이 안 바뀌면 다른 방법을 시도하고, 그래도 막히면 note(bug 또는 ux) 후 다른 부분을 테스트한다.
- 콘솔 오류가 새로 나오면 무엇을 하던 중이었는지와 함께 note(bug)로 남긴다.
- 실시간 전투는 반응이 늦을 수 있으니 key의 hold_ms로 길게 누르거나 wait로 상황을 보며 진행한다.
- 잘 된 점도 1~2개 note(good)로 남긴다. 할 일이 끝나면 finish. 한국어로 쓴다."""


class Runner:
    def __init__(self, page, name: str, mobile: bool):
        self.page, self.name, self.mobile = page, name, mobile
        self.console: list[str] = []
        self.notes: list[dict] = []
        self.shots = 0
        page.on("console", lambda m: self.console.append(f"console.{m.type}: {m.text[:300]}") if m.type == "error" else None)
        page.on("pageerror", lambda e: self.console.append(f"pageerror: {str(e)[:300]}"))
        page.on("requestfailed", lambda r: self.console.append(f"요청 실패: {r.url[:150]} ({r.failure})"))

    def shot(self) -> tuple[str, Path]:
        self.shots += 1
        path = OUT / f"{self.name}_{self.shots:03d}.jpg"
        self.page.screenshot(path=str(path), type="jpeg", quality=70)
        return base64.b64encode(path.read_bytes()).decode(), path

    def fps(self) -> float | None:
        try:
            f = self.page.evaluate("window.__pt ? window.__pt.frames : []")
            return round((len(f) - 1) / ((f[-1] - f[0]) / 1000), 1) if len(f) > 2 and f[-1] > f[0] else None
        except Exception:
            return None

    def act(self, name: str, a: dict) -> str:
        p = self.page
        if name == "click":
            (p.touchscreen.tap if self.mobile else p.mouse.click)(a["x"], a["y"])
            p.wait_for_timeout(300)
        elif name == "key":
            if a.get("hold_ms"):
                p.keyboard.down(a["key"])
                p.wait_for_timeout(min(int(a["hold_ms"]), 5000))
                p.keyboard.up(a["key"])
            else:
                p.keyboard.press(a["key"])
            p.wait_for_timeout(200)
        elif name == "drag":
            p.mouse.move(a["x1"], a["y1"])
            p.mouse.down()
            steps = max(5, int(a.get("ms", 300)) // 30)
            p.mouse.move(a["x2"], a["y2"], steps=steps)
            p.mouse.up()
        elif name == "type":
            p.keyboard.type(a["text"][:200], delay=30)
        elif name == "wait":
            p.wait_for_timeout(min(int(a["ms"]), 10000))
        else:
            return f"알 수 없는 도구 {name}"
        return "ok"


def trim_images(messages: list[dict]) -> None:
    """오래된 스크린샷은 글로 바꿔 토큰을 아낀다 (최근 KEEP_IMAGES장만 유지)."""
    seen = 0
    for m in reversed(messages):
        if not isinstance(m["content"], list):
            continue
        for b in m["content"]:
            inner = b.get("content") if b.get("type") == "tool_result" else None
            for blk in (inner if isinstance(inner, list) else [b]):
                if blk.get("type") == "image":
                    seen += 1
                    if seen > KEEP_IMAGES:
                        blk.clear()
                        blk.update({"type": "text", "text": "[이전 화면]"})


def play(client, browser, url: str, goal: str, steps: int, vp: str, usage: dict) -> dict:
    ctx = browser.new_context(**VIEWPORTS[vp], locale="ko-KR")
    ctx.add_init_script(FPS_SCRIPT)
    page = ctx.new_page()
    r = Runner(page, vp, vp == "mobile")
    t0 = time.time()
    page.goto(url, wait_until="load", timeout=60000)
    load_sec = round(time.time() - t0, 1)
    page.wait_for_timeout(1500)
    w, h = VIEWPORTS[vp]["viewport"]["width"], VIEWPORTS[vp]["viewport"]["height"]

    img, _ = r.shot()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": f"목표: {goal}\n화면: {vp} {w}x{h} (좌표는 이 크기 기준). 로딩 {load_sec}초. 행동 예산 {steps}턴."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}},
        {"type": "text", "text": f"콘솔 오류: {r.console[-5:] or '없음'} / FPS: {r.fps()}"}]}]
    summary, seen_errors = "", len(r.console)
    for step in range(steps):
        trim_images(messages)
        resp = client.messages.create(model=MODEL, max_tokens=1024, tools=TOOLS, messages=messages,
                                      system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}])
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
            usage[k] = usage.get(k, 0) + (getattr(resp.usage, k, 0) or 0)
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            messages.append({"role": "user", "content": "도구로 행동하거나 finish 하라."})
            continue
        results, done = [], False
        for i, b in enumerate(calls):
            if b.name == "finish":
                summary, done = b.input.get("summary", ""), True
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "끝"})
                continue
            if b.name == "note":
                _, path = r.shot()
                r.notes.append({**b.input, "viewport": vp, "step": step, "shot": str(path)})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": "기록함"})
                continue
            try:
                out = r.act(b.name, b.input)
            except Exception as e:
                out = f"실패: {type(e).__name__}: {str(e)[:200]}"
            if i == len(calls) - 1:      # 마지막 행동 뒤 화면을 돌려줌
                img, _ = r.shot()
                new_err = r.console[seen_errors:]
                seen_errors = len(r.console)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": [
                    {"type": "text", "text": f"{out} | 새 콘솔 오류: {new_err[-5:] or '없음'} | FPS: {r.fps()} | 남은 턴 {steps - step - 1}"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}]})
            else:
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
        if done:
            break
    ctx.close()
    return {"viewport": vp, "load_sec": load_sec, "notes": r.notes, "console": r.console, "summary": summary,
            "steps": step + 1}


def report(url: str, goal: str, runs: list[dict], usage: dict) -> str:
    sev = {"high": 0, "mid": 1, "low": 2}
    icon = {"bug": "🐞", "ux": "🧭", "balance": "⚖️", "perf": "🐢", "good": "👍"}
    lines = [f"# 🎮 AI 플레이테스트", f"- 대상: {url}", f"- 목표: {goal}"]
    for r in runs:
        lines += ["", f"## {r['viewport']} — {r['steps']}턴, 로딩 {r['load_sec']}초, 콘솔 오류 {len(r['console'])}건"]
        if r["summary"]:
            lines.append(r["summary"])
        for n in sorted(r["notes"], key=lambda n: (n["kind"] == "good", sev.get(n["severity"], 3))):
            lines.append(f"- {icon.get(n['kind'], '•')} [{n['severity']}] {n['text']}")
        if r["console"]:
            lines += ["", "콘솔 오류 (최대 10건):", *[f"    {c}" for c in list(dict.fromkeys(r["console"]))[:10]]]
    lines += ["", f"토큰: 입력 {usage.get('input_tokens', 0):,} (캐시 {usage.get('cache_read_input_tokens', 0):,}), "
                  f"출력 {usage.get('output_tokens', 0):,} · 모델 {MODEL}"]
    return "\n".join(lines)


def send_discord(text: str, notes: list[dict]) -> None:
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL')}/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/"
               f"{os.environ.get('GITHUB_RUN_ID')}") if os.environ.get("GITHUB_RUN_ID") else ""
    body = text if len(text) <= 1800 else text[:1750] + "\n…(전체는 실행 기록의 report.md)"
    if run_url:
        body += f"\n{run_url}"
    picks = sorted([n for n in notes if n["kind"] != "good"], key=lambda n: {"high": 0, "mid": 1}.get(n["severity"], 2))[:4]
    files = {f"files[{i}]": (Path(n["shot"]).name, Path(n["shot"]).read_bytes(), "image/jpeg") for i, n in enumerate(picks)}
    httpx.post(hook, data={"payload_json": json.dumps({"content": body, "username": "AI 플레이테스터"})},
               files=files or None, timeout=60)


def main():
    url = os.environ.get("PT_URL", "").strip()
    if not url:
        sys.exit("PT_URL이 없습니다.")
    goal = os.environ.get("PT_GOAL") or "처음부터 플레이하며 버그·불편·밸런스 문제를 찾아라"
    steps = int(os.environ.get("PT_STEPS") or 60)
    vps = [v.strip() for v in (os.environ.get("PT_VIEWPORTS") or "desktop,mobile").split(",") if v.strip() in VIEWPORTS]
    OUT.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    client, usage, runs = anthropic.Anthropic(max_retries=4), {}, []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        for vp in vps:
            try:
                runs.append(play(client, browser, url, goal, max(5, steps // len(vps)), vp, usage))
            except Exception as e:
                runs.append({"viewport": vp, "load_sec": "-", "notes": [], "console": [], "steps": 0,
                             "summary": f"실행 실패: {type(e).__name__}: {str(e)[:300]}"})
        browser.close()
    text = report(url, goal, runs, usage)
    (OUT / "report.md").write_text(text, encoding="utf-8")
    print(text)
    send_discord(text, [n for r in runs for n in r["notes"]])


if __name__ == "__main__":
    main()
