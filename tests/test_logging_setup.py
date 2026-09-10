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
