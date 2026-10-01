# -*- coding: utf-8 -*-
# 文件名：myquant/gui/batch_backtest_tab.py
"""
批量回测标签页（Tab6）
====================
被 myquant/gui/backtest_config_dialog.py 作为第 6 个标签页挂载，提供：
  - 标的来源三选：全市场扫描（processed）/ 预筛选池（stock_pool.yaml）/ 手输列表
  - 行情数据源二选：processed 真实行情（默认）/ 演示行情（调试）
  - 策略多选：QListWidget 多选，列表只读 batch_backtest.STRATEGY_REGISTRY
    （已注册 ma_cross/rsrs/grid/momentum/mean_reversion/multi_factor 六策略）
  - 参数覆盖：JSON 编辑框按策略覆盖默认参数（留空=各策略默认参数）
  - 引擎：模式 / 进程数 / 批次大小 / 内存降级阈值 / 强制全量
  - 执行：QThread 后台直调 HistoricalDataProvider + BatchBacktester
    （每选中策略依次批量回测，数据仅加载一次），信号桥实时刷新批次进度
  - 结果：各策略 summary 文本 + results.json 自动落盘 + 每策略汇总 CSV

设计原则（与项目纪律一致）：
  - 本文件只做 GUI 装配与参数收集，业务逻辑全部在 myquant 内
  - run_批量回测.py 保持零改动（薄入口纪律）
  - 路径不依赖 cwd：processed_dir / 缓存目录一律 project_root 绝对拼接
  - 防未来函数：真实行情统一走 HistoricalDataProvider，严格按 start/end 过滤
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from PySide6.QtCore import QDate, QObject, QSize, QThread, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from myquant.backtest.batch_backtest import STRATEGY_REGISTRY, BatchBacktester
from myquant.backtest.data_loader import HistoricalDataProvider
from myquant.models.backtest import CommissionModel, SlippageModel
from myquant.backtest.param_optimizer import make_demo_data
from myquant.utils.csv_utils import CSV_HEADER_MAPS, export_csv_cn
from myquant.utils.memory_watcher import MemoryWatcher

logger = logging.getLogger("myquant.gui.batch_backtest_tab")

# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_START_DATE: str = "2023-01-02"
DEFAULT_END_DATE: str = "2024-01-31"
DEFAULT_INITIAL_CAPITAL: float = 1_000_000.0
DEFAULT_MEM_WARNING_PCT: float = 70.0
DEFAULT_MEM_CRITICAL_PCT: float = 85.0
DEMO_BARS: int = 300
DEMO_SEED: int = 7
DEMO_MAX_SYMBOLS: int = 20  # 演示行情单次上限（调试用）
DEFAULT_BATCH_SIZE: int = 200
DEFAULT_STRATEGIES: Tuple[str, ...] = ("ma_cross",)  # 默认选中策略
RESULT_DIR_NAME: str = "batch_backtest"
RESULT_MAX_BLOCKS: int = 5000  # 结果区最大保留文本块数
RESULT_MIN_HEIGHT: int = 48  # 结果区最小高度（方案②：压缩结果区，为新增参数行腾空间）
RESULT_MAX_HEIGHT: int = 225  # 结果区可视高度上限（实测实际占高 450px，压缩至其 1/2，防底部大空白）
PARAMS_SCROLL_MAX_HEIGHT: int = 549  # 参数区可视高度上限（页高预算 810−操作行36−结果区225，内容基本全显、消除按钮行与结果区间空隙；窗口整体不加高）
TAB4_SIZEHINT_HEIGHT: int = 430  # Tab4 页 sizeHint 高度上限（窗口高度由其它页决定，整体不加高）
STRATEGY_CHECK_COLS: int = 3  # 策略勾选网格列数
PARAMS_EDIT_HEIGHT: int = 60  # 参数 JSON 编辑框高度
STOCK_POOL_YAML: str = "config/stock_pool.yaml"
PROCESSED_DIR_REL: Tuple[str, ...] = ("datasets", "processed")
ENGINE_MODES: Tuple[str, ...] = ("auto", "vectorized", "mixed", "event")
# 交易成本/绩效口径默认值（对齐 Tab3 与引擎内置默认；百分比为界面数值）
DEFAULT_COMMISSION_PCT: float = 0.02  # 佣金率（%，买卖同值）
DEFAULT_MIN_COMMISSION: float = 5.0  # 最低佣金（元）
DEFAULT_STAMP_TAX_PCT: float = 0.1  # 印花税（%，仅卖出）
DEFAULT_TRANSFER_FEE_PCT: float = 0.001  # 过户费（%，买卖双向）
DEFAULT_SLIPPAGE_PCT: float = 0.02  # 滑点（%，固定 bps 模型）
DEFAULT_BENCHMARK: str = "000300.SH"  # 基准指数代码
DEFAULT_RISK_FREE_PCT: float = 2.0  # 无风险利率（%）
ADJUST_MODES: Tuple[str, ...] = ("raw",)  # 复权方式（值；库内为不复权原始层）
ADJUST_LABELS: Tuple[str, ...] = ("不复权原始层 (raw)",)
LOG_LEVELS: Tuple[str, ...] = ("INFO", "DEBUG", "WARNING", "ERROR")
BENCHMARK_PRESETS: Tuple[str, ...] = ("000300.SH", "000905.SH", "000001.SH", "399006.SZ")
OUTPUT_DIR_PLACEHOLDER: str = "留空=默认 results/batch_backtest"
# 参数 JSON 编辑框占位说明（多策略时按策略名覆盖；留空=全部默认参数）
PARAMS_PLACEHOLDER: str = (
    '参数覆盖（JSON，可选）：\n'
    '{"ma_cross": {"fast_period": 5, "slow_period": 20}, "rsrs": {"rsrs_period": 18}}'
)


# ── 运行参数数据模型 ─────────────────────────────────────────────────
@dataclass(frozen=True)
class BatchRunOptions:
    """批量回测运行参数（GUI 收集 → Worker 装配）。

    Attributes:
        project_root: 项目根目录（用于解析绝对路径，不依赖 cwd）。
        symbols: 本次回测标的列表（GUI 线程已解析完成）。
        strategies: 选中的策略注册名列表（须均在 STRATEGY_REGISTRY 中）。
        params: 策略参数覆盖 {策略名: 参数字典}；缺失的策略用默认参数。
        start_date: 回测起始日期 YYYY-MM-DD。
        end_date: 回测结束日期 YYYY-MM-DD。
        initial_capital: 初始资金。
        mode: 引擎模式（auto/vectorized/mixed/event）。
        cpu_workers: 计算进程池大小；None 使用默认。
        batch_size: 每批标的数；None 表示一次全批。
        force_full: True 时忽略指纹差异强制全量重算。
        use_demo: True 时使用演示行情（仅调试）。
        mem_warning_pct: 内存告警阈值（系统内存百分比）。
        mem_critical_pct: 内存严重阈值（系统内存百分比）。
        cache_dir: 批量回测缓存根目录（按策略分子目录）。
        commission_rate: 佣金率（小数，买卖同值）。
        min_commission: 最低佣金（元）。
        stamp_tax: 印花税率（小数，仅卖出）。
        transfer_fee: 过户费率（小数，买卖双向）。
        slippage_pct: 滑点（界面百分比数值，Worker 内转 bps）。
        benchmark: 基准指数代码（绩效口径）。
        risk_free_rate: 无风险利率（小数）。
        adjust: 复权方式（raw：库内不复权原始层）。
        log_level: 日志级别（INFO/DEBUG/WARNING/ERROR）。
        incremental: True 增量回测（数据/参数未变化时复用缓存）。
        output_dir: 明细输出目录；空字符串表示默认 results/batch_backtest。
        save_trades: 保存交易明细 CSV。
        save_positions: 保存持仓市值序列 CSV。
        save_equity: 保存净值曲线 CSV。
    """

    project_root: Path
    symbols: List[str]
    strategies: List[str]
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    start_date: str = DEFAULT_START_DATE
    end_date: str = DEFAULT_END_DATE
    initial_capital: float = DEFAULT_INITIAL_CAPITAL
    mode: str = "auto"
    cpu_workers: Optional[int] = None
    batch_size: Optional[int] = None
    force_full: bool = False
    use_demo: bool = False
    mem_warning_pct: float = DEFAULT_MEM_WARNING_PCT
    mem_critical_pct: float = DEFAULT_MEM_CRITICAL_PCT
    cache_dir: Optional[Path] = None
    commission_rate: float = 0.0002
    min_commission: float = 5.0
    stamp_tax: float = 0.001
    transfer_fee: float = 0.00001
    slippage_pct: float = 0.02
    benchmark: str = DEFAULT_BENCHMARK
    risk_free_rate: float = 0.02
    adjust: str = "raw"
    log_level: str = "INFO"
    incremental: bool = True
    output_dir: str = ""
    save_trades: bool = False
    save_positions: bool = False
    save_equity: bool = False


# ── 后台批量回测 Worker（信号桥，不阻塞 GUI） ───────────────────────
class _BatchWorker(QObject):
    """后台批量回测执行器：加载数据 → 逐策略 BatchBacktester → 汇总落盘。

    通过 Signal 与 GUI 线程通信：
      log_line(str)           : 逐行日志。
      batch_done(str,int,int) : 批次完成回调（策略名, 当前批次号, 总批次数）。
      finished(bool,str)      : 全部结束（成功标志, 汇总文本/错误信息）。
    """

    log_line = Signal(str)
    batch_done = Signal(str, int, int)
    finished = Signal(bool, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """初始化后台执行器。"""
        super().__init__(parent)

    def _on_batch_done(self, strategy: str, batch_idx: int, total_batches: int) -> None:
        """批次回调：转发（含策略名）批次进度到 GUI 线程。

        Args:
            strategy: 当前策略名。
            batch_idx: 当前完成批次号（从 1 开始）。
            total_batches: 总批次数。
        """
        self.batch_done.emit(strategy, batch_idx, total_batches)

    @staticmethod
    def _save_summary_csv(cache_dir: Path, result: Any, ts: str) -> Tuple[Path, List[Dict[str, Any]]]:
        """把本次批量结果 per_symbol 汇总为 CSV。

        Args:
            cache_dir: 输出目录。
            result: 批量回测结果（含 per_symbol / plan）。
            ts: 时间戳字符串（文件名用）。

        Returns:
            (CSV 路径, 汇总行列表)：行数据供批量汇总报告（HTML/PDF）复用。
        """
        rows: List[Dict[str, Any]] = []
        for symbol in sorted(result.per_symbol.keys()):
            entry = result.per_symbol[symbol]
            metrics = entry.get("metrics") or {}
            rows.append(
                {
                    "symbol": symbol,
                    "status": entry.get("status", "error"),
                    "total_return": metrics.get("total_return"),
                    "sharpe_ratio": metrics.get("sharpe_ratio"),
                    "max_drawdown": metrics.get("max_drawdown"),
                    "trade_count": entry.get("trade_count"),
                    "equity_length": entry.get("equity_length"),
                    "mode": entry.get("mode", "-"),
                    "source": "重算" if symbol in result.plan.changed else "复用",
                    "error": entry.get("error") or "",
                }
            )
        csv_path = cache_dir / f"批量回测汇总_{ts}.csv"
        export_csv_cn(pd.DataFrame(rows), csv_path, header_map=CSV_HEADER_MAPS["batch_summary"])
        return csv_path, rows

    @staticmethod
    def _save_aux_outputs(
        cache_dir: Path,
        result: Any,
        ts: str,
        opts: BatchRunOptions,
    ) -> List[Path]:
        """按保存开关导出成交/净值/持仓市值明细（仅本次重算标的）。

        复用标的无明细（结果缓存只存摘要）；未勾选或无可导出数据时返回空列表。

        Args:
            cache_dir: 输出目录（策略子目录）。
            result: 批量回测结果（per_symbol 含 detail）。
            ts: 时间戳字符串（文件名用）。
            opts: 运行参数（含保存开关）。

        Returns:
            生成的明细文件路径列表。
        """
        trade_rows: List[Dict[str, Any]] = []
        equity_rows: List[Dict[str, Any]] = []
        position_rows: List[Dict[str, Any]] = []
        for symbol in sorted(result.per_symbol.keys()):
            detail = result.per_symbol[symbol].get("detail") or {}
            trades = detail.get("trades") or []
            equity = detail.get("equity") or []
            if opts.save_trades and trades:
                for trade in trades:
                    row = dict(trade)
                    row["symbol"] = symbol
                    trade_rows.append(row)
            if opts.save_equity and equity:
                for point in equity:
                    row = dict(point)
                    row["symbol"] = symbol
                    equity_rows.append(row)
            if opts.save_positions and equity:
                for point in equity:
                    position_rows.append(
                        {
                            "symbol": symbol,
                            "date": point.get("date", ""),
                            "position_value": point.get("position_value", 0.0),
                        }
                    )
        saved: List[Path] = []
        if trade_rows:
            path = cache_dir / f"批量成交明细_{ts}.csv"
            export_csv_cn(
                pd.DataFrame(trade_rows), path,
                header_map=CSV_HEADER_MAPS["trades"],
            )
            saved.append(path)
        if equity_rows:
            path = cache_dir / f"批量净值明细_{ts}.csv"
            export_csv_cn(
                pd.DataFrame(equity_rows), path,
                header_map=CSV_HEADER_MAPS["equity"],
            )
            saved.append(path)
        if position_rows:
            path = cache_dir / f"批量持仓明细_{ts}.csv"
            export_csv_cn(
                pd.DataFrame(position_rows), path,
                header_map=CSV_HEADER_MAPS["positions"],
            )
            saved.append(path)
        return saved

    @staticmethod
    def _save_batch_report(
        cache_dir: Path,
        strategy: str,
        result: Any,
        summary_rows: List[Dict[str, Any]],
        opts: BatchRunOptions,
        ts: str,
    ) -> List[Path]:
        """生成批量汇总报告（HTML + PDF，与汇总 CSV 同名前缀一套）。

        报告生成属附加产物：任一步失败仅记日志并返回空列表，
        不影响批量回测主流程与已落盘的 CSV。

        Args:
            cache_dir: 输出目录（策略子目录）。
            strategy: 当前策略注册名。
            result: 批量回测结果（取 plan 元信息）。
            summary_rows: _save_summary_csv 返回的汇总行列表。
            opts: 运行参数（期间/初始资金等元信息）。
            ts: 时间戳字符串（文件名用）。

        Returns:
            生成的报告文件路径列表（失败时为空列表）。
        """
        try:
            from myquant.backtest.batch_report import (
                BatchReportMeta,
                BatchSummaryReportGenerator,
            )
            meta = BatchReportMeta(
                strategy_name=strategy,
                start_date=opts.start_date,
                end_date=opts.end_date,
                initial_capital=opts.initial_capital,
                base_mode=result.plan.base_mode,
                force_full=result.plan.force_full,
                total=result.plan.total,
                recomputed=result.recomputed_count,
                reused=result.reused_count,
            )
            paths = BatchSummaryReportGenerator().generate_full_report(
                summary_rows, meta, cache_dir, ts,
            )
            return [Path(p) for p in paths.values()]
        except Exception as exc:  # noqa: BLE001 - 报告是附加产物，失败不阻断回测
            logger.warning("批量汇总报告生成失败（仅保留 CSV）: %s", exc)
            return []

    def run(self, opts: BatchRunOptions) -> None:
        """执行批量回测主流程（在 QThread 线程中调用，阻塞直至完成）。

        Args:
            opts: 批量回测运行参数。
        """
        try:
            logging.getLogger("myquant").setLevel(
                getattr(logging, opts.log_level, logging.INFO)
            )
            # ── 1. 数据准备（一次加载，多策略共用） ──
            self.log_line.emit("[1/4] 数据准备")
            self.log_line.emit(
                f"  日志级别: {opts.log_level} | 复权: {opts.adjust} "
                f"| 增量回测: {'开' if opts.incremental else '关'}"
            )
            if opts.use_demo:
                self.log_line.emit(f"  使用演示行情（{DEMO_BARS} 根/标的，种子 {DEMO_SEED}）")
                demo_symbols = opts.symbols[:DEMO_MAX_SYMBOLS]
                if len(opts.symbols) > DEMO_MAX_SYMBOLS:
                    self.log_line.emit(
                        f"  演示模式标的超过 {DEMO_MAX_SYMBOLS} 只，已截取前 {DEMO_MAX_SYMBOLS} 只"
                    )
                data = make_demo_data(symbols=demo_symbols, n_bars=DEMO_BARS, seed=DEMO_SEED)
                data = {s: df for s, df in data.items() if s in set(demo_symbols)}
            else:
                processed_dir = opts.project_root.joinpath(*PROCESSED_DIR_REL)
                provider = HistoricalDataProvider(processed_dir=processed_dir)
                self.log_line.emit(
                    f"  数据源: {processed_dir}（可用 {len(provider.get_available_symbols())} 只）"
                )
                data = provider.load_multiple(
                    opts.symbols, opts.start_date, opts.end_date,
                    adjust=opts.adjust,
                )

            if not data:
                self.finished.emit(False, "所选标的在回测日期范围内均无有效数据，已终止。")
                return
            self.log_line.emit(f"  加载成功: {len(data)} 只 | 日期 {opts.start_date} ~ {opts.end_date}")

            # ── 2. 内存监控器 ──
            self.log_line.emit("[2/4] 内存监控器")
            watcher = MemoryWatcher(
                warning_pct=opts.mem_warning_pct, critical_pct=opts.mem_critical_pct
            )

            # ── 3. 逐策略批量回测 ──
            self.log_line.emit("[3/4] 批量回测")
            cache_root = opts.cache_dir or (opts.project_root / "results" / RESULT_DIR_NAME)
            cache_root.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            summaries: List[str] = []
            for strategy in opts.strategies:
                self.log_line.emit(f"  ── 策略: {strategy} ──")
                strategy_params = opts.params.get(strategy) or {}
                strategy_cache = cache_root / strategy
                strategy_cache.mkdir(parents=True, exist_ok=True)
                commission = CommissionModel(
                    buy_rate=opts.commission_rate,
                    sell_rate=opts.commission_rate,
                    min_commission=opts.min_commission,
                    stamp_tax=opts.stamp_tax,
                    transfer_fee=opts.transfer_fee,
                )
                slippage = SlippageModel(
                    type="fixed_bps", fixed_bps=opts.slippage_pct * 100.0
                )
                include_detail = (
                    opts.save_trades or opts.save_positions or opts.save_equity
                )
                backtester = BatchBacktester(
                    strategy_name=strategy,
                    params=strategy_params,
                    data=data,
                    fingerprint_path=str(strategy_cache / "fingerprints.json"),
                    results_path=str(strategy_cache / "results.json"),
                    mode=opts.mode,
                    cpu_workers=opts.cpu_workers,
                    start_date=opts.start_date,
                    end_date=opts.end_date,
                    initial_capital=opts.initial_capital,
                    batch_size=opts.batch_size,
                    commission=commission,
                    slippage=slippage,
                    benchmark=opts.benchmark,
                    risk_free_rate=opts.risk_free_rate,
                    include_detail=include_detail,
                    memory_watcher=watcher,
                    on_batch_done=partial(self._on_batch_done, strategy),
                )
                result = backtester.run(
                    force_full=opts.force_full or not opts.incremental
                )
                csv_path, summary_rows = self._save_summary_csv(strategy_cache, result, ts)
                aux_files = self._save_aux_outputs(
                    strategy_cache, result, ts, opts
                )
                report_files = self._save_batch_report(
                    strategy_cache, strategy, result, summary_rows, opts, ts
                )
                aux_text = ""
                if aux_files:
                    aux_text = "\n  明细导出: " + ", ".join(
                        p.name for p in aux_files
                    )
                report_text = ""
                if report_files:
                    report_text = "\n  报告文档: " + ", ".join(
                        p.name for p in report_files
                    )
                summaries.append(
                    f"[策略 {strategy}] {result.plan.summary()}\n{result.summary()}\n"
                    f"  结果缓存: {strategy_cache / 'results.json'}\n"
                    f"  汇总CSV: {csv_path}{aux_text}{report_text}"
                )

            # ── 4. 汇总输出 ──
            self.log_line.emit("[4/4] 汇总输出")
            self.finished.emit(True, "\n\n".join(summaries))
        except Exception as exc:  # noqa: BLE001 - 后台线程必须隔离所有异常
            logger.exception("批量回测后台任务失败")
            self.finished.emit(False, f"{type(exc).__name__}: {exc}")


# ── Tab6 页面 ───────────────────────────────────────────────────────
class BatchBacktestTab(QWidget):
    """批量回测标签页（Tab6）：参数收集 + 后台执行 + 结果展示。

    Args:
        project_root: 项目根目录。
        default_start: 默认回测起始日期（来自主对话框 Tab1）。
        default_end: 默认回测结束日期（来自主对话框 Tab1）。
        parent: 父控件。
    """

    def __init__(
        self,
        project_root: Path,
        default_start: str = DEFAULT_START_DATE,
        default_end: str = DEFAULT_END_DATE,
        parent: Optional[QWidget] = None,
    ) -> None:
        """初始化批量回测标签页。"""
        if not isinstance(project_root, Path) or not project_root.is_dir():
            raise ValueError(f"project_root 必须为存在的目录，收到: {project_root!r}")
        super().__init__(parent)
        self._project_root = project_root
        self._default_start = default_start
        self._default_end = default_end
        self._thread: Optional[QThread] = None
        self._worker: Optional[_BatchWorker] = None
        self._running: bool = False
        self._build_ui()

    def sizeHint(self) -> QSize:
        """覆写页 sizeHint：限制 Tab4 页高，使窗口高度由其它标签页决定。

        参数区超高部分由 QScrollArea 内部滚动承载（方案②，窗口整体不加高）。
        """
        base = super().sizeHint()
        return QSize(base.width(), min(base.height(), TAB4_SIZEHINT_HEIGHT))

    # ── UI 构建（三行并排 + 操作/结果，紧凑布局） ──

    def _build_ui(self) -> None:
        """构建 Tab6 全部控件（方案②：参数区 QScrollArea 内滚动 + 结果区外置压缩）。

        参数区超高时内部滚动（可视高度封顶），结果区固定在页面底部，
        使 Tab4 页高不超过其它标签页，窗口整体高度不被撑高。
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 参数区（QScrollArea，内滚动，窗口不加高） ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMaximumHeight(PARAMS_SCROLL_MAX_HEIGHT)
        container = QWidget()
        inner = QVBoxLayout(container)
        inner.setSpacing(4)

        # ── 第一行：标的来源 + 策略多选 左右并排 ──
        row_top = QHBoxLayout()
        row_top.addWidget(self._build_source_box(), 1)
        row_top.addWidget(self._build_strategy_box(), 1)
        inner.addLayout(row_top)

        # ── 第二行：参数覆盖 + 回测范围 左右并排 ──
        row_mid = QHBoxLayout()
        row_mid.addWidget(self._build_params_box(), 1)
        row_mid.addWidget(self._build_range_box(), 1)
        inner.addLayout(row_mid)

        # ── 第三行：引擎参数 + 行情数据源 左右并排 ──
        row_low = QHBoxLayout()
        row_low.addWidget(self._build_engine_box(), 1)
        row_low.addWidget(self._build_data_box(), 1)
        inner.addLayout(row_low)

        # ── 第四行：交易成本 + 绩效口径 左右并排 ──
        row_cost = QHBoxLayout()
        row_cost.addWidget(self._build_cost_box(), 2)
        row_cost.addWidget(self._build_perf_box(), 1)
        inner.addLayout(row_cost)

        # ── 第五行：输出设置 ──
        inner.addWidget(self._build_output_box())

        scroll.setWidget(container)
        root.addWidget(scroll, 1)

        # ── 操作区 ──
        row_ops = QHBoxLayout()
        self._btn_start = QPushButton("开始批量回测")
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_export = QPushButton("导出汇总 CSV")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._on_export_clicked)
        self._label_progress = QLabel("就绪")
        row_ops.addWidget(self._btn_start)
        row_ops.addWidget(self._btn_export)
        row_ops.addStretch(1)
        row_ops.addWidget(self._label_progress)
        root.addLayout(row_ops)

        # ── 结果区（外置固定，压缩高度） ──
        self._text_result = QPlainTextEdit()
        self._text_result.setReadOnly(True)
        self._text_result.setMinimumHeight(RESULT_MIN_HEIGHT)
        self._text_result.setMaximumHeight(RESULT_MAX_HEIGHT)
        self._text_result.setMaximumBlockCount(RESULT_MAX_BLOCKS)
        self._text_result.setPlaceholderText("批量回测进度与结果将在此显示……")
        root.addWidget(self._text_result, 1)

    def _build_source_box(self) -> QGroupBox:
        """构建「标的来源」分组框。"""
        box = QGroupBox("标的来源")
        form = QFormLayout(box)
        self._radio_all = QRadioButton("全市场扫描（datasets/processed）")
        self._radio_all.setChecked(True)
        self._radio_preset = QRadioButton("预筛选池（stock_pool.yaml）")
        self._radio_manual = QRadioButton("手输列表")
        self._radio_manual.toggled.connect(self._on_manual_toggled)
        group = QButtonGroup(self)
        group.addButton(self._radio_all)
        group.addButton(self._radio_preset)
        group.addButton(self._radio_manual)
        self._edit_manual = QLineEdit()
        self._edit_manual.setPlaceholderText("逗号分隔，如 600000.SH,000001.SZ")
        self._edit_manual.setEnabled(False)
        inner = QVBoxLayout()
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(4)
        inner.addWidget(self._radio_all)
        inner.addWidget(self._radio_preset)
        inner.addWidget(self._radio_manual)
        inner.addWidget(self._edit_manual)
        form.addRow(inner)
        return box

    def _build_strategy_box(self) -> QGroupBox:
        """构建「策略多选」分组框（QCheckBox 网格，只读 STRATEGY_REGISTRY）。"""
        box = QGroupBox("策略（可多选）")
        form = QVBoxLayout(box)
        registered = sorted(STRATEGY_REGISTRY.keys())
        if not registered:
            raise RuntimeError("batch_backtest.STRATEGY_REGISTRY 为空，批量回测不可用")
        grid = QGridLayout()
        grid.setSpacing(4)
        self._strategy_checks: Dict[str, QCheckBox] = {}
        for idx, name in enumerate(registered):
            check = QCheckBox(name)
            check.setChecked(name in DEFAULT_STRATEGIES)
            self._strategy_checks[name] = check
            grid.addWidget(check, idx // STRATEGY_CHECK_COLS, idx % STRATEGY_CHECK_COLS)
        form.addLayout(grid)
        note = QLabel("勾选即参与批量回测")
        note.setStyleSheet("color: #64748B; font-size: 11px;")
        form.addWidget(note)
        return box

    def _build_params_box(self) -> QGroupBox:
        """构建「参数覆盖」分组框（JSON 编辑框，留空=默认参数）。"""
        box = QGroupBox("参数覆盖（JSON，可选）")
        form = QVBoxLayout(box)
        self._edit_params = QPlainTextEdit()
        self._edit_params.setPlaceholderText(PARAMS_PLACEHOLDER)
        self._edit_params.setFixedHeight(PARAMS_EDIT_HEIGHT)
        form.addWidget(self._edit_params)
        return box

    def _build_range_box(self) -> QGroupBox:
        """构建「回测范围」分组框。"""
        box = QGroupBox("回测范围")
        form = QFormLayout(box)
        self._date_start = QDateEdit()
        self._date_start.setCalendarPopup(True)
        self._date_start.setDisplayFormat("yyyy-MM-dd")
        self._date_start.setDate(QDate.fromString(self._default_start, "yyyy-MM-dd"))
        self._date_end = QDateEdit()
        self._date_end.setCalendarPopup(True)
        self._date_end.setDisplayFormat("yyyy-MM-dd")
        self._date_end.setDate(QDate.fromString(self._default_end, "yyyy-MM-dd"))
        self._spin_capital = QSpinBox()
        self._spin_capital.setRange(10_000, 1_000_000_000)
        self._spin_capital.setSingleStep(100_000)
        self._spin_capital.setValue(int(DEFAULT_INITIAL_CAPITAL))
        row_dates = QHBoxLayout()
        row_dates.addWidget(QLabel("起"))
        row_dates.addWidget(self._date_start, 1)
        row_dates.addWidget(QLabel("止"))
        row_dates.addWidget(self._date_end, 1)
        form.addRow("回测区间", row_dates)
        form.addRow("初始资金", self._spin_capital)
        return box

    def _build_engine_box(self) -> QGroupBox:
        """构建「引擎与性能」分组框（紧凑两列布局）。"""
        box = QGroupBox("引擎与性能")
        form = QFormLayout(box)
        self._combo_mode = QComboBox()
        self._combo_mode.addItems(list(ENGINE_MODES))
        self._combo_mode.setCurrentText("auto")
        self._combo_mode.setToolTip("auto=按策略能力自动选择（推荐）")
        self._spin_cpu = QSpinBox()
        self._spin_cpu.setRange(1, 64)
        self._spin_cpu.setSpecialValueText("自动")
        self._spin_cpu.setValue(1)
        self._spin_batch = QSpinBox()
        self._spin_batch.setRange(0, 5000)
        self._spin_batch.setValue(DEFAULT_BATCH_SIZE)
        self._spin_batch.setSpecialValueText("全部一批")
        self._spin_batch.setToolTip("每批标的数（0 值显示“全部一批”）")
        self._spin_mem_warning = QSpinBox()
        self._spin_mem_warning.setRange(10, 100)
        self._spin_mem_warning.setValue(int(DEFAULT_MEM_WARNING_PCT))
        self._spin_mem_warning.setSuffix(" %")
        self._spin_mem_critical = QSpinBox()
        self._spin_mem_critical.setRange(10, 100)
        self._spin_mem_critical.setValue(int(DEFAULT_MEM_CRITICAL_PCT))
        self._spin_mem_critical.setSuffix(" %")
        self._chk_force_full = QCheckBox("强制全量")
        self._chk_force_full.setToolTip("本次强制全量重算，忽略指纹差异")
        self._combo_log_level = QComboBox()
        self._combo_log_level.addItems(list(LOG_LEVELS))
        self._combo_log_level.setCurrentText("INFO")
        self._chk_incremental = QCheckBox("增量回测")
        self._chk_incremental.setChecked(True)
        self._chk_incremental.setToolTip(
            "勾选：数据/参数未变化时复用缓存，只重算变更标的；\n"
            "取消勾选：每次全量重算（与「强制全量」等价，但作用于默认行为）"
        )
        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("引擎模式"))
        row_mode.addWidget(self._combo_mode, 1)
        row_mode.addWidget(QLabel("进程数"))
        row_mode.addWidget(self._spin_cpu, 1)
        row_batch = QHBoxLayout()
        row_batch.addWidget(QLabel("每批标的数"))
        row_batch.addWidget(self._spin_batch, 1)
        row_batch.addWidget(self._chk_force_full)
        row_mem = QHBoxLayout()
        row_mem.addWidget(QLabel("内存告警"))
        row_mem.addWidget(self._spin_mem_warning, 1)
        row_mem.addWidget(QLabel("严重"))
        row_mem.addWidget(self._spin_mem_critical, 1)
        row_log = QHBoxLayout()
        row_log.addWidget(QLabel("日志级别"))
        row_log.addWidget(self._combo_log_level, 1)
        row_log.addWidget(self._chk_incremental)
        form.addRow(row_mode)
        form.addRow(row_batch)
        form.addRow(row_mem)
        form.addRow(row_log)
        return box

    def _build_data_box(self) -> QGroupBox:
        """构建「行情数据源」分组框。"""
        box = QGroupBox("行情数据源")
        form = QVBoxLayout(box)
        self._radio_real = QRadioButton("processed 真实行情（默认）")
        self._radio_real.setChecked(True)
        self._radio_demo = QRadioButton("演示行情（调试）")
        data_group = QButtonGroup(self)
        data_group.addButton(self._radio_real)
        data_group.addButton(self._radio_demo)
        row_adjust = QHBoxLayout()
        row_adjust.addWidget(QLabel("复权方式"))
        self._combo_adjust = QComboBox()
        for label, value in zip(ADJUST_LABELS, ADJUST_MODES):
            self._combo_adjust.addItem(label, value)
        self._combo_adjust.setCurrentIndex(0)
        self._combo_adjust.setToolTip("找不到对应复权文件时自动降级为可用文件")
        row_adjust.addWidget(self._combo_adjust, 1)
        form.addWidget(self._radio_real)
        form.addWidget(self._radio_demo)
        form.addLayout(row_adjust)
        return box

    def _build_cost_box(self) -> QGroupBox:
        """构建「交易成本」分组框（佣金/最低佣金/印花税/过户费/滑点）。"""
        box = QGroupBox("交易成本")
        form = QFormLayout(box)
        self._spin_commission = QDoubleSpinBox()
        self._spin_commission.setRange(0.0, 0.5)
        self._spin_commission.setDecimals(3)
        self._spin_commission.setSingleStep(0.005)
        self._spin_commission.setValue(DEFAULT_COMMISSION_PCT)
        self._spin_commission.setSuffix(" %")
        self._spin_commission.setToolTip("佣金率（买卖同值，默认万2）")
        self._spin_min_commission = QDoubleSpinBox()
        self._spin_min_commission.setRange(0.0, 100.0)
        self._spin_min_commission.setDecimals(1)
        self._spin_min_commission.setSingleStep(1.0)
        self._spin_min_commission.setValue(DEFAULT_MIN_COMMISSION)
        self._spin_min_commission.setSuffix(" 元")
        self._spin_stamp_tax = QDoubleSpinBox()
        self._spin_stamp_tax.setRange(0.0, 0.5)
        self._spin_stamp_tax.setDecimals(3)
        self._spin_stamp_tax.setSingleStep(0.01)
        self._spin_stamp_tax.setValue(DEFAULT_STAMP_TAX_PCT)
        self._spin_stamp_tax.setSuffix(" %")
        self._spin_stamp_tax.setToolTip("印花税（仅卖出，默认千1）")
        self._spin_transfer_fee = QDoubleSpinBox()
        self._spin_transfer_fee.setRange(0.0, 0.05)
        self._spin_transfer_fee.setDecimals(4)
        self._spin_transfer_fee.setSingleStep(0.0005)
        self._spin_transfer_fee.setValue(DEFAULT_TRANSFER_FEE_PCT)
        self._spin_transfer_fee.setSuffix(" %")
        self._spin_transfer_fee.setToolTip("过户费（买卖双向，默认万0.1）")
        self._spin_slippage = QDoubleSpinBox()
        self._spin_slippage.setRange(0.0, 0.5)
        self._spin_slippage.setDecimals(3)
        self._spin_slippage.setSingleStep(0.005)
        self._spin_slippage.setValue(DEFAULT_SLIPPAGE_PCT)
        self._spin_slippage.setSuffix(" %")
        self._spin_slippage.setToolTip("固定滑点（默认 2bp=0.02%）")
        row_c1 = QHBoxLayout()
        row_c1.addWidget(QLabel("佣金率"))
        row_c1.addWidget(self._spin_commission, 1)
        row_c1.addWidget(QLabel("最低佣金"))
        row_c1.addWidget(self._spin_min_commission, 1)
        row_c2 = QHBoxLayout()
        row_c2.addWidget(QLabel("印花税"))
        row_c2.addWidget(self._spin_stamp_tax, 1)
        row_c2.addWidget(QLabel("过户费"))
        row_c2.addWidget(self._spin_transfer_fee, 1)
        row_c3 = QHBoxLayout()
        row_c3.addWidget(QLabel("滑点"))
        row_c3.addWidget(self._spin_slippage, 1)
        row_c3.addStretch(1)
        form.addRow(row_c1)
        form.addRow(row_c2)
        form.addRow(row_c3)
        return box

    def _build_perf_box(self) -> QGroupBox:
        """构建「绩效口径」分组框（基准指数 + 无风险利率）。"""
        box = QGroupBox("绩效口径")
        form = QFormLayout(box)
        self._combo_benchmark = QComboBox()
        self._combo_benchmark.setEditable(True)
        self._combo_benchmark.addItems(list(BENCHMARK_PRESETS))
        self._combo_benchmark.setCurrentText(DEFAULT_BENCHMARK)
        self._combo_benchmark.setToolTip("基准指数代码（绩效口径，可编辑输入）")
        self._spin_risk_free = QDoubleSpinBox()
        self._spin_risk_free.setRange(0.0, 10.0)
        self._spin_risk_free.setDecimals(2)
        self._spin_risk_free.setSingleStep(0.1)
        self._spin_risk_free.setValue(DEFAULT_RISK_FREE_PCT)
        self._spin_risk_free.setSuffix(" %")
        self._spin_risk_free.setToolTip("无风险利率（夏普计算口径，默认 2%）")
        form.addRow("基准指数", self._combo_benchmark)
        form.addRow("无风险利率", self._spin_risk_free)
        return box

    def _build_output_box(self) -> QGroupBox:
        """构建「输出设置」分组框（明细输出目录 + 保存开关）。"""
        box = QGroupBox("输出设置")
        form = QFormLayout(box)
        self._edit_output_dir = QLineEdit()
        self._edit_output_dir.setPlaceholderText(OUTPUT_DIR_PLACEHOLDER)
        self._edit_output_dir.setToolTip(
            "明细 CSV 输出目录；留空=默认 results/batch_backtest/<策略>/"
        )
        self._chk_save_trades = QCheckBox("保存交易明细")
        self._chk_save_positions = QCheckBox("保存持仓市值")
        self._chk_save_equity = QCheckBox("保存净值曲线")
        for chk in (
            self._chk_save_trades,
            self._chk_save_positions,
            self._chk_save_equity,
        ):
            chk.setToolTip("仅本次重算标的（复用标的无明细）；不落结果缓存")
        row_save = QHBoxLayout()
        row_save.addWidget(self._chk_save_trades)
        row_save.addWidget(self._chk_save_positions)
        row_save.addWidget(self._chk_save_equity)
        row_save.addStretch(1)
        form.addRow("明细输出目录", self._edit_output_dir)
        form.addRow("保存开关", row_save)
        return box

    # ── 信号槽 ──

    def _on_manual_toggled(self, checked: bool) -> None:
        """手输列表单选切换时启用/禁用输入框。

        Args:
            checked: 手输单选是否选中。
        """
        self._edit_manual.setEnabled(checked)

    def _scan_all_symbols(self) -> List[str]:
        """扫描 datasets/processed 目录全部标的（parquet/csv）。

        Returns:
            排序后的标的代码列表；目录不存在时返回空列表。
        """
        data_dir = self._project_root.joinpath(*PROCESSED_DIR_REL)
        if not data_dir.is_dir():
            logger.warning("processed 目录不存在: %s", data_dir)
            return []
        return sorted(
            f.stem
            for f in data_dir.iterdir()
            if f.is_file() and f.suffix.lower() in (".parquet", ".csv")
        )

    def _load_preset_codes(self) -> List[str]:
        """读取 config/stock_pool.yaml 的 codes 列表。

        Returns:
            标的代码列表；文件缺失或 codes 为空时返回空列表。
        """
        pool_file = self._project_root / STOCK_POOL_YAML
        if not pool_file.is_file():
            logger.warning("预筛选池文件不存在: %s", pool_file)
            return []
        try:
            import yaml
            data = yaml.safe_load(pool_file.read_text(encoding="utf-8")) or {}
            codes = (data.get("stock_pool", {}) or {}).get("codes") or []
            return [str(c) for c in codes if str(c).strip()]
        except Exception as exc:  # noqa: BLE001 - 配置解析失败不阻断 GUI
            logger.warning("预筛选池解析失败: %s（%s）", pool_file, exc)
            return []

    def _resolve_symbols(self) -> Tuple[List[str], str]:
        """按当前单选解析本次标的列表与来源描述。

        Returns:
            (标的列表, 来源描述文本)。

        Raises:
            ValueError: 手输列表为空，或全市场扫描结果为空。
        """
        if self._radio_all.isChecked():
            symbols = self._scan_all_symbols()
            if not symbols:
                raise ValueError("全市场扫描结果为空（datasets/processed 无数据文件）")
            return symbols, f"全市场扫描（{len(symbols)} 只）"
        if self._radio_preset.isChecked():
            codes = self._load_preset_codes()
            if codes:
                return codes, f"预筛选池（{len(codes)} 只）"
            fallback = self._scan_all_symbols()
            if not fallback:
                raise ValueError("预筛选池为空，且全市场扫描也为空，无法回测")
            QMessageBox.warning(
                self,
                "预筛选池为空",
                f"config/stock_pool.yaml 的 codes 为空（可能流动性筛选筛空）。\n"
                f"本次已自动回退为全市场扫描（{len(fallback)} 只）。",
            )
            return fallback, f"预筛选池（空→回退全市场 {len(fallback)} 只）"
        text = self._edit_manual.text().strip()
        symbols = [s.strip() for s in text.split(",") if s.strip()]
        if not symbols:
            raise ValueError("手输标的列表为空，请填写逗号分隔的代码")
        return symbols, f"手输列表（{len(symbols)} 只）"

    def _parse_params_override(self) -> Dict[str, Dict[str, Any]]:
        """解析参数覆盖 JSON 编辑框内容。

        Returns:
            {策略名: 参数字典}；编辑框为空时返回空字典。

        Raises:
            ValueError: JSON 格式错误或顶层不是对象。
        """
        text = self._edit_params.toPlainText().strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"参数 JSON 格式错误: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("参数 JSON 顶层必须是对象 {策略名: {参数: 值}}")
        result: Dict[str, Dict[str, Any]] = {}
        for name, params in data.items():
            if not isinstance(params, dict):
                raise ValueError(f"策略 {name!r} 的参数必须是 JSON 对象")
            result[str(name)] = params
        return result

    def _collect_options(self) -> Tuple[BatchRunOptions, str]:
        """收集当前界面参数为运行参数对象。

        Returns:
            (批量回测运行参数, 标的来源描述文本)。

        Raises:
            ValueError: 日期非法 / 未选策略 / 手输为空等参数问题。
        """
        symbols, source_label = self._resolve_symbols()
        start_date = self._date_start.date().toString("yyyy-MM-dd")
        end_date = self._date_end.date().toString("yyyy-MM-dd")
        if start_date > end_date:
            raise ValueError(f"起始日期 {start_date} 晚于结束日期 {end_date}")
        strategies = [
            name for name, check in self._strategy_checks.items() if check.isChecked()
        ]
        if not strategies:
            raise ValueError("请至少勾选一个策略")
        params = self._parse_params_override()
        benchmark = self._combo_benchmark.currentText().strip()
        if not benchmark:
            raise ValueError("基准指数不能为空")
        return BatchRunOptions(
            project_root=self._project_root,
            symbols=symbols,
            strategies=strategies,
            params=params,
            start_date=start_date,
            end_date=end_date,
            initial_capital=float(self._spin_capital.value()),
            mode=self._combo_mode.currentText(),
            cpu_workers=None if self._spin_cpu.value() == 1 else self._spin_cpu.value(),
            batch_size=None if self._spin_batch.value() == 0 else self._spin_batch.value(),
            force_full=self._chk_force_full.isChecked(),
            use_demo=self._radio_demo.isChecked(),
            mem_warning_pct=float(self._spin_mem_warning.value()),
            mem_critical_pct=float(self._spin_mem_critical.value()),
            commission_rate=self._spin_commission.value() / 100.0,
            min_commission=self._spin_min_commission.value(),
            stamp_tax=self._spin_stamp_tax.value() / 100.0,
            transfer_fee=self._spin_transfer_fee.value() / 100.0,
            slippage_pct=self._spin_slippage.value(),
            benchmark=benchmark,
            risk_free_rate=self._spin_risk_free.value() / 100.0,
            adjust=str(self._combo_adjust.currentData()),
            log_level=self._combo_log_level.currentText(),
            incremental=self._chk_incremental.isChecked(),
            output_dir=self._edit_output_dir.text().strip(),
            save_trades=self._chk_save_trades.isChecked(),
            save_positions=self._chk_save_positions.isChecked(),
            save_equity=self._chk_save_equity.isChecked(),
        ), source_label

    def _on_start_clicked(self) -> None:
        """开始按钮：收集参数 → 启动后台线程 → 禁用手动输入。"""
        if self._running:
            return
        try:
            opts, source_label = self._collect_options()
        except (ValueError, RuntimeError) as exc:
            QMessageBox.critical(self, "参数错误", str(exc))
            return

        self._running = True
        self._btn_start.setEnabled(False)
        self._btn_export.setEnabled(False)
        self._text_result.clear()
        self._append_result(
            f"=== 批量回测启动 ==="
            f"\n标的来源: {source_label}"
            f"\n策略: {', '.join(opts.strategies)}"
            f"\n引擎: {opts.mode} | 日期: {opts.start_date} ~ {opts.end_date}"
            f"\n数据源: {'演示行情' if opts.use_demo else 'processed 真实行情'}"
            f"\n成本: 佣金{opts.commission_rate:.4%} | 最低{opts.min_commission:.0f}元 "
            f"| 印花{opts.stamp_tax:.3%} | 过户{opts.transfer_fee:.5%} | 滑点{opts.slippage_pct:.2f}%"
            f"\n绩效: 基准{opts.benchmark} | 无风险{opts.risk_free_rate:.2%} | 复权{opts.adjust}"
        )
        self._label_progress.setText("运行中……")

        self._thread = QThread(self)
        self._worker = _BatchWorker()
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(lambda: self._worker.run(opts))
        self._worker.log_line.connect(self._append_result)
        self._worker.batch_done.connect(self._on_batch_done)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

    def _on_batch_done(self, strategy: str, batch_idx: int, total_batches: int) -> None:
        """批次进度更新。

        Args:
            strategy: 当前策略名。
            batch_idx: 当前完成批次号。
            total_batches: 总批次数。
        """
        self._label_progress.setText(f"[{strategy}] 批次 {batch_idx}/{total_batches}")

    def _on_worker_finished(self, success: bool, message: str) -> None:
        """后台任务结束：恢复界面并展示结果/错误。

        Args:
            success: 是否成功。
            message: 汇总文本或错误信息。
        """
        self._running = False
        self._btn_start.setEnabled(True)
        if success:
            self._label_progress.setText("完成")
            self._btn_export.setEnabled(True)
            self._append_result("\n" + message)
            QMessageBox.information(self, "批量回测完成", "批量回测已结束，详见结果区。")
        else:
            self._label_progress.setText("失败")
            self._append_result(f"\n[失败] {message}")
            QMessageBox.critical(self, "批量回测失败", message)

    def _on_export_clicked(self) -> None:
        """导出 CSV 按钮：打开汇总 CSV 所在目录（按策略子目录找最新文件）。"""
        cache_root = self._project_root / "results" / RESULT_DIR_NAME
        latest: List[Path] = []
        if cache_root.is_dir():
            for strategy_dir in sorted(cache_root.iterdir()):
                if strategy_dir.is_dir():
                    latest.extend(
                        sorted(strategy_dir.glob("批量回测汇总_*.csv"), reverse=True)
                    )
        latest.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if latest:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(latest[0].parent)))
            return
        QMessageBox.information(self, "暂无结果", "尚未生成汇总 CSV，请先运行一次批量回测。")

    def _append_result(self, text: str) -> None:
        """向结果区追加一行文本（线程安全：跨线程 Signal 自动排队）。"""
        self._text_result.appendPlainText(text)


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def _main() -> int:
    """模块自测：offscreen 实例化 Tab6，校验控件完整性与参数收集逻辑。"""
    import os
    import sys
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))

    app = QApplication.instance() or QApplication([])
    tab = BatchBacktestTab(project_root=project_root)
    print(f"[1] Tab6 实例化成功: {type(tab).__name__}")
    registered = sorted(tab._strategy_checks.keys())
    print(f"[2] 策略多选（STRATEGY_REGISTRY）: {registered}")
    selected = [name for name, cb in tab._strategy_checks.items() if cb.isChecked()]
    print(f"[3] 默认勾选策略: {selected}")
    symbols, label = tab._resolve_symbols()
    print(f"[4] 默认标的来源: {label}")
    opts, source_label = tab._collect_options()
    print(f"[5] 参数收集 OK: 标的 {len(opts.symbols)} 只 | 策略 {opts.strategies} | 来源 {source_label}")
    if not opts.symbols or not opts.strategies:
        print("[FAIL] 标的或策略为空")
        return 1
    if opts.commission_rate != 0.0002 or opts.risk_free_rate != 0.02:
        print("[FAIL] 成本/绩效默认值转换异常")
        return 1
    if opts.adjust != "raw" or opts.log_level != "INFO" or not opts.incremental:
        print("[FAIL] 复权/日志/增量默认值异常")
        return 1
    if tab._spin_min_commission.value() != 5.0 or tab._spin_stamp_tax.value() != 0.1:
        print("[FAIL] 成本默认值异常")
        return 1
    print("[6] 新控件默认值: 佣金"
          f"{tab._spin_commission.value()}% | 基准{tab._combo_benchmark.currentText()} "
          f"| 复权{tab._combo_adjust.currentData()} | 日志{tab._combo_log_level.currentText()}")
    print("[PASS] Tab6 自测通过")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
