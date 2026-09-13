"""行处理责任链测试: 链序契约 / CREATE_GAME 边界回调 / 行级抽牌补事件。"""
from hsbot import pipeline as pl


def _build(boundary_calls, draws):
    class FakeParser:
        def read_line(self, line):
            if "BOOM" in line:
                raise RuntimeError("boom")

    return pl.build_line_pipeline(
        parser_getter=FakeParser,
        on_create_boundary=lambda: boundary_calls.append(1),
        friendly_getter=lambda: 2,
        on_draw=lambda eid, cid, pid: draws.append((eid, cid, pid)))


def test_chain_order_and_boundary():
    ctx = pl.StreamContext()
    p = _build(boundary_calls := [], draws := [])

    create = "D 09:55:31.7568067 GameState.DebugPrintPower() - CREATE_GAME"
    p.feed(create, ctx)
    assert boundary_calls == [1]                 # 边界回调先于喂解析器
    assert ctx.game_lines == [create]            # 切片缓冲从 CREATE_GAME 开始
    assert ctx.tail_open is True                 # 默认开尾

    p.feed("D 09:55:31.7568067 GameState.DebugPrintPower() - BLOCK_END", ctx)
    assert ctx.tail_open is False                # 块收口: 尾包完整

    p.feed("D 09:55:31.7568067 GameState.DebugPrintPower() - TAG_CHANGE Entity=1 "
           "tag=TURN value=1 ", ctx)
    assert ctx.tail_open is True                 # 新包头行: 重新扣发


def test_show_draw_only_for_friendly():
    ctx = pl.StreamContext()
    boundary, draws = [], []
    p = _build(boundary, draws)
    line = ("D x GameState.DebugPrintPower() - SHOW_ENTITY - Updating "
            "Entity=[entityName=X id=9 zone=DECK zonePos=0 cardId= player=2] "
            "CardID=ABC_1")
    p.feed(line, ctx)
    assert draws == [(9, "ABC_1", 2)]
