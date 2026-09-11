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
