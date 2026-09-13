"""卡牌效果编译层 —— 文本语法表 → CardEffect IR → 增量缓存(2026-09-13 P1)。

编译器管线(docs/EFFECTS_DESIGN.md):
    cards.json 的 text/type 字段 ──GRAMMAR_FAMILIES 规则族表──► CardEffect IR
    ──EffectCache──► 求值
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
class ManaGain:
    """回费事实: 获得法力水晶数(临时/复原均按等效回费计, 不区分持续期)。"""
    amount: int


@dataclass(frozen=True)
class CostDown:
    """减费事实: 法力值消耗减少 N 点(建造水晶塔/生命缚誓者的礼物/伺机待发族)。
    scope 记录文本里的 归属:类目 词(hand:法术 / next:星灵 …), 由措辞直接提取,
    供后续按类目精细化; 当前求值只用 amount(等效回费面值)。"""
    amount: int
    scope: str = ""


@dataclass(frozen=True)
class CostUp:
    """加费事实: 法力值消耗增加 N 点(死灵光环族)。与 CostDown 分离,
    避免误入回费口径; 文本不区分指向敌我, 由消费方按对局取舍。"""
    amount: int


@dataclass(frozen=True)
class Draw:
    """抽牌事实: 数量(固定); scope 记归属/限定: both=双方玩家,
    opponent=对手抽, deck=从牌库抽, lowest/highest=按费用挑, 其余=类目词
    (法术/武器/随从…)或空。条件句(如果/每当)不建模, 与既有族同口径。"""
    amount: int
    scope: str = ""


@dataclass(frozen=True)
class Armor:
    """护甲事实: 获得护甲值($d 形式=英雄技能模板值)。"""
    amount: int


@dataclass(frozen=True)
class Buff:
    """属性增益事实: +攻/+血; 0=该侧无数值(+N攻击力 / +N生命值)。"""
    attack: int = 0
    health: int = 0


@dataclass(frozen=True)
class Summon:
    """召唤事实: 数量与模板属性(文本未给=0); scope="opponent"=为对手召唤。"""
    count: int
    attack: int = 0
    health: int = 0
    scope: str = ""


@dataclass(frozen=True)
class SpellPower:
    """法术伤害增益事实: 打出后法强 +N(虚灵改装师附魔/法强随从)。文本只要含
    "法术伤害+N" 即如实编译; 抉择条件分支(如顺水漂流)是否计入由消费方按
    choose_one 标记取舍, IR 不裁决。"""
    amount: int


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


# ---------- 语法规则族表(唯一可扩展点: 加一行规则 = 加一种效果措辞) ----------
# 同族 = 同一数值语义的不同措辞, 特异性排序先命中先用; 异族各取一条 ——
# 一张卡多种效果(如 法力虹吸: 伤害+减费)全部入 IR, 不再只认第一条。

_HITS = {"两次": 2, "三次": 3, "四次": 4}

# 语法表用的汉字数词(水晶/张数/召唤数; 仅本表使用)
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(tok: str) -> int:
    """规则表数值: 阿拉伯数字或单个汉字数词(表内措辞只出现单字数词)。"""
    return int(tok) if tok.isdigit() else _CN_NUM[tok]


# 类目词可含 <b>关键词</b> 标签(VAC_409e "你的下一个<b>战吼</b>随从"), 词类允许内嵌标签
_CAT = r"((?:<[^>]+>|[^<，。;；]){1,6}?)"


GRAMMAR_FAMILIES: list[tuple[str, list]] = [
    # 伤害族: $N=引擎标记的可加成伤害(scaled), 裸数字=固定伤害(scaled=False,
    # 如虚灵改装师"造成1点伤害") —— 解析器只报告事实, 是否计入斩杀由
    # analysis.burst_damage 按口径决定。同族特异性排序先命中先用。
    ("damage", [
        (re.compile(r"对一个敌人造成(\$?)(\d+)点伤害(两次|三次|四次)?"),
         lambda m: Damage(int(m.group(2)), _HITS.get(m.group(3) or "", 1),
                          scaled=bool(m.group(1)))),
        (re.compile(r"对所有敌人造成(\$?)(\d+)点伤害"),
         lambda m: Damage(int(m.group(2)), scope="all_enemies",
                          scaled=bool(m.group(1)))),
        (re.compile(r"造成(\$?)(\d+)点伤害，随机"),
         lambda m: Damage(int(m.group(2)), scope="random_split",
                          scaled=bool(m.group(1)))),
        (re.compile(r"造成(\$?)(\d+)点伤害"),
         lambda m: Damage(int(m.group(2)), scaled=bool(m.group(1)))),
        # 对所有随从/角色(VAC_953 奉献形): scope 区别于打脸系, 供斩杀口径细分
        (re.compile(r"对所有(?:敌方随从|随从|角色)造成\s*(\$?)(\d+)点伤害"),
         lambda m: Damage(int(m.group(2)), scope="all", scaled=bool(m.group(1)))),
    ]),
    ("heal", [
        (re.compile(r"恢复\$(\d+)点生命"),
         lambda m: Heal(int(m.group(1)))),
        # #N=引擎"固定值"占位(英雄技能), 与裸数字同义(治疗不吃法强)
        (re.compile(r"恢复#?(\d+)点生命值"),
         lambda m: Heal(int(m.group(1)))),
    ]),
    # 回费(激活/幸运币/复原等): 水晶数用汉字或阿拉伯数字, 文本内含换行/空格/「空的」
    ("mana", [
        (re.compile(r"(?:在本回合中[，,]?\s*)?获得\s*([一二两三四五六七八九十\d])\s*个"
                    r"\s*空?\s*的?\s*法力水晶"),
         lambda m: ManaGain(_num(m.group(1)))),
        (re.compile(r"复原\s*([一二两三四五六七八九十\d])\s*个\s*空?\s*的?\s*法力水晶"),
         lambda m: ManaGain(_num(m.group(1)))),
    ]),
    # 减费族: 手牌中X牌 / (你所施放的)下一个X / 你的下一张X牌 —— 的 字可省;
    # 2026-09-13 扩(出现牌驱动): 附慕裸句(法力值消耗减少(N)点)/本牌自减/EN
    # "Costs (N) less"/抽到的牌/使其(→target, 不入回费口径)/你的X类目。
    ("cost_down", [
        (re.compile(r"使你手牌中(?:所有|一张随机)?([^\s，。;；]{1,4}?)牌的?"
                    r"法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"hand:{m.group(1)}")),
        (re.compile(r"(?:在本回合中[，,]?\s*)?你[所]?施?放的?下一个([^\s，。]{1,4}?)"
                    r"(?:牌)?的?法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"next:{m.group(1)}")),
        (re.compile(r"你的下一张([^\s，。]{1,4}?)牌的?法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"next:{m.group(1)}")),
        (re.compile(r"使你手牌中的?(?:所有|随机一张|一张随机)?" + _CAT +
                    r"牌的法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"hand:{m.group(1)}")),
        (re.compile(r"你的下一(?:张|个)" + _CAT + r"牌?的法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"next:{m.group(1)}")),
        (re.compile(r"使抽到的牌法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(1)), "drawn")),
        (re.compile(r"你的" + _CAT + r"的法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(2)), f"your:{m.group(1)}")),
        (re.compile(r"(?:使其|它的|其)法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(1)), "target")),
        (re.compile(r"本牌的?法力值消耗(?:便)?减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(1)), "self")),
        (re.compile(r"^法力值消耗减少（(\d+)）点"),
         lambda m: CostDown(int(m.group(1)), "self")),
        (re.compile(r"Costs\s*\((\d+)\)\s*less"),
         lambda m: CostDown(int(m.group(1)), "self")),
    ]),
    # 抽牌族: 前置否定镜 (?<!每)(?<!手) 挡掉"每抽一张牌(动态减费条件)"与
    # "对手抽…牌(对手侧事件)", 抽牌事实只认牌自己的抽牌效果
    ("draw", [
        (re.compile(r"(?:每个玩家|双方玩家)抽([一二两三四五六七八九十\d])张牌"),
         lambda m: Draw(_num(m.group(1)), "both")),
        (re.compile(r"(?<!在)(?<!手)你的对手抽([一二两三四五六七八九十\d])张牌"),
         lambda m: Draw(_num(m.group(1)), "opponent")),
        (re.compile(r"抽(?:取)?你法力值消耗(最低|最高)的牌"),
         lambda m: Draw(1, {"最低": "lowest", "最高": "highest"}[m.group(1)])),
        (re.compile(r"从你的牌库中抽([一二两三四五六七八九十\d])张"),
         lambda m: Draw(_num(m.group(1)), "deck")),
        (re.compile(r"(?<!每)(?<!手)抽(?:取|出)?([一二两三四五六七八九十\d])张"),
         lambda m: Draw(_num(m.group(1)))),
    ]),
    # 属性增益族(+X/+X 与 +N攻 / +N血; $a=英雄技能攻击力模板)
    ("buff", [
        (re.compile(r"\+(\d+)\s*/\s*\+(\d+)"),
         lambda m: Buff(int(m.group(1)), int(m.group(2)))),
        (re.compile(r"[+＋]\$a(\d+)攻击力"),
         lambda m: Buff(int(m.group(1)), 0)),
        (re.compile(r"[+＋](\d+)攻击力"),
         lambda m: Buff(int(m.group(1)), 0)),
        (re.compile(r"[+＋](\d+)生命值"),
         lambda m: Buff(0, int(m.group(1)))),
    ]),
    # 法强增益族: <b> 标签/「使其获得」前缀容错(虚灵改装师战吼附魔);
    # 抉择分支(顺水漂流)照常打标, 消费方按 choose_one 取舍(见类注释)
    ("spellpower", [
        (re.compile(r"法术伤害\s*[+＋]\s*(\d+)"),
         lambda m: SpellPower(int(m.group(1)))),
    ]),
    # 召唤族: 数量=单字数词+量词, 属性模板 N/M 可省(复制/图腾类)
    ("summon", [
        (re.compile(r"为你的对手召唤([一二两三四五六七八九十])(?:个|只|条|名)"
                    r"(?:[^，。;；]*?(\d+)\s*/\s*(\d+))?"),
         lambda m: Summon(_CN_NUM[m.group(1)], int(m.group(2) or 0),
                          int(m.group(3) or 0), scope="opponent")),
        (re.compile(r"召唤([一二两三四五六七八九十])(?:个|只|条|名)"
                    r"(?:[^，.。;；]*?(\d+)\s*/\s*(\d+))?"),
         lambda m: Summon(_CN_NUM[m.group(1)], int(m.group(2) or 0),
                          int(m.group(3) or 0))),
    ]),
    # 护甲族: $d=英雄技能模板值; 其余裸数字
    ("armor", [
        (re.compile(r"获得\$d(\d+)点护甲值"),
         lambda m: Armor(int(m.group(1)))),
        (re.compile(r"[+＋]\$d(\d+)点?护甲值"),
         lambda m: Armor(int(m.group(1)))),
        (re.compile(r"获得(\d+)点护甲值"),
         lambda m: Armor(int(m.group(1)))),
    ]),
    # 加费族(死灵光环/前沿哨所族): 与减费镜像, amount=增加点数
    ("cost_up", [
        (re.compile(r"法力值消耗增加（(\d+)）点"),
         lambda m: CostUp(int(m.group(1)))),
    ]),
]

# 机制标记表(文本通道): 卡牌数据没有对应标签时, 按显示文本措辞识别。
# 加一行 = 认识一种新措辞(数据级标签见 MECHANIC_MARKS, 优先于本表)。
MECHANICS = [
    (re.compile(r"置于牌库底"), "deck_bottom"),   # 发现其余选项置底(波涛形塑类:
                                                 # 引擎只有泛化 DISCOVER, 无置底标签)
    (re.compile(r"探底"), "deck_bottom"),          # 措辞兜底(DREDGE 标签通常已在数据里)
    (re.compile(r"每当你施放[^。]*抽一张牌"), "cast_draw"),  # 拍卖师族: 施法抽牌触发
                                                 # 注意: 触发句同时会被抽牌族裸规则
                                                 # 误标一条 Draw(1)(否定镜只看"抽"
                                                 # 前一字, 挡不住"后，抽"); 消费方
                                                 # 忽略 Draw 即可, 触发语境镜留待下批
]

# 机制标记表(数据通道): HsJson cards.json 的 mechanics[](引擎审核的统一标签)
# → IR 机制标记。加一行 = 认识一种引擎机制, 全部该类卡自动生效, 不逐卡写死。
# 收录口径(2026-09-13, 出现牌 mechanics[] 词表盘点): 玩家可见的关键词机制;
# 引擎内部旗标(TRIGGER_VISUAL/AURA/ENCHANTMENT_INVISIBLE/TAG_ONE_TURN_EFFECT/
# AFFECTED_BY_SPELL_POWER/ImmuneToSpellpower/CANT_*/InvisibleDeathrattle)不打标。
MECHANIC_MARKS = {
    "DREDGE": "deck_bottom",     # 探底: 选项即当前牌库底三张(水栖形态类)
    "CHARGE": "charge",          # 冲锋
    "RUSH": "rush",              # 突袭
    "TAUNT": "taunt",            # 嘲讽
    "DIVINE_SHIELD": "divine_shield",   # 圣盾
    "WINDFURY": "windfury",      # 风怒
    "LIFESTEAL": "lifesteal",    # 吸血
    "STEALTH": "stealth",        # 潜行
    "FREEZE": "freeze",          # 冻结
    "REBORN": "reborn",          # 复生
    "DISCOVER": "discover",      # 发现
    "CHOOSE_ONE": "choose_one",  # 抉择
    "BATTLECRY": "battlecry",    # 战吼
    "DEATHRATTLE": "deathrattle",       # 亡语
    "SECRET": "secret",          # 奥秘
    "OUTCAST": "outcast",        # 流放
    "SPELLBURST": "spellburst",  # 法术迸发
    "TRADEABLE": "tradeable",    # 可交易
    "QUEST": "quest",            # 任务
    "OVERLOAD": "overload",      # 过载
    "COMBO": "combo",            # 连击
    "INSPIRE": "inspire",        # 激励
    "START_OF_GAME_KEYWORD": "start_of_game",   # 开局触发(对战开始时)
    "ELUSIVE": "elusive",        # 扰魔(不能被指向)
    "CORRUPT": "corrupt",        # 腐蚀
    "FORGE": "forge",            # 锻造
    "TITAN": "titan",            # 泰坦
    "COLOSSAL": "colossal",      # 巨型
    "STARSHIP": "starship",      # 星舰
    "STARSHIP_PIECE": "starship_piece",  # 星舰组件
}

# 编译语义版本: 语法表/机制表语义变化时 +1, 强制缓存全量重编译
COMPILER_VERSION = "7"


def source_hash(card: dict) -> str:
    raw = json.dumps([COMPILER_VERSION, card.get("id"), card.get("text"),
                      card.get("type"), card.get("mechanics")],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compile_card(card: dict) -> CardEffect:
    """文本 + 静态字段 → IR。同族措辞先命中先用(特异性序); 异族各取一条
    —— 多效果卡(伤害+减费等)全量入 IR; 全部未覆盖 → Unknown(诚实降级)。"""
    text = card.get("text") or ""
    effects = []
    for _family, rules in GRAMMAR_FAMILIES:
        for rx, build in rules:
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
        elif isinstance(e, ManaGain):
            out.append({"k": "mana", "amount": e.amount})
        elif isinstance(e, CostDown):
            out.append({"k": "cost_down", "amount": e.amount, "scope": e.scope})
        elif isinstance(e, CostUp):
            out.append({"k": "cost_up", "amount": e.amount})
        elif isinstance(e, Draw):
            out.append({"k": "draw", "amount": e.amount, "scope": e.scope})
        elif isinstance(e, Armor):
            out.append({"k": "armor", "amount": e.amount})
        elif isinstance(e, Buff):
            out.append({"k": "buff", "attack": e.attack, "health": e.health})
        elif isinstance(e, Summon):
            out.append({"k": "summon", "count": e.count, "attack": e.attack,
                        "health": e.health, "scope": e.scope})
        elif isinstance(e, SpellPower):
            out.append({"k": "spellpower", "amount": e.amount})
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
        elif k == "mana":
            out.append(ManaGain(int(it["amount"])))
        elif k == "cost_down":
            out.append(CostDown(int(it["amount"]), it.get("scope", "")))
        elif k == "cost_up":
            out.append(CostUp(int(it["amount"])))
        elif k == "draw":
            out.append(Draw(int(it["amount"]), it.get("scope", "")))
        elif k == "armor":
            out.append(Armor(int(it["amount"])))
        elif k == "buff":
            out.append(Buff(int(it.get("attack", 0)), int(it.get("health", 0))))
        elif k == "summon":
            out.append(Summon(int(it["count"]), int(it.get("attack", 0)),
                              int(it.get("health", 0)), it.get("scope", "")))
        elif k == "spellpower":
            out.append(SpellPower(int(it["amount"])))
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
