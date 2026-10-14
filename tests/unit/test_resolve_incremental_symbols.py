# 文件名：tests/unit/test_resolve_incremental_symbols.py
"""resolve_incremental_symbols 范围实时推导单测（增量防焊死）。

验证：
  - 与全量清洗同一规则源：config/data_clean_run.json 的 exclude_rules + exclude_st
  - 规则外标的（指数/基金/板块/北交所）被剔除，规则内标的保留
  - ST 名单串联（exclude_st=true 时命中剔除）
  - json 缺失 → 回退 DEFAULT_EXCLUDE_RULES（14 条静态兜底，与 config 实际口径一致，#87 补齐）且不抛错
  - 防焊死反例：规则增删 → 范围跟随变化（绝不读取任何历史标的数字）
  - 空池 / 统计字段完整
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

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


def _write_run_json(
    tmp_path: Path, rules: List[str], exclude_st: bool = False,
    suspend_days: int = 0,
) -> Path:
    """写入 config/data_clean_run.json（14 条规则口径的测试替身）。"""
    path = tmp_path / "config" / "data_clean_run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg: Dict[str, Any] = {"exclude_rules": rules, "exclude_st": exclude_st}
    if suspend_days > 0:
        cfg["exclude_suspended_days"] = suspend_days
    path.write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8",
    )
    return path


def _write_st_yaml(tmp_path: Path, items: Dict[str, str]) -> Path:
    """写入 config/st_blacklist.yaml（ST 名单替身，顶层 symbol: name 映射）。"""
    path = tmp_path / "config" / "st_blacklist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'{k}: "{v}"' for k, v in items.items())
    path.write_text(body + "\n", encoding="utf-8")
    return path


def _write_db(tmp_path: Path, last_dates: Dict[str, str]) -> None:
    """写入 database/quant.db（market_data daily/hfq 最后交易日替身，供长期停牌规则）。"""
    db_path = tmp_path / "database" / "quant.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE market_data (symbol TEXT, trade_date TEXT, "
            "period TEXT, adjust TEXT)"
        )
        for sym, td in last_dates.items():
            conn.execute(
                "INSERT INTO market_data (symbol, trade_date, period, adjust) "
                "VALUES (?, ?, 'daily', 'raw')",
                (sym, td),
            )
        conn.commit()
    finally:
        conn.close()


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
        """config/data_clean_run.json 不存在 → 回退 DEFAULT_EXCLUDE_RULES（14 条），不抛错。"""
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        # 14 条兜底含 "68" → 689009.SH 被剔除；有 000.SH → 000300.SH 仍剔除
        # （#87：兜底与 config 实际口径一致，防 config 缺失时 B 股 90/20、科创板 68 复活）
        assert "000300.SH" not in keep
        assert "689009.SH" not in keep
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["original"] == 7
        assert stats["after"] == len(keep)
        assert set(DEFAULT_EXCLUDE_RULES) == set(_RULES_14)  # 兜底 = 14 条口径

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
        """返回统计字典含 original/after/excluded_prefix/excluded_st/excluded_suspended。"""
        _write_run_json(tmp_path, _RULES_14)
        _, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert set(stats.keys()) == {"original", "after", "excluded_prefix", "excluded_st", "excluded_suspended"}
        assert stats["after"] == stats["original"] - len(stats["excluded_prefix"]) - len(stats["excluded_st"]) - len(stats["excluded_suspended"])

    def test_exclude_st无名单不报错(self, tmp_path: Path) -> None:
        """exclude_st=true 但 st_blacklist.yaml 缺失 → 按空名单处理，不剔除、不报错。"""
        _write_run_json(tmp_path, _RULES_14, exclude_st=True)
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["excluded_st"] == []


    # ── #79 长期停牌/退市剔除（60 天动态规则） ──

    def test_长期停牌启用_超阈值剔除(self, tmp_path: Path) -> None:
        """exclude_suspended_days=60：最后交易日距今 >60 天剔除，≤60 天保留。"""
        ref = datetime.now().strftime("%Y-%m-%d")
        old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        recent = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        _write_run_json(tmp_path, _RULES_14, suspend_days=60)
        _write_db(tmp_path, {"000001.SZ": old, "600519.SH": recent, "600000.SH": recent})
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path, ref_date=ref)
        assert "000001.SZ" not in keep       # 100 天 → 剔除
        assert "600519.SH" in keep           # 10 天 → 保留
        assert "600000.SH" in keep
        assert stats["excluded_suspended"] == ["000001.SZ"]
        assert stats["after"] == 2

    def test_长期停牌_边界等于阈值保留(self, tmp_path: Path) -> None:
        """gap == 60 天 → 保留（严格大于才剔除）。"""
        ref = "2026-10-01"
        _write_run_json(tmp_path, _RULES_14, suspend_days=60)
        _write_db(tmp_path, {"600519.SH": "2026-08-02"})  # 60 天整
        keep, stats = resolve_incremental_symbols(["600519.SH"], tmp_path, ref_date=ref)
        assert keep == ["600519.SH"]
        assert stats["excluded_suspended"] == []

    def test_长期停牌_DB无记录保留(self, tmp_path: Path) -> None:
        """候选标的中 DB 无行情记录 → 无从判定，保留（不阻断）。"""
        ref = "2026-10-01"
        _write_run_json(tmp_path, _RULES_14, suspend_days=60)
        _write_db(tmp_path, {"600519.SH": "2026-09-01"})
        keep, stats = resolve_incremental_symbols(
            ["600519.SH", "000001.SZ"], tmp_path, ref_date=ref,
        )
        assert "000001.SZ" in keep  # DB 无记录 → 保留
        assert "600519.SH" in keep
        assert stats["excluded_suspended"] == []

    def test_长期停牌_未启用时不受影响(self, tmp_path: Path) -> None:
        """未配置 exclude_suspended_days（旧 config 兼容）→ 不启用、全部保留。"""
        _write_run_json(tmp_path, _RULES_14)
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["excluded_suspended"] == []

    def test_长期停牌_DB缺失不报错(self, tmp_path: Path) -> None:
        """启用规则但 quant.db 不存在 → 跳过该规则，不抛错、全保留。"""
        _write_run_json(tmp_path, _RULES_14, suspend_days=60)
        keep, stats = resolve_incremental_symbols(_POOL, tmp_path)
        assert keep == ["000001.SZ", "600519.SH", "600000.SH"]
        assert stats["excluded_suspended"] == []