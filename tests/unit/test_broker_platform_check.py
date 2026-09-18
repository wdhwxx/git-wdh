# -*- coding: utf-8 -*-
"""
券商适配器平台校验测试（审计疑点3）
=====================================
覆盖:
  1. paper/sim 任意平台可通过
  2. qmt/ptrade 在非 Windows 平台（Linux/Darwin，含 Docker live 容器）提前拦截
  3. qmt/ptrade 在 Windows 平台放行（SDK 占位设计，核心动作另行明确报错）
  4. 大小写不敏感

回归依据: 审计疑点3（Docker live --broker qmt/ptrade 平台不匹配）
"""
import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_SPEC = importlib.util.spec_from_file_location(
    "run_shipan_entry", PROJECT_ROOT / "run_实盘入口.py"
)
ENTRY = importlib.util.module_from_spec(_SPEC)
assert _SPEC is not None and _SPEC.loader is not None
_SPEC.loader.exec_module(ENTRY)


class TestPlatformValidation:
    """validate_broker_platform 平台校验（monkeypatch 自动还原 sys.platform）"""

    def test_paper任意平台通过(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for plat in ("linux", "darwin", "win32"):
            monkeypatch.setattr(sys, "platform", plat)
            assert ENTRY.validate_broker_platform("paper") is None

    def test_sim任意平台通过(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for plat in ("linux", "darwin", "win32"):
            monkeypatch.setattr(sys, "platform", plat)
            assert ENTRY.validate_broker_platform("sim") is None

    def test_未指定broker通过(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert ENTRY.validate_broker_platform(None) is None
        assert ENTRY.validate_broker_platform("") is None

    @pytest.mark.parametrize("plat", ["linux", "darwin"])
    def test_qmt非Windows拦截(self, monkeypatch: pytest.MonkeyPatch, plat: str) -> None:
        monkeypatch.setattr(sys, "platform", plat)
        err = ENTRY.validate_broker_platform("qmt")
        assert err is not None
        assert "仅支持 Windows" in err
        assert "qmt" in err

    def test_ptrade非Windows拦截(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        err = ENTRY.validate_broker_platform("ptrade")
        assert err is not None
        assert "仅支持 Windows" in err

    def test_qmt_windows放行(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert ENTRY.validate_broker_platform("qmt") is None

    def test_ptrade_windows放行(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert ENTRY.validate_broker_platform("ptrade") is None

    def test_大小写不敏感(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert ENTRY.validate_broker_platform("QMT") is None
        monkeypatch.setattr(sys, "platform", "linux")
        assert ENTRY.validate_broker_platform("PTRADE") is not None
