# 文件名：tests/unit/test_exception_swallowing.py
"""
test_exception_swallowing.py - 异常吞噬规模大测试（KP-P1-07 / T-23）
====================================================================
覆盖修复：
  1. routes.py 无 fastapi 时启动 raise（不再静默降级占位）。
  2. connection.py close_all 关闭连接异常记 warning（不再裸 pass 吞掉）。
  3. CI ruff BLE001 专项护栏存在（新增裸 except 即失败）。

依赖: pytest, myquant.api.routes, myquant.database.connection, pathlib
"""

from __future__ import annotations

from pathlib import Path

import pytest

from myquant.api.routes import FASTAPI_AVAILABLE, create_app
from myquant.database.connection import DatabaseConnection

_PROJ = Path(__file__).resolve().parent.parent.parent


class TestCreateAppNoFastapi:
    def test_无fastapi_启动raise(self, monkeypatch) -> None:
        """DoD：无 fastapi 时 create_app 显式 raise RuntimeError（不静默降级）。"""
        if not FASTAPI_AVAILABLE:
            pytest.skip("本环境无 fastapi，无法验证降级路径")  # P2-2 定性：设计性跳过——fastapi 在 web extras（pyproject），CI 装 dev+benchmark+gui+web 故 CI 真跑；仅本地无 web extras 时跳过
        monkeypatch.setattr("myquant.api.routes.FASTAPI_AVAILABLE", False)
        with pytest.raises(RuntimeError, match="fastapi未安装"):
            create_app()


class TestConnectionCloseAll:
    def test_close_all_异常warning不中断(self, monkeypatch, caplog) -> None:
        """DoD：close_all 关闭异常记 warning，不中断清理（不再裸 pass 吞噬）。"""
        conn = DatabaseConnection(db_path=":memory:")
        monkeypatch.setattr(conn, "_thread_conns", {1: _BadClose()})

        with caplog.at_level("WARNING"):
            conn.close_all()
        assert any("关闭连接异常" in r.message for r in caplog.records)


class TestCiBleBaseline:
    def test_ci_ble001_专项护栏存在(self) -> None:
        """DoD：CI lint 含 BLE001 专项护栏 + 基线变量，新增裸 except 阻断。"""
        text = (_PROJ / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "BLE001_BASELINE" in text
        assert "ruff BLE001 专项护栏" in text
        assert "--select BLE001" in text


class _BadClose:
    def close(self) -> None:
        raise RuntimeError("close boom")
