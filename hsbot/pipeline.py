"""行处理责任链 —— 每行日志依序流过专职处理器(2026-09-13 架构化)。

责任链(Chain of Responsibility)+ 流上下文(StreamContext, 流状态唯一维护点):
每个处理器一个类、一个职责; 链的顺序即架构契约, 由 LinePipeline 组装。
在 watcher._feed_many 中逐行调用 —— 顺序与旧行内逻辑逐语义等价。

链序(不可随意调换):
  TailState   行形判定 → 流的尾包完整性标志
  Boundary    CREATE_GAME 边界: 结算上一局切片/新局切片缓冲/重置 player manager
  GameLines   当局原始行缓冲(训练切片用)
  PlayerName  真名采集(Player=/PlayerName=)
  Hint        括号 cardId/player 采集(hslog 丢弃信息的兜底)
  Feed        喂 hslog 解析器
  ShowDraw    括号形式 SHOW_ENTITY 的行级抽牌补事件(须在 Feed 之后读解析前状态)
"""
from __future__ import annotations

import logging
import re

from .adapter import feed_line

log = logging.getLogger(__name__)

_CREATE_GAME_MARK = "GameState.DebugPrintPower() - CREATE_GAME"
_BRACKET_CID_RE = re.compile(r"id=(\d+)(?:(?!id=)[^\n])*?cardId=([A-Za-z0-9_]+)")
_BRACKET_PLAYER_RE = re.compile(r"id=(\d+)(?:(?!id=)[^\n])*?player=(\d+)")
_SHOW_DRAW_RE = re.compile(r"id=(\d+)[^\n]*?zone=DECK[^\n]*?player=(\d+)[^\n]*?CardID=([A-Za-z0-9_]+)")
_PLAYER_NAME_RE = re.compile(r"Player(?:Name)?=(\S+#\d+)")
_GAMESTATE_LINE_RE = re.compile(r"(?:GameState|PowerTaskList)\.\w+\(\) - (.+)")
_PACKET_SECTION_RE = re.compile(
    r"GameState\.(?:DebugPrintPower|DebugPrintEntityChoices|DebugPrintChoices"
    r"|SendChoices|DebugPrintEntitiesChosen)\(\) - (.+)")


def _tail_state(line: str) -> str | None:
    """行形 → 对"packet 树尾包完整性"的影响: header=新包开尾, cont=尾包续写,
    other=组边界/块收口(尾包已写完整), None=无关行。"""
    m = _PACKET_SECTION_RE.search(line)
    if m is not None:
        content = m.group(1)
        if content.startswith("BLOCK_END"):
            return "other"                 # 块收口: 块及其子包不再增长
        return "cont" if content[:1].isspace() else "header"
    m = _GAMESTATE_LINE_RE.search(line)
    if m is not None and not m.group(1)[:1].isspace():
        return "other"
    return None


class StreamContext:
    """日志流缓冲 —— 流状态的唯一维护点(跨局存活, 局边界处由外部清空)。"""

    def __init__(self) -> None:
        self.tail_open = True                       # 尾包可能仍在续写
        self.partial = ""                           # 末行半截缓冲
        self.pend_cid: dict[int, str] = {}          # 括号 cardId 线索
        self.pend_ctrl: dict[int, int] = {}         # 括号 controller 线索
        self.pend_names: list[str] = []             # 真名缓冲
        self.pend_draws: list[tuple[int, str, int]] = []   # 行级抽牌 hint(批尾派发)
        self.game_lines: list | None = None         # 当局原始行(训练切片)
        # 已终结局的冻结切片, 键 = parser 局下标(0 基)。边界(CREATE_GAME)时交接:
        # 上一局的导出发生在批尾, 而切片缓冲届时已被新局头部行占据 → 边界先冻结
        self.frozen_game_lines: dict[int, list[str]] = {}


class LineHandler:
    """责任链节点: process 一行, 然后交给后继。"""

    def __init__(self, nxt: "LineHandler | None" = None) -> None:
        self._nxt = nxt

    def link(self, nxt: "LineHandler") -> "LineHandler":
        self._nxt = nxt
        return nxt

    def handle(self, line: str, ctx: "StreamContext") -> None:
        self.process(line, ctx)
        if self._nxt is not None:
            self._nxt.handle(line, ctx)

    def process(self, line: str, ctx: "StreamContext") -> None:
        raise NotImplementedError


class TailStateHandler(LineHandler):
    def process(self, line, ctx) -> None:
        st = _tail_state(line)
        if st == "header":
            ctx.tail_open = True     # 新包刚开尾, 子行可能还在路上
        elif st == "other":
            ctx.tail_open = False    # 上一包组的子行已写尽, 尾包完整


class BoundaryHandler(LineHandler):
    """CREATE_GAME 边界: 结算上一局切片/开新切片缓冲/重置 player manager。"""

    def __init__(self, on_boundary, nxt=None) -> None:
        super().__init__(nxt)
        self._on_boundary = on_boundary   # 边界回调(watcher 提供具体动作)

    def process(self, line, ctx) -> None:
        if _CREATE_GAME_MARK in line:
            self._on_boundary()
            ctx.game_lines = []


class GameLinesHandler(LineHandler):
    def process(self, line, ctx) -> None:
        if ctx.game_lines is not None:
            ctx.game_lines.append(line)


class PlayerNameHandler(LineHandler):
    def process(self, line, ctx) -> None:
        if "Player=" in line or "PlayerName=" in line:
            m = _PLAYER_NAME_RE.search(line)
            if m and m.group(1) not in ctx.pend_names:
                ctx.pend_names.append(m.group(1))


class HintHandler(LineHandler):
    def process(self, line, ctx) -> None:
        if "Entity=[" in line:            # hslog 丢弃括号里的 cardId/player, 这里兜底
            if "cardId=" in line:
                for m in _BRACKET_CID_RE.finditer(line):
                    if m.group(2):
                        ctx.pend_cid[int(m.group(1))] = m.group(2)
            if "player=" in line:
                for m in _BRACKET_PLAYER_RE.finditer(line):
                    ctx.pend_ctrl[int(m.group(1))] = int(m.group(2))


class FeedHandler(LineHandler):
    """喂 hslog 解析器(唯一与解析器交互的节点)。"""

    def __init__(self, parser_getter, nxt=None) -> None:
        super().__init__(nxt)
        self._parser_getter = parser_getter   # () -> LogParser(parser 可被整体替换)

    def process(self, line, ctx) -> None:
        feed_line(self._parser_getter(), line)


class ShowDrawHandler(LineHandler):
    """括号形式 SHOW_ENTITY(抽牌揭示主形态)hslog 不解析 —— 行级补抽牌事件。"""

    def __init__(self, friendly_getter, on_draw, nxt=None) -> None:
        super().__init__(nxt)
        self._friendly_getter = friendly_getter   # () -> pid | None
        self._on_draw = on_draw                   # (eid, cid, pid) -> None

    def process(self, line, ctx) -> None:
        if "SHOW_ENTITY" not in line or "zone=DECK" not in line \
                or "GameState." not in line:
            return
        m = _SHOW_DRAW_RE.search(line)
        friendly = self._friendly_getter()
        if m and friendly is not None and int(m.group(2)) == friendly:
            self._on_draw(int(m.group(1)), m.group(3), int(m.group(2)))


class LinePipeline:
    """责任链组装与入口。"""

    def __init__(self, head: LineHandler) -> None:
        self._head = head

    def feed(self, line: str, ctx: StreamContext) -> None:
        self._head.handle(line, ctx)


def build_line_pipeline(*, parser_getter, on_create_boundary,
                        friendly_getter, on_draw) -> LinePipeline:
    """按架构契约组装链序(见模块 docstring)。"""
    tail = TailStateHandler()
    boundary = BoundaryHandler(on_boundary=on_create_boundary)
    game_lines = GameLinesHandler()
    names = PlayerNameHandler()
    hints = HintHandler()
    feed = FeedHandler(parser_getter=parser_getter)
    show_draw = ShowDrawHandler(friendly_getter=friendly_getter, on_draw=on_draw)

    tail.link(boundary)
    boundary.link(game_lines)
    game_lines.link(names)
    names.link(hints)
    hints.link(feed)
    feed.link(show_draw)
    return LinePipeline(tail)
