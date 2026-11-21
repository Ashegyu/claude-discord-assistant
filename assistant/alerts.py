"""알림 정리: 급한 것만 바로, 나머지는 조용한 시간엔 모아뒀다가 묶어서.

등급
- urgent : 언제든 바로 (출발 알림, 직접 건 리마인더, 긴급 메일)
- normal : 바로, 단 조용한 시간이면 모아둠 (중요 메일, 재발급·예산 경고, 정기 리포트)
- low    : 항상 모아둠 (백업 성공 같은 확인용)
모아둔 것은 아침 브리핑과 config alerts.digest 시각(기본 12:30, 18:30)에 한 번에 보낸다.
버튼이 달린 알림(메일 → 할 일·답장 초안)은 묶음 뒤에 따로 보낸다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

SCHEMA = "CREATE TABLE IF NOT EXISTS alert_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, level TEXT, text TEXT, info TEXT)"
LEVELS = ("urgent", "normal", "low")


def _cfg(cfg: dict) -> dict:
    return cfg.get("alerts") or {}


def init(store) -> None:
    store._exec(SCHEMA)


def in_range(spec: str, now: datetime) -> bool:
    """'23:00-07:00' 처럼 자정을 넘는 범위도 처리."""
    if not spec:
        return False
    a, b = spec.split("-")
    t = now.strftime("%H:%M")
    return a <= t < b if a <= b else (t >= a or t < b)


def is_quiet(cfg: dict, now: datetime) -> bool:
    c = _cfg(cfg)
    return in_range(c.get("quiet", ""), now) or any(in_range(r, now) for r in c.get("focus", []) or [])


def decide(cfg: dict, level: str, now: datetime) -> str:
    """'send' 또는 'queue'"""
    if not _cfg(cfg).get("enabled", True) or level == "urgent":
        return "send"
    if level == "low":
        return "queue"
    return "queue" if is_quiet(cfg, now) else "send"


def enqueue(store, text: str, level: str = "normal", info: dict | None = None) -> None:
    init(store)
    store._exec("INSERT INTO alert_queue (ts, level, text, info) VALUES (?,?,?,?)",
                (time.time(), level, text, json.dumps(info, ensure_ascii=False) if info else ""))


def drain(store) -> list[dict]:
    init(store)
    rows = store._exec("SELECT id, ts, level, text, info FROM alert_queue ORDER BY id")
    if rows:
        store._exec("DELETE FROM alert_queue WHERE id <= ?", (rows[-1][0],))
    return [{"ts": ts, "level": lv, "text": tx, "info": json.loads(inf) if inf else None} for _i, ts, lv, tx, inf in rows]


def pending_count(store) -> int:
    init(store)
    return store._exec("SELECT COUNT(*) FROM alert_queue")[0][0]


def digest(items: list[dict], tz, title: str = "📥 모아둔 알림") -> tuple[str, list[dict]]:
    """(버튼 없는 것들을 묶은 글, 버튼이 있어 따로 보낼 것들)"""
    plain = [i for i in items if not i["info"]]
    separate = [i for i in items if i["info"]]
    if not plain:
        return "", separate
    lines = [f"{title} ({len(plain)}개)"]
    for i in plain:
        t = datetime.fromtimestamp(i["ts"], tz).strftime("%H:%M")
        body = i["text"].strip()
        first, *rest = body.split("\n")
        extra = " " + " / ".join(x.strip() for x in rest[:2] if x.strip()) if rest else ""
        lines.append(f"`{t}` {first}{extra}"[:300])
    return "\n".join(lines), separate
