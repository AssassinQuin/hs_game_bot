# 设计:CLI 子命令化 + 日志双通道

日期:2026-09-10
状态:已获用户批准(形态:子命令模式;UI:链路行+精简快照)

## 背景与问题

1. `python -m hsbot` 有 10 个 CLI 参数,其中 6 个是纯配置覆盖(deck/deck-code/logs-dir/data-dir/battletag/no-overlay),config.yaml 已全部覆盖,CLI 只是冗余通道;日常使用只需一条裸命令。
2. 诊断信息全用 `print("! ...")` 散落 8 处;`main.py` 里 `logging.basicConfig(level=ERROR)` 无 FileHandler,adapter.py 已有的 `log.debug`(脏数据跳过、friendly 探测失败等)全部被吞,无法落盘调试。
3. 业务输出三路同文本(控制台/会话 .log/悬浮窗),悬浮窗被完整快照块刷屏,而文件里又只有 UI 看到的文本,两头都不合适。
4. 附带发现:`throttle_ms` 是死配置——全仓库仅 config.py 定义,无任何消费者,`--throttle-ms` 是假参数。

## 目标

- 日常 `python -m hsbot` 零参数;调试/批处理入口显式子命令。
- 诊断日志走 logging 库:文件完整(DEBUG+时间/模块/堆栈),控制台只出 WARNING+。
- 业务消息结构化:UI 收简洁版,会话 .log 文件收完整版。

## 方案

### A. CLI 子命令化(hsbot/main.py)

```text
python -m hsbot                      # 日常:实时监控,读 config.yaml
python -m hsbot replay <Power.log>   # 回放调试:自动关悬浮窗,控制台输出
python -m hsbot import-all           # 批量导训练语料
python -m hsbot --config other.yaml  # 唯一保留的 flag(可带子命令)
```

- argparse 结构:顶层 `--config` + subparsers(`replay <log>`、`import-all`);无子命令 = live 模式。
- 删除 6 个纯配置 flag(deck/deck-code/logs-dir/data-dir/battletag/no-overlay)与 `--throttle-ms`。
- `Config.load(cli dict)` 机制不变;replay 子命令注入 `{"replay": path, "overlay_enabled": False}`。
- 不给 replay 加速度参数:throttle_ms 从未实现,不为死功能造假参数;将来真需要回放限速再加。

### B. 诊断日志(logging)

main 统一配置双 handler:

| handler | 目的地 | 级别 | 格式 |
|---|---|---|---|
| FileHandler | `data/logs/hsbot.log` | DEBUG | `%(asctime)s %(levelname)s %(name)s: %(message)s` |
| StreamHandler(stderr) | 控制台 | WARNING+ | 简短(无时间戳) |

改写点(print → logging):

- config.py:108 配置解析失败 → `log.error`
- carddb.py:28/30 卡表加载失败 → `log.error/warning`
- knowledge.py:54 dbfId 映射失败 → `log.warning`
- corpus.py:170/232 语料解析/导入失败 → `log.warning/error`
- watcher.py:302 事件处理异常、363 训练样本导出失败 → `log.exception`(堆栈进文件;UI 侧仍发一行 notice,见 C)
- overlay.py:42 console 分发的 print 保留(这是业务输出通道,不是诊断)

例外:`import-all` 的统计表(corpus.py:242-247)是命令结果,保留 stdout。

### C. 业务输出:结构化双文本

render.py 新增 `snapshot_line(st, ...)` 生成单行精简快照:

```text
── T6 我:水晶6 │ 手牌8 │ 牌库12+2 │ 场上2随从 ──
```

(回合、水晶、手牌数、牌库已知+未知、双方向随从数;具体字段以实现时 store 可用数据为准)

OutputHub 消息从裸 str 改为结构化 Msg(定义放 overlay.py,与 OutputHub 同处):

```python
@dataclass
class Msg:
    kind: str   # "chain" | "snapshot" | "game_end" | "notice"
    ui: str     # 简洁版 → 控制台 + 悬浮窗
    full: str   # 完整版 → 会话 .log 文件(缺省 = ui)
```

- 链路行/终局行/回合头/notice:ui = full(本身已短)。
- 快照:ui = `snapshot_line()`,full = 现有 `snapshot_block()` 完整块(含 game_end 行拼接)。
- OutputHub 分发:控制台 `print(ui)`;会话 `.log` 文件写 full;悬浮窗队列放 `(kind, ui)`,OverlayWindow 按 kind 上色(snapshot 浅黄、game_end 浅蓝,tkinter Text tag)。
- watcher `_emit` 改为构造 Msg;诊断类 `_emit("! ...")` 同时 `log.exception` + 发 `kind="notice"` 单行。
- main 永远构造 OutputHub(无 overlay 时 `to_overlay=False`),消除 `out=print` 分支。
  行为增强:无 overlay 的运行(含 replay)现在也会经 watcher 的 `hub.set_file()` 落盘会话 `.log`(full 版)——以前仅悬浮窗开启时才有这份文件,现在任何模式都可离线查完整台账。
- scripts/replay_capture.py 适配:`out=lambda m: lines.append(m.full)`——baseline 语义不变,仍是完整快照文本。

### D. 文档/配置同步

- docs/PROJECT.md:59-60 旧命令示例 → 子命令形式;:33 `--import-all` 措辞改 `import-all` 子命令;:64/127/142 的 `--replay` 措辞同步。
- docs/M1_MONITOR.md:21 "命令行同名参数(--deck / ...)可临时覆盖" → 改写为"配置一律 config.yaml;CLI 仅有 --config + replay/import-all 子命令"。
- config.yaml 不含被删字段,无需改动;`DEFAULTS["throttle_ms"]` 与 `_INT` 引用一并删除。

## 非目标(明确不做)

- OverlayWindow 不做折叠/交互,只按 kind 上色。
- 不加回放速度控制参数。
- 不引入第三方日志库(标准库 logging 足够)。
- examples/、scripts/ 其他脚本的 argparse 不动。

## 验收标准

1. `python -m hsbot --help` 只见 `--config` 与两个子命令;`python -m hsbot` 直接进入 live 监控。
2. `python -m hsbot replay examples/Power.log` 控制台输出链路行+单行快照,无悬浮窗。
3. `data/logs/hsbot.log` 存在且含 adapter 的 DEBUG 行;控制台无 DEBUG 噪音。
4. 会话 `.log` 文件(watcher set_file 的那份)含完整快照块;悬浮窗只显示单行快照。
5. `python3 scripts/replay_capture.py examples/Power.log -o /tmp/base_new` 产出的 baseline 与改造前 diff 一致(full 版语义未变)。
6. 现有测试全部通过;`python -m hsbot import-all` 统计表照常打印。
