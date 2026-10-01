# 文件名：tests/unit/test_cleanup_service.py
"""cleanup_service 测试残留清理 pattern 保护语义单测（F6）。

验证：
  - TEST_CLEAN_REPORT_PATTERN 只匹配测试报告（clean_all_TEST_*），
    生产全量清洗报告 clean_all_full_clean_*.json 受保护不误删；
  - TEST_BACKUP_PATTERN 只匹配测试备份（backup_TEST_*），
    真实清洗备份 backup_*_quant_before_clean.db 受保护不误删。
"""

from __future__ import annotations

import fnmatch

from myquant.utils.cleanup_service import TEST_BACKUP_PATTERN, TEST_CLEAN_REPORT_PATTERN


class TestCleanReportPattern:
    """全量清洗测试报告 pattern（A 类，outputs 目录）。"""

    def test_生产报告不匹配(self) -> None:
        """clean_all_full_clean_*.json（生产）→ 不匹配，保护。"""
        names = [
            "clean_all_full_clean_20260930_222426.json",
            "clean_all_full_clean_20260929_113550.json",
        ]
        for name in names:
            assert not fnmatch.fnmatch(name, TEST_CLEAN_REPORT_PATTERN), name

    def test_测试报告匹配(self) -> None:
        """clean_all_TEST_*（测试）→ 匹配，可清理。"""
        assert fnmatch.fnmatch("clean_all_TEST_x.json", TEST_CLEAN_REPORT_PATTERN)
        assert fnmatch.fnmatch("clean_all_TEST_20261001_000000.json", TEST_CLEAN_REPORT_PATTERN)


class TestBackupPattern:
    """全量清洗测试备份 pattern（B 类，backups 目录）。"""

    def test_真实备份不匹配(self) -> None:
        """backup_*_quant_before_clean.db（真实清洗备份）→ 不匹配，保护。"""
        names = [
            "backup_20260929_113550_quant_before_clean.db",
            "backup_20260930_222426_quant_before_clean.db",
            "backup_20261001_093000_quant_before_clean.db",
        ]
        for name in names:
            assert not fnmatch.fnmatch(name, TEST_BACKUP_PATTERN), name

    def test_测试备份匹配(self) -> None:
        """backup_TEST_*_quant_before_clean.db（测试）→ 匹配，可清理。"""
        assert fnmatch.fnmatch("backup_TEST_x_quant_before_clean.db", TEST_BACKUP_PATTERN)
