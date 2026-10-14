# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_candidate_pool.py
"""
候选池交叉验证（myquant/backtest/candidate_pool.py）单元测试（G0-9 补测）
==========================================================
覆盖：
  - _load_completed_entries：文件缺失 / 结构非法 / completed 过滤 / 绩效字段非法跳过
  - _filter_entries：阈值防御校验 + 硬过滤逻辑
  - build_candidate_pool：双策略交集 / 综合夏普降序 / TopN 标记 / 空池报错 /
    top_n 非法报错 / CSV 中文表头导出
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from myquant.backtest.candidate_pool import (
    OUTPUT_COLUMNS,
    build_candidate_pool,
)


def _write_results(cache_dir: Path, results: dict) -> None:
    """写一个策略缓存 results.json。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "results.json").write_text(
        json.dumps({"results": results}, ensure_ascii=False), encoding="utf-8",
    )


def _completed(
    total_return: float,
    sharpe: float,
    max_drawdown: float,
    trade_count: int,
) -> dict:
    """构造一条 completed 缓存条目。"""
    return {
        "status": "completed",
        "trade_count": trade_count,
        "metrics": {
            "total_return": total_return,
            "sharpe_ratio": sharpe,
            "max_drawdown": max_drawdown,
        },
    }


# ─────────────────────────────────────────────
# _load_completed_entries
# ─────────────────────────────────────────────
class TestLoadCompletedEntries:
    def test_cache_dir_missing_raises(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _load_completed_entries

        with pytest.raises(FileNotFoundError):
            _load_completed_entries(tmp_path / "no_such_dir")

    def test_results_file_missing_raises(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _load_completed_entries

        d = tmp_path / "rsrs"
        d.mkdir()
        with pytest.raises(FileNotFoundError):
            _load_completed_entries(d)

    def test_missing_results_key_raises(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _load_completed_entries

        d = tmp_path / "rsrs"
        d.mkdir()
        (d / "results.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError):
            _load_completed_entries(d)

    def test_only_completed_kept_and_fields_parsed(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _load_completed_entries

        d = tmp_path / "rsrs"
        (d / "results.json").parent.mkdir(parents=True, exist_ok=True)
        _write_results(d, {
            "600001.SH": _completed(0.10, 1.2, 0.05, 20),
            "600002.SH": _completed(0.05, 0.6, 0.08, 15),
            "600003.SH": {"status": "error", "trade_count": 0, "metrics": {}},
        })
        entries = _load_completed_entries(d)
        assert set(entries) == {"600001.SH", "600002.SH"}
        assert entries["600001.SH"]["sharpe"] == 1.2
        assert entries["600001.SH"]["trades"] == 20

    def test_bad_metrics_fields_skipped_with_warning(
        self, tmp_path: Path,
    ) -> None:
        from myquant.backtest.candidate_pool import _load_completed_entries

        d = tmp_path / "rsrs"
        d.mkdir(parents=True, exist_ok=True)
        _write_results(d, {
            "600001.SH": {"status": "completed", "trade_count": 10,
                          "metrics": {"total_return": "bad", "sharpe_ratio": 1.0,
                                      "max_drawdown": 0.1}},
            "600002.SH": _completed(0.1, 1.0, 0.1, 10),
        })
        entries = _load_completed_entries(d)
        # 字段非法条目被跳过，合法条目保留
        assert set(entries) == {"600002.SH"}


# ─────────────────────────────────────────────
# _filter_entries
# ─────────────────────────────────────────────
class TestFilterEntries:
    def test_invalid_thresholds_raise(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _filter_entries

        with pytest.raises(ValueError):
            _filter_entries({}, 0.0, 10, 0.1)
        with pytest.raises(ValueError):
            _filter_entries({}, 0.5, 0, 0.1)
        with pytest.raises(ValueError):
            _filter_entries({}, 0.5, 10, 1.0)

    def test_filter_keeps_only_passing(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import _filter_entries

        entries = {
            "A": {"sharpe": 1.0, "trades": 20, "max_drawdown": 0.05},
            "B": {"sharpe": 0.3, "trades": 20, "max_drawdown": 0.05},
            "C": {"sharpe": 1.0, "trades": 5, "max_drawdown": 0.05},
            "D": {"sharpe": 1.0, "trades": 20, "max_drawdown": 0.15},
        }
        passed = _filter_entries(entries, 0.5, 10, 0.10)
        assert passed == {"A"}


# ─────────────────────────────────────────────
# build_candidate_pool
# ─────────────────────────────────────────────
class TestBuildCandidatePool:
    def _make_pairs(self, tmp_path: Path) -> tuple[Path, Path]:
        """写 rsrs/ma_cross 两个缓存，构造一组满足过滤的标的。"""
        rsrs = tmp_path / "rsrs"
        ma = tmp_path / "ma_cross"
        _write_results(rsrs, {
            "000001.SZ": _completed(0.10, 1.0, 0.05, 20),
            "000002.SZ": _completed(0.06, 0.8, 0.06, 15),
            "000003.SZ": _completed(0.10, 1.0, 0.05, 20),
        })
        _write_results(ma, {
            "000001.SZ": _completed(0.09, 1.1, 0.04, 18),
            "000002.SZ": _completed(0.07, 0.9, 0.05, 12),
            "000003.SZ": _completed(0.02, 0.2, 0.10, 8),  # 不通过过滤
        })
        return rsrs, ma

    def test_builds_pool_sorted_by_avg_sharpe(self, tmp_path: Path) -> None:
        rsrs, ma = self._make_pairs(tmp_path)
        pool = build_candidate_pool(rsrs, ma)
        assert set(pool["symbol"]) == {"000001.SZ", "000002.SZ"}
        assert list(pool.columns) == OUTPUT_COLUMNS
        # 综合夏普降序
        avg = list(pool["avg_sharpe"])
        assert avg == sorted(avg, reverse=True)
        # 000001 avg_sharpe=(1.0+1.1)/2=1.05，000002=(0.8+0.9)/2=0.85
        assert pool.iloc[0]["symbol"] == "000001.SZ"

    def test_top_n_marked(self, tmp_path: Path) -> None:
        rsrs, ma = self._make_pairs(tmp_path)
        pool = build_candidate_pool(rsrs, ma, top_n=1)
        assert int(pool["is_top30"].sum()) == 1
        assert bool(pool.iloc[0]["is_top30"]) is True

    def test_empty_dual_pool_raises(self, tmp_path: Path) -> None:
        rsrs = tmp_path / "rsrs"
        ma = tmp_path / "ma_cross"
        _write_results(rsrs, {"A": _completed(0.1, 0.4, 0.1, 20)})   # 夏普不过
        _write_results(ma, {"A": _completed(0.1, 0.4, 0.1, 20)})
        with pytest.raises(ValueError):
            build_candidate_pool(rsrs, ma)

    def test_invalid_top_n_raises(self, tmp_path: Path) -> None:
        rsrs, ma = self._make_pairs(tmp_path)
        with pytest.raises(ValueError):
            build_candidate_pool(rsrs, ma, top_n=0)

    def test_csv_export_with_chinese_headers(self, tmp_path: Path) -> None:
        from myquant.backtest.candidate_pool import CSV_HEADERS_ZH

        rsrs, ma = self._make_pairs(tmp_path)
        out = tmp_path / "pool.csv"
        build_candidate_pool(rsrs, ma, out_path=out)
        assert out.is_file()
        df = pd.read_csv(out, encoding="utf-8-sig")
        # 中文表头已写入
        assert "股票代码" in df.columns
        assert "综合夏普比率" in df.columns
        # 中文表头与英文列一一对应
        assert set(df.columns) == set(CSV_HEADERS_ZH.values())
