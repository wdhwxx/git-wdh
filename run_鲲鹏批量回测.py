# -*- coding: utf-8 -*-
# 文件名：run_鲲鹏批量回测.py
"""
鲲鹏壹号策略全池批量回测薄入口（M2 方案A两段式）
====================================================
阶段一：vectorized 全池扫描（3226 标的，多进程并行 + 指纹增量缓存，无资金约束）
阶段二：按成交笔数取 top N → mixed 精算（max_position_pct 资金约束生效），含成交/净值明细

本脚本为薄入口：仅做 参数解析 → 读配置 → 调用 myquant.backtest.batch_backtest（BatchBacktester）
与 myquant.backtest.data_loader（HistoricalDataProvider），不包含任何策略/引擎业务实现。

用法:
  python run_鲲鹏批量回测.py                            # 默认读 config/backtest_kunpeng.yaml
  python run_鲲鹏批量回测.py --cpu 8 --batch-size 50     # 多进程 + 分批
  python run_鲲鹏批量回测.py --refine-top 30             # 阶段二 mixed 精算 top 30
  python run_鲲鹏批量回测.py --no-refine                 # 只跑阶段一（全池扫描）
  python run_鲲鹏批量回测.py --symbols 600519.SH,000001.SZ  # 覆盖标的池（冒烟/局部验证）
  python run_鲲鹏批量回测.py --start 2024-01-01 --end 2025-12-31  # 覆盖回测区间
  python run_鲲鹏批量回测.py --force-full                # 忽略指纹增量，强制全量重算

退出码: 0=成功；1=配置/参数错误；2=运行失败
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("run_kunpeng_batch")

# ── 常量（模块顶部，无魔法数字） ─────────────────────────────
DEFAULT_CONFIG: str = "config/backtest_kunpeng.yaml"
DEFAULT_MODE: str = "vectorized"
DEFAULT_REFINE_TOP: int = 30
DEFAULT_CACHE_DIR: str = "results/batch_backtest/kunpeng"
DEFAULT_OUTPUT_DIR: str = "outputs/kunpeng_batch"
VALID_MODES: Tuple[str, ...] = ("auto", "event", "mixed", "vectorized")
TIMESTAMP_FMT: str = "%Y%m%d_%H%M%S"


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。

    Returns:
        项目根目录绝对路径。
    """
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    return script_dir


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。

    Returns:
        已配置的 ArgumentParser。
    """
    parser = argparse.ArgumentParser(
        prog="run_鲲鹏批量回测",
        description="Pythonwdhmm — 鲲鹏壹号策略全池批量回测（两段式：vectorized 扫描 + mixed 精算）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python run_鲲鹏批量回测.py
  python run_鲲鹏批量回测.py --cpu 8 --batch-size 50
  python run_鲲鹏批量回测.py --refine-top 30 --no-refine
  python run_鲲鹏批量回测.py --symbols 600519.SH,000001.SZ --no-refine""",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=DEFAULT_CONFIG,
        help=f"回测配置文件路径（默认 {DEFAULT_CONFIG}）",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=DEFAULT_MODE,
        help=f"阶段一引擎模式（默认 {DEFAULT_MODE}；可选 {VALID_MODES}）",
    )
    parser.add_argument(
        "--cpu", dest="cpu_workers",
        type=int,
        default=None,
        help="计算进程池大小（None 使用默认并行度）",
    )
    parser.add_argument(
        "--batch-size", dest="batch_size",
        type=int,
        default=None,
        help="每批标的数（None 一次全批）",
    )
    parser.add_argument(
        "--refine-top", dest="refine_top",
        type=int,
        default=DEFAULT_REFINE_TOP,
        help=f"阶段二 mixed 精算的成交笔数 top N（默认 {DEFAULT_REFINE_TOP}；--no-refine 时忽略）",
    )
    parser.add_argument(
        "--no-refine",
        action="store_true",
        help="只跑阶段一（全池扫描），不做 mixed 精算",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="覆盖标的池（逗号分隔，如 600519.SH,000001.SZ；默认读配置 stock_pool.codes）",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="回测起始日期（YYYY-MM-DD，覆盖配置文件）",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="回测结束日期（YYYY-MM-DD，覆盖配置文件）",
    )
    parser.add_argument(
        "--force-full",
        action="store_true",
        help="忽略指纹增量，强制全量重算",
    )
    parser.add_argument(
        "--cache-dir", dest="cache_dir",
        type=str,
        default=DEFAULT_CACHE_DIR,
        help=f"指纹/结果缓存目录（默认 {DEFAULT_CACHE_DIR}）",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help=f"汇总结果输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="启用详细日志输出（DEBUG级别）",
    )
    return parser


def load_config(path: str) -> Dict[str, Any]:
    """读取并校验回测配置文件。

    Args:
        path: YAML 配置文件路径。

    Returns:
        解析后的配置字典。

    Raises:
        FileNotFoundError: 配置文件不存在时抛出。
        ValueError: 配置缺少必需段（backtest/stock_pool/strategy）时抛出。
    """
    cfg_path = Path(path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {cfg_path}")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"配置必须是 YAML 映射对象: {path}")
    for key in ("backtest", "stock_pool", "strategy"):
        if key not in cfg:
            raise ValueError(f"配置缺少必需段 [{key}]: {path}")
    return cfg


def resolve_symbols(cfg: Dict[str, Any], override: Optional[str]) -> List[str]:
    """确定回测标的池：CLI --symbols 优先，否则取配置 stock_pool.codes。

    Args:
        cfg: 配置字典。
        override: CLI 标的覆盖串（逗号分隔），None 表示不覆盖。

    Returns:
        去重后的标的代码列表。

    Raises:
        ValueError: 标的池为空时抛出。
    """
    if override:
        symbols = [s.strip() for s in override.split(",") if s.strip()]
    else:
        codes = cfg["stock_pool"].get("codes", [])
        symbols = [str(c) for c in codes if str(c).strip()]
    symbols = list(dict.fromkeys(symbols))  # 去重保序
    if not symbols:
        raise ValueError("标的池为空（--symbols 或 stock_pool.codes 均无标的）")
    return symbols


def build_commission(cfg: Dict[str, Any]) -> Any:
    """从 sim_broker 段构造 CommissionModel（费用模型，写实标准）。

    Args:
        cfg: 配置字典。

    Returns:
        CommissionModel 实例。
    """
    from myquant.models.backtest import CommissionModel

    sb = cfg.get("sim_broker", {})
    return CommissionModel(
        buy_rate=float(sb.get("commission_rate", 0.0002)),
        sell_rate=float(sb.get("commission_rate", 0.0002)),
        min_commission=float(sb.get("min_commission", 5.0)),
        stamp_tax=float(sb.get("stamp_tax_rate", 0.001)),
        transfer_fee=float(sb.get("transfer_fee_rate", 1.0e-05)),
    )


def build_slippage(cfg: Dict[str, Any]) -> Any:
    """从 sim_broker 段构造 SlippageModel（滑点模型）。

    Args:
        cfg: 配置字典。

    Returns:
        SlippageModel 实例。
    """
    from myquant.models.backtest import SlippageModel

    sb = cfg.get("sim_broker", {})
    mode = sb.get("slippage_mode", "ratio")
    value = float(sb.get("slippage_value", 0.0002))
    if mode == "ratio":
        return SlippageModel(type="fixed_bps", fixed_bps=value * 10000.0)
    return SlippageModel(type="fixed_bps", fixed_bps=2.0)


def load_market_data(
    cfg: Dict[str, Any],
    symbols: List[str],
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """加载真实行情数据（HistoricalDataProvider，processed 目录）。

    Args:
        cfg: 配置字典。
        symbols: 标的代码列表。
        start_date: 回测起始日期。
        end_date: 回测结束日期（防未来函数：所有标的统一 end_date）。

    Returns:
        {symbol: DataFrame} 行情字典（无数据标的不含在内）。

    Raises:
        RuntimeError: 全部标的无数据时抛出。
    """
    from myquant.backtest.data_loader import HistoricalDataProvider

    data_cfg = cfg.get("data", {})
    data_dir = data_cfg.get("data_dir", "./datasets/processed")
    if not Path(data_dir).is_absolute():
        data_dir = str(Path(find_project_root()) / data_dir)
    provider = HistoricalDataProvider(
        processed_dir=data_dir,
        default_adjust=data_cfg.get("adjust", "raw"),
        data_format=data_cfg.get("data_format", "parquet"),
    )
    data = provider.load_multiple(
        symbols,
        start_date=start_date or None,
        end_date=end_date or None,
        adjust=data_cfg.get("adjust", "raw"),
    )
    if not data:
        raise RuntimeError("全部标的数据加载失败（processed 目录为空或无有效行情）")
    return data


def summarize_per_symbol(
    result: Any,
) -> List[Dict[str, Any]]:
    """从批量回测结果提取每标的摘要行（按成交笔数降序）。

    Args:
        result: BatchBacktestResult 实例。

    Returns:
        摘要行列表：{symbol, status, trade_count, total_return, sharpe, max_drawdown, mode}。
    """
    rows: List[Dict[str, Any]] = []
    for symbol, r in result.per_symbol.items():
        metrics = r.get("metrics", {}) or {}
        rows.append({
            "symbol": symbol,
            "status": r.get("status", "?"),
            "trade_count": int(r.get("trade_count", 0) or 0),
            "total_return": float(metrics.get("total_return", 0.0) or 0.0),
            "sharpe_ratio": float(metrics.get("sharpe_ratio", 0.0) or 0.0),
            "max_drawdown": float(metrics.get("max_drawdown", 0.0) or 0.0),
            "mode": r.get("mode", ""),
        })
    rows.sort(key=lambda x: (-x["trade_count"], -x["total_return"]))
    return rows


def run_stage(
    strategy_name: str,
    params: Dict[str, Any],
    data: Dict[str, Any],
    cache_dir: Path,
    mode: str,
    cpu_workers: Optional[int],
    batch_size: Optional[int],
    start_date: str,
    end_date: str,
    initial_capital: float,
    benchmark: str,
    commission: Any,
    slippage: Any,
    include_detail: bool,
    force_full: bool,
) -> Tuple[Any, float]:
    """执行一个阶段的批量回测（BatchBacktester）。

    Args:
        strategy_name: 策略注册名。
        params: 策略构造参数。
        data: 行情字典 {symbol: DataFrame}。
        cache_dir: 指纹/结果缓存目录。
        mode: 引擎模式（vectorized/mixed/...）。
        cpu_workers: 进程池大小。
        batch_size: 每批标的数。
        start_date: 起始日期。
        end_date: 结束日期。
        initial_capital: 初始资金。
        benchmark: 基准指数代码。
        commission: CommissionModel 或 None。
        slippage: SlippageModel 或 None。
        include_detail: 是否保留成交/净值明细。
        force_full: 是否强制全量重算。

    Returns:
        (BatchBacktestResult, 耗时秒数)。
    """
    from myquant.backtest.batch_backtest import BatchBacktester

    cache_dir.mkdir(parents=True, exist_ok=True)
    backtester = BatchBacktester(
        strategy_name=strategy_name,
        params=params,
        data=data,
        fingerprint_path=str(cache_dir / "fingerprints.json"),
        results_path=str(cache_dir / "results.json"),
        mode=mode,
        cpu_workers=cpu_workers,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        frequency="1d",
        commission=commission,
        slippage=slippage,
        benchmark=benchmark,
        include_detail=include_detail,
        batch_size=batch_size,
    )
    t0 = time.perf_counter()
    result = backtester.run(force_full=force_full)
    elapsed = time.perf_counter() - t0
    return result, elapsed


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    """将字典落盘为 UTF-8 JSON（目录自动创建）。

    Args:
        path: 输出文件路径。
        payload: 待写数据。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)


def main() -> int:
    """主入口：解析参数 → 加载数据 → 阶段一扫描 → 阶段二精算 → 汇总输出。

    Returns:
        退出码（0=成功，1=配置错误，2=运行失败）。
    """
    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in sys.argv else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    project_root = find_project_root()
    root_str = str(project_root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    args = setup_argument_parser().parse_args()

    try:
        if args.mode not in VALID_MODES:
            raise ValueError(f"非法引擎模式: {args.mode!r}（可选 {VALID_MODES}）")
        cfg = load_config(args.config)
        bt = cfg["backtest"]
        start_date = args.start or str(bt.get("start_date", ""))
        end_date = args.end or str(bt.get("end_date", ""))
        initial_capital = float(bt.get("initial_capital", 1_000_000.0))
        benchmark = str(bt.get("benchmark", ""))
        strategy_name = str(cfg["strategy"].get("name", "kunpeng"))
        params = cfg["strategy"].get("params", {})
        if not isinstance(params, dict):
            raise ValueError("strategy.params 必须是映射对象")
        symbols = resolve_symbols(cfg, args.symbols)
        commission = build_commission(cfg)
        slippage = build_slippage(cfg)
    except (ValueError, FileNotFoundError, yaml.YAMLError) as exc:
        print(f"[错误] 配置/参数解析失败: {exc}")
        return 1

    cache_dir = Path(args.cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = project_root / cache_dir
    out_dir = Path(args.output or DEFAULT_OUTPUT_DIR)
    if not out_dir.is_absolute():
        out_dir = project_root / out_dir
    stamp = time.strftime(TIMESTAMP_FMT)

    print("=" * 72)
    print("  鲲鹏壹号策略全池批量回测（M2 方案A两段式）")
    print(f"  配置: {args.config} | 策略: {strategy_name} | 标的: {len(symbols)} 个")
    print(f"  区间: {start_date} ~ {end_date} | 阶段一模式: {args.mode}")
    print(f"  缓存: {cache_dir} | 阶段二 top: {args.refine_top if not args.no_refine else '关闭'}")
    print("=" * 72)

    # ── 1. 数据加载（真实行情） ──
    try:
        logger.info("加载真实行情数据（%d 个标的）...", len(symbols))
        t0 = time.perf_counter()
        data = load_market_data(cfg, symbols, start_date, end_date)
        logger.info("数据加载完成: %d 个标的有有效数据（耗时 %.1fs）", len(data), time.perf_counter() - t0)
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return 2

    # ── 2. 阶段一：vectorized 全池扫描 ──
    try:
        logger.info("阶段一启动: %s 模式全池扫描", args.mode)
        result1, elapsed1 = run_stage(
            strategy_name, params, data, cache_dir, args.mode,
            args.cpu_workers, args.batch_size, start_date, end_date,
            initial_capital, benchmark, commission, slippage,
            include_detail=False, force_full=args.force_full,
        )
    except Exception as exc:
        logger.error("阶段一回测失败: %s", exc, exc_info=True)
        print(f"[错误] 阶段一回测失败: {exc}")
        return 2

    rows1 = summarize_per_symbol(result1)
    ok1 = [r for r in rows1 if r["status"] == "completed"]
    signal1 = [r for r in ok1 if r["trade_count"] > 0]
    print(f"\n[阶段一完成] 标的 {result1.plan.total} | 成功 {len(ok1)} | "
          f"产生信号 {len(signal1)} | 耗时 {elapsed1:.1f}s")
    print(f"  引擎模式: {result1.plan.base_mode} | 批次: {result1.batch_count} | "
          f"内存水位: {result1.memory_level}")

    # ── 3. 阶段二：mixed 精算 top N ──
    stage2_payload: Dict[str, Any] = {"enabled": False, "reason": "no_refine"}
    result2: Any = None
    rows2: List[Dict[str, Any]] = []
    if not args.no_refine and signal1:
        top_n = max(1, args.refine_top)
        top_symbols = [r["symbol"] for r in signal1[:top_n]]
        logger.info("阶段二启动: mixed 精算 top %d 标的 %s", len(top_symbols), top_symbols)
        data2 = {s: data[s] for s in top_symbols if s in data}
        try:
            result2, elapsed2 = run_stage(
                strategy_name, params, data2, cache_dir / "refine", "mixed",
                args.cpu_workers, None, start_date, end_date,
                initial_capital, benchmark, commission, slippage,
                include_detail=True, force_full=True,
            )
            rows2 = summarize_per_symbol(result2)
            stage2_payload = {
                "enabled": True,
                "mode": "mixed",
                "elapsed_seconds": round(elapsed2, 2),
                "symbols": rows2,
            }
            print(f"\n[阶段二完成] mixed 精算 {len(rows2)} 个标的 | 耗时 {elapsed2:.1f}s")
            for r in rows2:
                print(f"  {r['symbol']}: 成交 {r['trade_count']} 笔 | "
                      f"总收益 {r['total_return']:.2%} | 夏普 {r['sharpe_ratio']:.2f} | "
                      f"最大回撤 {r['max_drawdown']:.2%}")
        except Exception as exc:
            logger.error("阶段二精算失败: %s", exc, exc_info=True)
            print(f"[错误] 阶段二精算失败（阶段一结果已保留）: {exc}")
            stage2_payload = {"enabled": False, "reason": f"error: {exc}"}
    elif not signal1:
        print("\n[提示] 阶段一无标的产生信号，跳过阶段二")
        stage2_payload = {"enabled": False, "reason": "no_signal"}

    # ── 4. 报告文档生成（CSV / HTML / PDF） ──
    try:
        import csv as _csv

        from myquant.backtest.batch_report import (
            BatchReportMeta,
            BatchSummaryReportGenerator,
        )

        report_dir = out_dir / f"kunpeng_batch_{stamp}"
        report_dir.mkdir(parents=True, exist_ok=True)

        def _write_rows_csv(rows: List[Dict[str, Any]], path: Path) -> None:
            """将汇总行写为 utf-8-sig CSV（中文 Excel 直接打开）。"""
            if not rows:
                return
            headers = ["symbol", "status", "trade_count", "total_return",
                       "sharpe_ratio", "max_drawdown", "mode"]
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = _csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                for r in rows:
                    writer.writerow(r)

        # 4.1 汇总 CSV：阶段一全量 + 阶段二 top N
        _write_rows_csv(rows1, report_dir / "阶段一_全池扫描汇总.csv")
        if rows2:
            _write_rows_csv(rows2, report_dir / "阶段二_mixed精算汇总.csv")

        # 4.2 批量汇总 HTML + PDF（阶段一全量：统计卡 + 分布图 + Top N 榜）
        meta1 = BatchReportMeta(
            strategy_name=strategy_name, start_date=start_date, end_date=end_date,
            initial_capital=initial_capital, base_mode=result1.plan.base_mode,
            force_full=args.force_full, total=result1.plan.total,
            recomputed=result1.recomputed_count, reused=result1.reused_count,
        )
        gen1 = BatchSummaryReportGenerator(f"鲲鹏壹号策略全池扫描汇总（{result1.plan.base_mode}）")
        reports1 = gen1.generate_full_report(rows1, meta1, report_dir, ts=f"{stamp}_stage1")

        # 4.3 阶段二 top N 汇总 HTML + PDF
        reports2: Dict[str, str] = {}
        if stage2_payload.get("enabled") and rows2:
            meta2 = BatchReportMeta(
                strategy_name=strategy_name, start_date=start_date, end_date=end_date,
                initial_capital=initial_capital, base_mode="mixed",
                force_full=True, total=len(rows2), recomputed=len(rows2), reused=0,
            )
            gen2 = BatchSummaryReportGenerator("鲲鹏壹号策略 top N mixed 精算汇总")
            reports2 = gen2.generate_full_report(rows2, meta2, report_dir, ts=f"{stamp}_stage2")

        # 4.4 阶段二每标的成交/净值明细 CSV
        detail_files: List[str] = []
        if result2 is not None:
            for symbol, r in result2.per_symbol.items():
                detail = r.get("detail")
                if not detail:
                    continue
                sub = report_dir / "stage2_detail" / symbol
                sub.mkdir(parents=True, exist_ok=True)
                for key, fname in (("trades", "trades.csv"), ("equity", "equity.csv")):
                    items = detail.get(key)
                    if items:
                        with open(sub / fname, "w", encoding="utf-8-sig", newline="") as f:
                            writer = _csv.DictWriter(f, fieldnames=list(items[0].keys()))
                            writer.writeheader()
                            writer.writerows(items)
                        detail_files.append(str(sub / fname))
        logger.info("报告文档生成完成: html=%s pdf=%s", reports1, reports2)
    except Exception as exc:
        logger.error("报告文档生成失败（JSON 汇总仍会落盘）: %s", exc, exc_info=True)
        print(f"[警告] 报告文档生成失败: {exc}")
        reports1, reports2, detail_files = {}, {}, []

    # ── 5. 汇总 JSON 落盘 ──
    try:
        report: Dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "config_file": args.config,
            "strategy": strategy_name,
            "params": params,
            "symbols_total": len(symbols),
            "symbols_loaded": len(data),
            "range": {"start": start_date, "end": end_date},
            "stage1": {
                "mode": result1.plan.base_mode,
                "elapsed_seconds": round(elapsed1, 2),
                "total": result1.plan.total,
                "recomputed": result1.recomputed_count,
                "reused": result1.reused_count,
                "batch_count": result1.batch_count,
                "memory_level": result1.memory_level,
                "ok_count": len(ok1),
                "signal_count": len(signal1),
                "top": rows1[:50],
            },
            "stage2": stage2_payload,
            "reports": {
                "stage1_html": reports1.get("html", ""),
                "stage1_pdf": reports1.get("pdf", ""),
                "stage2_html": reports2.get("html", ""),
                "stage2_pdf": reports2.get("pdf", ""),
                "stage1_csv": str(report_dir / "阶段一_全池扫描汇总.csv"),
                "stage2_csv": str(report_dir / "阶段二_mixed精算汇总.csv") if rows2 else "",
                "stage2_detail": detail_files,
            },
        }
        out_dir = report_dir
        write_json(out_dir / "summary.json", report)
        write_json(out_dir / "stage1_all.csv.json", rows1)
        print(f"\n[已保存] 汇总报告: {out_dir / 'summary.json'}")
        print(f"[已保存] 阶段一全量: {out_dir / 'stage1_all.csv.json'}")
        for k, v in (("阶段一 HTML", reports1.get("html")), ("阶段一 PDF", reports1.get("pdf")),
                     ("阶段二 HTML", reports2.get("html")), ("阶段二 PDF", reports2.get("pdf")),
                     ("阶段一 CSV", str(report_dir / "阶段一_全池扫描汇总.csv")),
                     ("阶段二 CSV", str(report_dir / "阶段二_mixed精算汇总.csv") if rows2 else None)):
            if v:
                print(f"[已保存] {k}: {v}")
        if detail_files:
            print(f"[已保存] 阶段二明细 CSV: {len(detail_files)} 个文件（stage2_detail/ 下）")
    except Exception as exc:
        logger.error("汇总落盘失败: %s", exc, exc_info=True)
        print(f"[错误] 汇总落盘失败: {exc}")
        return 2

    print("=" * 72)
    print("回测完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
