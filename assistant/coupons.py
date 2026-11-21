"""기프티콘 지갑: 스크린샷 비서가 알아본 기프티콘을 원본 이미지와 함께 보관한다.

매장에서 "스벅 쿠폰 보여줘" → 원본(바코드) 이미지를 디스코드로 보냄. "썼어" → 지갑에서 빼고 To Do 알림도 완료.
"""
from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

from . import todo
from .tools.base import tool

DIR = Path(os.environ.get("DATA_DIR", "data")) / "coupons"
SCHEMA = """CREATE TABLE IF NOT EXISTS coupons (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand TEXT, item TEXT, code TEXT, expires TEXT,
  image TEXT, todo_id TEXT, created REAL, used REAL)"""


def init(store) -> None:
    store._exec(SCHEMA)


def add(store, info: dict, image: bytes | None, ext: str = "png") -> tuple[int, bool]:
    """(쿠폰 번호, 새로 추가됐는지). 같은 쿠폰 번호가 이미 있으면 그걸 돌려준다."""
    init(store)
    code = (info.get("code") or "").replace(" ", "").replace("-", "")
    if code:
        hit = store._exec("SELECT id FROM coupons WHERE replace(replace(code,' ',''),'-','')=? AND used IS NULL", (code,))
        if hit:
            return hit[0][0], False
    title = info.get("title", "")
    brand = info.get("store") or (title.split()[0] if title else "")
    cid = store.insert("INSERT INTO coupons (brand,item,code,expires,image,todo_id,created) VALUES (?,?,?,?,?,?,?)",
                       (brand, title, info.get("code", ""), (info.get("due") or "")[:10], "", "", time.time()))
    if image:
        DIR.mkdir(parents=True, exist_ok=True)
        path = DIR / f"{cid}.{ext}"
        path.write_bytes(image)
        store._exec("UPDATE coupons SET image=? WHERE id=?", (str(path), cid))
    return cid, True


def link_todo(store, cid: int, todo_id: str) -> None:
    store._exec("UPDATE coupons SET todo_id=? WHERE id=?", (todo_id, cid))


def active(store, today: date) -> list[dict]:
    init(store)
    rows = store._exec("SELECT id,brand,item,code,expires,image,todo_id FROM coupons WHERE used IS NULL")
    out = []
    for i, b, it, c, e, img, tid in rows:
        left = (date.fromisoformat(e) - today).days if e else None
        out.append({"id": i, "brand": b, "item": it, "code": c, "expires": e, "days_left": left, "image": img, "todo_id": tid})
    return sorted(out, key=lambda x: (x["days_left"] is None, x["days_left"] if x["days_left"] is not None else 0))


def lines(store, today: date) -> list[str]:
    out = []
    for c in active(store, today):
        d = c["days_left"]
        when = "기한 없음" if d is None else ("만료됨" if d < 0 else ("오늘 만료" if d == 0 else f"D-{d}"))
        out.append(f"`#{c['id']}` {c['item'] or c['brand']} — {c['expires'] or ''} ({when})")
    return out


def find(store, today: date, query: str) -> list[dict]:
    q = str(query).strip().lstrip("#")
    items = active(store, today)
    if q.isdigit():
        return [c for c in items if c["id"] == int(q)]
    ql = q.lower()
    return [c for c in items if ql in (c["item"] or "").lower() or ql in (c["brand"] or "").lower()]


async def mark_used(cfg: dict, store, cid: int) -> str:
    row = store._exec("SELECT item, todo_id FROM coupons WHERE id=?", (cid,))
    if not row:
        return "그 쿠폰이 없어요."
    store._exec("UPDATE coupons SET used=? WHERE id=?", (time.time(), cid))
    note = ""
    if row[0][1] and todo.enabled():
        try:
            await todo.complete(cfg, store, row[0][1])
            note = " (만료 알림도 완료 처리)"
        except Exception:
            note = " (To Do 알림은 이미 없어요)"
    return f"🎁 #{cid} {row[0][0]} 사용 처리했어요{note}"


# ───────────── 지휘자 도구 ─────────────
@tool("coupon_list", "기프티콘 지갑의 안 쓴 쿠폰 목록 (만료 임박 순). 브랜드로 근처 매장을 찾을 때도 먼저 이걸 본다.",
      {"properties": {}}, group="money")
async def coupon_list(ctx):
    return lines(ctx.store, date.today()) or "지갑이 비어 있어요. 기프티콘 스크린샷을 보내면 저장돼요."


@tool("coupon_show", "기프티콘 원본 이미지(바코드)를 디스코드로 보낸다. query는 쿠폰 번호나 브랜드·상품 이름 일부.",
      {"properties": {"query": {"type": "string"}}, "required": ["query"]}, group="money")
async def coupon_show(ctx, query: str):
    hits = find(ctx.store, date.today(), query)
    if not hits:
        return "맞는 쿠폰이 없어요. coupon_list로 확인하세요."
    if len(hits) > 1:
        return "여러 개가 맞아요. 번호로 다시 골라 주세요: " + "; ".join(f"#{c['id']} {c['item']}" for c in hits)
    c = hits[0]
    send = ctx.extras.get("send_file")
    if not c["image"] or not Path(c["image"]).exists() or not send:
        return f"#{c['id']} {c['item']} — 이미지가 없어요. 쿠폰 번호: {c['code'] or '없음'}"
    await send(Path(c["image"]), f"🎁 #{c['id']} {c['item']} (만료 {c['expires'] or '?'})" + (f"\n번호: {c['code']}" if c["code"] else ""))
    return "이미지를 보냈어요. (답변에서 다시 보낼 필요 없음)"


@tool("coupon_used", "기프티콘을 사용 처리한다 (지갑에서 빼고 만료 알림 To Do도 완료). query는 번호나 이름 일부.",
      {"properties": {"query": {"type": "string"}}, "required": ["query"]}, group="money")
async def coupon_used(ctx, query: str):
    hits = find(ctx.store, date.today(), query)
    if len(hits) != 1:
        return "맞는 쿠폰이 없어요." if not hits else "여러 개가 맞아요: " + "; ".join(f"#{c['id']} {c['item']}" for c in hits)
    return await mark_used(ctx.cfg, ctx.store, hits[0]["id"])
