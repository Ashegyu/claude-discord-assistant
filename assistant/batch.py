"""배치 처리 (Message Batches API: 표준가 50%, 보통 1시간 이내, 최대 24시간).

쓰임새
  - bulk_job 도구: "커밋 100개 각각 요약" 같은 대량·비긴급 작업
  - 야간 메일 요약: 하루치 메일을 메일마다 하나의 요청으로 → 끝나면 한 번 종합

비용 최적화
  - 모든 요청이 같은 system(공통 지시)을 쓰고, 그 블록에 1시간 캐시 → 배치 할인 + 캐시 읽기가 겹쳐 적용
    (배치는 처리가 5분을 넘길 수 있어 문서 권장대로 1시간 TTL 사용)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("batch")
OUT = Path(os.environ.get("DATA_DIR", "data")) / "batch"
MAX_ITEMS = 500


def _requests(model: str, instruction: str, items: list[str], max_tokens: int = 1024) -> list[dict]:
    system = [
        {"type": "text", "text": "너는 대량 처리 작업자다. 각 요청의 항목 하나에 대해 지시대로만 결과를 한국어로 간결하게 쓴다."},
        {"type": "text", "text": f"## 공통 지시\n{instruction}", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
    ]
    return [{"custom_id": f"item-{i:04d}",
             "params": {"model": model, "max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": f"## 항목 {i + 1}\n{it[:20000]}"}]}}
            for i, it in enumerate(items[:MAX_ITEMS])]


async def _collect_source(source: dict) -> list[str]:
    t = source.get("type")
    if t == "github_commits":
        from .tools.dev import gh
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(days=int(source.get("days", 7)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        repo, items, page = source["repo"], [], 1
        while len(items) < MAX_ITEMS:
            cs = await gh("GET", f"/repos/{repo}/commits", params={"since": since, "per_page": 100, "page": page})
            items += [f"{c['sha'][:7]} {c['commit']['author']['date']}\n{c['commit']['message']}" for c in cs]
            if len(cs) < 100:
                break
            page += 1
        return items
    if t == "mail":
        from .tools.mail import GMAIL

        found = await asyncio.to_thread(GMAIL.search, source.get("query", "newer_than:1d"), int(source.get("limit", 50)))
        return [f"보낸 사람: {m['from']}\n제목: {m['subject']}\n날짜: {m['date']}\n{m['snippet']}" for m in found]
    raise ValueError(f"알 수 없는 source.type: {t}")


async def submit(client, store, cfg, *, kind: str, title: str, channel_id: str, instruction: str,
                 items: list[str], meta: dict, max_tokens: int = 1024) -> str:
    reqs = _requests(cfg["models"]["batch"], instruction, items, max_tokens)
    batch = await client.messages.batches.create(requests=reqs)
    store.add_batch(batch.id, kind, title, channel_id, {**meta, "count": len(reqs), "instruction": instruction})
    log.info("배치 제출 %s (%s, %d건)", batch.id, title, len(reqs))
    return batch.id


async def submit_bulk(ctx, title: str, instruction: str, items: list[str], source: dict | None, combine: bool) -> str:
    if source and not items:
        items = await _collect_source(source)
    if not items:
        return "처리할 항목이 없습니다."
    client = ctx.extras["client"]
    bid = await submit(client, ctx.store, ctx.cfg, kind="bulk", title=title, channel_id=ctx.channel_id,
                       instruction=instruction, items=items, meta={"combine": combine, "items_preview": items[:3]})
    return f"배치 작업을 등록했습니다 ({len(items[:MAX_ITEMS])}건, id {bid}). 보통 1시간 안에 끝나며, 끝나면 결과를 이 채널로 보냅니다."


async def _results(client, bid: str) -> tuple[dict[str, str], list[tuple[str, dict]]]:
    out: dict[str, str] = {}
    usage: list[tuple[str, dict]] = []
    res = client.messages.batches.results(bid)
    if asyncio.iscoroutine(res):
        res = await res
    async for r in res:
        o = r.result
        if o.type == "succeeded":
            out[r.custom_id] = "".join(b.text for b in o.message.content if b.type == "text")
            usage.append((o.message.model, o.message.usage.model_dump() if hasattr(o.message.usage, "model_dump") else {}))
        else:
            out[r.custom_id] = f"(실패: {o.type})"
    return out, usage


async def poll(client, store, cfg, send) -> None:
    """끝난 배치를 찾아 결과를 정리해 보낸다. send(channel_id, text, file_path|None)."""
    for bid, kind, title, channel_id, meta_json in store.open_batches():
        meta = json.loads(meta_json)
        b = await client.messages.batches.retrieve(bid)
        if b.processing_status != "ended":
            continue
        results, usage = await _results(client, bid)
        for model, u in usage:
            store.record_usage(model, f"batch:{kind}", u, batch=True)
        store.close_batch(bid)

        ordered = [results[k] for k in sorted(results)]
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{bid}.md"
        path.write_text(f"# {title}\n\n지시: {meta.get('instruction', '')}\n\n" +
                        "\n\n---\n\n".join(f"### {i + 1}\n{t}" for i, t in enumerate(ordered)), encoding="utf-8")

        if kind == "mail_digest":
            text = await combine(client, store, cfg, MAIL_DIGEST_COMBINE, ordered)
            await send(channel_id, f"📬 **오늘의 메일 요약** ({meta.get('count')}통)\n\n{text}", None)
        else:
            text = await combine(client, store, cfg, f"아래는 '{title}' 작업의 항목별 결과다. 전체를 종합해 핵심 발견·패턴·주목할 항목을 정리하라.",
                                 ordered) if meta.get("combine", True) else ""
            await send(channel_id, f"✅ 배치 완료: **{title}** ({meta.get('count')}건)\n\n{text}".strip(), path)


async def combine(client, store, cfg, instruction: str, parts: list[str]) -> str:
    body = "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(parts))[:150000]
    resp = await client.messages.create(
        model=cfg["models"]["worker"], max_tokens=2000,
        messages=[{"role": "user", "content": f"{instruction}\n\n디스코드에 보낼 1500자 이내 한국어로.\n\n{body}"}])
    store.record_usage(cfg["models"]["worker"], "batch:combine", resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    return "".join(b.text for b in resp.content if b.type == "text")


MAIL_EXTRACT = ("메일 한 통을 분석해 다음 형식 한 줄로만 답하라:\n"
                "[중요도: 긴급/중요/보통/광고] [분류: 업무/결제/개발/개인/알림/광고] 보낸 사람 — 핵심 한 문장 | 해야 할 일(없으면 -)")
MAIL_DIGEST_COMBINE = ("아래는 오늘 받은 메일을 한 통씩 분석한 결과다. 디스코드용으로 정리하라: "
                       "1) 해야 할 일 목록 2) 중요 메일 3) 나머지는 분류별 개수만. 광고는 개수만.")


async def nightly_mail_digest(client, store, cfg, channel_id: str) -> str | None:
    from .tools.mail import GMAIL

    if not GMAIL.enabled:
        return None
    mails = await asyncio.to_thread(GMAIL.search, "newer_than:1d", 100)
    ignore = {s.lower() for s in cfg["mail"].get("ignore_senders", [])}
    mails = [m for m in mails if m["from_addr"] not in ignore]
    if not mails:
        return None
    items = [f"보낸 사람: {m['from']}\n제목: {m['subject']}\n날짜: {m['date']}\n미리보기: {m['snippet']}" for m in mails]
    return await submit(client, store, cfg, kind="mail_digest", title="오늘의 메일 요약", channel_id=channel_id,
                        instruction=MAIL_EXTRACT, items=items, meta={}, max_tokens=300)
