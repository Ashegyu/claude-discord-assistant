"""재발급 알림: 만료되는 비밀값·토큰을 미리 디스코드로 알린다.

1) 날짜 기반 — config.yaml renewals.items
   - every_days + last : 마지막 발급일로부터 주기 (예: 카카오 리프레시 토큰 약 2개월)
   - expires           : 정해진 만료일 (예: GitHub 토큰)
   `!renewed 키 [새 만료일]` 로 재발급 기록 → DB에 저장되어 config보다 우선.
2) 자동 감지 — 비서가 쓰는 GitHub 토큰은 API 응답 헤더로 만료일을 읽는다.
3) 상태 점검 — 비서가 쓰는 Microsoft·구글·카카오 토큰을 매일 한 번 갱신해 보고 실패하면 바로 알린다
   (카카오는 주기적으로 갱신해야 살아 있으므로 이 점검이 토큰 유지 역할도 한다).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta

import httpx

log = logging.getLogger("renewals")


def _cfg(cfg: dict) -> dict:
    return cfg.get("renewals") or {}


def _d(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def items(cfg: dict, store) -> list[dict]:
    """config 항목 + 기록(재발급일·새 만료일) + 자동 감지 항목."""
    out = []
    for it in _cfg(cfg).get("items", []):
        it = dict(it)
        rec = store.get(f"renew:{it['key']}") or {}
        it["last"] = rec.get("last") or it.get("last") or ""
        it["expires"] = rec.get("expires") or it.get("expires") or ""
        out.append(it)
    gh = store.get("renew_auto:github-assistant")
    if gh:
        out.append({"key": "github-assistant", "label": "GitHub 토큰 (비서 GITHUB_TOKEN)", "expires": gh, "auto": True,
                    "how": "GitHub → Settings → Developer settings → Fine-grained tokens → Regenerate → VM .env GITHUB_TOKEN 교체 후 재시작"})
    return out


def due_date(it: dict) -> date | None:
    if it.get("expires"):
        return _d(it["expires"])
    if it.get("every_days") and it.get("last"):
        last = _d(it["last"])
        return last + timedelta(days=int(it["every_days"])) if last else None
    return None


def status_lines(cfg: dict, store, today: date) -> list[str]:
    lines = []
    for it in items(cfg, store):
        d = due_date(it)
        if d:
            left = (d - today).days
            when = f"D-{left}" if left > 0 else ("오늘" if left == 0 else f"{-left}일 지남")
            lines.append(f"`{it['key']}` {it['label']} — {d:%Y-%m-%d} ({when})")
        else:
            need = "마지막 발급일" if it.get("every_days") else "만료일"
            lines.append(f"`{it['key']}` {it['label']} — {need} 미설정 → `!renewed {it['key']} YYYY-MM-DD`")
    return lines


def alerts(cfg: dict, store, today: date) -> list[str]:
    """오늘 보낼 알림 (D-14·7·3·1·0, 지난 뒤엔 매일). 같은 날 같은 항목은 한 번만."""
    warn = set(_cfg(cfg).get("warn_days", [14, 7, 3, 1, 0]))
    msgs = []
    for it in items(cfg, store):
        d = due_date(it)
        sent_key = f"renew_sent:{it['key']}"
        if store.get(sent_key) == today.isoformat():
            continue
        if not d:
            if not store.get(f"renew_unset:{it['key']}"):   # 날짜 미설정은 한 번만 안내
                store.put(f"renew_unset:{it['key']}", True)
                need = "마지막 발급일" if it.get("every_days") else "만료일"
                msgs.append(f"📅 **{it['label']}**의 {need}이 설정되지 않아 재발급 알림을 줄 수 없어요.\n"
                            f"`!renewed {it['key']} YYYY-MM-DD` 로 알려주세요.")
            continue
        left = (d - today).days
        if left in warn or left < 0:
            store.put(sent_key, today.isoformat())
            head = (f"⏳ **{it['label']}** 재발급 {left}일 전 ({d:%m/%d})" if left > 0 else
                    f"⚠️ **{it['label']}** 오늘 재발급하세요" if left == 0 else
                    f"🚨 **{it['label']}** 재발급 시기가 {-left}일 지났어요 ({d:%m/%d})")
            tail = f"\n방법: {it['how']}" if it.get("how") else ""
            done = (f"\n끝나면 `!renewed {it['key']}`" + (" 새만료일" if it.get("expires") and not it.get("auto") else "")
                    if not it.get("auto") else "\n(재발급하면 자동으로 새 만료일을 읽어요)")
            msgs.append(head + tail + done)
    return msgs


def mark_renewed(cfg: dict, store, key: str, new_date: str = "", today: date | None = None) -> str:
    today = today or date.today()
    it = next((i for i in items(cfg, store) if i["key"] == key), None)
    if not it:
        return f"`{key}` 항목이 없어요. `!renewals` 로 키를 확인하세요."
    if it.get("auto"):
        return "이 항목은 자동으로 만료일을 읽어요. 재발급 후 봇을 재시작하면 다음 점검 때 반영돼요."
    rec = store.get(f"renew:{key}") or {}
    if it.get("every_days"):
        d = _d(new_date) or today          # 주기형: 날짜를 주면 그 날을 마지막 발급일로
        rec["last"] = d.isoformat()
    else:
        d = _d(new_date)
        if not d:
            return f"새 만료일을 같이 적어 주세요: `!renewed {key} YYYY-MM-DD`"
        rec["expires"] = d.isoformat()
    store.put(f"renew:{key}", rec)
    store.put(f"renew_sent:{key}", "")
    store.put(f"renew_unset:{key}", False)
    nd = due_date({**it, **rec})
    return f"기록했어요. 다음 재발급: {nd:%Y-%m-%d}" if nd else "기록했어요."


# ───────────── 자동 감지·상태 점검 ─────────────
async def github_token_expiry() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        return None
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.github.com/rate_limit", headers={"Authorization": f"Bearer {tok}"})
    if r.status_code == 401:
        raise RuntimeError("GitHub 토큰이 만료됐거나 잘못됐어요")
    exp = r.headers.get("github-authentication-token-expiration")   # 예: "2027-01-01 00:00:00 UTC" (만료 없는 토큰은 헤더 없음)
    return exp[:10] if exp else None


async def health_check(cfg: dict, store) -> list[str]:
    """비서가 쓰는 토큰을 실제로 갱신해 보고, 실패한 것만 돌려준다."""
    problems = []
    try:
        exp = await github_token_expiry()
        store.put("renew_auto:github-assistant", exp or "")
    except Exception as e:
        problems.append(f"GitHub: {e} → 새 토큰을 VM .env GITHUB_TOKEN에 넣고 재시작")

    from . import todo
    if todo.enabled():
        try:
            await todo.lists()
        except Exception as e:
            problems.append(f"Microsoft To Do: {e} → VM에서 `.venv/bin/python deploy/ms_auth.py`")

    from .tools import google
    if google.TOKEN.exists():
        try:
            await asyncio.to_thread(google._creds)
        except Exception as e:
            problems.append(f"구글: 토큰 갱신 실패 ({type(e).__name__}) → SSH 터널로 `deploy/google_auth.py` 다시 실행")

    from .tools import life
    if os.environ.get("KAKAO_REST_API_KEY") and (os.environ.get("KAKAO_REFRESH_TOKEN") or life.KAKAO_TOKEN_FILE.exists()):
        try:
            await life._kakao_access()      # 갱신해 두면 리프레시 토큰이 만료 전에 자동 교체돼 계속 살아 있음
        except Exception as e:
            problems.append(f"카카오(비서): {e} → kakao_init.py로 새 리프레시 토큰 → VM .env KAKAO_REFRESH_TOKEN, data/kakao.json 삭제 후 재시작")
    return problems
