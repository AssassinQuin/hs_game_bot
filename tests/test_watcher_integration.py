"""端到端: fixture 日志 -> watcher 回放 -> 链路/快照/JSONL 输出。"""
import json
import re
from pathlib import Path

from hsbot.carddb import CardDB
from hsbot.config import Config
from hsbot.watcher import Watcher

FIXTURE = Path(__file__).parent / "fixtures" / "mini_game.log"


def _run(tmp_path):
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path)})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lambda m: lines.append(m.full))
    w.run_replay(FIXTURE)
    return "\n".join(lines), tmp_path / "sessions"


def test_replay_produces_chain_snapshot_jsonl(tmp_path):
    text, sessions = _run(tmp_path)
    m = re.search(r"新对局 ([0-9a-f]{8})", text)
    assert m, "新对局通知未带对局 hash"
    gid = m.group(1)
    assert f"[{gid}·T" in text                   # 链路行首同带对局 hash
    assert "完整快照" in text                      # 回合切换触发了快照
    assert "对局结束" in text                      # 终局行
    assert "1=WON" in text and "2=LOST" in text     # 终局行双方胜负完整(批尾快照)
    assert "快照导出失败" not in text              # 旧路径的失败模式必须消失
    assert "打出 CS2_029" in text                  # PLAY 块延迟发(fixture 友方未解析, 事件仍按 ctrl 发)
    assert "触发" in text                          # 新事件(spec §3.4)
    # 注: 三条抽牌路径均以 friendly 为门, fixture 未解析友方故无"抽到"行 ——
    # 抽牌路径由 test_store_core / test_store_blocks 单测覆盖
    jsonl = sorted(sessions.glob("*/game_001.jsonl"))
    assert jsonl, "JSONL 未落盘"
    payload = json.loads(jsonl[0].read_text(encoding="utf-8").splitlines()[0])
    assert payload["reason"] and payload["players"] and "me" in payload
    assert payload["game_id"] == gid    # 快照持久化与控制台输出同一对局 hash(合并主键)


def test_mulligan_offer_prints_when_group_closes(tmp_path):
    """起手可留必须随发牌即出(2026-09-13 实测: 旧扣包规则拖到玩家确认留牌才一起出)。
    Choices 包组以非包节头行(DebugPrintPowerList)收口, 该批尾必须立即处理。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lambda m: lines.append(m.full))
    src = FIXTURE.parent.joinpath("mulligan_offer.log").read_text(
        encoding="utf-8").splitlines()

    w._feed_many(src[:-1])            # 尾包(Choices)仍在续写, 扣发
    w._after_batch()
    assert not any("起手可留" in t for t in lines)

    w._feed_many(src[-1:])            # 组边界行: 上一包组已写完整
    w._after_batch()
    offer = [t for t in lines if "起手可留" in t]
    assert offer, "包组收口后起手可留仍未发出"
    assert "第1张、第2张、第3张" in offer[-1]   # 发牌时刻牌名日志里尚未揭示


def test_stat_zone_emits_on_change_only(tmp_path):
    """上部信息区: 对局中批尾发 KIND_STAT(敌血/斩杀/法强/回费/费用),
    状态未变的后续批尾不重发(变化才发, 零冗余流量)。"""
    from hsbot.overlay import KIND_STAT

    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    src = FIXTURE.parent.joinpath("mulligan_offer.log").read_text(
        encoding="utf-8").splitlines()
    w._feed_many(src)
    w._after_batch()
    stats = [m for m in msgs if m.kind == KIND_STAT]
    assert stats, "对局中信息区未发射"
    assert "敌" in stats[-1].ui and "斩杀" in stats[-1].ui
    w._after_batch()                     # 状态未变: 不再发
    assert len([m for m in msgs if m.kind == KIND_STAT]) == len(stats)


def test_advice_msg_carries_machine_fields(tmp_path):
    """留牌建议 Msg 必须带 data=advice 机读字段(2026-09-14 三区布局:
    悬浮窗推荐区从机读字段渲染, 不从文本反推)。"""
    from hsbot.overlay import KIND_ADVICE

    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    stub_adv = {"keep": ["X"], "drop": [], "per_card": {},
                "opp_class": "PRIEST", "coin": False}

    class Stub:
        @staticmethod
        def advise(offered, cls, coin, explore=False, seed=None):
            return dict(stub_adv)

    w.analyzer.mulligan = Stub()       # 注入建议器(enrich 经 analyzer 取用)
    w.run_replay(FIXTURE.parent / "mulligan_offer.log")
    advs = [m for m in msgs if m.kind == KIND_ADVICE]
    assert advs, "留牌建议未走 KIND_ADVICE 通道"
    assert advs[0].data == stub_adv, "advice Msg 未携带机读字段"


def test_tail_state_classification():
    """尾包完整性判定的行形契约: 包节头/续写/组边界(含 PowerTaskList 镜像流)。"""
    from hsbot.watcher import _tail_state
    P = "D 09:55:31.7568067 "
    assert _tail_state(P + "GameState.DebugPrintPower() - TAG_CHANGE Entity=1 tag=TURN value=1 ") == "header"
    assert _tail_state(P + "GameState.DebugPrintPower() -     tag=ZONE value=HAND ") == "cont"
    assert _tail_state(P + "GameState.DebugPrintPower() - BLOCK_END") == "other"
    assert _tail_state(P + "GameState.DebugPrintEntityChoices() -   Entities[0]=[id=51 cardId=]") == "cont"
    assert _tail_state(P + "GameState.DebugPrintEntityChoices() - id=1 Player=x ChoiceType=MULLIGAN") == "header"
    assert _tail_state(P + "GameState.DebugPrintPowerList() - Count=3") == "other"
    assert _tail_state(P + "GameState.DebugPrintGame() - BuildNumber=250339") == "other"
    assert _tail_state(P + "PowerTaskList.DebugDump() - ID=1 ParentID=0 TaskCount=94") == "other"
    assert _tail_state(P + "PowerTaskList.DebugPrintPower() - TAG_CHANGE Entity=2 tag=MULLIGAN_STATE value=INPUT ") == "other"
    assert _tail_state(P + "PowerTaskList.DebugPrintPower() -     CREATE_GAME") is None


def test_second_game_with_swapped_sides_keeps_turn_flow(tmp_path):
    """2026-09-13 实测回归: hslog player_manager 跨局带旧 名字→player_id 映射,
    换边局(第2局主客互换)的 DebugPrintGame 改名撞 InconsistentPlayerIdError 被
    feed_line 吞掉 → CURRENT_PLAYER 全解析到同一 key → 回合切换事件全灭。
    每局 CREATE_GAME 处必须重置 manager。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    w.run_replay(FIXTURE.parent / "two_games_swap.log")
    ui = "\n".join(m.ui for m in msgs)
    full = "\n".join(m.full for m in msgs)
    gids = re.findall(r"──── ([0-9a-f]{8}) · 我的第1回合开始", ui)
    assert len(gids) == 2 and gids[0] != gids[1]   # 两局 hash 各自唯一(合并主键)
    assert re.search(rf"── T\d+ {gids[0]}\(回合结束\)", ui)   # 第1局: 对手回合触发快照
    assert re.search(rf"── T\d+ {gids[1]}\(回合结束\)", ui)   # 第2局对手回合快照不再被吞
    # 主客归位: 两局快照里"我"都是湫然(名字可能因 manager 污染一致地错,
    # 强归位由真实日志回放验收; 这里至少锁定换边局的回合流程不被吞)
    assert "我  湫然#51704" in full


def test_training_includes_raw_log_slice(tmp_path):
    """当局 Power.log 完整切片随训练样本落盘(回放验收/复现用)。"""
    # training_dir 必须隔离: 缺省是相对仓库的真实语料目录 —— 本测试曾把合成
    # 的 mini_game 样本写进 data/training/未知卡组/live_g01.*(审计 2026-09-14 低#8)
    cfg = Config.load({"overlay_enabled": False, "auto_training": True,
                       "auto_train_models": False,   # 模型链会带真实 config 起子进程写 trainer/data
                       "data_dir": str(tmp_path),
                       "training_dir": str(tmp_path / "training"),
                       "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    from hsbot.persist import SessionStore
    w = Watcher(cfg, carddb, out=lambda m: None)
    w.store = SessionStore(cfg.sessions_dir)
    w._live = True                       # 模拟实时来源(回放不自动导训练)
    w._feed_many(FIXTURE.read_text(encoding="utf-8").splitlines())
    w._after_batch()
    w._fire_pending_game_end()
    logs = list(cfg.training_dir.glob("**/*.power.log"))
    assert logs, "原始日志切片未落盘"
    snaps = sorted((cfg.sessions_dir).glob("*/game_001.jsonl"))
    assert snaps, "会话快照未落盘"
    payload = json.loads(snaps[0].read_text(encoding="utf-8").splitlines()[0])
    assert "recent_events" in payload, "富化事件未随快照持久化"
    evs = payload["recent_events"]
    assert evs and all("kind" in e for e in evs), "富化事件列表为空/缺 kind"
    text = logs[0].read_text(encoding="utf-8")
    assert "CREATE_GAME" in text
    jsonl = logs[0].name.replace(".power.log", ".jsonl")
    assert (logs[0].parent / jsonl).exists(), "切片与样本不在同目录/不同名"


def test_saved_decklist_fills_deck_side(tmp_path):
    """回放/会话中途挂载没有 Decks.log: 用已落盘的 decklist.json 兜底 ——
    信息区库侧(斩杀/回费/费用)照常计算, 不再降级为 ?。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    (tmp_path / "decks").mkdir()
    (tmp_path / "decks" / "decklist.json").write_text(json.dumps(
        {"name": "奇迹德",
         "code": "AAEBAfHGBwL9jQaluwYO/gHTA4/2AuC+A/DUA7ClBK7ABNXSBIHUBIDKBpD0"
                 "BpX0BqqvB4LaBwAA", "cards": {}}, ensure_ascii=False),
        encoding="utf-8")
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    w.run_replay(FIXTURE.parent / "mulligan_offer.log")
    stats = [m.ui for m in msgs if m.ui.startswith("敌 ")]
    assert stats, "信息区未发射"
    assert all("库?" not in s for s in stats), "库侧未用已保存卡组计算"
    assert any(re.search(r"库\d", s) for s in stats)


def test_opponent_real_name_collected(tmp_path):
    """2026-09-13 实测: 开局对手名是 UNKNOWN HUMAN PLAYER, 真名在留牌确认等
    后续行才出现 —— 行级采集后终局行/快照应显示真名而非 UNKNOWN/PlayerN。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    w = Watcher(cfg, carddb, out=lambda m: None)
    src = FIXTURE.parent.joinpath("mulligan_offer.log").read_text(
        encoding="utf-8").splitlines()
    w._feed_many(src)
    w._after_batch()
    # 追加对手真名的确认行
    w._feed_many(["D 09:55:57.5968189 GameState.DebugPrintEntitiesChosen() - "
                  "id=1 Player=拯救网瘾对手#5318 EntitiesCount=3"])
    w._after_batch()
    opp = w.gs.opponent_key()
    assert w.gs.name(opp) == "拯救网瘾对手#5318"
    assert w.gs.name(w.gs.friendly_key) == "湫然#51704"


def test_attach_survives_batch_exception(tmp_path, monkeypatch):
    """2026-09-13 实测: 追平历史时终局快照异常曾一路炸穿 _attach 杀死监控线程
    (悬浮窗静默假死)。追平失败必须吞掉并继续实时。"""
    cfg = Config.load({"overlay_enabled": False, "auto_training": False,
                       "data_dir": str(tmp_path), "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    w = Watcher(cfg, carddb, out=lambda m: None)

    calls = {"n": 0}
    def boom():
        calls["n"] += 1
        raise RuntimeError("模拟追平期异常")
    monkeypatch.setattr(w, "_after_batch", boom)

    offset = w._attach(FIXTURE)           # 不得向外抛异常
    assert offset > 0                     # 仍返回文件尾偏移, 实时可继续
    assert calls["n"] == 1                # 首批即炸, 兜底后跳过余下历史


def test_live_training_export_dedup(tmp_path):
    """审计 2026-09-13 中#8: live 导出与 import-all 共用 _imported.json 索引,
    重复 attach 同一会话时已导过的 (session|局号) 不再重导。"""
    from .conftest import mk_create_game
    from hsbot.consts import game_hash
    from hsbot.corpus import CorpusExporter

    cfg = Config.load({"overlay_enabled": False, "auto_training": True,
                       "auto_train_models": False,   # 模型链会带真实 config 起子进程写 trainer/data
                       "data_dir": str(tmp_path),
                       "training_dir": str(tmp_path / "training")})
    ex = CorpusExporter(cfg, CardDB("/nonexistent.json"))

    class _T:
        ts = "20:00:00.0"
        packets = [mk_create_game()]

    path = ex.export_if_new(_T(), session="S", idx=1)
    assert path is not None and path.exists()
    meta = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert meta["game_id"] == game_hash("S", "20:00:00.0")   # meta 带对局 hash(可复算)
    assert meta["linked_effects"] == []                       # 联动卡效果台账(终局态)
    assert ex.export_if_new(_T(), session="S", idx=1) is None    # 已收录: 跳过
    assert ex.export_if_new(_T(), session="S", idx=2) is not None  # 新局照常导
    done = json.loads(ex.index_path.read_text(encoding="utf-8"))
    assert done == ["S|1", "S|2"]


def test_two_games_one_batch_exports_each_game(tmp_path):
    """审计 2026-09-14 一#2: 上一局终局行与下一局 CREATE_GAME 同批(attach 追平
    常见)时, 每局的终局导出/切片/事件必须仍按"该局自己的树与行"结算:
      a) 中间局的 store 不得用被边界重置的 player manager 建(玩家解析失败→事件全灭);
      b) 局的 .power.log 原始切片不得被下一局头部行覆写/污染;
      c) jsonl 的对局 hash 逐局各得其所(不得两局都导成 games[-1])。
    回放入口按 CREATE_GAME 切段永不跨局, 故直接 _feed_many 一批喂入复现。
    """
    from hsbot.persist import SessionStore

    cfg = Config.load({"overlay_enabled": False, "auto_training": True,
                       "auto_train_models": False,   # 模型链会带真实 config 起子进程写 trainer/data
                       "data_dir": str(tmp_path),
                       "training_dir": str(tmp_path / "training"),
                       "battletag": "湫然#51704"})
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")
    msgs: list = []
    w = Watcher(cfg, carddb, out=msgs.append)
    w.store = SessionStore(cfg.sessions_dir)
    w.session_name = "two_games_one_batch"
    w.decks_path = None
    w.model_trainer.request = lambda: None          # 测试不起训练子进程
    w._live = True                                  # 模拟实时来源(才走自动导出)
    w._feed_many((FIXTURE.parent / "two_games_one_batch.log")
                 .read_text(encoding="utf-8").splitlines())
    w._after_batch()
    w._fire_pending_game_end()                      # 第二局的终局(批内未到下一边界)

    ui = "\n".join(m.ui for m in msgs)
    gids = re.findall(r"── 新对局 ([0-9a-f]{8})", ui)
    assert len(gids) == 2 and gids[0] != gids[1]    # 两局各自建作用域
    ends = re.findall(r"([0-9a-f]{8})(?: 快照)?\(终局\)", ui)
    assert sorted(set(ends)) == sorted(set(gids)), ui  # 每局都发出自己的终局快照
    # 中间局的友方绑定不等批尾(作用域将被下一局替换) —— 终局快照须已解析"我"
    assert len(re.findall(r"\(终局\) 我:", ui)) == 2
    # 每局的 jsonl 各自落盘, 且 hash 与各自终局行对齐(不得两局同 hash)
    jsonls = {}
    for p in sorted(cfg.training_dir.glob("**/*_g*.jsonl")):
        meta = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        jsonls[meta["game_id"]] = p
    assert set(jsonls) == set(gids), f"jsonl hash 集合不符: {sorted(jsonls)} vs {sorted(gids)}"
    # 原始切片逐局各含自己的行, 不串局
    raws = sorted(cfg.training_dir.glob("**/*.power.log"),
                  key=lambda p: p.read_text(encoding="utf-8"))
    assert len(raws) == 2, f"应有两局切片, 实得 {[p.name for p in raws]}"
    g1_text, g2_text = (r.read_text(encoding="utf-8") for r in raws)
    assert "21:00:00" in g1_text and "21:01:00" not in g1_text   # 第1局切片无第2局行
    assert "21:01:00" in g2_text and "21:00:00" not in g2_text   # 第2局切片无第1局行
    assert g1_text.count("CREATE_GAME") == 1 and g2_text.count("CREATE_GAME") == 1


# ================= AutoModelTrainer(审计 2026-09-14 一#7) =================

class _NoopThread:
    def start(self): pass


def test_auto_trainer_disabled_by_config(tmp_path, monkeypatch):
    """auto_train_models=False → 不起训练线程(修复死开关: 旧键名不在 config
    schema, getattr 恒真, 无法关闭)。"""
    from hsbot.watcher import AutoModelTrainer
    spawned = []
    monkeypatch.setattr("hsbot.watcher.threading.Thread",
                        lambda *a, **k: spawned.append(1) or _NoopThread())
    cfg = Config.load({"data_dir": str(tmp_path), "auto_train_models": False})
    AutoModelTrainer(cfg, None).request()
    assert spawned == []
    # 键在 schema 里(未知键会被白名单静默丢弃的旧行为已修, 见 config 测试)


def test_auto_trainer_absolute_data_dir():
    """子进程 --data-dir 必须绝对路径: bot 从非仓库目录启动时相对路径会让
    trainer 与 bot 各对一套语料/模型目录(全链路静默错位)。"""
    from hsbot.watcher import AutoModelTrainer
    t = AutoModelTrainer(Config.load({}), None)      # 缺省相对 "data"
    for cmd in t.commands():
        i = cmd.index("--data-dir")
        assert Path(cmd[i + 1]).is_absolute(), cmd


def test_auto_trainer_failure_visible(tmp_path, monkeypatch):
    """失败 ≠ 无变化: 子命令失败必须显性上报, 不得以"完成/无变化"收尾。"""
    from hsbot.watcher import AutoModelTrainer

    class _P:
        returncode = 1

        def communicate(self, timeout=None):
            return ("", "boom")

    monkeypatch.setattr("hsbot.watcher.subprocess.Popen", lambda *a, **k: _P())
    msgs: list = []
    AutoModelTrainer(Config.load({"data_dir": str(tmp_path)}),
                     msgs.append)._run()              # 直接跑(不起线程)
    assert msgs and any("失败" in m for m in msgs), msgs
    assert not any("完成" in m or "无变化" in m for m in msgs)


def test_auto_trainer_exit_cleanup(tmp_path, monkeypatch):
    """退出清理: atexit 注册 + 清理会终止在跑的子进程(孤儿最长 900s)。"""
    import atexit
    from hsbot.watcher import AutoModelTrainer
    reg = []
    monkeypatch.setattr(atexit, "register", lambda fn: reg.append(fn))
    t = AutoModelTrainer(Config.load({"data_dir": str(tmp_path)}), None)
    assert t._cleanup in reg                          # 已注册退出钩子
    killed = []

    class _P:
        def poll(self):
            return None

        def terminate(self):
            killed.append(1)

    t._proc = _P()
    t._cleanup()
    assert killed == [1]


def test_auto_trainer_popen_decodes_utf8(tmp_path, monkeypatch):
    """子进程输出是 UTF-8(喂了 PYTHONIOENCODING), 但父进程 text=True 未钉
    encoding → 本机 GBK locale 在 _readerthread 解码崩(实测 2026-09-14 晚),
    communicate 吞空 → 成功训练被正则漏掉、上报成"无变化"(违 R12)。"""
    from hsbot.watcher import AutoModelTrainer
    captured = {}

    class _P:
        returncode = 0

        def communicate(self, timeout=None):
            return ("模型已保存: data/models/mulligan/测试/v002\n", "")

    def fake_popen(*a, **k):
        captured.update(k)
        return _P()

    monkeypatch.setattr("hsbot.watcher.subprocess.Popen", fake_popen)
    msgs: list = []
    AutoModelTrainer(Config.load({"data_dir": str(tmp_path)}),
                     msgs.append)._run()              # 直接跑(不起线程)
    assert captured.get("encoding") == "utf-8"        # ← 修复前缺失(红)
    assert any("v002" in m for m in msgs), msgs       # UTF-8 输出能被捕获解析
