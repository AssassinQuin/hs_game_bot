"""渲染层 —— 事件流 → 链路行 / 快照块 / 终局行(纯函数, 不持有状态)。

链路行设计(注册制): 每种事件一个渲染函数, 用 @chain_renderer(kind) 注册;
chain_line 只负责行首([对局hash·T回合·阵营])与分发, 不含任何具体格式。
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
from .consts import class_zh
from .knowledge import DeckKnowledge, Ledger
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
    gid = evt.get("game_id")
    turn = f"{gid}·T{evt.get('turn', 0)}" if gid else f"T{evt.get('turn', 0)}"
    return f"[{turn}·{_side(evt.get('actor'), evt.get('friendly'))}]"


def chain_line(evt: dict, carddb: CardDB) -> str | None:
    kind = evt.get("kind")
    renderer = _CHAIN_RENDERERS.get(kind)
    if renderer is None:                      # text/未知种类: msg 兜底
        return f"{_head(evt)} {evt.get('msg', '')}"
    line = renderer(evt, carddb)
    return None if line is None else f"{_head(evt)} {line}"  # None = 渲染层判弃


@chain_renderer("mulligan_offer")
def _render_mulligan_offer(evt: dict, carddb: CardDB) -> str:
    """留牌建议(有 advice 富化)高亮行; 无建议 → 回退"起手可留"。"""
    adv = evt.get("advice")
    if not adv:
        return evt.get("msg", "")
    return mulligan_advice_text(adv)


@chain_renderer("play_offer")
def _render_play_offer(evt: dict, carddb: CardDB) -> str | None:
    """出牌建议(T2)链路行; 无 advice(无模型/门槛未达/无候选)→ None 判弃:
    事件事实已入 rich_events, 显示整环静默(实跑语料不足时输出零变化)。"""
    adv = evt.get("advice")
    return play_offer_text(adv) if adv else None


def play_offer_text(adv: dict) -> str:
    """出牌建议事实 → 主行措辞(docs/PLAY_ADVICE.md §2 统计最优级):
    `推荐: 名A(ΔP +x.x%) > 名B(ΔP +x.x%) > 不动`; ΔP = 相对"不动"基线的
    P(胜) 增量(机读 delta_p), 结论词"推荐/不动"归 render, 负值照实显示;
    二连名按出牌序用 → 相连(与可斩行动作同形)。"""
    def fmt(c):
        return f"{'→'.join(c['names'])}(ΔP {c['delta_p'] * 100:+.1f}%)"

    parts = [fmt(c) for c in adv.get("candidates") or []]
    return ("推荐: " + " > ".join(parts) + " > 不动") if parts else "推荐: 不动"


def play_offer_rows(adv: dict | None) -> list[tuple[str, str]]:
    """出牌建议事实 → 推荐区行[(文本, 色调)]: 主行(advice 色, 与链路行同源)
    + 证据行(dim, 语料局数+模型版本 —— 开口门槛的诚实披露)。缺证据事实只出
    主行; 无建议/无候选 → [](诚实: 绝不编造)。"""
    if not adv or not adv.get("candidates"):
        return []
    rows = [(play_offer_text(adv), "advice")]
    n = adv.get("n_games")
    if n:
        rows.append((f"语料{n}局 · 依据 价值模型 {adv.get('version') or '?'}",
                     "dim"))
    return rows


def mulligan_advice_text(adv: dict) -> str:
    """留牌建议事实 → 主行措辞(链路行与悬浮窗推荐区共用, 零平行格式)。"""
    def fmt(cid):
        info = adv.get("per_card", {}).get(cid) or {}
        gain = info.get("gain")
        name = str(info.get("name") or cid)
        return f"{name}({gain * 100:+.1f}%)" if gain is not None else name

    coin_txt = "后手" if adv.get("coin") else "先手"
    keep = "、".join(fmt(c) for c in adv.get("keep") or []) or "无"
    drop = "、".join(fmt(c) for c in adv.get("drop") or []) or "无"
    line = (f"【留牌建议·vs{class_zh(adv.get('opp_class', ''))}·{coin_txt}】"
            f"留 {keep} │ 换 {drop}")
    # v3 组合事实 → 措辞(spec §5.3); 无 v3 键时与 v2 逐字节一致(零变化钉子)
    seen: set = set()
    anti_names = []
    for p in (adv.get("v3") or {}).get("anti_synergy") or []:
        key = frozenset(p)
        if key in seen:                      # 协同对称双键只措辞一次
            continue
        seen.add(key)
        anti_names.append(f"{fmt(p[0]).split('(')[0]}+{fmt(p[1]).split('(')[0]}")
    if anti_names:
        line += " │ 不宜同留: " + "、".join(anti_names)
    return line


# 悬浮窗推荐区: 评分器键 → 中文依据(措辞归 render; 未知键原样展示)
_SCORER_ZH = {"v3_base": "基座", "v3": "蒸馏", "tabpfn": "TabPFN",
              "lr": "LR", "table": "统计表"}


def advice_rows(adv: dict | None) -> list[tuple[str, str]]:
    """留牌建议事实 → 推荐区行[(文本, 色调)](悬浮窗"推荐打法"区)。
    主行 = mulligan_advice_text(与链路行同源); 证据行 = 卡组胜率+依据+版本。
    色调只有 advice(金)/dim(小字) 两种, 颜色映射归悬浮窗。无 adv → []。"""
    if not adv:
        return []
    rows = [(mulligan_advice_text(adv), "advice")]
    wr = adv.get("deck_wr")
    if wr is not None:
        scorer = _SCORER_ZH.get(adv.get("scorer"), adv.get("scorer") or "?")
        tail = f" · 依据 {scorer} {adv.get('version') or ''}".rstrip()
        rows.append((f"卡组胜率 {wr * 100:.0f}%{tail}", "dim"))
    return rows


# ================= 留牌结论词: 事实 → 中文(输出层政策) =================
# 阈值属于"何时开口/如何措辞"的展示政策, 改这里不影响 mulligan_ai 的统计计算。

MULL_GAIN_KEEP = 0.03       # 增益 ≥ +3% → 建议留
MULL_GAIN_DROP = -0.03      # 增益 ≤ −3% → 建议换
MULL_N_ADVICE_MIN = 6       # 无先验的卡: 样本低于此值只报"样本不足"
_MULL_SRC_ZH = {"coin": "同先手", "class": "本职业", "all": "全体",
                "prior": "专家先验", "none": "无数据"}
_MULL_LEVEL_ZH = {"near": "近似手牌", "same": "同职业同手"}


def mulligan_verdict(adv: dict) -> str:
    """逐卡结论词: mulligan_ai 的事实 → 中文。有专家先验的卡不受样本门槛限制。"""
    gain, src = adv.get("gain", 0.0), adv.get("src")
    if src == "prior":
        return ("建议留" if gain >= MULL_GAIN_KEEP
                else "建议换" if gain <= MULL_GAIN_DROP else "先验中性")
    if src == "none" or (adv.get("n", 0) < MULL_N_ADVICE_MIN
                         and not adv.get("prior")):
        return "样本不足"
    return ("建议留" if gain >= MULL_GAIN_KEEP
            else "建议换" if gain <= MULL_GAIN_DROP else "中性")


def mulligan_src_zh(src: str) -> str:
    """结论出处键 → 中文(同先手/本职业/全体/专家先验/无数据)。"""
    return _MULL_SRC_ZH.get(src, src or "")


def mulligan_matched_zh(ev: dict | None) -> str:
    """同情境匹配事实 → 中文一行; 无可比对局照实说。"""
    if not ev or ev.get("level") == "none":
        return "无可比对局"
    kw, kl = ev["keep"]
    dw, dl = ev["drop"]
    name = _MULL_LEVEL_ZH.get(ev.get("level"), ev.get("level"))
    return (f"{name}{ev['n']}局: 留{kw + kl}({kw}胜{kl}负) "
            f"换{dw + dl}({dw}胜{dl}负)")


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
def _render_trigger(evt: dict, carddb: CardDB) -> str | None:
    name = carddb.name(evt.get("card_id"))
    if name == "?":
        # 开局未揭示的触发不落行: 同一触发块常随后以已揭示形态重放, 届时自然
        # 以真名出一次(2026-09-13 实测 开局"#NN"噪声/同牌两行); 对局中的暗牌
        # 附魔等仍报实体号便于对日志。
        if (evt.get("turn") or 0) <= 1 and not evt.get("host"):
            return None
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


# ================= 上部信息区(悬浮窗两区布局的上区) =================

def stat_fields(st: GameStore, *, knowledge, carddb: CardDB, analyzer,
                plan: dict | None = None) -> dict | None:
    """信息区机读字段(唯一事实来源): 敌血甲/斩杀构成/法强/回费/费用。
    文本版(stat_text)与悬浮窗分格面板(overlay)都从它渲染, 不做平行计算。
    斩杀口径(2026-09-13 用户定版): 手牌伤害 + 牌库剩余伤害(台账期望组成,
    均按当前实际法强加成: 法术/技能=(基础+法强)×段数, 其余按基础值×段数)
    + 场面总攻
    —— 理论上限粗估, 非出牌链搜索。友方或对手未解析 → None(信息区保持原样)。
    plan: 斩杀线机读事实(analysis.lethal_plan 产物, None=未算/开关关闭),
    原样透传入 dict(零变化铁律: None 时不改变任何现有输出)。"""
    me, opp = st.friendly_key, st.opponent_key()
    if me is None or opp is None:
        return None
    sp = st.spellpower(me)
    burst_hand = ramp_hand = cost_hand = 0
    disc_cards = disc_total = 0
    disc_srcs: list[str] = []

    # 减费目标费表(2026-09-14 "0 水晶不需要"): 回费格的减费面值按可减目标
    # 封顶 —— hand:法术 → 手牌法术实时费; next:星灵(卡表 set=SPACE, 与
    # planner/pieces 同一判定) → 手牌+牌库剩余星灵牌费。逐实体记 id,
    # 评估一张牌的回费时不把这张牌自己当减费目标。
    def _rcost(e, cid) -> int:
        base = carddb.cost(cid)
        tag = e.tags.get(GameTag.COST)
        c = tag if tag is not None else base
        return c if c is not None else 0

    def _is_space(cid) -> bool:
        raw = carddb.raw(cid)
        return raw is not None and raw.get("set") == "SPACE"

    hand_spells: list = []               # [(实体id, 实时费)] 手牌法术
    hand_space: list = []                # [(实体id, 实时费)] 手牌星灵牌
    # 玩家级光环附魔(修 6)按减费语义过滤 —— 挂在玩家实体上的附魔未必是
    # 减费光环(二轮审计 F1: VAC_422e 游客附魔/攻击光环都会被无差别计入),
    # IR 层可区分: 只有含 CostDown 的才进减费来源
    player_auras = [cid for cid in st.aura_enchantments_on_player(me)
                    if analyzer.cost_downs(cid)]
    for e in st.hand(me):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        if carddb.cardtype(cid) == "SPELL":
            hand_spells.append((e.id, _rcost(e, cid)))
        if _is_space(cid):
            hand_space.append((e.id, _rcost(e, cid)))
    # 牌库剩余星灵牌 [(cid, 费)]×张数; 牌库侧评估自身时剔除一张(自己不能
    # 减自己), 手牌侧不剔(同 cid 的牌库拷贝仍是合法目标)
    deck_space: list = []
    if knowledge is not None:
        deck_space = [(cid, carddb.cost(cid) or 0)
                      for cid, n in knowledge.ledger.remaining.items()
                      if _is_space(cid) for _ in range(n)]

    for e in st.hand(me):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        d = analyzer.burst_damage(cid, sp)
        if d:
            burst_hand += d
        # 等效回费: 水晶 + 减费面值(按类目可减目标封顶, 0=减费全虚的有效事实)
        r = analyzer.mana_ramp_value(
            cid, {"hand": [c for eid, c in hand_spells if eid != e.id],
                  "next": [c for eid, c in hand_space if eid != e.id]
                          + [c for _cid2, c in deck_space]})
        if r:
            ramp_hand += r
        # 费用口径(2026-09-13 用户定版): 只计法术牌的费用总和(手/库/组同规则)
        base = carddb.cost(cid)
        tag = e.tags.get(GameTag.COST)
        if carddb.cardtype(cid) == "SPELL":
            c = tag if tag is not None else base
            cost_hand += c if c is not None else 0
        # 手牌减费在身事实(引擎 COST 标签 < 基础费): 归属=附魔名(解析层关联);
        # 卡级附魔查不到 → 玩家级光环(2026-09-17 审计修 6: SC_755e2 ATTACHED=
        # 玩家实体, 每帧算一次, 不在逐牌循环里重复查)
        if tag is not None and base is not None and tag < base:
            disc_cards += 1
            disc_total += base - tag
            names = [carddb.name(x) for x in st.enchantments_on(e.id)] \
                or [carddb.name(x) for x in player_auras]
            for nm in names:
                if nm not in disc_srcs:
                    disc_srcs.append(nm)
    burst_board = st.board_attack(me)
    # 牌库侧(伤害潜力/回费/费用)按台账期望组成; 通用模式(无卡组)诚实降级为 ?
    burst_deck = ramp_deck = deck_cost = list_cost = None
    if knowledge is not None:
        burst_deck = ramp_deck = deck_cost = list_cost = 0
        for cid, n in knowledge.ledger.remaining.items():
            d = analyzer.burst_damage(cid, sp)
            if d:
                burst_deck += d * n
            if carddb.cardtype(cid) == "SPELL":
                deck_cost += (carddb.cost(cid) or 0) * n
            space_t = [c for _eid, c in hand_space]
            skipped = False
            for cid2, c2 in deck_space:
                if cid2 == cid and not skipped:
                    skipped = True            # 剔除自身一张: 自己不能减自己
                    continue
                space_t.append(c2)
            r = analyzer.mana_ramp_value(
                cid, {"hand": [c for _eid, c in hand_spells],
                      "next": space_t})
            if r:
                ramp_deck += r * n
        for cid, n in knowledge.decklist.items():
            if carddb.cardtype(cid) == "SPELL":
                list_cost += (carddb.cost(cid) or 0) * n
    lethal = burst_hand + (burst_deck or 0) + burst_board
    hero = st.hero(opp)
    enemy = st.hero_total_hp(opp) if hero is not None else None
    mana = st.mana_now(me)               # 当前可用法力(法力格缺数据时 UI 默认 1)
    return {
        "enemy_total": enemy, "enemy_hp": st.hero_hp(opp),
        "enemy_armor": st.hero_armor(opp),
        "lethal": lethal, "lethal_hand": burst_hand, "lethal_deck": burst_deck,
        "lethal_board": burst_board,
        "can_kill": enemy is not None and lethal > 0 and lethal >= enemy,
        "spellpower": sp,
        "mana": mana, "mana_res": st.mana_fields(me)["res"],
        "ramp": ramp_hand + (ramp_deck or 0), "ramp_hand": ramp_hand,
        "ramp_deck": ramp_deck,
        "cost_list": list_cost, "cost_deck": deck_cost, "cost_hand": cost_hand,
        "discount": {"cards": disc_cards, "total": disc_total,
                     "sources": disc_srcs},   # 归因失败诚实空表(修 6: 不再出 "?")
        # 斩杀线 plan 原样透传(契约 §3); _carddb 供 plan_line/stat_text 出名
        # (stat_text 不得再收 carddb 形参 —— watcher 调用点签名不变)
        "plan": plan, "_carddb": carddb,
    }


def _plan_name(carddb, cid) -> str:
    """plan 动作卡名: carddb 查询失败/未收录 → 回退 card_id(诚实降级, 不抛)。"""
    if carddb is not None:
        try:
            n = carddb.name(cid)
        except Exception:                     # noqa: BLE001  假/半残卡表不拖垮渲染
            n = None
        if n and n != "?":
            return n
    return str(cid) if cid else "?"


def _plan_act(carddb, action) -> str:
    """单个 plan 动作 (card_id, cost) → `名(N费)`; 异形动作尽力降级不抛。"""
    try:
        cid, cost = action
    except (TypeError, ValueError):
        return _plan_name(carddb, action)
    return f"{_plan_name(carddb, cid)}({cost if cost is not None else '?'}费)"


def _lethal_line(plan: dict, carddb) -> str:
    """可斩行措辞(与既有钉子逐字节一致, 不得改动): 名A(N费)→… 伤X+场Y ≥ Z。"""
    acts = plan.get("actions") or []
    seq = "→".join(_plan_act(carddb, a) for a in acts) \
        if acts else "(无动作)"               # 退化输入: 无可出动作仍报构成
    q = lambda v: "?" if v is None else str(v)               # noqa: E731
    return (f"可斩: {seq} 伤{q(plan.get('face_det'))}"
            f"+场{q(plan.get('board_atk'))} ≥ {q(plan.get('enemy_total'))}")


def plan_line(f: dict) -> str | None:
    """plan 事实 → 推荐打法主行(输出语义两级契约, 结论词归 render):
    lethal=True → `可斩: 名A(N费)→名B(M费) 伤{face_det}+场{board_atk}
    ≥ {enemy_total}`(与控制台 stat_text 第三行同源, 金色调);
    非可斩 → `最优: … 伤X+场Y vs 敌Z`(总伤未达敌血绝不写"可斩"二字),
    face_exp>0 时追加 `+期望N` 注记(期望分量绝不与确定伤合并);
    空线退化(actions 空 且 零确定伤 且 零期望) → None(诚实: 无建议可给)。
    缺失数值以 ? 诚实降级。"""
    plan = f.get("plan") or {}
    carddb = f.get("_carddb")
    if plan.get("lethal"):
        return _lethal_line(plan, carddb)
    acts = plan.get("actions") or []
    if not acts and not plan.get("face_det") and not plan.get("face_exp"):
        return None
    seq = "→".join(_plan_act(carddb, a) for a in acts) \
        if acts else "(无动作)"
    q = lambda v: "?" if v is None else str(v)               # noqa: E731
    exp = plan.get("face_exp")
    exp_txt = f"+期望{exp}" if exp else ""
    return (f"最优: {seq} 伤{q(plan.get('face_det'))}"
            f"+场{q(plan.get('board_atk'))}{exp_txt}"
            f" vs 敌{q(plan.get('enemy_total'))}")


def plan_data_line(f: dict) -> str | None:
    """线行支撑数据(dim 小字): 剩费=mana_trace 末位(打完整线后剩余法力,
    T1 契约字段, 旧形态缺失时整段省略), 未覆盖张数=uncovered_n。
    机读事实驱动零编造: 零值/缺失不占位, 两项全无 → None(不出数据行)。"""
    plan = f.get("plan") or {}
    parts = []
    trace = plan.get("mana_trace") or ()
    if trace and trace[-1] is not None:
        parts.append(f"剩费{trace[-1]}")
    unc = plan.get("uncovered_n")
    if unc:
        parts.append(f"未覆盖{unc}张")
    return " · ".join(parts) if parts else None


def plan_rows(f: dict) -> list[tuple[str, str]]:
    """plan 事实 → 推荐区行[(文本, 色调)](悬浮窗"推荐打法"区, 常驻建议):
    首行 = plan_line(可斩金/advice 色; 非可斩最优行常规/stat 色 —— 与
    _StatPanel"可斩才转金"同一语义两级); 次行 = plan_data_line(dim)。
    空线退化/无 plan → [](诚实: 无建议可给, 绝不编造)。"""
    line = plan_line(f)
    if line is None:
        return []
    tone = "advice" if (f.get("plan") or {}).get("lethal") else "stat"
    rows = [(line, tone)]
    data = plan_data_line(f)
    if data:
        rows.append((data, "dim"))
    return rows


def stat_text(f: dict) -> str:
    """信息区字段 → 两行文本(控制台/会话文件用; 悬浮窗走分格面板)。
    手牌减费在身时行尾追加 减N(来源) 段; 无减费保持两段。
    plan.lethal 时末尾追加第三行"可斩: 线 伤X+场Y ≥ Z"(措辞归 render);
    无 plan/非可斩 → 与两行版逐字节一致(零变化铁律: 非可斩的"最优"行
    只进悬浮窗推荐区(render.plan_rows), 绝不漏进控制台输出)。"""
    q = lambda v: "?" if v is None else str(v)               # noqa: E731
    if f["enemy_total"] is None:
        enemy_txt = "?"
    else:
        enemy_txt = f"{f['enemy_total']}({f['enemy_hp']}血+{f['enemy_armor']}甲)"
    kill_mark = ",可斩" if f["can_kill"] else ""
    line2 = (f"回费 +{f['ramp']}(手{f['ramp_hand']}+库{q(f['ramp_deck'])})"
             f" │ 费 组{q(f['cost_list'])}/库{q(f['cost_deck'])}/手{f['cost_hand']}")
    disc = f.get("discount") or {}
    if disc.get("total"):
        srcs = "·".join(disc.get("sources") or [])
        line2 += f" │ 减{disc['total']}" + (f"({srcs})" if srcs else "")
    txt = (f"敌 {enemy_txt} │ 斩杀 {f['lethal']}"
           f"(手{f['lethal_hand']}+库{q(f['lethal_deck'])}"
           f"+场{f['lethal_board']}{kill_mark})"
           f" │ 法强 {f['spellpower']}\n{line2}")
    plan = f.get("plan") or {}
    line3 = _lethal_line(plan, f.get("_carddb")) if plan.get("lethal") else None
    if line3:
        txt += f"\n{line3}"
    return txt


def top_summary(st: GameStore, *, knowledge, carddb: CardDB, analyzer,
                plan: dict | None = None) -> str | None:
    """两行信息区文本 = stat_fields(事实) + stat_text(措辞) 的组合入口。
    plan 原样透传(stat_fields → stat_text)。"""
    f = stat_fields(st, knowledge=knowledge, carddb=carddb, analyzer=analyzer,
                    plan=plan)
    return stat_text(f) if f is not None else None


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


def snapshot_line(st: GameStore, game_id: str, reason: str) -> str:
    """单行精简快照(UI 用): 悬浮窗/控制台只看节奏, 完整版见 snapshot_block。
    对局身份 = game_id hash(与链路行首/训练 jsonl meta 同源), 供跨文件对齐。"""
    reason_cn = _REASON_CN.get(reason, reason)
    me = st.friendly_key
    if me is None:                          # 友方未解析: 只报对局hash与原因
        return f"── {game_id} 快照({reason_cn}) ──"
    f = st.mana_fields(me)
    opp = st.opponent_key()
    opp_atk = st.board_attack(opp) if opp is not None else 0
    sp = st.spellpower(me)
    return (f"── T{st.turn} {game_id}({reason_cn})"
            f" 我:水晶{st.mana_now(me)}/{f['res']} │ 手牌{len(st.hand(me))}"
            f" │ 牌库{st.deck_count(me)} │ 场上{len(st.board(me))}"
            f" │ 对面场攻{opp_atk}"
            + (f" │ 法强{sp}" if sp else "") + " ──")


def snapshot_block(st: GameStore, led: Ledger | None, *, knowledge: DeckKnowledge | None,
                   deck_name: str, generic: bool, game_id: str,
                   chain_summary: list[str],
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
                 f" │ {game_id} │ {mode}·{fmt}{flag}")

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
            if led.lost:
                lines.append(f"  被偷    : {_fmt_counts(led.lost, knowledge.decklist, carddb, True)}")
            lines.append(f"  牌库剩余: {_fmt_counts(led.remaining, knowledge.decklist, carddb, False)}"
                         f" (实际{led.deck_actual}张)")

    if chain_summary:
        lines.append(f"本回合链路(共{len(chain_summary)}张): {' → '.join(chain_summary)}")
    lines.append("════════════════════")
    return "\n".join(lines)


def game_end_line(st: GameStore) -> str:
    states = " / ".join(f"{st.name(k)}={st.playstate(k)}" for k in st.player_keys())
    return f"──── 对局结束 ──── {states}"
