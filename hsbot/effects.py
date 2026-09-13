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
class Mechanic:
    """非数值机制标记(通用): 由机制表按文本模式统一编译, 消费方只认 kind,
    不逐卡写死。kind: "deck_bottom" = 未选项即当前牌库底(置于牌库底/探底)。"""
    kind: str


@dataclass(frozen=True)
class Unknown:
    raw: str               # 语法表未覆盖: 诚实降级, 不产生标注


@dataclass(frozen=True)
class CardEffect:
    card_id: str
    cardtype: str
    effects: tuple                 # tuple[Damage | Heal | Mechanic | Unknown, ...]
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

# 机制标记表(文本通道): 卡牌数据没有对应标签时, 按显示文本措辞识别。
# 加一行 = 认识一种新措辞(数据级标签见 MECHANIC_MARKS, 优先于本表)。
MECHANICS = [
    (re.compile(r"置于牌库底"), "deck_bottom"),   # 发现其余选项置底(波涛形塑类:
                                                 # 引擎只有泛化 DISCOVER, 无置底标签)
    (re.compile(r"探底"), "deck_bottom"),          # 措辞兜底(DREDGE 标签通常已在数据里)
]

# 机制标记表(数据通道): HsJson cards.json 的 mechanics[](引擎审核的统一标签)
# → IR 机制标记。加一行 = 认识一种引擎机制, 全部该类卡自动生效, 不逐卡写死。
MECHANIC_MARKS = {
    "DREDGE": "deck_bottom",     # 探底: 选项即当前牌库底三张(水栖形态类)
}

# 编译语义版本: 语法表/机制表语义变化时 +1, 强制缓存全量重编译
COMPILER_VERSION = "2"


def source_hash(card: dict) -> str:
    raw = json.dumps([COMPILER_VERSION, card.get("id"), card.get("text"),
                      card.get("type"), card.get("mechanics")],
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
    marks = {MECHANIC_MARKS[m] for m in (card.get("mechanics") or [])
             if m in MECHANIC_MARKS}
    for rx, kind in MECHANICS:
        if rx.search(text):
            marks.add(kind)
    effects.extend(Mechanic(kind) for kind in sorted(marks))
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
        elif isinstance(e, Mechanic):
            out.append({"k": "mechanic", "kind": e.kind})
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
        elif k == "mechanic":
            out.append(Mechanic(it.get("kind", "")))
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
