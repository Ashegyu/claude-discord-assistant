"""봇 생존 감시: healthchecks.io 같은 서비스에 5분마다 '살아 있음' 신호를 보낸다.

신호가 끊기면(봇 죽음·VM 꺼짐·네트워크 끊김) 그 서비스가 디스코드로 알려준다.
신호 본문에 메모리 상황을 실어 보내서, 죽기 직전 메모리가 모자랐는지 기록에서 볼 수 있다.
.env: HEALTHCHECK_URL=https://hc-ping.com/<uuid>
"""
from __future__ import annotations

import os

import httpx


def memory_info() -> str:
    def meminfo(key: str) -> int:
        try:
            for line in open("/proc/meminfo"):
                if line.startswith(key + ":"):
                    return int(line.split()[1]) // 1024
        except OSError:
            pass
        return -1

    rss = -1
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) // 1024
    except OSError:
        pass
    return (f"rss={rss}MB avail={meminfo('MemAvailable')}MB total={meminfo('MemTotal')}MB "
            f"swap_free={meminfo('SwapFree')}MB/{meminfo('SwapTotal')}MB")


async def ping(suffix: str = "", body: str = "") -> bool:
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url.rstrip("/") + suffix, content=(body or memory_info()).encode())
        return r.status_code < 300
    except Exception:
        return False
