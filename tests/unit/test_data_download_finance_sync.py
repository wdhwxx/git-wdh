# 文件名：tests/unit/test_data_download_finance_sync.py
"""GUI「财务CSV增量更新」选项接入单测（缺口A GUI化，蓝图 #75）。

覆盖：
  - OPTIONS/DISPLAY_ORDER/EXEC_ORDER/TASKS 注册完整性
  - _entry_finance_sync 调用 FinancialSyncService 并返回同构汇总（monkeypatch）
  - 执行顺序：finance_sync 在 finance_import 之前（先增量同步后入库+因子）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from myquant.gui.data_download_dialog import (
    DISPLAY_ORDER,
    EXEC_ORDER,
    OPTIONS,
    TASKS,
    _entry_finance_sync,
)


class TestFinanceSyncRegistration:
    """GUI 选项注册。"""

    def test_选项与顺序注册完整(self) -> None:
        """finance_sync 已注册到 OPTIONS/DISPLAY_ORDER/EXEC_ORDER/TASKS。"""
        assert "finance_sync" in OPTIONS
        assert OPTIONS["finance_sync"][0] == "财务CSV增量更新"
        assert DISPLAY_ORDER.index("finance_sync") == DISPLAY_ORDER.index("finance") + 1
        assert EXEC_ORDER.index("finance_sync") == EXEC_ORDER.index("finance") + 1
        assert TASKS["finance_sync"][0] == OPTIONS["finance_sync"][0]
        assert TASKS["finance_sync"][2] is False  # 无需代码输入参数

    def test_执行顺序在入库之前(self) -> None:
        """EXEC_ORDER 中 finance_sync 先于 finance_import（增量同步→入库+因子）。"""
        assert EXEC_ORDER.index("finance_sync") < EXEC_ORDER.index("finance_import")


class TestEntryFinanceSync:
    """_entry_finance_sync 汇总输出。"""

    def test_调用服务并返回同构汇总(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """monkeypatch FinancialSyncService → 汇总字典字段与其它任务一致。"""
        class FakeStats:
            symbols = ["601091.SH"]
            fetched_symbols = 1
            skipped_symbols = 0
            new_records = 13
            csv_path = "x.csv"
            import_stats = {"total_rows": 329735, "success_rows": 329735, "symbol_count": 5315}
            missing_after = []

        class FakeService:
            def __init__(self, project_root: Path) -> None:
                self._root = project_root

            def run_sync(self, auto_import: bool = True, progress_callback: Any = None) -> FakeStats:
                assert auto_import is True
                return FakeStats()

        monkeypatch.setattr(
            "myquant.data_access.financial_sync_service.FinancialSyncService",
            FakeService,
        )
        result = _entry_finance_sync(Path(r"D:\Projects\Pythonwdhmm"))
        assert result["total"] == 1
        assert result["success"] == 1
        assert result["failed"] == 0
        assert result["records"] == 13
        assert result["csv_path"] == "x.csv"
        assert result["import"]["symbol_count"] == 5315
        assert result["missing_after"] == []
