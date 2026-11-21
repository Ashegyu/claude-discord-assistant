"""크레딧 가드: 기록된 토큰 사용량 × 모델별 가격으로 비용을 계산하고, 월 예산을 넘으면 절약 모드.

가격 (USD / 100만 토큰, 2026-11 공식 가격표): config.yaml budget.prices 로 덮어쓸 수 있다.
캐시 쓰기는 5분/1시간 구분 없이 기록되므로 1시간 가격(더 비싼 쪽)으로 계산 → 실제보다 약간 높게 나온다.
배치는 50% 할인. GitHub Actions(플레이테스트·daily-digest)처럼 봇 밖에서 쓴 비용은 포함되지 않는다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_PRICES = {  # input, output, cache_write(1h), cache_read
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 8.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 4.0, "cache_read": 0.20},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.20, "cache_read": 0.01},
}
FALLBACK = "claude-sonnet-5-5"   # 모르는 모델은 Sonnet 가격으로 (보수적)
SEARCH_USD_PER_1K = 10.0         # 웹 검색 1,000회당 (토큰 비용은 별도로 위에서 계산됨)


def _cfg(cfg: dict) -> dict:
    return cfg.get("budget") or {}


def prices(cfg: dict) -> dict:
    return {**DEFAULT_PRICES, **(_cfg(cfg).get("prices") or {})}


def row_cost(model: str, inp: int, cache_read: int, cache_write: int, out: int, batch: bool, table: dict) -> float:
    p = table.get(model) or table[FALLBACK]
    usd = (inp * p["input"] + out * p["output"] + cache_write * p["cache_write"] + cache_read * p["cache_read"]) / 1e6
    return usd * (0.5 if batch else 1.0)


def cost_since(store, cfg: dict, since: float) -> tuple[float, dict]:
    """(총액, 역할별 금액)"""
    table, total, by_role = prices(cfg), 0.0, {}
    for model, role, batch, inp, cr, cw, out, _n in store.usage_since(since):
        c = row_cost(model, inp or 0, cr or 0, cw or 0, out or 0, bool(batch), table)
        total += c
        key = (role or "").split(":")[0] + (" (배치)" if batch else "")
        by_role[key] = by_role.get(key, 0.0) + c
    n = store.searches_since(since)
    if n:
        fee = n * float(_cfg(cfg).get("search_usd_per_1k", SEARCH_USD_PER_1K)) / 1000
        total += fee
        by_role["웹 검색"] = fee
    return total, by_role


def searches_left_today(store, cfg: dict) -> int | None:
    """오늘 남은 웹 검색 횟수 (상한이 없으면 None)."""
    cap = int(_cfg(cfg).get("daily_web_searches") or 0)
    if not cap:
        return None
    tz = ZoneInfo(cfg["timezone"])
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return cap - store.searches_since(start)


def month_start(tz: ZoneInfo, now: datetime | None = None) -> float:
    now = now or datetime.now(tz)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def month_key(tz: ZoneInfo, now: datetime | None = None) -> str:
    return (now or datetime.now(tz)).strftime("%Y-%m")


def save_mode(store, cfg: dict) -> bool:
    """이번 달 절약 모드인가 (예산 초과로 켜지고, !budget off로 이번 달만 끌 수 있음)."""
    tz = ZoneInfo(cfg["timezone"])
    return store.get("budget_save") == month_key(tz) and store.get("budget_override") != month_key(tz)


def check(store, cfg: dict, now: datetime | None = None) -> list[str]:
    """예산 점검 → 보낼 알림. 80% 경고와 100% 절약 모드 전환은 달마다 한 번."""
    bc = _cfg(cfg)
    limit = float(bc.get("monthly_usd") or 0)
    if not limit:
        return []
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    mk = month_key(tz, now)
    spent, by_role = cost_since(store, cfg, month_start(tz, now))
    ratio = spent / limit
    msgs = []
    top = ", ".join(f"{k} ${v:.2f}" for k, v in sorted(by_role.items(), key=lambda x: -x[1])[:3])
    if ratio >= 1.0 and store.get("budget_save") != mk:
        store.put("budget_save", mk)
        if bc.get("save_mode", True):
            msgs.append(f"🛑 **이번 달 API 비용 ${spent:.2f} / ${limit:.0f}** — 예산을 넘어 **절약 모드**로 바꿨어요.\n"
                        "깊은 모드·어드바이저·플레이테스트·자기 개선을 멈추고 기본 대화와 알림만 해요. "
                        f"다음 달 1일에 자동으로 풀려요. 지금 풀려면 `!budget off`\n많이 쓴 곳: {top}")
        else:
            msgs.append(f"🛑 **이번 달 API 비용 ${spent:.2f} / ${limit:.0f}** — 예산을 넘었어요. 많이 쓴 곳: {top}")
    elif ratio >= float(bc.get("warn_ratio", 0.8)) and store.get("budget_warned") != mk and ratio < 1.0:
        store.put("budget_warned", mk)
        msgs.append(f"⚠️ 이번 달 API 비용 **${spent:.2f} / ${limit:.0f}** ({ratio:.0%}). 많이 쓴 곳: {top}")
    return msgs


def status(store, cfg: dict) -> str:
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    today, _ = cost_since(store, cfg, now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month, by_role = cost_since(store, cfg, month_start(tz, now))
    limit = float(_cfg(cfg).get("monthly_usd") or 0)
    day = now.day
    days_in_month = (now.replace(month=now.month % 12 + 1, day=1) if now.month < 12
                     else now.replace(year=now.year + 1, month=1, day=1)).toordinal() - now.replace(day=1).toordinal()
    forecast = month / day * days_in_month if day else month
    lines = [f"💵 오늘 ${today:.3f} · 이번 달 ${month:.2f}" + (f" / ${limit:.0f} ({month / limit:.0%})" if limit else ""),
             f"   월말 예상 ${forecast:.2f}" + (" · **절약 모드**" if save_mode(store, cfg) else "")]
    if by_role:
        lines.append("   " + ", ".join(f"{k} ${v:.2f}" for k, v in sorted(by_role.items(), key=lambda x: -x[1])[:5]))
    cap = int(_cfg(cfg).get("daily_web_searches") or 0)
    today_n = store.searches_since(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    month_n = store.searches_since(month_start(tz, now))
    lines.append(f"🔎 웹 검색: 오늘 {today_n}회" + (f" / {cap}" if cap else "") + f" · 이번 달 {month_n}회")
    return "\n".join(lines)


def set_override(store, cfg: dict, off: bool) -> str:
    tz = ZoneInfo(cfg["timezone"])
    mk = month_key(tz)
    if off:
        store.put("budget_override", mk)
        return "이번 달은 절약 모드를 껐어요. 비용 알림은 계속 와요."
    store.put("budget_override", "")
    store.put("budget_save", mk)
    return "절약 모드를 켰어요 (이번 달)."

