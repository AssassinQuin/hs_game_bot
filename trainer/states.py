"""状态快照 —— GameStore → 决策点状态事实(素材的原子单元)。

只做状态序列化(机读事实, 无中文、无结论): 双方"可见信息"严格时点化,
对手手牌只有数量 —— 训练素材的铁律是绝不泄露未来/隐藏信息(见 docs/MULLIGAN_AI.md §4)。
flatten 把快照压成定长数值向量供表格基座(TabPFN/GBM)消费; v1 用聚合特征
(费用/攻血聚合, 不含卡 ID onehot)以泛化到新卡 —— ID 特征留给语料上千后的版本。
"""
from __future__ import annotations

from hearthstone.enums import GameTag

from hsbot.carddb import CardDB
from hsbot.store import GameStore, atk, hp_total, is_taunt


def _fatigue(st: GameStore, key) -> int:
    e = st._player_entity(key)          # noqa: SLF001  疲劳计数挂玩家实体
    return (e.tags.get(GameTag.FATIGUE, 0) if e is not None else 0)


def _side(st: GameStore, carddb: CardDB, key) -> dict:
    mana = st.mana_fields(key)
    hero = st.hero(key)
    hand = [{"cid": st.cid_of(e.id), "cost": carddb.cost(st.cid_of(e.id))}
            for e in st.hand(key)]
    board = [{"cid": st.cid_of(e.id), "atk": atk(e), "hp": hp_total(e),
              "taunt": is_taunt(e)} for e in st.board(key)]
    return {"hp": st.hero_hp(key), "armor": st.hero_armor(key),
            "hand": hand, "board": board,
            "deck": st.deck_count(key), "secrets": st.secret_count(key),
            "mana": st.mana_now(key), "mana_cap": mana.get("res", 0),
            "overload": mana.get("overload", 0), "fatigue": _fatigue(st, key),
            "spellpower": st.spellpower(key),
            "class": carddb.card_class(hero.card_id)
            if hero is not None and hero.card_id else None}


def snapshot(st: GameStore, carddb: CardDB) -> dict | None:
    """当前局面 → 结构化状态。主客未定(留牌前)返回 None —— 那不是决策点。"""
    me, opp = st.friendly_key, st.opponent_key()
    if me is None or opp is None:
        return None
    return {"turn": st.turn, "my_turn": st.is_my_turn(),
            "me": _side(st, carddb, me), "opp": _side(st, carddb, opp)}


def _hand_feats(hand: list) -> list:
    costs = [c["cost"] for c in hand if c["cost"] is not None]
    return [len(hand), sum(costs), min(costs) if costs else 0,
            max(costs) if costs else 0,
            sum(1 for c in costs if c <= 3)]


def _board_feats(board: list) -> list:
    return [len(board), sum(b["atk"] for b in board), sum(b["hp"] for b in board),
            sum(1 for b in board if b["taunt"])]


def flatten(snap: dict) -> tuple[list, list]:
    """快照 → 定长数值向量 + 特征名(顺序即契约, 增字段只许追加在尾部)。"""
    me, opp = snap["me"], snap["opp"]
    names = []
    vec = []

    def put(name, value):
        names.append(name)
        vec.append(float(value))

    put("turn", snap["turn"])
    for tag, side in (("me", me), ("opp", opp)):
        put(f"{tag}_hp", side["hp"])
        put(f"{tag}_armor", side["armor"])
        put(f"{tag}_deck", side["deck"])
        put(f"{tag}_secrets", side["secrets"])
        put(f"{tag}_fatigue", side["fatigue"])
        put(f"{tag}_spellpower", side["spellpower"])
        for i, v in enumerate(_hand_feats(side["hand"])):
            put(f"{tag}_hand_{i}", v)          # 张数/总费/最低/最高/低费数
        for i, v in enumerate(_board_feats(side["board"])):
            put(f"{tag}_board_{i}", v)         # 随从数/总攻/总血/嘲讽数
    put("mana", me["mana"])
    put("mana_cap", me["mana_cap"])
    put("overload", me["overload"])
    put("hp_diff", (me["hp"] + me["armor"]) - (opp["hp"] + opp["armor"]))
    put("board_atk_diff", sum(b["atk"] for b in me["board"])
        - sum(b["atk"] for b in opp["board"]))
    return vec, names
