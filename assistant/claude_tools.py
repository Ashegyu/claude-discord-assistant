"""정해진 양식(도구)으로 Claude 답을 받는 비동기 공통 함수.

일부 모델은 tool_choice 강제를 지원하지 않으므로, 실패하면 auto + 지시문으로 다시 요청한다.
"""
from __future__ import annotations

import anthropic


def _append(prompt, text: str):
    if isinstance(prompt, str):
        return prompt + text
    return [*prompt, {"type": "text", "text": text}]


async def acall_tool(client, store, model: str, prompt, tool: dict, role: str, max_tokens: int = 1000) -> dict:
    kw = dict(model=model, max_tokens=max_tokens, tools=[tool])
    try:
        resp = await client.messages.create(tool_choice={"type": "tool", "name": tool["name"]},
                                            messages=[{"role": "user", "content": prompt}], **kw)
    except anthropic.BadRequestError as e:
        if "tool_choice" not in str(e):
            raise
        resp = await client.messages.create(
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": _append(prompt, f"\n\n반드시 {tool['name']} 도구로 제출하라.")}], **kw)
    if store is not None:
        store.record_usage(model, role, resp.usage.model_dump() if hasattr(resp.usage, "model_dump") else {})
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use":
            return dict(b.input or {})
    return {}
