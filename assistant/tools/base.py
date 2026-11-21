"""도구 등록부.

도구는 @tool 데코레이터로 등록하고, 각 전문 에이전트는 group 이름으로 자기 도구만 받는다.
confirm=True인 도구는 실행 전에 디스코드 [실행]/[취소] 버튼으로 사용자 확인을 받는다.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Context:
    """도구 실행 시 넘겨주는 실행 환경."""
    cfg: dict
    store: Any
    channel_id: str
    confirm: Callable[[str], Awaitable[bool]]          # 사용자 확인 (디스코드 버튼)
    notify: Callable[[str], Awaitable[None]]           # 진행 상황 짧게 알리기
    extras: dict = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[..., Any]
    group: str
    confirm: bool = False
    summarize: Callable[[dict], str] | None = None     # 확인 창에 보여줄 설명

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}

    async def run(self, ctx: Context, args: dict) -> str:
        if self.confirm:
            desc = self.summarize(args) if self.summarize else f"{self.name} {json.dumps(args, ensure_ascii=False)}"
            if not await ctx.confirm(desc):
                return "사용자가 취소했습니다. 실행하지 않았습니다."
        try:
            res = self.handler(ctx, **args)
            if inspect.isawaitable(res):
                res = await res
        except TypeError as e:
            return f"오류: 인자가 맞지 않습니다 ({e})"
        except Exception as e:  # 도구 오류는 모델에게 돌려주고 스스로 대처하게
            return f"오류: {type(e).__name__}: {e}"
        if not isinstance(res, str):
            res = json.dumps(res, ensure_ascii=False, default=str)
        return res[:30000]


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, schema: dict, group: str, confirm: bool = False,
         summarize: Callable[[dict], str] | None = None):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, {"type": "object", **schema}, fn, group, confirm, summarize)
        return fn
    return deco


def tools_for(*groups: str) -> list[Tool]:
    return [t for t in REGISTRY.values() if t.group in groups]


async def to_thread(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)
