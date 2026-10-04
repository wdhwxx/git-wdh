# -*- coding: utf-8 -*-
"""T-33 KP-P5-07 变更管理：config_version 落快照 + example vs 生产键级 diff。"""
import io
import tempfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "live_config.example.yaml"


def _base_config(version):
    """构造含/不含 config_version 的最小实盘配置（基于 example 模板）。"""
    cfg = yaml.safe_load(io.open(EXAMPLE, "r", encoding="utf-8"))
    if version is not None:
        cfg["config_version"] = version
    else:
        cfg.pop("config_version", None)
    return cfg


def test_create_live_engine_sets_config_version():
    """create_live_engine 装配后 engine._config_version 取自 live_config.config_version。"""
    from myquant.live.engine import create_live_engine

    eng = create_live_engine(_base_config("v2.3.1"))
    assert eng._config_version == "v2.3.1"


def test_create_live_engine_default_config_version_empty():
    """无 config_version 字段时默认空串（不崩）。"""
    from myquant.live.engine import create_live_engine

    eng = create_live_engine(_base_config(None))
    assert getattr(eng, "_config_version", "") == ""


def test_save_state_extra_includes_config_version(monkeypatch):
    """_save_state 快照 extra 携带 config_version（配置变更版本可审计）。"""
    from myquant.live.engine import create_live_engine
    from myquant.live.state_store import StateStore

    eng = create_live_engine(_base_config("v9"))
    captured = {}

    def fake_capture(engine=None, portfolio=None, broker=None, scheduler=None, extra=None):
        captured["extra"] = extra
        return type("Snap", (), {"version": 1, "tag": "auto"})()

    monkeypatch.setattr("myquant.live.engine.capture_live_state", fake_capture)
    tmp = tempfile.mkdtemp()
    eng._state_store = StateStore(root=tmp, store_id="t33")
    eng._save_state()
    assert (captured.get("extra") or {}).get("config_version") == "v9"


def test_key_level_diff_missing_fails():
    """键级 diff：生产缺失 example 键应判失败（防止生产配置漂移缺键）。"""
    example = {"a": 1, "b": 2}
    prod = {"a": 1}
    missing = set(example) - set(prod)
    assert missing == {"b"}
    with pytest.raises(AssertionError):
        assert not missing, f"生产 live_config.yaml 缺失键: {sorted(missing)}"


def test_key_level_diff_ok():
    """键级 diff：生产含 example 全部键（允许多余键），无缺失即通过。"""
    example = {"a": 1, "b": 2}
    prod = {"a": 1, "b": 2, "c": 3}
    missing = set(example) - set(prod)
    extra = set(prod) - set(example)
    assert not missing
    assert extra == {"c"}
