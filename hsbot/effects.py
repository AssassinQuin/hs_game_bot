"""卡牌效果编译层 —— 文本语法表 → CardEffect IR → 增量缓存(2026-09-13 P1)。

编译器管线(docs/EFFECTS_DESIGN.md):
    cards.json 的 text/type 字段 ──GRAMMAR 规则表──► CardEffect IR ──EffectCache──► 求值
增量: 按 (id+text+type) 的 sha1 指纹, 未变化的牌直接复用缓存; 持久化到
data/cache/effects.json, 重启零重编译。
本层不做输出格式化, 不维护对局状态(职责边界见 docs/EFFECTS_DESIGN.md)。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .persist import atomic_write_text

# ---------- IR(不可变纯数据; 规则全部在语法表与求值器) ----------

@dataclass(frozen=True)
class Damage:
    base: int              # $N 的 N
    hits: int = 1          # 「两次」→ 2
    scope: str = "single"  # single / all_enemies / random_split
    scaled: bool = True    # 是否吃法强


@dataclass(frozen=True)
class Heal:
    base: int
    hits: int = 1


@dataclass(frozen=True)
class Unknown:
    raw: str               # 语法表未覆盖: 诚实降级, 不产生标注


@dataclass(frozen=True)
class CardEffect:
    card_id: str
    cardtype: str
    effects: tuple                 # tuple[Damage | Heal | Unknown, ...]
    source_hash: str               # 增量键: 参与编译的原始字段指纹


# ---------- 语法规则表(唯一可扩展点: 加一行规则 = 加一种效果语义) ----------

_HITS = {"两次": 2, "三次": 3, "四次": 4}

GRAMMAR = [
    # 按特异性排序, 先命中先得
    (re.compile(r"对一个敌人造成\$(\d+)点伤害(两次|三次|四次)?"),
     lambda m: Damage(int(m.group(1)), _HITS.get(m.group(2) or "", 1))),
    (re.compile(r"对所有敌人造成\$(\d+)点伤害"),
     lambda m: Damage(int(m.group(1)), scope="all_enemies")),
    (re.compile(r"造成\$(\d+)点伤害，随机"),
     lambda m: Damage(int(m.group(1)), scope="random_split")),
    (re.compile(r"造成\$(\d+)点伤害"),
     lambda m: Damage(int(m.group(1)))),
    (re.compile(r"恢复\$(\d+)点生命"),
     lambda m: Heal(int(m.group(1)))),
]


def source_hash(card: dict) -> str:
    raw = json.dumps([card.get("id"), card.get("text"), card.get("type")],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compile_card(card: dict) -> CardEffect:
    """文本 + 静态字段 → IR。语法表未覆盖 → Unknown(诚实降级)。"""
    text = card.get("text") or ""
    effects = []
    for rx, build in GRAMMAR:
        m = rx.search(text)
        if m:
            effects.append(build(m))
            break
    if not effects:
        effects.append(Unknown(text[:60]))
    return CardEffect(card_id=card.get("id") or "",
                      cardtype=card.get("type") or "",
                      effects=tuple(effects), source_hash=source_hash(card))


# ---------- 增量缓存 ----------

def _effects_to_json(effects: tuple) -> list[dict]:
    out = []
    for e in effects:
        if isinstance(e, Damage):
            out.append({"k": "damage", "base": e.base, "hits": e.hits,
                        "scope": e.scope, "scaled": e.scaled})
        elif isinstance(e, Heal):
            out.append({"k": "heal", "base": e.base, "hits": e.hits})
        else:
            out.append({"k": "unknown"})
    return out


def _effects_from_json(items: list[dict]) -> tuple:
    out = []
    for it in items:
        k = it.get("k")
        if k == "damage":
            out.append(Damage(int(it["base"]), int(it.get("hits", 1)),
                              it.get("scope", "single"), bool(it.get("scaled", True))))
        elif k == "heal":
            out.append(Heal(int(it["base"]), int(it.get("hits", 1))))
        else:
            out.append(Unknown(""))
    return tuple(out)


class EffectCache:
    """IR 增量缓存: 内存 dict 命中 + effects.json 磁盘持久化(dirty 回写)。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self._memo: dict[str, CardEffect] = {}
        self._dirty = False
        if self.path and self.path.exists():
            try:
                for cid, item in json.loads(
                        self.path.read_text(encoding="utf-8")).items():
                    self._memo[cid] = CardEffect(
                        card_id=cid, cardtype=item.get("type", ""),
                        effects=_effects_from_json(item.get("fx", [])),
                        source_hash=item.get("h", ""))
            except Exception:  # noqa: BLE001  坏缓存等价于无缓存
                self._memo = {}

    def get_or_compile(self, card: dict | None) -> CardEffect | None:
        if not card or not card.get("id"):
            return None
        cid = card["id"]
        h = source_hash(card)
        hit = self._memo.get(cid)
        if hit is not None and hit.source_hash == h:
            return hit                                 # 增量: 未变化直接复用
        ir = compile_card(card)                        # 只编译新牌/文本变化的牌
        self._memo[cid] = ir
        self._dirty = True
        return ir

    def __contains__(self, cid: str) -> bool:
        return cid in self._memo

    def save(self) -> None:
        if not (self._dirty and self.path):
            return
        data = {cid: {"h": ir.source_hash, "type": ir.cardtype,
                      "fx": _effects_to_json(ir.effects)}
                for cid, ir in self._memo.items()}
        atomic_write_text(self.path,
                          json.dumps(data, ensure_ascii=False))
        self._dirty = False
