"""渲染层 —— 链路行 / 快照块 / 终局行(纯函数, 不持有状态)。

消费 GameStore(活引用只读); 输出格式与 M1 版一致, 新增 trigger/fatigue/death 行。
"""
from __future__ import annotations

from collections import Counter

from hearthstone.enums import GameTag

from .carddb import CardDB
from .knowledge import DeckKnowledge, Ledger
from .store import (GameStore, atk, hp_total, is_generated, is_taunt,
                    zone_pos)


def _side(actor, friendly) -> str:
    if actor is not None and actor == friendly:
        return "我"
    if friendly is not None and actor is not None:
        return "对面"
    return "?"


def chain_line(evt: dict, carddb: CardDB) -> str:
    head = f"[T{evt.get('turn', 0)}·{_side(evt.get('actor'), evt.get('friendly'))}]"
    kind = evt["kind"]
    if kind == "play":
        name = carddb.name(evt["card_id"])
        base, tag = evt.get("cost_base"), evt.get("cost_tag")
        if base is None and tag is None:
            cost = "(?费)"
        elif base is None:
            cost = f"(实付{tag}费)"
        elif tag is None or tag == base:
            cost = f"({base}费)"
        else:
            cost = f"({base}费→实付{tag})"
        sub = evt.get("suboption")
        if sub is not None:
            cost += f" 抉择{sub + 1}"
        left = evt.get("mana_left")
        left_txt = f" 剩{left}费" if left is not None else ""
        verb = "使用技能" if evt.get("is_power") else "打出"
        return f"{head} {verb} {name} {cost}{left_txt}"
    if kind == "discover":
        picked = "、".join(carddb.name(c) for c in evt.get("picked") or []) or "(无)"
        bottom = " → ".join(carddb.name(c) for c in evt.get("bottom") or []) or "(无)"
        return (f"{head} 发现({evt.get('src_name', '?')}): 取 {picked}"
                f" │ 置底(按序): {bottom}")
    if kind == "draw":
        return f"{head} 抽到 {carddb.name(evt['card_id'])}"
    if kind == "gain":
        creator_name = carddb.name(evt.get("creator")) if evt.get("creator") else None
        gen = f"[衍生·{creator_name}]" if creator_name and creator_name != "?" else "[衍生]"
        return f"{head} 入手 {carddb.name(evt['card_id'])} {gen}"
    if kind == "back_to_deck":
        return f"{head} 置入牌库 {carddb.name(evt['card_id'])}"
    if kind == "cost":
        return f"{head} 手牌费用变动 {carddb.name(evt['card_id'])} {evt['old']}→{evt['new']}费"
    if kind == "attack":
        def hero_txt(cid):
            n = carddb.name(cid)
            return f"英雄({n})" if n != "?" else "英雄"
        atk_txt = (hero_txt(evt.get("attacker_card_id")) if evt.get("attacker_is_hero")
                   else carddb.name(evt.get("attacker_card_id")))
        if evt.get("target_is_hero"):
            tgt = "敌方英雄" if evt.get("actor") == evt.get("friendly") else "我的英雄"
        else:
            tgt = carddb.name(evt.get("target_card_id"))
        return f"{head} 攻击 {atk_txt} → {tgt}"
    if kind == "shuffle":
        return f"{head} 牌库被洗牌(牌位重排)"
    if kind == "trigger":
        return f"{head} 触发 {carddb.name(evt.get('card_id'))}"
    if kind == "fatigue":
        return f"{head} 疲劳"
    if kind == "death":
        return f"{head} 阵亡 {carddb.name(evt.get('card_id'))}"
    return f"{head} {evt.get('msg', '')}"


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
                     f" │ 本回合已用{f['used']}费")
        lines.append(f"    下一回合: {st.mana_next_turn(me)}费{ol}")
        hs = []
        for i, e in enumerate(st.hand(me), 1):
            base = carddb.cost(e.card_id)
            tag = e.tags.get(GameTag.COST)
            cost = tag if tag is not None else (base if base is not None else "?")
            mark = "⬇" if (tag is not None and base is not None and tag < base) else ""
            gen = "[衍生]" if is_generated(e) else ""
            hs.append(f"{i}{carddb.name(e.card_id)}({cost}费{mark}){gen}")
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
