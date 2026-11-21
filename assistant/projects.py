"""프로젝트 스레드: 디스코드 스레드(또는 채널)를 프로젝트와 연결해, 그 안에서는 저장소·최근 커밋을 기본으로 알고 대화.

`!project football-academy` — 홈 채널에서 쓰면 새 스레드를 만들어 연결, 스레드 안에서 쓰면 그 스레드를 연결.
대화 기록은 원래 채널(스레드)마다 따로라서 프로젝트별로 섞이지 않는다.
"""
from __future__ import annotations

import time

from .tools import dev


def resolve_repo(cfg: dict, name: str) -> str:
    name = name.strip()
    repos = (cfg.get("github") or {}).get("repos", [])
    for r in repos:
        if r.lower() == name.lower() or r.split("/")[-1].lower() == name.lower():
            return r
    return name if "/" in name else ""


def bind(store, channel_id: str, cfg: dict, name: str) -> dict:
    p = {"name": name.split("/")[-1], "repo": resolve_repo(cfg, name)}
    store.put(f"project:{channel_id}", p)
    return p


def get(store, channel_id: str) -> dict | None:
    return store.get(f"project:{channel_id}") or None


def unbind(store, channel_id: str) -> None:
    store.put(f"project:{channel_id}", {})


async def recent_commits(store, repo: str) -> list[str]:
    """최근 커밋 5개 (30분 캐시)."""
    key = f"project_commits:{repo}"
    cached = store.get(key) or {}
    if cached.get("at", 0) > time.time() - 1800:
        return cached.get("lines", [])
    try:
        items = await dev.gh("GET", f"/repos/{repo}/commits", params={"per_page": 5})
        lines = [f"{c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0][:80]}" for c in items]
    except Exception:
        lines = cached.get("lines", [])
    store.put(key, {"at": time.time(), "lines": lines})
    return lines


async def context_text(store, channel_id: str) -> str:
    p = get(store, channel_id)
    if not p:
        return ""
    lines = [f"[프로젝트 스레드] 이 대화는 '{p['name']}' 프로젝트 전용이다."]
    if p.get("repo"):
        lines.append(f"GitHub 저장소: {p['repo']} — 커밋·이슈·워크플로·코드 질문은 따로 말이 없으면 이 저장소 기준.")
        commits = await recent_commits(store, p["repo"])
        if commits:
            lines.append("최근 커밋:\n" + "\n".join(f"- {c}" for c in commits))
    lines.append("개발 일지(Notion)·메모를 찾을 때도 이 프로젝트 이름으로 좁힌다.")
    return "\n".join(lines)
