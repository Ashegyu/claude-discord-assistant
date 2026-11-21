"""설정 읽기: config.yaml(저장소 기본값) 위에 config.local.yaml(이 VM 전용, Git에 안 올라감)을 덮어쓴다.

VM에서 config.yaml을 직접 고치면 `!update`(git pull)가 충돌하므로, VM에서 바꾸는 값은 config.local.yaml에만 둔다.
디스코드에서 `!set talkcal.enabled true` 로 쓰고 `!config` 로 확인한다.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "config.local.yaml"


def merge(base: dict, over: dict) -> dict:
    """사전은 안쪽까지 합치고, 그 밖의 값(목록 포함)은 통째로 바꾼다."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_local(path: Path | None = None) -> dict:
    path = path or LOCAL
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}


def load(root: Path | None = None) -> dict:
    root = root or ROOT
    base = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    return merge(base, read_local(root / "config.local.yaml"))


def set_local(key: str, raw: str, base: dict, path: Path | None = None) -> str:
    """'a.b.c' 경로에 값을 써넣는다. 기본 설정에 없는 키는 오타일 가능성이 커서 거절한다."""
    path = path or LOCAL
    parts = [p for p in key.strip().split(".") if p]
    if not parts:
        return "사용법: `!set 경로 값` 예: `!set talkcal.enabled true`"
    node = base
    for p in parts:
        if not isinstance(node, dict) or p not in node:
            near = ", ".join(list(node)[:12]) if isinstance(node, dict) else ""
            return f"`{key}` 는 설정에 없는 항목이에요." + (f" 여기서 쓸 수 있는 것: {near}" if near else "")
        node = node[p]
    if isinstance(node, dict):
        return f"`{key}` 는 묶음이에요. 그 안의 항목을 지정하세요: {', '.join(list(node)[:12])}"
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        return "값을 읽지 못했어요. 문자열은 따옴표로 감싸 보세요."
    if node is not None and value is not None and type(node) is not type(value) and not (isinstance(node, (int, float)) and isinstance(value, (int, float))):
        return f"형식이 달라요: 지금 값은 {node!r} ({type(node).__name__}), 넣으려는 값은 {value!r} ({type(value).__name__})"
    local = read_local(path)
    cur = local
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    path.write_text("# 이 VM 전용 설정 (config.yaml 위에 덮어씀). !set 으로 고쳐요.\n"
                    + yaml.safe_dump(local, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return f"`{key}`: {node!r} → {value!r} 로 저장했어요. `!restart` 하면 적용돼요."


def unset_local(key: str, path: Path | None = None) -> str:
    path = path or LOCAL
    local, parts = read_local(path), key.strip().split(".")
    cur = local
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return f"`{key}` 는 VM 전용 설정에 없어요."
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return f"`{key}` 는 VM 전용 설정에 없어요."
    del cur[parts[-1]]
    path.write_text(yaml.safe_dump(local, allow_unicode=True, sort_keys=False) if local else "", encoding="utf-8")
    return f"`{key}` 를 기본값으로 되돌렸어요. `!restart` 하면 적용돼요."


def show_local(path: Path | None = None) -> str:
    local = read_local(path)
    if not local:
        return "VM 전용 설정이 없어요 (모두 기본값). 바꾸려면 `!set 경로 값` 예: `!set briefing.time \"07:30\"`"
    return "⚙️ **VM 전용 설정** (config.local.yaml)\n```yaml\n" + yaml.safe_dump(local, allow_unicode=True, sort_keys=False)[:1700] + "```"


def fixed_remote(url: str, repo: str) -> str | None:
    """git 원격 주소가 같은 주인의 다른 저장소 이름을 가리키면, 설정의 저장소(self_improve.repo)로 바꾼 주소를 돌려준다.

    저장소 이름을 바꾼 뒤 같은 이름으로 새 저장소가 생기면 GitHub의 자동 전달이 끊기므로, 시작할 때 맞춰 둔다.
    주소 안의 로그인 정보(https://토큰@github.com/…)와 .git 꼬리는 그대로 둔다. 바꿀 게 없으면 None.
    """
    import re
    m = re.match(r"^(.*github\.com[/:])([^/]+)/([^/]+?)(\.git)?/?$", (url or "").strip())
    if not m or "/" not in (repo or ""):
        return None
    owner, name = repo.split("/", 1)
    if m.group(2).lower() != owner.lower() or m.group(3).lower() == name.lower():
        return None
    return f"{m.group(1)}{m.group(2)}/{name}{m.group(4) or ''}"


def sync_git_remote(cfg: dict, root: Path | None = None) -> str:
    """시작할 때 한 번: origin 주소를 설정의 저장소 이름에 맞춘다. 결과를 한 줄로 돌려준다 (로그용)."""
    import subprocess
    root = root or ROOT
    repo = (cfg.get("self_improve") or {}).get("repo", "")
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        new = fixed_remote(url, repo)
        if not new:
            return ""
        subprocess.run(["git", "remote", "set-url", "origin", new], cwd=root, check=True, timeout=10)
        return f"git 원격 주소를 {repo}로 맞췄어요"
    except Exception as e:
        return f"git 원격 주소 확인 실패: {e}"
