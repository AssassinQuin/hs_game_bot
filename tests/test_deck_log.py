"""Decks.log 单遍解析(冗余#2: codes 与排队时间线同源)与跨午夜日期锚定(中#7)。"""
from datetime import date, datetime

from hsbot.knowledge import deck_timeline, parse_decks_log, session_date

_C1 = "AAEBAafrBAbgwQKP9gLa5wLZuQPWmQOhBgL9jQaluwYAAA"
_C2 = "AAEBAQcGkAfDFvKFEN2+AsPqAp3wArnSAovoAgAA"
_C3 = "AAEBAZ/ICAvSqwKX0wKDZwKggAPUBuAFiQ7grAKY0AKggAPA7gDAAA"

LOG = "\n".join([
    "D 23:50:01.1234567 Finding Game With Deck opponent=甲",
    "D 23:50:01.2234567 ### 奇迹德",
    f"D 23:50:01.3234567 {_C1}",
    "W 00:10:00.0000000 Finding Game With Deck opponent=乙",
    "W 00:10:00.1000000 ### 海盗战",
    f"W 00:10:00.2000000 {_C2}",
    "D 12:00:00.0000000 ### 编辑未打",
    f"D 12:00:00.1000000 {_C3}",
    "",
])


def _write(tmp_path):
    p = tmp_path / "Decks.log"
    p.write_text(LOG, encoding="utf-8")
    return p


def test_codes_include_edit_only_entries(tmp_path):
    """无 Finding 的 '### 名 + code'(编辑卡组)也进名字→代码表 ——
    只看排队序列会丢"编辑了但上次玩的是别的"的最新代码。"""
    codes = parse_decks_log(_write(tmp_path))
    assert set(codes) == {"奇迹德", "海盗战", "编辑未打"}


def test_timeline_only_finding_sequences(tmp_path):
    tl = deck_timeline(_write(tmp_path), day=date(2026, 9, 12))
    assert [name for _t, name, _c in tl] == ["奇迹德", "海盗战"]
    assert [code for _t, _n, code in tl] == [_C1, _C2]


def test_timeline_anchored_to_session_day(tmp_path):
    """审计 中#7: 行时刻的日期来自会话目录名锚, 不再拼"今天" ——
    会话 23:49 开始、凌晨 00:10 的对局, 其 23:50 的排队行仍归会话日。"""
    tl = deck_timeline(_write(tmp_path), day=date(2026, 9, 12))
    assert tl[0][0] == datetime(2026, 9, 12, 23, 50, 1)
    assert tl[1][0] == datetime(2026, 9, 12, 0, 10, 0)


def test_timeline_default_day_is_today(tmp_path):
    tl = deck_timeline(_write(tmp_path))
    assert tl[0][0].date() == date.today()


def test_session_date_from_dirname():
    assert session_date("Hearthstone_2026_09_12_23_49_30") == date(2026, 9, 12)
    assert session_date("replay_Hearthstone_2026_09_12_23_49_30") == date(2026, 9, 12)
    assert session_date("live") is None
