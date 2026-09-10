# CLI 子命令化 + 日志双通道 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 10 个 CLI 参数缩成 `--config` + `replay/import-all` 子命令;诊断日志改 logging(文件 DEBUG 完整/控制台 WARNING+);业务输出改结构化 Msg(UI 简洁版、会话 .log 完整版)。

**Architecture:** 三层各自独立可测——render 新增单行快照纯函数;overlay 的 OutputHub 消息从裸 str 改 `Msg(kind, ui, full)` 三路分发;watcher `_emit` 构造 Msg 并把异常改 `log.exception`。main 装配子命令 argparse + logging 双 handler,永远构造 OutputHub。

**Tech Stack:** Python 3.10+(标准库 argparse/logging/dataclasses/queue/tkinter),pytest。

**Spec:** `docs/superpowers/specs/2026-09-10-cli-and-logging-design.md`

## Global Constraints

- commit message 中文,前缀沿用仓库惯例(feat:/refactor:/docs:/test:/chore:)
- 不动 examples/、scripts/ 其他脚本的 argparse;不动 config.yaml
- `Msg.full` 缺省 = `ui`(链路行/终局行/通知行单文本)
- `OutputHub.__call__` 写文件用 `msg.full`,控制台打印 `msg.ui`,悬浮窗队列放 `(msg.kind, msg.ui)`
- 悬浮窗上色仅 snapshot(浅黄 `#ffd479`)与 game_end(浅蓝 `#7ec8ff`),其余默认色
- 会话 .log 文件 = `store.path.with_suffix(".log")`(watcher 现状),内容为 full 版
- 每个任务结束时 `python3 -m pytest tests/ -q` 全绿(当前基线 32 passed)

---

### Task 1: render.snapshot_line() 单行精简快照

**Files:**
- Modify: `hsbot/render.py`(在 `snapshot_block` 之前插入新函数)
- Test: `tests/test_render_knowledge.py`

**Interfaces:**
- Consumes: `GameStore` 现有方法 `st.turn / st.friendly_key / st.opponent_key() / st.mana_fields(key)['res'] / st.mana_now(key) / st.hand(key) / st.deck_count(key) / st.board(key)`(全部为 snapshot_block 已用接口,签名见 `hsbot/store.py`)
- Produces: `snapshot_line(st: GameStore, game_no: int, reason: str) -> str`——Task 3 的 watcher `_snapshot` 调用

- [ ] **Step 1: 写失败测试**

在 `tests/test_render_knowledge.py` 末尾追加(复用同文件已有的 `_store()/_board()` 构造器):

```python
from hsbot.render import snapshot_line  # 加到文件顶部 import 行


def test_snapshot_line_compact_single_line():
    st, _ = _store()
    _board(st)
    line = snapshot_line(st, 1, "turn_end")
    assert line.startswith("── T")          # 回合头
    assert "水晶" in line                    # 我方水晶
    assert "手牌1" in line                   # _board 构造了 1 张手牌 CS2_029
    assert "牌库" in line
    assert "场上1" in line                   # 对面场面 CS2_120 ×1
    assert "\n" not in line                 # 必须单行
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_render_knowledge.py::test_snapshot_line_compact_single_line -v`
Expected: FAIL,`ImportError: cannot import name 'snapshot_line'`

- [ ] **Step 3: 实现 snapshot_line**

在 `hsbot/render.py` 的 `_REASON_CN` 定义之后、`snapshot_block` 之前插入:

```python
def snapshot_line(st: GameStore, game_no: int, reason: str) -> str:
    """单行精简快照(UI 用): 悬浮窗/控制台只看节奏, 完整版见 snapshot_block。"""
    reason_cn = _REASON_CN.get(reason, reason)
    me = st.friendly_key
    if me is None:                          # 友方未解析: 只报局号与原因
        return f"── 第{game_no}局快照({reason_cn}) ──"
    f = st.mana_fields(me)
    opp = st.opponent_key()
    opp_board = len(st.board(opp)) if opp is not None else 0
    return (f"── T{st.turn} 第{game_no}局({reason_cn})"
            f" 我:水晶{st.mana_now(me)}/{f['res']} │ 手牌{len(st.hand(me))}"
            f" │ 牌库{st.deck_count(me)} │ 场上{len(st.board(me))}"
            f" │ 对面场上{opp_board} ──")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_render_knowledge.py -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add hsbot/render.py tests/test_render_knowledge.py
git commit -m "feat: render.snapshot_line 单行精简快照(UI 简洁版)"
```

---

### Task 2: overlay.Msg + OutputHub 双文本分发 + 悬浮窗按 kind 上色

**Files:**
- Modify: `hsbot/overlay.py`
- Test: `tests/test_output_hub.py`(新建)

**Interfaces:**
- Consumes: 无(纯改造本模块)
- Produces(Task 3/5 依赖):
  - `Msg(kind: str, ui: str, full: str | None = None)` dataclass,`full is None` 时自动等于 `ui`
  - `OutputHub.__call__(msg: Msg) -> None`(旧 str 签名作废)
  - `OutputHub.q` 队列元素变为 `(kind, ui)` 元组(OverlayWindow.poll 消费)
  - `kind` 取值约定:`"chain" | "snapshot" | "game_end" | "notice"`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_output_hub.py`:

```python
"""OutputHub 双文本分发: ui 进控制台/队列, full 进文件。"""
import queue

from hsbot.overlay import Msg, OutputHub


def test_msg_full_defaults_to_ui():
    m = Msg("chain", "[T1·我] 抽到 X")
    assert m.full == m.ui


def test_hub_dispatch_console_queue_file(tmp_path, capsys):
    hub = OutputHub(console=True, to_overlay=True)
    hub.set_file(tmp_path / "session.log")
    hub(Msg("chain", "链路行"))
    hub(Msg("snapshot", ui="── T1 单行 ──", full="完整快照块\n多行"))
    out = capsys.readouterr().out
    assert "链路行" in out and "── T1 单行 ──" in out
    assert "完整快照块" not in out                 # 控制台只见 ui
    assert hub.q.get_nowait() == ("chain", "链路行")
    assert hub.q.get_nowait() == ("snapshot", "── T1 单行 ──")
    content = (tmp_path / "session.log").read_text(encoding="utf-8")
    assert "链路行" in content
    assert "完整快照块\n多行" in content            # 文件见 full


def test_hub_without_overlay_and_console(tmp_path):
    hub = OutputHub(console=False, to_overlay=False)
    assert hub.q is None
    hub(Msg("notice", "x"))                     # 不抛错即可
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_output_hub.py -v`
Expected: FAIL,`ImportError: cannot import name 'Msg'`

- [ ] **Step 3: 实现 Msg 与双文本分发**

`hsbot/overlay.py` 改三处。

(1) 顶部 import 区(第 9-13 行)加 dataclass,并在模块级加颜色表:

```python
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path

_MAX_LINES = 1500  # 窗口内保留的最大行数(防止内存/渲染膨胀)

_KIND_COLORS = {"snapshot": "#ffd479", "game_end": "#7ec8ff"}  # 其余 kind 默认色


@dataclass
class Msg:
    """业务输出消息: ui 版进控制台/悬浮窗, full 版进会话 .log 文件。"""

    kind: str                  # chain / snapshot / game_end / notice
    ui: str
    full: str | None = None    # 缺省 = ui

    def __post_init__(self) -> None:
        if self.full is None:
            self.full = self.ui
```

(2) `OutputHub.__call__` 整体替换(原 31-42 行):

```python
    def __call__(self, msg: Msg) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    with self._file.open("a", encoding="utf-8") as fp:
                        fp.write(msg.full + "\n")
                except OSError:
                    pass
        if self.q is not None:
            self.q.put((msg.kind, msg.ui))
        if self.console:
            print(msg.ui)
```

(3) `OverlayWindow.run` 里:在 `sb = tk.Scrollbar(...)` 行之前加 tag 配置,并把 `poll()` 里 insert 行改为解包元组:

```python
        for tag, color in _KIND_COLORS.items():
            txt.tag_configure(tag, foreground=color)
```

poll 改为:

```python
        def poll() -> None:
            lines = []
            while len(lines) < 400:
                try:
                    lines.append(self.q.get_nowait())
                except queue.Empty:
                    break
            if lines:
                txt.configure(state="normal")
                for kind, text in lines:
                    txt.insert("end", text + "\n", kind)
                count = int(float(txt.index("end-1c")))
                if count > _MAX_LINES:
                    txt.delete("1.0", f"{count - _MAX_LINES + 1}.0")
                txt.see("end")
                txt.configure(state="disabled")
            root.after(120, poll)
```

(4) 模块 docstring 第一段(第 3 行)`三路分发(控制台 / 会话记录文件 / overlay 队列)` 后补一句:`消息为 Msg 结构: 控制台/悬浮窗收 ui 简洁版, 文件收 full 完整版。`

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_output_hub.py -v && python3 -m pytest tests/ -q`
Expected: 新测试 PASS;全套无回归(watcher 尚未接 Msg,`test_watcher_integration` 仍走旧 str 路径——`Watcher.out` 默认此时还是 print,不受本任务影响)

- [ ] **Step 5: Commit**

```bash
git add hsbot/overlay.py tests/test_output_hub.py
git commit -m "refactor: OutputHub 消息结构化 Msg(kind,ui,full) 三路分发+悬浮窗按 kind 上色"
```

---

### Task 3: watcher 接 Msg(快照 ui/full 分裂)+ 异常 log.exception + 消费方适配

**Files:**
- Modify: `hsbot/watcher.py`(`_emit`/`_route`/`_snapshot`/`_process_tree`/`_export_training`/`__init__`)
- Modify: `scripts/replay_capture.py:19`
- Modify: `tests/test_watcher_integration.py:16-17`
- Test: `tests/test_watcher_integration.py`(现有测试即回归)

**Interfaces:**
- Consumes: `Msg`(Task 2)、`snapshot_line(st, game_no, reason)`(Task 1)
- Produces:
  - `Watcher.__init__(cfg, carddb, out=None, hub=None)`:`out` 回调签名变为接收 `Msg`(默认 sink 打印 `msg.ui`)
  - `_emit(msg: str | Msg, kind: str = "notice")`——str 入参自动包 `Msg(kind, msg)`,现有 8 处 str 调用点(会话通知/回合标题/新对局/训练导出)无需改动

- [ ] **Step 1: 抓改造前 baseline(验收锚点,watcher 未动)**

```bash
python3 scripts/replay_capture.py examples/Power.log -o /tmp/baseline_old
```
Expected: `examples/Power.log: N 行 -> /tmp/baseline_old/Power.txt`,无 `!!快照失败`

- [ ] **Step 2: 改 watcher(一次到位,测试在 Step 3)**

`hsbot/watcher.py` 六处:

(1) 顶部 import 区加 logging(第 9-11 行区域):

```python
import logging
import re
import time
```

模块级(`_CREATE_GAME_MARK` 定义之前)加:

```python
log = logging.getLogger("hsbot.watcher")
```

(2) `from .render import ...`(第 19 行)补 `snapshot_line`:

```python
from .render import chain_line, game_end_line, snapshot_block, snapshot_line
```

并从 `.overlay` 导入 Msg(顶部相对 import 区):

```python
from .overlay import Msg
```

(3) `__init__` 里默认 out sink(原 44 行):

```python
        self.out = out if out is not None else (lambda m: print(m.ui))
```

(4) `_route` 末尾链路行(原 337-339 行)加 kind:

```python
        line = chain_line(evt, self.carddb)
        self.chain.append(line)
        self._emit(line, "chain")
```

(5) `_process_tree` 异常(原 301-302 行)与 `_export_training` 异常(原 362-363 行)改双写(堆栈进日志文件 + UI 单行提示):

```python
            except Exception as exc:  # noqa: BLE001  单包事件失败不拖垮监控
                log.exception("事件处理异常")
                self._emit(f"! 事件处理异常: {type(exc).__name__}: {exc}")
```

```python
        except Exception:  # noqa: BLE001
            log.exception("训练样本导出失败")
            self._emit("! 训练样本导出失败")
```

(6) `_snapshot` 输出段(原 373-379 行)改 ui/full 分裂:

```python
        block = snapshot_block(
            self.gs, led, knowledge=self.knowledge, deck_name=self.cfg.deck_name,
            generic=self.generic, game_no=self.game_no, chain_lines=self.chain,
            chain_summary=self.summary, carddb=self.carddb, reason=reason)
        if reason == "game_end":
            block += "\n" + game_end_line(self.gs)
            ui = snapshot_line(self.gs, self.game_no, reason) + "\n" + game_end_line(self.gs)
            self._emit(Msg("game_end", ui=ui, full=block))
        else:
            self._emit(Msg("snapshot", ui=snapshot_line(self.gs, self.game_no, reason),
                           full=block))
```

(7) `_emit`(原 394-396 行):

```python
    def _emit(self, msg: str | Msg, kind: str = "notice") -> None:
        if self.mute:
            return
        self.out(msg if isinstance(msg, Msg) else Msg(kind, msg))
```

- [ ] **Step 3: 适配两个消费方**

`scripts/replay_capture.py` 第 16-19 行:

```python
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lambda m: lines.append(m.full))
```
(`lines` 类型注解不变——仍收 full 文本,baseline 语义不变)

`tests/test_watcher_integration.py` 第 16-17 行同款:

```python
    lines: list[str] = []
    w = Watcher(cfg, carddb, out=lambda m: lines.append(m.full))
```

- [ ] **Step 4: 回归 + baseline diff**

Run:
```bash
python3 -m pytest tests/ -q
python3 scripts/replay_capture.py examples/Power.log -o /tmp/baseline_new
diff /tmp/baseline_old/Power.txt /tmp/baseline_new/Power.txt && echo BASELINE-IDENTICAL
```
Expected: 测试全绿;diff 无输出且打印 `BASELINE-IDENTICAL`

- [ ] **Step 5: Commit**

```bash
git add hsbot/watcher.py scripts/replay_capture.py tests/test_watcher_integration.py
git commit -m "refactor: watcher 输出 Msg 化(快照 ui 单行/full 完整块)+异常 log.exception+消费方取 full"
```

---

### Task 4: 诊断日志双 handler + 各模块 print→logging

**Files:**
- Modify: `hsbot/main.py`(logging 配置,先于子命令化,本任务不动 argparse)
- Modify: `hsbot/config.py:105-109`、`hsbot/carddb.py`(27-30 行区域)、`hsbot/knowledge.py:54`、`hsbot/corpus.py:170,232`
- Test: `tests/test_logging_setup.py`(新建)

**Interfaces:**
- Consumes: 无
- Produces: `main._setup_logging(data_dir: Path) -> None`(Task 5 复用);`data/logs/hsbot.log` 文件 handler(DEBUG)/stderr handler(WARNING+)

- [ ] **Step 1: 写失败测试**

新建 `tests/test_logging_setup.py`:

```python
"""诊断日志双通道: 文件 DEBUG 完整 / 控制台 WARNING+。"""
import logging

from hsbot.main import _setup_logging


def test_setup_logging_dual_handlers(tmp_path):
    _setup_logging(tmp_path)
    root = logging.getLogger()
    fh, sh = root.handlers[-2], root.handlers[-1]
    assert fh.level == logging.DEBUG and "hsbot.log" in fh.baseFilename
    assert sh.level == logging.WARNING
    logging.getLogger("hsbot.test").debug("调试细节")
    logging.getLogger("hsbot.test").warning("可见警告")
    for h in (fh, sh):
        root.handlers.remove(h)          # 清理, 不污染其他测试
    fh.flush()
    assert "调试细节" in open(fh.baseFilename, encoding="utf-8").read()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_logging_setup.py -v`
Expected: FAIL,`ImportError: cannot import name '_setup_logging'`

- [ ] **Step 3: 实现 _setup_logging 并替换 basicConfig**

`hsbot/main.py`:把第 21 行 `logging.basicConfig(...)` 删除,`main()` 内移到 `cfg` 构造之后调用;顶部 import 区加 `from pathlib import Path`;新增模块级函数(放在 `main` 之前):

```python
def _setup_logging(data_dir) -> None:
    """诊断日志双通道: 文件 DEBUG 完整(含时间/模块/堆栈), 控制台只出 WARNING+。"""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_dir / "hsbot.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)
```

`main()` 里原第 21 行位置只删不加;在 `cfg = Config.load({...})` 之后、`carddb = ...` 之前插一行:

```python
    _setup_logging(cfg.data_dir)
```

- [ ] **Step 4: 各模块 print→logging(四处)**

每处都是把 `print(f"! ...")` 换成 `log.xxx(...)`,并在文件顶部加 `import logging` 与模块级 `log = logging.getLogger(__name__)`(config.py/carddb.py/knowledge.py/corpus.py 均无现有 logger;adapter.py 已有,不动):

```python
# config.py:105-109(解析失败在 _setup_logging 之前发生, 经 logging lastResort 上 stderr, 与原 print 可见性一致)
log = logging.getLogger(__name__)
...
            except Exception as exc:  # noqa: BLE001
                log.error("config.yaml 解析失败(%s), 改用内置默认", exc)

# carddb.py:27-30
log = logging.getLogger(__name__)
...
            except Exception as exc:  # noqa: BLE001
                log.error("卡表加载失败(%s): %s —— 将以原始 card_id 显示", path, exc)
        else:
            log.warning("卡表不存在(%s) —— 将以原始 card_id 显示", path)

# knowledge.py:54
log = logging.getLogger(__name__)
...
                log.warning("卡组中 dbfId=%s 无法映射到 card_id, 已跳过", dbf)

# corpus.py:170 与 232
log = logging.getLogger(__name__)
...
                log.warning("训练样本 deck code 解析失败: %s", exc)
...
                    log.error("导入失败 %s 第%s局: %s", sess.name, i, exc)
```

注意:`corpus.py` 的 `report()`(242-247 行)是 import-all 的命令结果,保留 print 不动。

- [ ] **Step 5: 回归**

Run: `python3 -m pytest tests/ -q && python3 -m hsbot --replay examples/Power.log --no-overlay 2>&1 | tail -3`
Expected: 测试全绿;回放输出正常(`--replay/--no-overlay` 本任务仍在,Task 5 才删)

- [ ] **Step 6: Commit**

```bash
git add hsbot/main.py hsbot/config.py hsbot/carddb.py hsbot/knowledge.py hsbot/corpus.py tests/test_logging_setup.py
git commit -m "feat: 诊断日志 logging 化(文件 DEBUG/控制台 WARNING+)+各模块 print 替换"
```

---

### Task 5: main 子命令化 + 永远构造 OutputHub + 删 throttle_ms

**Files:**
- Modify: `hsbot/main.py`(argparse 段与装配段整体重写,含 docstring)
- Modify: `hsbot/config.py:18`(删 `"throttle_ms": 300,`)与 `config.py:73`(`_INT = ("overlay_font_size",)`)
- Test: `tests/test_cli.py`(新建)

**Interfaces:**
- Consumes: `_setup_logging`(Task 4)、`OutputHub/OverlayWindow/Msg`(Task 2)
- Produces: CLI 契约——`python -m hsbot [--config P] [replay <log> | import-all]`;replay 时 `overlay_enabled=False`、经 `Config.load` 注入 `replay=<log>`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_cli.py`:

```python
"""CLI 子命令: 无子命令=live, replay 自动关悬浮窗, import-all 分流。

--config 一律指向不存在路径, 隔离仓库根 config.yaml(否则测试结果随其内容漂移)。
"""
from unittest.mock import patch

from hsbot.main import build_config, main

NO_YAML = ["--config", "/nonexistent/config.yaml"]


def test_no_subcommand_is_live():
    cfg, _ = build_config(NO_YAML)
    assert cfg.replay == "" and cfg.overlay_enabled is True


def test_replay_subcommand_forces_no_overlay():
    cfg, _ = build_config(NO_YAML + ["replay", "x.log"])
    assert cfg.replay == "x.log" and cfg.overlay_enabled is False


def test_throttle_ms_field_removed():
    from hsbot.config import DEFAULTS
    assert "throttle_ms" not in DEFAULTS


def test_import_all_dispatch(tmp_path):
    with patch("hsbot.main.run_import_all") as mock_export:
        main(["--config", str(tmp_path / "none.yaml"), "import-all"])
    mock_export.assert_called_once()
    cfg_arg, carddb_arg = mock_export.call_args.args
    assert cfg_arg.replay == ""          # import-all 不注入 replay
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_cli.py -v`
Expected: FAIL,`ImportError: cannot import name 'build_config'`

- [ ] **Step 3: 重写 main.py**

整体替换为(保留 Task 4 已加的 `_setup_logging`):

```python
"""入口: python -m hsbot [--config config.yaml] [replay <log> | import-all]

配置一律 config.yaml(优先级: 子命令注入 > config.yaml > 内置默认)。
悬浮窗开启时: watcher 跑后台线程, tkinter 主循环占主线程(Windows 要求)。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from .carddb import CardDB
from .config import Config
from .watcher import Watcher


def _setup_logging(data_dir) -> None:
    """诊断日志双通道: 文件 DEBUG 完整(含时间/模块/堆栈), 控制台只出 WARNING+。"""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(log_dir / "hsbot.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    root.addHandler(sh)


def run_import_all(cfg, carddb) -> None:
    from .corpus import CorpusExporter
    CorpusExporter(cfg, carddb).import_all()


def build_config(argv: list[str] | None) -> Config:
    ap = argparse.ArgumentParser(prog="hsbot",
                                 description="奇迹德实时军师 (配置见 config.yaml)")
    ap.add_argument("--config", default=None, help="配置文件路径(默认 ./config.yaml)")
    sub = ap.add_subparsers(dest="cmd")
    p_replay = sub.add_parser("replay", help="重放静态日志(开发/验收用, 自动关悬浮窗)")
    p_replay.add_argument("log", help="Power.log 路径")
    sub.add_parser("import-all", help="批量解析 logs_dir 下所有会话日志 -> 训练语料")
    args = ap.parse_args(argv)

    overrides: dict = {"config": args.config}
    if args.cmd == "replay":
        overrides["replay"] = args.log
        overrides["overlay_enabled"] = False
    return Config.load(overrides), args


def main(argv=None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    cfg, args = build_config(argv)
    _setup_logging(cfg.data_dir)
    carddb = CardDB(cfg.cache_dir / "cards.zh.json")

    if args.cmd == "import-all":
        run_import_all(cfg, carddb)
        return

    # ---- 输出枢纽: 控制台 + 会话记录文件 + 悬浮窗(永远存在, 无悬浮窗时 console-only) ----
    hub = None
    if cfg.overlay_enabled:
        try:
            from .overlay import OutputHub, OverlayWindow
        except Exception as exc:  # noqa: BLE001  无 tkinter 环境降级
            print(f"! 悬浮窗不可用({exc}), 仅控制台输出")
            hub = OutputHub(console=True, to_overlay=False)
        else:
            hub = OutputHub(console=cfg.console_echo, to_overlay=True)
    if hub is None:
        hub = OutputHub(console=True, to_overlay=False)

    watcher = Watcher(cfg, carddb, out=hub, hub=hub)

    def run_bot() -> None:
        if cfg.replay:
            watcher.run_replay(cfg.replay)
        else:
            watcher.run_live()

    if hub.q is not None:        # 悬浮窗模式: tkinter 占主线程
        t = threading.Thread(target=run_bot, daemon=True, name="hsbot-watcher")
        t.start()
        try:
            OverlayWindow(cfg, hub.q).run()
        finally:
            print("悬浮窗已关闭, 监控退出。")
    else:
        try:
            run_bot()
        except KeyboardInterrupt:
            print("已退出。")


if __name__ == "__main__":
    main()
```

注意:`build_config` 返回 `(Config, args)` 元组;`Path` 在 main.py 顶层已 import(Task 4 加入),`_setup_logging` 内不再局部 import。

- [ ] **Step 4: 删 throttle_ms**

`hsbot/config.py`:删第 18 行 `"throttle_ms": 300,`;第 73 行改 `_INT = ("overlay_font_size",)`。

- [ ] **Step 5: 回归 + 冒烟**

Run:
```bash
python3 -m pytest tests/ -q
python3 -m hsbot --help
python3 -m hsbot replay examples/Power.log 2>&1 | grep -c "完整快照" || true
ls -la data/logs/hsbot.log
```
Expected: 测试全绿;`--help` 只见 `--config` 与 `replay/import-all` 两个子命令;replay 控制台 0 处"完整快照"(只有单行快照);hsbot.log 存在

- [ ] **Step 6: Commit**

```bash
git add hsbot/main.py hsbot/config.py tests/test_cli.py
git commit -m "refactor: CLI 子命令化(--config + replay/import-all)+永远构造 OutputHub+删死参数 throttle_ms"
```

---

### Task 6: 文档同步 + 全量验收

**Files:**
- Modify: `docs/PROJECT.md:33,59-64,127,142`
- Modify: `docs/M1_MONITOR.md:21,86`

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 文档与 CLI 现实一致

- [ ] **Step 1: 改 docs/PROJECT.md 五处**

```
:33  "按卡组分目录, --import-all"  →  "按卡组分目录, import-all 子命令"
:59  "python -m hsbot --deck 奇迹德 --logs-dir E:\battle\Hearthstone\Logs   # 实时监控（M1 起）"
     → "python -m hsbot                                          # 实时监控（M1 起, 配置见 config.yaml）"
:60  "python -m hsbot --replay examples/Power.log"
     → "python -m hsbot replay examples/Power.log"
:64  "`--replay` 的意义"  →  "`replay` 子命令的意义"
:127 "可用 `--replay` 先过一遍"  →  "可用 `replay` 子命令先过一遍"
:142 "M1 的 `--replay` 验收"  →  "M1 的 `replay` 子命令验收"
```

- [ ] **Step 2: 改 docs/M1_MONITOR.md 两处**

```
:21  "命令行同名参数（`--deck / --deck-code / --logs-dir / --battletag / --replay / --config`）可临时覆盖。"
     → "配置一律走本文件；CLI 仅有 `--config <路径>` 与 `replay <log>` / `import-all` 两个子命令（replay 自动关悬浮窗）。"
:86  "main.py         # 装配：以上各件 + 轮询循环（支持 --replay 重放模式）"
     → "main.py         # 装配：以上各件 + 轮询循环（支持 replay 子命令重放）"
```

- [ ] **Step 3: 全量验收(spec 六条)**

```bash
python3 -m pytest tests/ -q                                   # 全绿
python3 -m hsbot --help                                        # 只有 --config + 两子命令
python3 -m hsbot replay examples/Power.log | head -20          # 链路行+单行快照, 无完整快照块
SESS=$(ls -t data/sessions | head -1)
grep -c "完整快照" "data/sessions/$SESS/$(ls data/sessions/$SESS | grep '\.log$' | head -1)"   # >0 (spec 第4条: 会话 .log 存 full 版)
grep -c "DEBUG" data/logs/hsbot.log                            # >0 (adapter 调试行已落盘)
python3 scripts/replay_capture.py examples/Power.log -o /tmp/base_final
diff /tmp/baseline_old/Power.txt /tmp/base_final/Power.txt && echo IDENTICAL
```

Expected: 全部满足;最后 diff 输出 `IDENTICAL`(spec 第 4 条的悬浮窗侧验证:`test_output_hub.py` 已断言队列只收 ui 版)

- [ ] **Step 4: Commit**

```bash
git add docs/PROJECT.md docs/M1_MONITOR.md
git commit -m "docs: PROJECT/M1_MONITOR 命令示例同步子命令化 CLI"
```
