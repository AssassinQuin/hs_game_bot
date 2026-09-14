"""出牌推理层(T2) —— 世界节点 × 价值模型排序(docs/PLAY_ADVICE.md §1-T2)。

与 mulligan_ai 完全同款模式(已验证的热更新军师): 模型根 data/models/play/<卡组>/,
模型 = python -m trainer train 产物(value.pkl + value_meta.json + LATEST.json),
read_latest 按 mtime 热更新, 无模型/坏产物 → 静默降级(advise 返回 None)。

打分(深度 1 世界节点): 候选 = 每张可支付手牌 + 关键二连"法强牌→法术";
节点特征 = trainer/states.flatten 向量的特征差分(费−、手牌数−1、场面攻血+、
法强+、牌库−1 并入手牌期望), 不模拟 GameStore —— flatten 布局单点复用训练侧,
score = 价值模型 P(胜|局面)。分层铁律: 本层只回机读事实(候选/分数/ΔP),
措辞("推荐/不动")与结论阈值归 render。

开口门槛 PLAY_N_GAMES_MIN: 语料 <300 局不出排序, 只出 T1 斩杀线。
依据 = docs/PLAY_ADVICE.md §1-T2 原文"语料 <300 局不出排序"(§1-T5 的 MCTS
重启前提"价值模型在 300+ 局上 AUC 稳定达标"取同源数字); 统计功效教训来自
留牌 v3 评审(docs/MULLIGAN_AI.md §8): 小语料上评分噪声大, 门控不过就退回
低风险输出 —— 出牌建议直接影响操作, 宁静默勿瞎推。门槛随语料自动伸缩:
训练器重训产物的 value_meta.json n_games 达标即自动开口, 无需配置。
"""
from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path

from hearthstone.enums import GameTag

log = logging.getLogger(__name__)

# ── 开口门槛与输出规模(依据见模块 docstring; 结论词/展示归 render) ──
PLAY_N_GAMES_MIN = 300     # 语料局数门槛: 未达线 advise 恒 None(静默)
PLAY_TOP_K = 3             # 候选上限(与悬浮窗推荐区 top3 同一定版)


def models_root_for(data_dir, deck: str) -> Path:
    """模型根目录: data/models/play/<卡组安全名>(与 mulligan 同款转义)。"""
    safe = "".join("_" if c in '\\/:*?"<>|' else c for c in deck).strip() or "未知卡组"
    return Path(data_dir) / "models" / "play" / safe


def read_latest(root: Path | str) -> tuple[str | None, dict]:
    """读 LATEST 版本产物 → (版本, {"meta": value_meta.json, "model": 评分器})。
    缺产物/坏产物 → (None, {}): 静默降级为"无模型"(一条 warning 可排查)。"""
    latest = Path(root) / "LATEST.json"
    if not latest.exists():
        return None, {}
    try:
        ver = json.loads(latest.read_text(encoding="utf-8"))["version"]
        vdir = Path(root) / ver
        meta = json.loads((vdir / "value_meta.json").read_text(encoding="utf-8"))
        blob = pickle.loads((vdir / "value.pkl").read_bytes())
        return ver, {"meta": meta, "model": blob["model"]}
    except (ValueError, KeyError, OSError, EOFError, AttributeError,
            pickle.UnpicklingError, ModuleNotFoundError) as exc:
        # 坏产物静默变"无模型"无从排查(mulligan 审计同款教训): 留一条 warning
        log.warning("出牌价值模型读取失败(%s): %s —— 出牌建议静默降级", root, exc)
        return None, {}


# ════════════════════ 候选枚举(深度 1 世界节点) ════════════════════
# 候选是自描述机读事实(apply_candidate 纯函数消费, 不再回头查 store):
#   actions   = [(card_id, 实付费), ...] 有序(二连=法强在前)
#   cost      = 本次打出消耗的法力合计
#   hand_idx  = 手牌下标(snap.me.hand 与 st.hand 同序, 差分按此摘牌)
#   draws     = 打出后的抽牌期望张数(自身抽牌 + 施法抽牌引擎, 牌库不足时在
#               apply 侧按余牌封顶)
#   spellpower= 打出后的法强增量(法强牌)
#   board_add = 随从入场面事实 {cid/atk/hp/taunt}(非随从 None)

def candidate_actions(st, analyzer) -> list[dict]:
    """当前局面 → 深度 1 候选(可支付单打 + 关键二连"法强牌→法术")。
    非我方回合/友方未解析 → []。推理不改变 store, 不产生事件。"""
    me = st.friendly_key
    if me is None or not st.is_my_turn():
        return []
    mana = st.mana_now(me)
    carddb = analyzer.carddb
    hand = []
    for i, e in enumerate(st.hand(me)):
        cid = getattr(e, "card_id", None)
        if not cid:
            continue
        cost = e.tags.get(GameTag.COST)          # 减费在身的实际费优先
        if cost is None:
            cost = carddb.cost(cid)              # 卡表缺牌 → 0(诚实降级)
        hand.append((i, cid, cost if cost is not None else 0))
    playable = [h for h in hand if h[2] <= mana]
    engines = _cast_draw_engines(st, analyzer)

    def _single(entry) -> dict:
        i, cid, cost = entry
        ct = carddb.cardtype(cid)
        # cast_draw 触发句会被抽牌族裸规则误标一条 Draw(1)(effects.MECHANICS
        # 已注明"消费方忽略 Draw 即可"): 该抽属触发不属打出, 记 0
        own_draw = 0 if analyzer.has_mechanic(cid, "cast_draw") \
            else analyzer.draw_amount(cid)
        board_add = None
        if ct == "MINION":
            raw = carddb.raw(cid) or {}
            board_add = {"cid": cid,
                         "atk": int(raw.get("attack") or 0),
                         "hp": int(raw.get("health") or 0),
                         "taunt": "TAUNT" in (raw.get("mechanics") or [])}
        return {"actions": [(cid, cost)], "cost": cost, "hand_idx": [i],
                "draws": own_draw + (engines if ct == "SPELL" else 0),
                "spellpower": analyzer.spellpower_gain(cid),
                "board_add": board_add}

    out = [_single(h) for h in playable]
    # 关键二连: 法强牌→法术(法强只惠及后续法术, 顺序敏感 —— 与 docs §1-T2
    # "法强牌→法术"同款; 其余二连留给更深搜索, 骨架不做)
    sp_entries = [h for h in playable if analyzer.spellpower_gain(h[1]) > 0]
    if sp_entries:
        spells = [h for h in playable if carddb.cardtype(h[1]) == "SPELL"]
        for si, scid, scost in sp_entries:
            gain = analyzer.spellpower_gain(scid)
            for ti, tcid, tcost in spells:
                if ti == si or scost + tcost > mana:
                    continue
                out.append({"actions": [(scid, scost), (tcid, tcost)],
                            "cost": scost + tcost, "hand_idx": [si, ti],
                            "draws": analyzer.draw_amount(tcid) + engines,
                            "spellpower": gain, "board_add": None})
    return out


def _cast_draw_engines(st, analyzer) -> int:
    """我方场上施法抽牌引擎数(拍卖师族, 机制标记驱动不逐卡写死)。"""
    me = st.friendly_key
    return sum(1 for e in st.board(me)
               if e.card_id and analyzer.has_mechanic(e.card_id, "cast_draw"))


def apply_candidate(snap: dict, cand: dict) -> dict:
    """基线快照 + 候选 → 打出后世界节点的特征差分快照(纯函数, 不碰 store)。
    差分口径(docs §1-T2): 费−cost、手牌摘掉打出的牌、随从入场(攻血+嘲讽)、
    法强+、抽牌期望 = 牌库−n 并以匿名牌入手(成本未知 → 手牌费合计不变)。"""
    me = dict(snap["me"])
    me["hand"] = list(snap["me"]["hand"])
    me["board"] = list(snap["me"]["board"])
    for i in sorted(cand.get("hand_idx") or [], reverse=True):
        if 0 <= i < len(me["hand"]):
            me["hand"].pop(i)
    me["mana"] = max(0, me["mana"] - cand.get("cost", 0))
    n_draw = min(cand.get("draws", 0), me["deck"]) if me["deck"] else 0
    me["deck"] = me["deck"] - n_draw
    me["hand"].extend({"cid": None, "cost": None} for _ in range(n_draw))
    if cand.get("board_add"):
        me["board"].append(dict(cand["board_add"]))
    me["spellpower"] = me["spellpower"] + cand.get("spellpower", 0)
    return {"turn": snap["turn"], "my_turn": snap["my_turn"],
            "me": me, "opp": snap["opp"]}


# ════════════════════ 实时建议器(mulligan 同款热更新) ════════════════════

def _score(model, snap: dict) -> float:
    """世界节点 → P(胜): flatten 布局单点复用训练侧(trainer.states), 模型只
    认向量。模型对象来自 trainer 产物(value.pkl), 延迟 import 防循环依赖。"""
    from trainer.states import flatten
    vec, _ = flatten(snap)
    return float(model.predict_proba([vec])[0][1])


class PlayAdvisor:
    """实时出牌建议: 读 LATEST 价值模型(mtime 热更新), 同步出排序事实。

    用法: advisor.advise(store, analyzer) → 事实 dict | None(无模型/未达门槛/
    无候选)。事实字段(机读, 措辞归 render):
      kind="play_offer" / version / n_games / turn / baseline("不动"的 P(胜))
      candidates = [{actions[(cid, 实付费)...], names, cost, p, delta_p}, …]
    打分口径: 每个候选 = 基线快照 + 特征差分 → P(胜); delta_p = p − baseline
    (相对"不动"的胜率增量, docs §2 统计最优级)。"""

    def __init__(self, root: Path | str, carddb=None) -> None:
        self.root = Path(root)
        self.carddb = carddb
        self._stamp = None                  # LATEST mtime 失配即重读
        self._ver, self._meta, self._model = None, {}, None

    def _refresh(self) -> None:
        latest = self.root / "LATEST.json"
        try:
            stamp = latest.stat().st_mtime if latest.exists() else None
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        self._stamp = stamp
        self._ver, art = read_latest(self.root)
        self._meta = art.get("meta") or {}
        self._model = art.get("model")

    def _names(self, actions: tuple) -> list:
        out = []
        for cid, _cost in actions:
            try:
                n = self.carddb.name(cid) if self.carddb else cid
            except Exception:                 # noqa: BLE001  假/半残卡表不拖垮建议
                n = None
            out.append(str(n or cid))
        return out

    def advise(self, st, analyzer) -> dict | None:
        self._refresh()
        if self._model is None:
            return None                       # 无模型: 静默降级(只出 T1 斩杀线)
        n_games = int(self._meta.get("n_games") or 0)
        if n_games < PLAY_N_GAMES_MIN:
            return None                       # 语料门槛未达: 不出排序
        me = st.friendly_key
        if me is None or not st.is_my_turn():
            return None
        from trainer.states import snapshot as make_snapshot   # 接合点=文件
        snap = make_snapshot(st, analyzer.carddb)
        cands = candidate_actions(st, analyzer)
        if snap is None or not cands:
            return None
        baseline = _score(self._model, snap)
        scored = []
        for c in cands:
            p = _score(self._model, apply_candidate(snap, c))
            scored.append({"actions": list(c["actions"]), "cost": c["cost"],
                           "names": self._names(c["actions"]),
                           "p": p, "delta_p": p - baseline})
        scored.sort(key=lambda r: (-r["p"], r["cost"]))   # 平分取更省(planner 同思想)
        return {"kind": "play_offer", "version": self._ver, "n_games": n_games,
                "turn": snap["turn"], "baseline": baseline,
                "candidates": scored[:PLAY_TOP_K]}
