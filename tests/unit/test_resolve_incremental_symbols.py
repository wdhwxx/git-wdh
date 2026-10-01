# 文件名：tests/unit/test_resolve_incremental_symbols.py
"""resolve_incremental_symbols 范围实时推导单测（增量防焊死）。

验证：
  - 与全量清洗同一规则源：config/data_clean_run.json 的 exclude_rules + exclude_st
  - 规则外标的（指数/基金/板块/北交所）被剔除，规则内标的保留
  - ST 名单串联（exclude_st=true 时命中剔除）
  - json 缺失 → 回退 DEFAULT_EXCLUDE_RULES（10 条静态兜底）且不抛错
  - 防焊死反例：规则增删 → 范围跟随变化（绝不读取任何历史标的数字）
  - 空池 / 统计字段完整
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from myquant.data_access.full_clean_service import (
    DEFAULT_EXCLUDE_RULES,
    resolve_incremental_symbols,
)

_POOL: List[str] = [
    "000001.SZ",   # 平安银行：规则内（000.SH 不误伤 .SZ）
    "600519.SH",   # 贵州茅台：规则内
    "000300.SH",   # 沪深300：命中 000.SH → 剔除
    "689009.SH",   # 九号公司：命中 68 → 剔除
    "300750.SZ",   # 宁德时代：命中 30.SZ → 剔除
    "688111.SH",   # 金山办公：命中 688.SH → 剔除
    "600000.SH",   # 浦发银行：规则内
]

_RULES_14: List[str] = [
    "000.SH", "399", "880", "5.SH", "1.SZ", "30.SZ", "688.SH",
    "4", "8", "92", "68", "90", "20", "99",
]


def _write_run_json(tmp_path: Path, rules: List[str], exclude_st: bool = False) -> Path:
    """写入 config/data_clean_run.json（14 条规则口径的测试替身）。"""
    path = tmp_path / "config" / "data_clean_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"exclude_rules": rules, "exclude_st": exclude_st}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_st_yaml(tmp_path: Path, items: Dict[str, str]) -> Path:
    """写入 config/st_blacklist.yaml（ST 名单替身，顶层 symbol: name 映射）。"""
    path = tmp_path / "config" / "st_blacklist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'{k}: "{v}"' for k, v in items.items())
    path.write_text(body + "\n", encoding="utf-8")
    return path


class TestResolveIncrementalSymbols:
    """范围实时推导（防焊死核心）。"""

    def test_规则内保留规则外剔除(self, tmp_path: Path) -> None:
        """14 条规则 → 指数/基金/板块/科创板等规则外标的剔除，规则内保留。"""
        _write_run_json(tmp_path, _RULES_14)
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["original"] == 7
        assert stats["after"] == 3
        assert sorted(stats["excluded_prefix"]) == sorted(
            ["000300.SH", "689009.SH", "300750.SZ", "688111.SH"]
        )
        assert stats["excluded_st"] == []

    def test_ST名单串联剔除(self, tmp_path: Path) -> None:
        """exclude_st=true + ST 名单命中 → 从规则内结果再剔除。"""
        _write_run_json(tmp_path, _RULES_14, exclude_st=True)
        _write_st_yaml(tmp_path, {"000001.SZ": "*ST示例"})
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert "000001.SZ" not in keep  # 被 ST 名单剔除
        assert "600519.SH" in keep
        assert stats["excluded_st"] == ["000001.SZ"]

    def test_JSON缺失回退默认规则(self, tmp_path: Path) -> None:
        """config/data_clean_run.json 不存在 → 回退 DEFAULT_EXCLUDE_RULES（10 条），不抛错。"""
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        # 10 条兜底无 "68" → 689009.SH 保留；有 000.SH → 000300.SH 仍剔除
        assert "000300.SH" not in keep
        assert "689009.SH" in keep
        assert stats["original"] == 7
        assert stats["after"] == len(keep)

    def test_防焊死反例_规则变化范围跟随(self, tmp_path: Path) -> None:
        """规则增删 → 范围跟随变化（核心反例：绝不焊死任何标的数字）。"""
        # 规则 A：无 68 → 689009.SH 保留
        _write_run_json(tmp_path, [r for r in _RULES_14 if r != "68"])
        keep_a, _ = resolve_incremental_symbols(_POOL, tmp_path)
        assert "689009.SH" in keep_a
        # 规则 B：加回 68 → 689009.SH 被剔除
        _write_run_json(tmp_path, _RULES_14)
        keep_b, _ = resolve_incremental_symbols(_POOL, tmp_path)
        assert "689009.SH" not in keep_b
        # 范围必须跟随规则变化（防焊死反例通过）
        assert "689009.SH" in keep_a and "689009.SH" not in keep_b

    def test_空池返回空(self, tmp_path: Path) -> None:
        """空池 → keep 为空、统计 original=0，不报错。"""
        _write_run_json(tmp_path, _RULES_14)
        keep, stats = resolve_incremental_symbols([], tmp_path)
        assert keep == []
        assert stats["original"] == 0
        assert stats["after"] == 0

    def test_统计字段完整(self, tmp_path: Path) -> None:
        """返回统计字典含 original/after/excluded_prefix/excluded_st。"""
        _write_run_json(tmp_path, _RULES_14)
        _, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert set(stats.keys()) == {"original", "after", "excluded_prefix", "excluded_st"}
        assert stats["after"] == stats["original"] - len(stats["excluded_prefix"]) - len(stats["excluded_st"])

    def test_exclude_st无名单不报错(self, tmp_path: Path) -> None:
        """exclude_st=true 但 st_blacklist.yaml 缺失 → 按空名单处理，不剔除、不报错。"""
        _write_run_json(tmp_path, _RULES_14, exclude_st=True)
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["excluded_st"] == []
