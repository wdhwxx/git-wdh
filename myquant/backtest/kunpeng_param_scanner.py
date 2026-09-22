# -*- coding: utf-8 -*-
# 文件名：myquant/backtest/kunpeng_param_scanner.py
"""
kunpeng_param_scanner.py - 鲲鹏策略参数网格扫描器（业务逻辑，P3-6 自 scripts/ 下沉）
====================================================================================
原 scripts/kunpeng_param_scan.py 承载的网格定义、数据加载、批量回测、指标聚合、
排序与阶段调度逻辑，按「脚本薄入口、业务逻辑归 myquant」纪律下沉本模块；
scripts/kunpeng_param_scan.py 只保留 argparse 参数解析 + 本类调用。

设计原则:
  - 常量（路径/默认参数/网格）随本模块集中管理，入口脚本零业务
  - 与原脚本行为一致：真实数据 vectorized 两阶段扫描、聚合口径、Top 榜排序、JSON 落盘
  - 防未来函数：回测区间 start_date=2024-01-01 起至数据末端，无未来数据引用
"""

from __future__ import annotations

import itertools
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("backtest.kunpeng_param_scanner")


class KunpengParamScanner:
    """鲲鹏策略参数网格扫描器（真实数据 / vectorized / 两阶段）。"""

    # ── 默认参数（与 config/backtest_kunpeng.yaml strategy.params 一致）──
    BASE_PARAMS: Dict[str, Any] = {
        "vol_ratio_threshold": 4.5,
        "vol_count_window": 6,
        "vol_active_days": 5,
        "barslast_vol_window": 13,
        "barslast_ratio_window": 15,
        "ma_sticky_threshold": 1.22,
        "diff_dea_ratio": 0.018,
        "macd_abs_threshold": 0.10,
        "macd_rise_threshold": 0.0001,
        "exit_mode": "standard",
        "hold_days_cap": 60,
        "max_position_pct": 0.20,
        "max_positions": 5,
    }

    # ── 阶段A：信号参数网格 ──
    SIGNAL_GRID: Dict[str, tuple] = {
        "vol_ratio_threshold": (3.5, 4.5, 5.5),
        "vol_active_days": (4, 5, 6),
        "ma_sticky_threshold": (1.15, 1.22, 1.30),
        "diff_dea_ratio": (0.012, 0.018, 0.025),
        "macd_rise_threshold": (0.0001, 0.005),
    }

    # ── 阶段B：出场+仓位参数网格 ──
    EXIT_GRID: Dict[str, tuple] = {
        "exit_mode": ("conservative", "standard", "aggressive"),
        "hold_days_cap": (30, 60, 90),
        "max_position_pct": (0.10, 0.20, 0.30),
        "max_positions": (3, 5, 8),
    }

    START_DATE: str = "2024-01-01"
    INITIAL_CAPITAL: float = 1_000_000.0

    def __init__(
        self,
        project_root: Optional[Path] = None,
        processed_dir: Optional[str] = None,
        results_json: Optional[Path] = None,
        scan_cache: Optional[Path] = None,
        out_json: Optional[Path] = None,
        cpu_workers: int = 8,
    ) -> None:
        """初始化扫描器（路径均可注入，默认取项目根相对路径）。"""
        root = Path(project_root or Path(__file__).resolve().parents[2])
        self._project_root = root
        self._processed_dir = str(processed_dir or (root / "datasets" / "processed"))
        self._results_json = Path(
            results_json or (root / "results" / "batch_backtest" / "kunpeng" / "results.json")
        )
        self._scan_cache = Path(scan_cache or (root / "cache" / "param_scan"))
        self._out_json = Path(out_json or (root / "results" / "param_scan" / "scan_results.json"))
        self._cpu_workers = cpu_workers

    # ── 阶段1：活跃标的池 ──

    def load_active_symbols(self, top_n: int = 50, min_trades: int = 3) -> List[str]:
        """从已有全池结果中挑选活跃标的（成交笔数 >= min_trades，取 top_n）。"""
        with open(self._results_json, "r", encoding="utf-8") as f:
            d = json.load(f)
        rows = [
            (s, v.get("trade_count") or 0)
            for s, v in d["results"].items()
            if v.get("status") == "completed" and (v.get("trade_count") or 0) >= min_trades
        ]
        rows.sort(key=lambda x: -x[1])
        return [s for s, _ in rows[:top_n]]

    # ── 阶段2：数据加载 ──

    def load_data(self, symbols: List[str]) -> Dict[str, Any]:
        """加载真实行情（HistoricalDataProvider，hfq）。"""
        from myquant.backtest.data_loader import HistoricalDataProvider

        provider = HistoricalDataProvider(
            processed_dir=self._processed_dir,
            default_adjust="hfq",
            data_format="parquet",
        )
        return provider.load_multiple(
            symbols,
            start_date=self.START_DATE,
            end_date=None,
            adjust="hfq",
        )

    # ── 阶段3：单组合评估 ──

    def run_one(self, combo: Dict[str, Any], data: Dict[str, Any], tag: str) -> Dict[str, Any]:
        """跑一个参数组合的批量回测（vectorized），返回聚合指标。"""
        from myquant.backtest.batch_backtest import BatchBacktester

        params = dict(self.BASE_PARAMS)
        params.update(combo)
        bt = BatchBacktester(
            strategy_name="kunpeng",
            params=params,
            data=data,
            fingerprint_path=str(self._scan_cache / f"fp_{tag}.json"),
            results_path=str(self._scan_cache / f"res_{tag}.json"),
            mode="vectorized",
            cpu_workers=self._cpu_workers,
            start_date=self.START_DATE,
            end_date=None,
            initial_capital=self.INITIAL_CAPITAL,
            frequency="1d",
        )
        result = bt.run(force_full=True)

        rows = []
        for symbol, r in result.per_symbol.items():
            if r.get("status") != "completed":
                continue
            m = r.get("metrics") or {}
            rows.append(
                {
                    "symbol": symbol,
                    "trade_count": int(r.get("trade_count") or 0),
                    "total_return": float(m.get("total_return") or 0.0),
                    "sharpe_ratio": float(m.get("sharpe_ratio") or 0.0),
                    "max_drawdown": float(m.get("max_drawdown") or 0.0),
                }
            )
        signal_rows = [r for r in rows if r["trade_count"] > 0]
        n = len(signal_rows)
        agg = {
            "params": combo,
            "n_symbols": len(rows),
            "n_signal": n,
            "total_trades": sum(r["trade_count"] for r in signal_rows),
            "avg_sharpe": sum(r["sharpe_ratio"] for r in signal_rows) / n if n else None,
            "avg_return": sum(r["total_return"] for r in signal_rows) / n if n else None,
            "avg_drawdown": sum(r["max_drawdown"] for r in signal_rows) / n if n else None,
            "med_sharpe": sorted(r["sharpe_ratio"] for r in signal_rows)[n // 2] if n else None,
            "win_rate": sum(1 for r in signal_rows if r["total_return"] > 0) / n if n else None,
        }
        return agg

    # ── 阶段4：网格展开 ──

    @staticmethod
    def expand_grid(grid: Dict[str, tuple]) -> List[Dict[str, Any]]:
        """笛卡尔积展开参数网格。"""
        keys = list(grid.keys())
        combos = []
        for values in itertools.product(*[grid[k] for k in keys]):
            combos.append(dict(zip(keys, values)))
        return combos

    # ── 主扫描流程 ──

    def run(self, stage: str = "both", top_n: int = 50, min_trades: int = 3) -> int:
        """执行参数网格扫描，返回退出码（0=成功 / 2=无有效数据）。"""
        logger.info("=" * 72)
        logger.info("  鲲鹏策略参数网格扫描（真实数据 / vectorized）")
        logger.info("=" * 72)

        if not self._results_json.exists():
            logger.error("全池回测结果缺失: %s（请先运行 run_鲲鹏批量回测.py 生成结果）", self._results_json)
            return 2

        symbols = self.load_active_symbols(top_n=top_n, min_trades=min_trades)
        logger.info("[标的池] 成交>=%s 的活跃标的 top %s", min_trades, len(symbols))
        data = self.load_data(symbols)
        logger.info("[数据] 实际加载 %s 个标的（部分过滤后无数据被跳过）", len(data))
        if not data:
            logger.error("[错误] 无有效数据")
            return 2

        stages = []
        if stage in ("signal", "both"):
            stages.append(("A_signal", self.SIGNAL_GRID))
        if stage in ("exit", "both"):
            stages.append(("B_exit", self.EXIT_GRID))

        all_results = {
            "meta": {
                "start_date": self.START_DATE,
                "end_date": None,
                "n_symbols": len(data),
            },
            "stages": {},
        }
        for tag, grid in stages:
            combos = self.expand_grid(grid)
            logger.info("[阶段 %s] %s 个参数组合 × %s 标的", tag, len(combos), len(data))
            t0 = time.perf_counter()
            stage_rows = []
            for i, combo in enumerate(combos, 1):
                agg = self.run_one(combo, data, f"{tag}_{i}")
                stage_rows.append(agg)
                if i % 20 == 0 or i == len(combos):
                    logger.info("  进度 %s/%s（耗时 %.0fs）", i, len(combos), time.perf_counter() - t0)
            elapsed = time.perf_counter() - t0
            # 排序：优先信号数（>=1/3 标的），再按平均夏普降序
            stage_rows.sort(
                key=lambda r: (
                    -(r["n_signal"] >= max(3, len(data) // 3)),
                    -(r["avg_sharpe"] if r["avg_sharpe"] is not None else -999),
                )
            )
            all_results["stages"][tag] = {"elapsed_seconds": round(elapsed, 1), "rows": stage_rows}
            logger.info("[阶段 %s 完成] 耗时 %.0fs | Top5:", tag, elapsed)
            for r in stage_rows[:5]:
                logger.info(
                    "  夏普 %.3f | 收益 %.2%% | 回撤 %.2%% | 信号 %s/%s 笔%s | %s",
                    r["avg_sharpe"] if r["avg_sharpe"] is not None else float("nan"),
                    r["avg_return"] if r["avg_return"] is not None else 0.0,
                    r["avg_drawdown"] if r["avg_drawdown"] is not None else 0.0,
                    r["n_signal"],
                    r["n_symbols"],
                    r["total_trades"],
                    r["params"],
                )

        self._out_json.parent.mkdir(parents=True, exist_ok=True)
        with open(self._out_json, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)
        logger.info("[已保存] %s", self._out_json)
        return 0


if __name__ == "__main__":
    # 演示：扫描器自测（不跑真实扫描，仅验证组装与退出码契约）
    scanner = KunpengParamScanner()
    assert scanner.START_DATE == "2024-01-01"
    assert len(scanner.expand_grid({"a": (1, 2), "b": (3, 4)})) == 4
    assert scanner.load_active_symbols is not None
    print("KunpengParamScanner 组装自测通过")
