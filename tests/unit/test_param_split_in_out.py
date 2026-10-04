# -*- coding: utf-8 -*-
"""
KP-P2-05 参数寻优无样本外（T-05 台账 / 用户指令 T-04）
=======================================================
覆盖:
  1. 无分割模式向后兼容（全样本取顶，结论=样本内指标）
  2. in_sample_end 防御校验
  3. 分割模式：每参数组合输出 in_*/out_* 双区段指标 + 试验次数（=组合数×2）
  4. best()：样本内选参（寻优），结论字段=样本外（确认）——"样本内选中后样本外复算"
  5. ranked_in_sample / ranked_out_sample / ranked（结论）三套排序
  6. summary 输出 in/out 双列 + 试验次数
  7. walk_forward 滚动三段式（每折样本内寻优→样本外确认，聚合样本外结论）

回归依据: 审计 KP-P2-05 —— ranked()/best() 全样本取顶，无 in/out-sample 分割。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from myquant.backtest.param_optimizer import (  # noqa: E402
    ParamGrid,
    ParamOptimizationResult,
    ParamOptimizer,
    ParamSpec,
    ParamTrial,
    evaluate_params,
)


def _make_data(
    n_bars: int = 120,
    seed: int = 7,
    symbols: tuple = ("000001.SZ",),
) -> dict:
    """构造确定性演示行情（中文列，适配 param_optimizer 引擎路径）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2023-01-02", periods=n_bars, freq="B")
    data = {}
    for idx, symbol in enumerate(symbols):
        drift = 0.0006 + 0.0004 * idx
        noise = rng.normal(0.0, 0.01, n_bars)
        trend = drift * np.arange(n_bars)
        wave = 0.012 * np.sin(np.arange(n_bars) / (18.0 + idx))
        closes = 10.0 * (1.0 + trend + noise + wave)
        data[symbol] = pd.DataFrame(
            {
                "开盘价": closes * 0.997,
                "最高价": closes * 1.01,
                "最低价": closes * 0.99,
                "收盘价": closes,
                "成交量": 100_000.0 * (1.0 + 0.5 * rng.random(n_bars)),
                "成交额": closes * 100_000.0,
            },
            index=dates,
        )
    return data


def _make_grid() -> ParamGrid:
    return ParamGrid(
        (
            ParamSpec("fast_period", (3, 5)),
            ParamSpec("slow_period", (10, 15)),
        )
    )


def _run_split(in_sample_end: str) -> ParamOptimizationResult:
    opt = ParamOptimizer(
        strategy_name="ma_cross",
        grid=_make_grid(),
        data=_make_data(),
        objective="sharpe_ratio",
        start_date="2023-01-02",
        end_date="2024-01-31",
        in_sample_end=in_sample_end,
        cpu_workers=1,
    )
    return opt.optimize()


def _run_nosplit() -> ParamOptimizationResult:
    opt = ParamOptimizer(
        strategy_name="ma_cross",
        grid=_make_grid(),
        data=_make_data(),
        objective="sharpe_ratio",
        start_date="2023-01-02",
        end_date="2024-01-31",
        cpu_workers=1,
    )
    return opt.optimize()


@pytest.mark.lookahead
class TestNoSplitCompat:
    """无分割模式向后兼容（原全样本取顶行为不变）。"""

    def test_无分割_全样本结论(self) -> None:
        result = _run_nosplit()
        assert result.split_mode is False
        # 1 标的 x 4 组合 = 4 条聚合记录；试验次数=组合数
        assert result.total_count == 4
        assert result.evaluation_count == 4
        best = result.best()
        assert best is not None
        # 无分割：结论字段=样本内指标
        assert best["sharpe_ratio"] == best["in_sharpe_ratio"]
        assert best["out_sharpe_ratio"] is None

    def test_无分割_best等于ranked首位(self) -> None:
        result = _run_nosplit()
        best = result.best()
        ranked_top = result.ranked(top=1)[0]
        assert best["params"] == ranked_top["params"]


@pytest.mark.lookahead
class TestSplitMode:
    """分割模式：双区段指标 + 样本内选参 + 样本外结论。"""

    def test_in_sample_end_介于区间(self) -> None:
        result = _run_split("2023-07-01")
        assert result.split_mode is True
        # 1 标的 x 4 组合 = 4 条聚合记录；试验次数=4x2=8
        assert result.total_count == 4
        assert result.evaluation_count == 8
        rec = result.trials[0]
        for key in ("in_sharpe_ratio", "out_sharpe_ratio", "in_total_return"):
            assert key in rec
        # 结论字段=样本外
        assert rec["sharpe_ratio"] == rec["out_sharpe_ratio"]

    def test_in_sample_end_越界抛错(self) -> None:
        with pytest.raises(ValueError, match="in_sample_end 必须介于"):
            ParamOptimizer(
                strategy_name="ma_cross",
                grid=_make_grid(),
                data=_make_data(),
                start_date="2023-01-02",
                end_date="2024-01-31",
                in_sample_end="2025-01-01",
            )
        with pytest.raises(ValueError, match="必须同时提供"):
            ParamOptimizer(
                strategy_name="ma_cross",
                grid=_make_grid(),
                data=_make_data(),
                start_date="2023-01-02",
                end_date="",
                in_sample_end="2023-07-01",
            )

    def test_best_样本内选参样本外结论(self) -> None:
        """DoD 核心：样本内选中后在样本外复算，结论以样本外为准。"""
        result = _run_split("2023-07-01")
        best = result.best()
        assert best is not None
        # best 是样本内寻优的 Top1
        in_top = result.ranked_in_sample(top=1)[0]
        assert best["params"] == in_top["params"]
        assert best["_rank"] == 1
        # 结论字段（sharpe_ratio）= 样本外
        assert best["sharpe_ratio"] == best["out_sharpe_ratio"]
        # 样本外有实际绩效
        assert best["out_sharpe_ratio"] is not None

    def test_ranked_in_out_并存(self) -> None:
        result = _run_split("2023-07-01")
        in_ranked = result.ranked_in_sample(top=2)
        out_ranked = result.ranked_out_sample(top=2)
        assert len(in_ranked) == 2
        assert len(out_ranked) == 2
        # 结论排序=按样本外排序
        conclusion_ranked = result.ranked(top=2)
        assert [r["params"] for r in conclusion_ranked] == [
            r["params"] for r in out_ranked
        ]

    def test_summary_双列与试验次数(self) -> None:
        result = _run_split("2023-07-01")
        text = result.summary()
        assert "分割: 样本内→样本外" in text
        assert "样本内" in text and "样本外" in text
        assert "试验次数: 8" in text
        assert "最优参数组合（样本内寻优，结论以样本外为准）" in text


@pytest.mark.lookahead
class TestWalkForward:
    """滚动 walk-forward 三段式（KP-P2-05 第三阶段）。"""

    def test_walk_forward_两折(self) -> None:
        opt = ParamOptimizer(
            strategy_name="ma_cross",
            grid=_make_grid(),
            data=_make_data(n_bars=150),
            objective="sharpe_ratio",
            start_date="2023-01-02",
            end_date="2024-01-31",
            cpu_workers=1,
        )
        wf = opt.walk_forward(folds=2)
        assert wf.fold_count == 2
        for fold in wf.folds:
            assert "train_end" in fold and "test_end" in fold
            assert fold["best_params"] is not None
            assert fold["out_sharpe_ratio"] is not None
        assert "样本外平均夏普" in wf.summary()

    def test_walk_forward_折数校验(self) -> None:
        opt = ParamOptimizer(
            strategy_name="ma_cross",
            grid=_make_grid(),
            data=_make_data(),
            objective="sharpe_ratio",
            start_date="2023-01-02",
            end_date="2024-01-31",
            cpu_workers=1,
        )
        with pytest.raises(ValueError, match="folds 必须为"):
            opt.walk_forward(folds=1)


@pytest.mark.lookahead
class TestTrialSplitField:
    """ParamTrial split 字段与 evaluate_params 透传。"""

    def test_split字段透传(self) -> None:
        data = _make_data(n_bars=60)
        trial = ParamTrial(
            params={"fast_period": 3, "slow_period": 10},
            strategy_name="ma_cross",
            symbol="000001.SZ",
            data=data,
            start_date="2023-01-02",
            end_date="2023-06-30",
            split="out",
        )
        outcome = evaluate_params(trial)
        assert outcome["split"] == "out"
