"""解析层 —— 卡牌文本与效果指纹的唯一解释点(2026-09-13 分层化)。

三层职责边界:
  store(GameStore)  唯一状态维护: 实体/区域/标签 → 快照与纯状态事实;
  analysis(本层)    卡牌/效果解析: $N 文本伤害、伤害预估、隐藏触发指纹判定;
  render            纯输出: 只格式化事件字段, 不含任何游戏规则。

实时性: watcher._route 在渲染前调 enrich(evt, store) —— 派生结论当拍得出,
不滞后、不二次维护状态; 事件字典的派生字段(pred_dmg/card_id 推断/via)
由本层写入, store 与 render 对规则零感知。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from hearthstone.enums import GameTag

from .carddb import CardDB
from .consts import SPELLPOWER_TYPES
from .effects import (CostDown, Damage, Draw, EffectCache, Heal, Mechanic,
                      ManaGain, SpellPower, Unknown)
from .mulligan_ai import UNKNOWN, hero_class, is_coin

_CID_SCAN_RE = re.compile(r"cardId=([A-Za-z0-9_]+)")

_DMG_TYPES = SPELLPOWER_TYPES          # 只有法术/英雄技能吃法强(随从战吼不加成)


class EffectAnalyzer:
    def __init__(self, carddb: CardDB, cache: Optional[EffectCache] = None,
                 mulligan=None, play=None) -> None:
        self.carddb = carddb
        self.cache = cache or EffectCache()   # 无路径 = 仅内存增量
        self.mulligan = mulligan              # 留牌建议器(MulliganAdvisor), 可选
        self.play = play                      # 出牌建议器(PlayAdvisor), 可选

    # ---------- IR 访问 ----------
    def _compiled(self, card_id: str | None):
        return self.cache.get_or_compile(self.carddb.raw(card_id))

    # ---------- 卡牌文本解析($N 语义已编译进 IR) ----------
    def _damage_effect(self, card_id: str | None):
        ir = self._compiled(card_id)
        if ir is None:
            return None
        return next((e for e in ir.effects if isinstance(e, Damage)), None)

    def text_damage(self, card_id: str | None) -> tuple[int, int] | None:
        """(基础伤害, 段数); 非伤害牌返回 None。"""
        d = self._damage_effect(card_id)
        return (d.base, d.hits) if d else None

    # ---------- 效果预估 ----------
    def predict_damage(self, card_id: str | None, spellpower: int) -> dict | None:
        """预计伤害 {"total": 总伤, "hits": 段数}; 不吃法强的牌返回 None。
        口径(2026-09-13 用户定版): (基础+法强)×段数 —— 每段都含基础值与
        法强伤害, 法强0 时两段牌=基础×2(不是基础×1)。
        2026-09-14 法伤审计: 裸数字固定伤(scaled=False, 引擎不吃法强, 与
        burst_damage 同口径)也不预报 —— 预报只许漏方向, 绝不虚高。"""
        if self.carddb.cardtype(card_id) not in _DMG_TYPES:
            return None
        d = self._damage_effect(card_id)
        if not d or not d.scaled:
            return None
        return {"total": (d.base + spellpower) * d.hits, "hits": d.hits}

    def burst_damage(self, card_id: str | None, spellpower: int) -> int | None:
        """斩杀口径的单牌伤害潜力(当前法强下): (基础+法强)×段数 —— 法术/
        英雄技能吃法强, 随从战吼等按基础值×段数; 无伤害效果返回 None。
        固定伤害(文本裸数字, scaled=False, 目标受限如"对一个随从造成1点伤
        害")不计入 —— 解析器如实报告它(text_damage/IR), 斩杀只认可打脸的
        $N 语义伤害。信息区斩杀合计的求值单元。"""
        d = self._damage_effect(card_id)
        if not d or not d.scaled:
            return None
        sp = spellpower if self.carddb.cardtype(card_id) in _DMG_TYPES else 0
        return (d.base + sp) * d.hits

    def spellpower_gain(self, card_id: str | None) -> int:
        """单牌法强增益(打出后的法强增量, SpellPower IR 合计); 非法强牌返回 0。
        出牌建议(T2)的"法强牌"判定与差分求值单元。"""
        ir = self._compiled(card_id)
        if ir is None:
            return 0
        return sum(e.amount for e in ir.effects if isinstance(e, SpellPower))

    def draw_amount(self, card_id: str | None) -> int:
        """单牌抽牌量(Draw IR 合计); 对手侧抽牌(scope=opponent)不算自己的。
        cast_draw 触发句的误标 Draw(1) 由调用方按机制标记剔除(见 play_ai)。"""
        ir = self._compiled(card_id)
        if ir is None:
            return 0
        return sum(e.amount for e in ir.effects
                   if isinstance(e, Draw) and e.scope != "opponent")

    def mana_ramp(self, card_id: str | None) -> int | None:
        """单牌回费(获得/复原法力水晶数); 非回费牌返回 None。"""
        ir = self._compiled(card_id)
        if ir is None:
            return None
        total = sum(e.amount for e in ir.effects if isinstance(e, ManaGain))
        return total or None

    def mana_ramp_value(self, card_id: str | None,
                        discountable_costs: list | dict | None = None) -> int | None:
        """等效回费 = 水晶(获得/复原) + 减费面值(CostDown: 建造水晶塔"下一张
        星灵牌-2"、生命缚誓者的礼物"手牌法术-1"、无界空宇"本牌自减"等, 按面值
        计); scope="target"(使其/它的, 如侦察给发现牌减费)不算 —— 减的是尚未
        入手的牌, 不属等效回费(2026-09-13 定版沿用"侦察不计数"口径)。
        discountable_costs(可选)两种形态:
        * list: 全部减费共用目标费表;
        * dict: 按减费类目分表 {"hand": [手牌法术费…], "next": [星灵牌费…]},
          每条减费取自己 scope 前缀("hand:"/"next:")对应的目标费表。
        给定时按 2026-09-14 用户裁决"0 水晶不需要"计: 每条减费只计
        min(面值, 最大可减目标费) —— 目标全为 0 费/无目标则该减费是虚的,
        计 0; 缺省 None 保持旧口径按面值(未接线调用零变化)。
        信息区"回费"格的求值单元; 减费的实时在身状态由 store COST 标签
        (render.stat_fields discount)呈现, 不在此口径内。"""
        ir = self._compiled(card_id)
        if ir is None:
            return None
        total = sum(e.amount for e in ir.effects if isinstance(e, ManaGain))
        downs = [e for e in ir.effects if isinstance(e, CostDown)
                 and not e.scope.startswith("target")]
        if discountable_costs is not None:
            def _cap(e):
                costs = discountable_costs
                if isinstance(costs, dict):
                    costs = costs.get(
                        "hand" if e.scope.startswith("hand:") else "next") or []
                return max(costs) if costs else 0
            total += sum(min(e.amount, _cap(e)) for e in downs)
            return total            # 上下文口径: 0 是有效事实(减费全是虚的)
        total += sum(e.amount for e in downs)
        return total or None        # 旧口径: 零回费归 None(既有调用零变化)

    def cost_downs(self, card_id: str | None) -> list[CostDown]:
        """单牌全部减费效果(IR); 无则空表。"""
        ir = self._compiled(card_id)
        if ir is None:
            return []
        return [e for e in ir.effects if isinstance(e, CostDown)]

    # ---------- 机制查询(IR 标记驱动, 不逐卡写死) ----------
    def has_mechanic(self, card_id: str | None, kind: str) -> bool:
        """卡牌是否带某机制标记。标记来源两条通道, 在 effects 编译器统一:
        数据级(HsJson mechanics[] 标签, 如 DREDGE) + 文本级(语法表措辞)。"""
        ir = self._compiled(card_id)
        return ir is not None and any(
            isinstance(e, Mechanic) and e.kind == kind for e in ir.effects)

    def discover_bottom_mechanic(self, card_id: str | None) -> bool:
        """发现类机制: 未选项即当前牌库底(置于牌库底/探底)。"""
        return self.has_mechanic(card_id, "deck_bottom")

    def _chosen_sub_cid(self, evt: dict, store) -> str | None:
        """抉择所选子卡: store 查 PARENT_CARD 按钮实体(实体号序=抉择顺序),
        suboption 下标取所选(实测 SubOption=0/1)。按钮未揭示时按 a/b 命名
        惯例兜底 —— 卡表可证才采用, 不给事件流编造 id。"""
        sub = evt.get("suboption")
        if not isinstance(sub, int) or sub < 0:
            return None
        buttons = store.choose_one_buttons(evt.get("eid"))
        if sub < len(buttons):
            known = getattr(buttons[sub], "card_id", None)
            if known:
                return known
        cid = evt.get("card_id")
        guess = f"{cid}{'ab'[sub]}" if cid and sub in (0, 1) else None
        return guess if guess and self.carddb.raw(guess) else None

    # ---------- 事件实时富化 ----------
    def enrich(self, evt: dict, store, live: bool = True) -> dict:
        """按事件类型补充派生字段(不改 store 状态, 不改 store 的事件事实)。
        live=False = 历史追平: 跳过留牌推理(TabPFN 推理秒级, 追平时每局一次
        会把监控线程堵到"日志像没加载") —— 建议只服务实时显示, 历史无需。"""
        kind = evt.get("kind")
        if kind == "play":
            pred = self.predict_damage(evt.get("card_id"),
                                       store.spellpower(evt.get("actor")) or 0)
            if pred:
                evt = {**evt, "pred_dmg": pred}
            sub_cid = self._chosen_sub_cid(evt, store)
            if sub_cid:
                evt = {**evt, "suboption_card_id": sub_cid}
        elif kind == "mulligan_offer":
            # 留牌建议(责任链的分析环): 只对我方、只富化不改事件事实;
            # 无建议器/无模型 → 事件原样通过, 渲染层回退到"起手可留"。
            if live and self.mulligan is not None \
                    and evt.get("actor") == store.friendly_key:
                offered = [c for c in evt.get("offered") or []
                           if c and not is_coin(c)]     # 幸运币不可换, 不进决策
                opp_cid = next((cid for pid, cid in
                                (store.heroes_facts() or {}).items()
                                if pid != evt.get("actor")), None)
                advice = self.mulligan.advise(
                    offered, hero_class(opp_cid, self.carddb) or UNKNOWN,
                    coin=any(is_coin(c) for c in evt.get("offered") or []))
                if advice:
                    evt = {**evt, "advice": advice}
        elif kind == "play_offer":
            # 出牌建议(T2 责任链的分析环): 只对我方、只富化不改事件事实;
            # 无建议器/无模型/语料未达门槛 → 事件原样通过(渲染层判弃=静默)。
            if live and self.play is not None \
                    and evt.get("actor") == store.friendly_key:
                advice = self.play.advise(store, self)
                if advice:
                    evt = {**evt, "advice": advice}
        elif kind == "cost":
            eid = evt.get("eid")
            via = store.enchantments_on(eid) if eid is not None else []
            if not via and evt.get("ctx_eid") is not None:
                host_cid = store.cid_of(evt["ctx_eid"])
                if host_cid:
                    via = [host_cid]     # 光环式减益: 归因到所在触发块
            evt = {**evt, "via": via}
        return evt

    # ---------- 批量入口(parse-cards 子命令) ----------
    def compile_seen_cards(self, cache: EffectCache, card_ids) -> dict:
        """对出现过的卡牌做增量编译(已缓存的直接复用); 返回统计。
        covered/notext/uncovered = 新口径(至少一条非 Unknown 效果 / 无文本 /
        有文本未覆盖); damage/heal/unknown = 旧口径键(伤害/治疗优先, 其余归
        unknown, 含纯机制/减费卡), 兼容既有调用。kinds=按 IR kind 计卡数。"""
        stats = {"total": 0, "compiled": 0, "reused": 0, "missing": 0,
                 "damage": 0, "heal": 0, "unknown": 0, "covered": 0,
                 "notext": 0, "uncovered": 0, "kinds": {}}
        for cid in sorted(card_ids):
            stats["total"] += 1
            card = self.carddb.raw(cid)
            if card is None:
                stats["missing"] += 1
                continue
            was = cid in cache
            ir = cache.get_or_compile(card)
            stats["reused" if was else "compiled"] += 1
            known = [e for e in ir.effects if not isinstance(e, Unknown)]
            if known:
                stats["covered"] += 1
                for e in known:
                    name = type(e).__name__
                    stats["kinds"][name] = stats["kinds"].get(name, 0) + 1
            elif ir.effects and not ir.effects[0].raw:
                stats["notext"] += 1      # 无文本 token/附慕: 无从解析
            else:
                stats["uncovered"] += 1
            if any(isinstance(e, Damage) for e in ir.effects):
                stats["damage"] += 1
            elif any(isinstance(e, Heal) for e in ir.effects):
                stats["heal"] += 1
            else:
                stats["unknown"] += 1     # 旧口径: 无伤害无治疗
        cache.save()
        return stats


# ================= 斩杀线规划(T1, 接口契约 §2) =================

def lethal_plan(st, knowledge, analyzer, *, enabled: bool = True) -> dict | None:
    """手牌出牌线搜索(T1 斩杀 DFS)的机读事实编排: 状态事实只读 store/
    knowledge, 效果解析只走 analyzer, 搜索委托 planner 纯函数(planner
    在函数体内延迟 import —— 属并行切片且便于测试打桩)。
    返回 dict(actions/total/face_det/face_exp/lethal/enemy_total/
    board_atk/uncovered_n, 措辞零, 字段口径=接口契约 §2);
    None = 开关关 / 我方未解析 / 无手牌(渲染层按 lethal 决定是否加
    "可斩"行, 其余情况输出零变化)。"""
    if not enabled:
        return None
    me = st.friendly_key
    if me is None:
        return None
    hand = []
    for e in st.hand(me):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        cost = e.tags.get(GameTag.COST)        # 减费在身的实际费优先
        if cost is None:
            cost = analyzer.carddb.cost(cid)   # 卡表缺牌 → 0(诚实降级)
        hand.append((cid, cost if cost is not None else 0))
    if not hand:
        return None
    sp = st.spellpower(me)
    known_draws: list[tuple[str, int]] = []
    exp_per_draw = 0.0
    if knowledge is not None:
        led = knowledge.ledger
        # 确定抽牌队列, 队头=最先抽到: 顶牌恒确定; 底牌仅在顶底之间无未知牌
        # (unknown_middle==0, 全库已知)时才是确定抽——否则该抽位实际抽到的
        # 是未知牌, 底牌降级走期望通道(remaining 仍含底牌; 宁漏勿错,
        # 2026-09-14 审计修正, 见 tests/test_lethal_plan.py 台账侧用例)
        queue = [led.known_top]
        if led.unknown_middle == 0:
            # 2026-09-14 审计修正: 底牌队列剔除顶牌(rebuild 的 bottom_map 含
            # pos=1 顶牌, 直接拼会双计), 且按牌位升序 = 抽牌序
            queue += [cid for cid, pos in sorted(
                ((c, p) for c, p in led.known_bottom if p > 1),
                key=lambda cp: cp[1])]
        for cid in queue:
            if cid:
                known_draws.append((cid, analyzer.carddb.cost(cid) or 0))
        remaining = led.remaining
        deck_left = sum(remaining.values())
        # 期望注记(不进可斩判定): 当前实际法强的单卡伤害 × 台账剩余组成
        exp_per_draw = (sum(n * (analyzer.burst_damage(cid, sp) or 0)
                            for cid, n in remaining.items()) / max(1, deck_left))
    from planner.dfs import best_line          # 延迟 import(测试可打桩)
    from planner.pieces import build_piece
    from planner.simstate import initial_state
    # 场上引擎接线(审计 2026-09-14 高#1): 我方 cast_draw 随从数 = engines
    # ——拍卖师在场施法抽牌, known_draws 队列随之消耗(奇迹德 OTK 核心通道)
    board_cids = [e.card_id for e in st.board(me) if e.card_id]
    engines = sum(board_cids.count(cid)
                  for cid in {c for c in board_cids
                              if build_piece(c, 0, analyzer).engine})
    pieces = {(cid, cost): build_piece(cid, cost, analyzer)
              for cid, cost in dict.fromkeys(hand + known_draws)}
    opp = st.opponent_key()
    enemy_total = st.hero_total_hp(opp) if opp is not None else None
    plan = best_line(
        initial_state(st.mana_now(me), tuple(sorted(hand)), sp,
                      known_draws=tuple(known_draws), engines=engines,
                      turn=st.friendly_turn_number(), enemy_total=enemy_total,
                      board_atk=st.board_face_attack(me)),
        pieces, exp_per_draw=exp_per_draw)
    return {"actions": list(plan.actions), "total": plan.total,
            "face_det": plan.face_det, "face_exp": plan.face_exp,
            "lethal": plan.lethal, "enemy_total": plan.enemy_total,
            "board_atk": plan.board_atk,
            # 缺牌(卡表无条目)=效果未知: 与线内未覆盖张合并诚实计数
            # (缺牌 inert 进不了线, 但它是斩杀线可信度的一部分; 审计 低#9)
            "uncovered_n": plan.uncovered_n
            + sum(1 for cid, _c in hand if analyzer.carddb.raw(cid) is None),
            # 每步打出后的剩余费(planner 事实原样透传; 悬浮窗数据行"剩费N"用)
            "mana_trace": plan.mana_trace}


def collect_card_ids(log_paths) -> set:
    """扫描日志文件中出现过的全部 cardId(只采集, 不解析)。"""
    ids: set = set()
    for p in log_paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _CID_SCAN_RE.finditer(text):
            ids.add(m.group(1))
    return ids
