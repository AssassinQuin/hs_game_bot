"""渲染层 —— 事件流 → 链路行 / 快照块 / 终局行(纯函数, 不持有状态)。

链路行设计(注册制): 每种事件一个渲染函数, 用 @chain_renderer(kind) 注册;
chain_line 只负责行首([T回合·阵营])与分发, 不含任何具体格式。
新增一种事件的完整步骤:
  1. store 衍生出事件(dict, kind="xxx", 附带渲染所需字段);
  2. 本文件加一个 @chain_renderer("xxx") 函数, 返回行内容(不含行首);
  3. 颜色无需处理 —— 悬浮窗按行首人物与事件类型自动着色。
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from hearthstone.enums import GameTag

from .carddb import CardDB
from .knowledge import DeckKnowledge, Ledger
from .mulligan_ai import class_zh
from .store import (GameStore, atk, hp_total, is_generated, is_taunt,
                    zone_pos)

# ================= 链路行: 注册制渲染器 =================

_CHAIN_RENDERERS: dict[str, Callable[[dict, CardDB], str]] = {}


def chain_renderer(kind: str) -> Callable:
    """注册一种事件的链路行渲染函数, 签名 (evt, carddb) -> str(不含行首)。"""
    def deco(fn: Callable[[dict, CardDB], str]) -> Callable[[dict, CardDB], str]:
        _CHAIN_RENDERERS[kind] = fn
        return fn
    return deco


def _side(actor, friendly) -> str:
    if actor is not None and actor == friendly:
        return "我"
    if friendly is not None and actor is not None:
        return "对面"
    return "?"


def _head(evt: dict) -> str:
    return f"[T{evt.get('turn', 0)}·{_side(evt.get('actor'), evt.get('friendly'))}]"


def chain_line(evt: dict, carddb: CardDB) -> str:
    kind = evt.get("kind")
    renderer = _CHAIN_RENDERERS.get(kind)
    if renderer is None:                      # text/未知种类: msg 兜底
        return f"{_head(evt)} {evt.get('msg', '')}"
    return f"{_head(evt)} {renderer(evt, carddb)}"


@chain_renderer("mulligan_offer")
def _render_mulligan_offer(evt: dict, carddb: CardDB) -> str:
    """留牌建议(有 advice 富化)高亮行; 无建议 → 回退"起手可留"。"""
    adv = evt.get("advice")
    if not adv:
        return evt.get("msg", "")

    def fmt(cid):
        info = adv.get("per_card", {}).get(cid) or {}
        gain = info.get("gain")
        name = str(info.get("name") or cid)
        return f"{name}({gain * 100:+.1f}%)" if gain is not None else name

    coin_txt = "后手" if adv.get("coin") else "先手"
    keep = "、".join(fmt(c) for c in adv.get("keep") or []) or "无"
    drop = "、".join(fmt(c) for c in adv.get("drop") or []) or "无"
    return (f"【留牌建议·vs{class_zh(adv.get('opp_class', ''))}·{coin_txt}】"
            f"留 {keep} │ 换 {drop}")


@chain_renderer("play")
def _render_play(evt: dict, carddb: CardDB) -> str:
    base, tag = evt.get("cost_base"), evt.get("cost_tag")
    if base is None and tag is None:
        cost = "(?费)"
    elif tag is None:
        cost = f"({base}费)"
    elif base is None:
        cost = f"(实付{tag}费)"
    elif tag == base:
        cost = f"({base}费)"
    else:
        cost = f"({tag}费,原{base}费)"
    sub = evt.get("suboption")
    if sub is not None:
        cost += f" 抉择{sub + 1}"
        sub_cid = evt.get("suboption_card_id")   # 抉择所选子卡(随事件保存)
        if sub_cid:
            cost += f"·{carddb.name(sub_cid)}"
    left = evt.get("mana_left")
    left_txt = f" 剩{left}费" if left is not None else ""
    pred = evt.get("pred_dmg")               # 解析层产物: {"total": 总伤, "hits": 段数}
    if pred:
        hits = pred.get("hits", 1)
        seg = f"({hits}段)" if hits > 1 else ""
        left_txt += f" → 预计{pred['total']}伤{seg}"
    verb = "使用技能" if evt.get("is_power") else "打出"
    return f"{verb} {carddb.name(evt['card_id'])} {cost}{left_txt}"


@chain_renderer("prepare")
def _render_prepare(evt: dict, carddb: CardDB) -> str:
    return f"预备完成 {carddb.name(evt.get('card_id'))}"


@chain_renderer("discover")
def _render_discover(evt: dict, carddb: CardDB) -> str:
    picked = "、".join(carddb.name(c) for c in evt.get("picked") or []) or "(无)"
    bottom = " → ".join(carddb.name(c) for c in evt.get("bottom") or []) or "(无)"
    return (f"发现({evt.get('src_name', '?')}): 取 {picked}"
            f" │ 置底(按序): {bottom}")


@chain_renderer("draw")
def _render_draw(evt: dict, carddb: CardDB) -> str:
    return f"抽到 {carddb.name(evt['card_id'])}"


@chain_renderer("gain")
def _render_gain(evt: dict, carddb: CardDB) -> str:
    creator = carddb.name(evt.get("creator")) if evt.get("creator") else None
    gen = f"[衍生·{creator}]" if creator and creator != "?" else "[衍生]"
    return f"入手 {carddb.name(evt['card_id'])} {gen}"


@chain_renderer("back_to_deck")
def _render_back_to_deck(evt: dict, carddb: CardDB) -> str:
    return f"置入牌库 {carddb.name(evt['card_id'])}"


@chain_renderer("cost")
def _render_cost(evt: dict, carddb: CardDB) -> str:
    via = "".join("〔%s〕" % carddb.name(v) for v in (evt.get("via") or []))
    return (f"手牌费用变动 {carddb.name(evt['card_id'])} "
            f"{evt['old']}→{evt['new']}费{via}")


@chain_renderer("attack")
def _render_attack(evt: dict, carddb: CardDB) -> str:
    def hero_txt(cid):
        n = carddb.name(cid)
        return f"英雄({n})" if n != "?" else "英雄"

    atk_txt = (hero_txt(evt.get("attacker_card_id")) if evt.get("attacker_is_hero")
               else carddb.name(evt.get("attacker_card_id")))
    if evt.get("target_is_hero"):
        tgt = "敌方英雄" if evt.get("actor") == evt.get("friendly") else "我的英雄"
    else:
        tgt = carddb.name(evt.get("target_card_id"))
    return f"攻击 {atk_txt} → {tgt}"


@chain_renderer("hero_power")
def _render_hero_power(evt: dict, carddb: CardDB) -> str:
    cid = evt.get("card_id")
    name = carddb.name(cid) if cid else f"#{evt.get('eid')}"
    return f"英雄技能 → {name}"


@chain_renderer("shuffle")
def _render_shuffle(evt: dict, carddb: CardDB) -> str:
    return "牌库被洗牌(牌位重排)"


@chain_renderer("trigger")
def _render_trigger(evt: dict, carddb: CardDB) -> str:
    name = carddb.name(evt.get("card_id"))
    if name == "?" and evt.get("eid") is not None:
        name = f"#{evt['eid']}"     # 未揭示实体(对面暗牌附魔等): 报实体号便于对日志
    if "START_OF_GAME" in str(evt.get("keyword") or ""):
        name += "(开局)"            # 藏在牌库里的开局触发(引擎未揭示身份)
    host = evt.get("host")
    if host:
        name += f"〔{carddb.name(host)}〕"   # 连锁: 该触发挂在哪张牌上
    return f"触发 {name}"


@chain_renderer("fatigue")
def _render_fatigue(evt: dict, carddb: CardDB) -> str:
    n = evt.get("count")
    return f"疲劳 第{n}抽(-{n}血)" if n else "疲劳"


@chain_renderer("death")
def _render_death(evt: dict, carddb: CardDB) -> str:
    return f"阵亡 {carddb.name(evt.get('card_id'))}"


@chain_renderer("spellpower")
def _render_spellpower(evt: dict, carddb: CardDB) -> str:
    prev, total = evt.get("prev"), evt.get("total")
    if prev is not None:
        return f"场上法强 {prev}→{total}"
    return f"场上法强 {total}"


# ================= 快照块 / 终局行 =================

def _fmt_counts(cnt: Counter, decklist: dict[str, int], carddb: CardDB,
                with_total: bool) -> str:
    parts = []
    for cid, n in sorted(cnt.items(), key=lambda kv: (-kv[1], carddb.name(kv[0]))):
        name = carddb.name(cid)
        parts.append(f"{name}{n}/{decklist[cid]}" if with_total else f"{name}{n}")
    return " · ".join(parts) if parts else "(无)"


def _board_txt(st: GameStore, key, carddb: CardDB) -> str:
    cells = []
    for e in st.board(key):
        t = "嘲讽" if is_taunt(e) else ""
        cells.append(f"{carddb.name(e.card_id)}({atk(e)}/{hp_total(e)}{t})")
    return " ".join(cells) if cells else "(无)"


_REASON_CN = {"turn_end": "回合结束", "game_end": "终局", "flush": "日志截断"}


def snapshot_line(st: GameStore, game_no: int, reason: str) -> str:
    """单行精简快照(UI 用): 悬浮窗/控制台只看节奏, 完整版见 snapshot_block。"""
    reason_cn = _REASON_CN.get(reason, reason)
    me = st.friendly_key
    if me is None:                          # 友方未解析: 只报局号与原因
        return f"── 第{game_no}局快照({reason_cn}) ──"
    f = st.mana_fields(me)
    opp = st.opponent_key()
    opp_board = len(st.board(opp)) if opp is not None else 0
    sp = st.spellpower(me)
    return (f"── T{st.turn} 第{game_no}局({reason_cn})"
            f" 我:水晶{st.mana_now(me)}/{f['res']} │ 手牌{len(st.hand(me))}"
            f" │ 牌库{st.deck_count(me)} │ 场上{len(st.board(me))}"
            f" │ 对面场上{opp_board}"
            + (f" │ 法强{sp}" if sp else "") + " ──")


def snapshot_block(st: GameStore, led: Ledger | None, *, knowledge: DeckKnowledge | None,
                   deck_name: str, generic: bool, game_no: int,
                   chain_lines: list[str], chain_summary: list[str],
                   carddb: CardDB, reason: str) -> str:
    me, opp = st.friendly_key, st.opponent_key()
    generic = generic or knowledge is None
    mode = st.meta.get("GameType", "?")
    fmt = st.meta.get("FormatType", "")
    reason_cn = _REASON_CN.get(reason, reason)
    lines: list[str] = []

    ft = st.friendly_turn_number() if me is not None else 0
    flag = " │ [通用模式: 非所选卡组]" if generic else ""
    lines.append(f"════════ 完整快照 ════════ 回合T{st.turn}(我的第{ft}回合·{reason_cn})"
                 f" │ 第{game_no}局 │ {mode}·{fmt}{flag}")

    if me is not None:
        hero = st.hero(me)
        hname = carddb.name(hero.card_id) if hero else "?"
        f = st.mana_fields(me)
        ol = f" (过载-{f['overload']})" if f["overload"] else ""
        lines.append(f"我  {st.name(me)} [{hname}] {st.hero_hp(me)}血{st.hero_armor(me)}甲"
                     f" │ 水晶 {st.mana_now(me)}/{f['res']} │ 牌库{st.deck_count(me)}"
                     f" │ 本回合已用{f['used']}费"
                     + (f" │ 法强{st.spellpower(me)}" if st.spellpower(me) else ""))
        lines.append(f"    下一回合: {st.mana_next_turn(me)}费{ol}")
        hs = []
        for i, e in enumerate(st.hand(me), 1):
            base = carddb.cost(e.card_id)
            tag = e.tags.get(GameTag.COST)
            cost = tag if tag is not None else (base if base is not None else "?")
            mark = "⬇" if (tag is not None and base is not None and tag < base) else ""
            gen = "[衍生]" if is_generated(e) else ""
            fx = "".join("〔%s〕" % carddb.name(x) for x in st.enchantments_on(e.id))
            hs.append(f"{i}{carddb.name(e.card_id)}({cost}费{mark}){gen}{fx}")
        lines.append(f"    手牌{len(hs)}(位置序): {' '.join(hs) if hs else '(空)'}")
        lines.append(f"    场面: {_board_txt(st, me, carddb)}"
                     f"   墓地:{st.graveyard_count(me)}张 奥秘:{st.secret_count(me)}")

    if opp is not None:
        hero = st.hero(opp)
        hname = carddb.name(hero.card_id) if hero else "?"
        of = st.mana_fields(opp)
        opp_mana = (f" │ 水晶 {st.mana_now(opp)}/{of['res']}"
                    if st.current_key() == opp else
                    f" │ 下回合水晶 {st.mana_next_turn(opp)}/{of['res']}")
        lines.append(f"对面 {st.name(opp)} [{hname}] {st.hero_hp(opp)}血{st.hero_armor(opp)}甲"
                     f"{opp_mana} │ 牌库{st.deck_count(opp)} │ 手牌{len(st.hand(opp))}(不可见)")
        lines.append(f"    场面: {_board_txt(st, opp, carddb)}")

    if led is not None:
        lines.append("── 牌库序(已知部分) ──")
        top = (f"下一抽: {carddb.name(led.known_top)} [探底置顶]"
               if led.known_top else "下一抽: 未知")
        kb = " ".join(f"[{carddb.name(c)}·底{pos}]" for c, pos in led.known_bottom) or "无"
        lines.append(f"    {top}")
        lines.append(f"    底部已知: {kb}    中段: {led.unknown_middle}张未知")
        if led.known_unpositioned:
            kn = " ".join(f"[{carddb.name(c)}]" for c in led.known_unpositioned)
            lines.append(f"    已见·位置未知(换牌换回等): {kn}")

        if not generic:
            lines.append(f"── {deck_name or '卡组'}组件台账 ──")
            lines.append(f"  在手    : {_fmt_counts(led.in_hand, knowledge.decklist, carddb, True)}")
            lines.append(f"  已消耗  : {_fmt_counts(led.used, knowledge.decklist, carddb, True)}")
            lines.append(f"  牌库剩余: {_fmt_counts(led.remaining, knowledge.decklist, carddb, False)}"
                         f" (实际{led.deck_actual}张)")

    if chain_summary:
        lines.append(f"本回合链路(共{len(chain_summary)}张): {' → '.join(chain_summary)}")
    lines.append("════════════════════")
    return "\n".join(lines)


def game_end_line(st: GameStore) -> str:
    states = " / ".join(f"{st.name(k)}={st.playstate(k)}" for k in st.player_keys())
    return f"──── 对局结束 ──── {states}"
