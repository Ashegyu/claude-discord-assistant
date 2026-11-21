"""GitHub · Notion 도구.  환경변수: GITHUB_TOKEN, NOTION_TOKEN"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from .base import tool

GH = "https://api.github.com"
NOTION = "https://api.notion.com/v1"


async def gh(method: str, path: str, **kw) -> dict | list:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, GH + path, headers=headers, **kw)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _repo(ctx, repo: str) -> str:
    if "/" in repo:
        return repo
    for r in ctx.cfg["github"]["repos"]:  # 'my-game'처럼 이름만 줘도 됨
        if r.split("/")[-1] == repo:
            return r
    return repo


REPO_DESC = "레포 (예: my-game 또는 owner/my-game)"


@tool("github_commits", "레포의 최근 커밋 목록 (해시, 날짜, 메시지 첫 줄).",
      {"properties": {"repo": {"type": "string", "description": REPO_DESC},
                      "days": {"type": "integer", "default": 3}, "limit": {"type": "integer", "default": 30}},
       "required": ["repo"]}, group="github")
async def github_commits(ctx, repo: str, days: int = 3, limit: int = 30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cs = await gh("GET", f"/repos/{_repo(ctx, repo)}/commits", params={"since": since, "per_page": min(limit, 100)})
    return [{"sha": c["sha"][:7], "date": c["commit"]["author"]["date"],
             "message": c["commit"]["message"].split("\n")[0][:200]} for c in cs] or "커밋 없음"


@tool("github_commit_diff", "커밋 하나의 변경 파일과 diff (큰 파일은 잘림).",
      {"properties": {"repo": {"type": "string", "description": REPO_DESC}, "sha": {"type": "string"}},
       "required": ["repo", "sha"]}, group="github")
async def github_commit_diff(ctx, repo: str, sha: str):
    d = await gh("GET", f"/repos/{_repo(ctx, repo)}/commits/{sha}")
    parts = [f"{d['commit']['message']}\n파일 {len(d.get('files', []))}개 +{d['stats']['additions']}/-{d['stats']['deletions']}"]
    budget = 20000
    for f in d.get("files", []):
        chunk = f"\n### {f['filename']} (+{f['additions']}/-{f['deletions']})\n{(f.get('patch') or '')[:4000]}"
        if re.search(r"\.(json|lock|svg|png|jpe?g|webp|min\.js|map)$", f["filename"]):
            chunk = f"\n### {f['filename']} (데이터 파일, diff 생략)"
        parts.append(chunk[:budget])
        budget -= len(chunk)
        if budget <= 0:
            parts.append("\n…(이하 생략)")
            break
    return "".join(parts)


@tool("github_issues", "레포의 이슈·PR 목록.",
      {"properties": {"repo": {"type": "string", "description": REPO_DESC},
                      "state": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"}},
       "required": ["repo"]}, group="github")
async def github_issues(ctx, repo: str, state: str = "open"):
    items = await gh("GET", f"/repos/{_repo(ctx, repo)}/issues", params={"state": state, "per_page": 30})
    return [{"number": i["number"], "type": "PR" if "pull_request" in i else "Issue", "state": i["state"],
             "title": i["title"], "updated": i["updated_at"]} for i in items] or "없음"


@tool("github_create_issue", "레포에 이슈를 만든다 (사용자 확인 후 실행).",
      {"properties": {"repo": {"type": "string", "description": REPO_DESC}, "title": {"type": "string"},
                      "body": {"type": "string", "default": ""}},
       "required": ["repo", "title"]}, group="github", confirm=True,
      summarize=lambda a: f"🐛 이슈 생성: `{a.get('repo')}`\n**{a.get('title')}**\n{(a.get('body') or '')[:300]}")
async def github_create_issue(ctx, repo: str, title: str, body: str = ""):
    i = await gh("POST", f"/repos/{_repo(ctx, repo)}/issues", json={"title": title, "body": body})
    return f"이슈를 만들었습니다: {i['html_url']}"


def _wf_desc() -> str:
    return "등록된 GitHub Actions 워크플로를 실행한다 (사용자 확인 후). 이름은 config.yaml github.workflows의 키."


@tool("github_run_workflow", _wf_desc(),
      {"properties": {"name": {"type": "string", "description": "예: devlog"},
                      "inputs": {"type": "object", "description": "워크플로 입력값 덮어쓰기 (선택)"}},
       "required": ["name"]}, group="github", confirm=True,
      summarize=lambda a: f"▶️ 워크플로 실행: **{a.get('name')}** {a.get('inputs') or ''}")
async def github_run_workflow(ctx, name: str, inputs: dict | None = None):
    wf = ctx.cfg["github"]["workflows"].get(name)
    if not wf:
        return f"등록되지 않은 워크플로입니다. 가능: {', '.join(ctx.cfg['github']['workflows'])}"
    await gh("POST", f"/repos/{wf['repo']}/actions/workflows/{wf['file']}/dispatches",
             json={"ref": "main", "inputs": {**wf.get("inputs", {}), **(inputs or {})}})
    return f"실행을 요청했습니다: https://github.com/{wf['repo']}/actions/workflows/{wf['file']}"


# ───────────── Notion ─────────────
async def notion(method: str, path: str, body: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, NOTION + path, json=body, headers={
            "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}", "Notion-Version": "2022-06-28"})
    if r.status_code >= 300:
        raise RuntimeError(f"Notion {r.status_code}: {r.text[:300]}")
    return r.json()


def _plain(rich: list) -> str:
    return "".join(t.get("plain_text", "") for t in rich or [])


@tool("notion_devlog_search", "Notion '개발 일지' DB에서 일지를 찾는다 (레포·기간·키워드).",
      {"properties": {"keyword": {"type": "string", "default": ""}, "repo": {"type": "string", "default": ""},
                      "days": {"type": "integer", "default": 14}}}, group="notion")
async def notion_devlog_search(ctx, keyword: str = "", repo: str = "", days: int = 14):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    flt = [{"property": "날짜", "date": {"on_or_after": since}}]
    if repo:
        flt.append({"property": "레포", "select": {"equals": repo.split("/")[-1]}})
    if keyword:
        flt.append({"or": [{"property": "제목", "title": {"contains": keyword}},
                           {"property": "한 줄 요약", "rich_text": {"contains": keyword}}]})
    res = await notion("POST", f"/databases/{ctx.cfg['notion']['devlog_database_id']}/query",
                       {"filter": {"and": flt}, "sorts": [{"property": "날짜", "direction": "descending"}], "page_size": 20})
    out = []
    for p in res.get("results", []):
        pr = p["properties"]
        out.append({"title": _plain(pr["제목"]["title"]), "date": (pr["날짜"]["date"] or {}).get("start"),
                    "summary": _plain(pr["한 줄 요약"]["rich_text"]), "commits": pr["커밋 수"]["number"],
                    "url": p["url"], "page_id": p["id"]})
    return out or "해당 일지 없음"


@tool("notion_read_page", "Notion 페이지 본문을 텍스트로 읽는다.",
      {"properties": {"page_id": {"type": "string"}}, "required": ["page_id"]}, group="notion")
async def notion_read_page(ctx, page_id: str):
    res = await notion("GET", f"/blocks/{page_id}/children?page_size=100")
    lines = []
    for b in res.get("results", []):
        t = b["type"]
        rich = b.get(t, {}).get("rich_text")
        if rich is not None:
            prefix = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ", "bulleted_list_item": "- ",
                      "numbered_list_item": "1. ", "to_do": "- [ ] "}.get(t, "")
            lines.append(prefix + _plain(rich))
    return "\n".join(lines)[:15000] or "(본문 없음)"


@tool("notion_write_memo", "Notion에 메모 페이지를 만든다 (사용자 확인 후).",
      {"properties": {"title": {"type": "string"}, "content": {"type": "string", "description": "본문 (줄바꿈으로 문단 구분)"}},
       "required": ["title", "content"]}, group="notion", confirm=True,
      summarize=lambda a: f"📝 Notion 메모 작성: **{a.get('title')}**\n{(a.get('content') or '')[:300]}")
async def notion_write_memo(ctx, title: str, content: str):
    parent = ctx.cfg["notion"].get("memo_parent_page_id")
    if not parent:
        return "메모를 만들 부모 페이지가 설정되지 않았습니다 (config.yaml notion.memo_parent_page_id)."
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": line[:1900]}}]}}
              for line in content.split("\n") if line.strip()][:90]
    p = await notion("POST", "/pages", {"parent": {"page_id": parent},
                                         "properties": {"title": {"title": [{"text": {"content": title[:200]}}]}},
                                         "children": blocks})
    return f"메모를 만들었습니다: {p['url']}"
