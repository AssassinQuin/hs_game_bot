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
    for e in st.hand(me):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        d = analyzer.burst_damage(cid, sp)
        if d:
            burst_hand += d
        r = analyzer.mana_ramp_value(cid)      # 等效回费: 水晶 + 减费面值
        if r:
            ramp_hand += r
        # 费用口径(2026-09-13 用户定版): 只计法术牌的费用总和(手/库/组同规则)
        base = carddb.cost(cid)
        tag = e.tags.get(GameTag.COST)
        if carddb.cardtype(cid) == "SPELL":
            c = tag if tag is not None else base
            cost_hand += c if c is not None else 0
        # 手牌减费在身事实(引擎 COST 标签 < 基础费): 归属=附魔名(解析层关联)
        if tag is not None and base is not None and tag < base:
            disc_cards += 1
            disc_total += base - tag
            for en in st.enchantments_on(e.id):
                nm = carddb.name(en)
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
            r = analyzer.mana_ramp_value(cid)
            if r:
                ramp_deck += r * n
        for cid, n in knowledge.decklist.items():
            if carddb.cardtype(cid) == "SPELL":
                list_cost += (carddb.cost(cid) or 0) * n
    lethal = burst_hand + (burst_deck or 0) + burst_board
    hero = st.hero(opp)
    enemy = st.hero_total_hp(opp) if hero is not None else None
    return {
        "enemy_total": enemy, "enemy_hp": st.hero_hp(opp),
        "enemy_armor": st.hero_armor(opp),
        "lethal": lethal, "lethal_hand": burst_hand, "lethal_deck": burst_deck,
        "lethal_board": burst_board,
        "can_kill": enemy is not None and lethal > 0 and lethal >= enemy,
        "spellpower": sp,
        "ramp": ramp_hand + (ramp_deck or 0), "ramp_hand": ramp_hand,
        "ramp_deck": ramp_deck,
        "cost_list": list_cost, "cost_deck": deck_cost, "cost_hand": cost_hand,
        "discount": {"cards": disc_cards, "total": disc_total,
                     "sources": disc_srcs or (["?"] if disc_total else [])},
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


def plan_line(f: dict) -> str | None:
    """可斩线第三行(plan 事实 → 措辞, 结论词"可斩"归 render):
    `可斩: 名A(N费)→名B(M费) 伤{face_det}+场{board_atk} ≥ {enemy_total}`。
    无 plan / 非可斩 → None(调用方输出零变化); 缺失数值以 ? 诚实降级。"""
    plan = f.get("plan") or {}
    if not plan.get("lethal"):
        return None
    acts = plan.get("actions") or []
    seq = "→".join(_plan_act(f.get("_carddb"), a) for a in acts) \
        if acts else "(无动作)"               # 退化输入: 无可出动作仍报构成
    q = lambda v: "?" if v is None else str(v)               # noqa: E731
    return (f"可斩: {seq} 伤{q(plan.get('face_det'))}"
            f"+场{q(plan.get('board_atk'))} ≥ {q(plan.get('enemy_total'))}")


def stat_text(f: dict) -> str:
    """信息区字段 → 两行文本(控制台/会话文件用; 悬浮窗走分格面板)。
    手牌减费在身时行尾追加 减N(来源) 段; 无减费保持两段。
    plan.lethal 时末尾追加第三行"可斩: 线 伤X+场Y ≥ Z"(措辞归 render);
    无 plan/非可斩 → 与两行版逐字节一致(零变化铁律)。"""
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
    line3 = plan_line(f)
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
