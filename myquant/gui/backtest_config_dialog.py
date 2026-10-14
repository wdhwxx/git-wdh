# -*- coding: utf-8 -*-
# 文件名：myquant/gui/backtest_config_dialog.py
"""
回测配置窗口（v1）
==================
在回测入口脚本之上提供一个图形化配置窗口：运行回测前先弹窗，
勾选/填写你关心的条件，点按钮后自动生成 backtest_config.yaml 并启动回测。

v1 聚焦「股票池过滤」（用户点名需求）：
  - 勾选剔除常用板块（科创板 / 创业板 / 北交所）
  - 自定义排除代码前缀（如 30、92，逗号分隔）
  - 实时预览最终的排除前缀列表

同时提供基础回测设置：起止日期 / 初始资金 / 基准 / 策略选择。

设计原则：
  - 窗口本身不写任何回测业务逻辑，只负责「生成配置 + 调起入口」
  - 生成的配置与 config/backtest_config.example.yaml 结构一致，可被 run_回测入口.py 直接读取
  - 策略参数沿用 config/strategy_params.example.yaml 的默认值（后续版本在窗口内细化）

依赖: PySide6（可选依赖，pip install -e ".[gui]"）

2026-09-10（Tab 合并·第一步 + 标题高亮）：
  - 「策略与仓位」Tab 已删除：策略参数（RSRS）+ 仓位管理 并入「基础设置」页
    回测设置下方左右并排；控件 self.spin_* 属性名、信号连接、默认值全部原样保留。
  - 标题栏改为自绘高亮：FramelessWindowHint + QLabel 霓虹发光 + 最小化/关闭按钮 + 拖动。
2026-09-10（Tab 合并·第二步）：
  - 「输出与成本」Tab 已删除：交易成本 + 输出 并入「引擎与性能」页
    （行2 内存自动降级 | 交易成本 等高并排，行3 输出全宽；控件/信号/默认值全部原样保留，
    注释行按预览图省略）；引擎与性能页现有模块布局不变；4 标签页：基础设置/股票池/引擎与性能/批量回测。
2026-09-13（看板生成 Tab）：
  - 新增第 6 标签页「看板生成」：标的代码直接输入（逗号分隔多只），结束日期默认当天；
    生成看板 + 打开看板按钮；成功后自动清理旧看板（保留最新 N 个）；取代根目录 看板快速生成.py（已废弃删除）。
  - 主「基础设置」页结束日期默认值同步改为当天。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from myquant.core.logger import get_logger

logger = get_logger(__name__)

from PySide6.QtCore import QDate, QObject, QRect, Qt, QUrl, Signal  # noqa: E402
from PySide6.QtGui import QColor, QDesktopServices, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from myquant.gui.batch_backtest_tab import BatchBacktestTab  # noqa: E402

# ─────────────────────────────────────────────
# 回测子进程监视器（完成后弹提示）
# ─────────────────────────────────────────────

class _ProcWatcher(QObject):
    """监视回测子进程退出，退出时发 finished 信号（跨线程安全）"""
    finished = Signal(int)  # 退出码

    def __init__(self, proc: "subprocess.Popen") -> None:
        super().__init__()
        self._proc = proc

    def start(self) -> None:
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        try:
            code = self._proc.wait()
        except Exception:
            code = -1
        try:
            self.finished.emit(code)
        except RuntimeError:
            pass  # 对话框已关闭，忽略


# ─────────────────────────────────────────────
# 回测子进程输出桥（stdout 逐行转发到 GUI 线程）
# ─────────────────────────────────────────────

class _ProgressBridge(QObject):
    """把子进程 stdout 的每一行文本安全转发到 GUI 线程（跨线程 Signal）"""
    line_received = Signal(str)  # 已去首尾空白的原始文本行

    def __init__(self, parent=None) -> None:
        super().__init__(parent)


# ─────────────────────────────────────────────
# 路径探测
# ─────────────────────────────────────────────

def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


# ─────────────────────────────────────────────
# 常量：预置板块过滤
# ─────────────────────────────────────────────

# 每个板块对应的代码前缀（30 开头即创业板 300/301，92 为北交所新代码段）
PRESET_FILTERS: Dict[str, List[str]] = {
    "科创板 (688 开头)": ["688"],
    "创业板 (30 开头)": ["30"],
    "北交所 (4/8/92 开头)": ["4", "8", "92"],
}

# 回测进度面板（方案A：子进程 stdout → PIPE → 面板滚动显示）
# 占位提示（面板为空时展示）
PROGRESS_PLACEHOLDER: str = (
    "点击「生成并启动回测」后，回测进度将在此实时滚动显示；"
    "回测过程不再弹出命令行窗口。"
)
# 面板最大保留行数（防内存膨胀，超出自动丢弃最旧行）
PROGRESS_MAX_BLOCKS: int = 5000
# 面板最小/最大高度（保持窗口整体观感，不随内容撑开）
PROGRESS_MIN_HEIGHT: int = 200
PROGRESS_MAX_HEIGHT: int = 340


# 可用策略（兜底，正常情况下从注册器动态读取）
FALLBACK_STRATEGIES: List[str] = [
    "rsrs", "grid", "momentum", "mean_reversion", "multi_factor",
]


def load_registered_strategies() -> List[str]:
    """动态读取已注册策略列表；失败时使用兜底列表"""
    try:
        from myquant.strategies.registry import discover_strategies, list_strategies
        discover_strategies()
        names = list_strategies()
        if names:
            return names
    except Exception as e:
        logger.debug("策略发现失败，使用兜底列表: %s", e)
    return FALLBACK_STRATEGIES


def load_strategy_params(project_root: Path) -> Dict:
    """读取 strategy_params.example.yaml，返回 {策略名: 参数字典}"""
    params_file = project_root / "config" / "strategy_params.example.yaml"
    if not params_file.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(params_file.read_text(encoding="utf-8")) or {}
        return data
    except Exception:
        return {}


# ─────────────────────────────────────────────
# 配置生成（与 example 结构一致）
# ─────────────────────────────────────────────

def build_config_dict(
    *,
    project_root: Path,
    start_date: str,
    end_date: str,
    initial_capital: float,
    benchmark: str,
    strategy_name: str,
    exclude_prefixes: List[str],
    output_dir: str,
    preset_codes: Optional[List[str]] = None,
    # ★ 策略参数（窗口自定义，覆盖默认值）
    rsrs_period: int = 18,
    rsrs_norm_window: int = 60,
    buy_threshold: float = 0.7,
    sell_threshold: float = -0.7,
    take_profit_pct: float = 0.20,
    stop_loss_pct: float = 0.15,
    # ★ 仓位管理参数
    max_single_position: float = 0.20,
    max_total_positions: int = 5,
    max_total_position_pct: float = 0.95,
    # ★ 交易成本参数
    commission_rate: float = 0.0002,
    stamp_tax_rate: float = 0.001,
    slippage_value: float = 0.0002,
    # ★ 引擎与性能优化参数（P2 全套开关，与 example.yaml 的 engine/performance 段一致）
    engine_mode: str = "auto",
    parallel_enabled: bool = True,
    cpu_workers: Optional[int] = None,
    check_pickle: bool = True,
    backtest_logging_enabled: bool = True,
    backtest_logging_level: str = "INFO",
    timer_enabled: bool = True,
    light_guard_enabled: bool = True,
    light_guard_strict: bool = False,
    mc_skip: bool = False,
    incremental_enabled: bool = True,
    incremental_force_full: bool = False,
    memory_enabled: bool = True,
    memory_warning_pct: float = 70.0,
    memory_critical_pct: float = 85.0,
) -> Dict:
    """
    构建完整的 backtest 配置字典。

    结构与 config/backtest_config.example.yaml 保持一致，
    并新增 stock_pool.exclude_prefixes 字段保存窗口里选的过滤前缀。
    若 preset_codes 不为空，则写入 stock_pool.codes（标的预筛选结果）。

    ★ 策略参数/仓位参数/交易成本参数 均可在窗口自定义，
      窗口值覆盖 strategy_params.example.yaml 的默认值。
    """
    # 策略默认参数（来自 strategy_params.example.yaml，取对应策略块）
    sp = load_strategy_params(project_root)
    strategy_params = dict(sp.get(strategy_name, {}) or {})

    # ★ 窗口自定义参数覆盖默认值（RSRS 策略相关参数）
    strategy_params.update({
        "period": rsrs_period,
        "norm_window": rsrs_norm_window,
        "buy_threshold": buy_threshold,
        "sell_threshold": sell_threshold,
        "take_profit_pct": take_profit_pct,
        "stop_loss_pct": stop_loss_pct,
    })

    config: Dict = {
        "backtest": {
            "name": f"{strategy_name}_backtest_gui",
            "start_date": start_date,
            "end_date": end_date,
            "initial_capital": int(initial_capital),
            "benchmark": benchmark,
            "time_frame": "1d",
        },
        "stock_pool": {
            "type": "preset_filtered",  # 统一用 preset_filtered，codes 里放标的列表
            # ★ 窗口选择的排除前缀（回测/实盘股票池过滤共用）
            "exclude_prefixes": sorted(set(exclude_prefixes)),
            "three_factor": {
                "volatility_window": 20,
                "momentum_window": 60,
                "price_quantile": 0.33,
                "factor_quantile": 0.33,
            },
            # ★ 标的列表（启用预筛选时是筛选结果，未启用时是全市场扫描结果）
            "codes": preset_codes if preset_codes else [],
        },
        "strategy": {
            "name": strategy_name,
            "params": strategy_params,
        },
        "sim_broker": {
            "commission_rate": commission_rate,
            "min_commission": 5.0,
            "stamp_tax_rate": stamp_tax_rate,
            "transfer_fee_rate": 0.00001,
            "slippage_mode": "ratio",
            "slippage_value": slippage_value,
            "enable_t1": True,
            "lot_size": 100,
            "price_limit_pct": 0.10,
            "enforce_limit": True,
        },
        "position": {
            "max_single_position": max_single_position,
            "max_total_positions": max_total_positions,
            "max_total_position_pct": max_total_position_pct,
        },
        "data": {
            "data_dir": "./datasets/processed",
            "data_format": "parquet",
            "date_column": "date",
            "adjust": "raw",
            "price_columns": {
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
            },
            "volume": "volume",
            "pct_chg": "pct_chg",
        },
        "metrics": {
            "risk_free_rate": 0.03,
            "annual_trade_days": 252,
        },
        "output": {
            "base_dir": output_dir,
            "save_trades": True,
            "save_positions": True,
            "save_equity_curve": True,
            "generate_report": True,
            "report_title": f"{strategy_name} 策略回测报告",
            "log_level": "INFO",
        },
        "lookahead_guard": {
            "enabled": True,
            "strict_mode": True,
        },
        # ★ 真实性门禁开关（P2-07：GUI 补 gate 段，与 config/backtest_config.yaml 对齐）
        #   leakage_gate：未来函数/泄露审计；realism_gate：涨跌停/T+1/滑点真实性审计
        "leakage_gate": {
            "enabled": True,
            "audit_violations": True,
        },
        "realism_gate": {
            "enabled": True,
            "audit_violations": True,
        },
        # ★ 引擎模式（三模式调度：auto自动 / event / mixed / vectorized）
        "engine": {
            "mode": engine_mode,
        },
        # ★ 性能优化开关（P2 全套，由 run_回测入口.py 应用）
        "performance": {
            "parallel": {
                "enabled": parallel_enabled,
                "cpu_workers": cpu_workers,
                "check_pickle": check_pickle,
            },
            "backtest_logging": {
                "enabled": backtest_logging_enabled,
                "level": backtest_logging_level,
                "sampling": True,
            },
            "timer": {
                "enabled": timer_enabled,
            },
            "light_lookahead_guard": {
                "enabled": light_guard_enabled,
                "strict": light_guard_strict,
            },
            "monte_carlo": {
                "skip": mc_skip,
            },
            "incremental": {
                "enabled": incremental_enabled,
                "force_full": incremental_force_full,
            },
            "memory": {
                "enabled": memory_enabled,
                "warning_pct": memory_warning_pct,
                "critical_pct": memory_critical_pct,
            },
        },
    }
    return config


# ─────────────────────────────────────────────
# 主窗口
# ─────────────────────────────────────────────

class BacktestConfigDialog(QDialog):
    """回测配置对话框"""

    def __init__(self, project_root: Path, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.strategy_params = load_strategy_params(project_root)
        # 标的预筛选结果（启用预筛选并点击按钮后填充）
        self._preset_codes: Optional[List[str]] = None
        self._preset_meta: Optional[Dict] = None
        # 方案A：回测子进程输出桥 + 子进程句柄（供进度面板实时显示）
        self._progress_bridge = _ProgressBridge(self)
        self._progress_bridge.line_received.connect(self._append_progress)
        self._backtest_proc: Optional[subprocess.Popen] = None
        # 候选交叉池（Tab5）：子进程输出桥 + 句柄 + 输出行缓存（完成弹窗解析用）
        self._crosspool_bridge = _ProgressBridge(self)
        self._crosspool_bridge.line_received.connect(self._append_crosspool_output)
        self._crosspool_proc: Optional[subprocess.Popen] = None
        self._crosspool_lines: List[str] = []
        # 看板生成页：子进程句柄 + 本次看板输出路径（完成回调/打开按钮用）
        self._dash_proc: Optional[subprocess.Popen] = None
        self._dash_out_path: str = ""

        self.setWindowTitle("鲲鹏量化交易系统-回测配置")
        self.setMinimumWidth(860)
        # ★ 2026-09-10 自绘标题栏（高亮方案）：隐藏系统标题栏，标题由窗口内 QLabel 高亮显示
        #   （最小化/关闭按钮与窗口拖动均在自绘标题栏内实现）
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self._apply_tech_style()

        self._build_ui()
        self._refresh_filter_preview()
        # ★ 整体协调：初始尺寸按 sizeHint 收敛；高度再按屏幕可用高度自适应封顶
        #   （Qt6 尺寸为逻辑像素，DPI 缩放后物理高度会放大；限制在屏幕可用高 92%
        #    以内可保证 100%/125%/150% 缩放下窗口都不溢出屏幕）
        screen_avail = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        max_h = min(880, int(screen_avail.height() * 0.92))
        self.resize(min(self.sizeHint().width(), 1060), min(self.sizeHint().height(), max_h))
        # 窗口居中于当前屏幕
        self.move(screen_avail.center() - self.rect().center())

    # ── 自绘标题栏拖动（2026-09-10） ──
    def mousePressEvent(self, event: QMouseEvent) -> None:
        """按住标题栏区域（顶部 32px）记录拖拽偏移，其余区域交回默认处理"""
        if event.button() == Qt.LeftButton and event.position().y() <= 32:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """拖动标题栏时移动窗口"""
        offset = getattr(self, '_drag_offset', None)
        if offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """松开左键清除拖拽偏移"""
        if hasattr(self, '_drag_offset'):
            del self._drag_offset
        super().mouseReleaseEvent(event)

    # ── 样式（科技渐变风 · 方案三 2026-09-05） ──
    def _apply_tech_style(self) -> None:
        """深紫渐变 + 霓虹青紫强调 + 标签页分类；标题栏带最小化按钮"""
        self.setStyleSheet("""
            QDialog {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0B1120, stop:1 #1E1B4B);
            }
            QGroupBox {
                background-color: rgba(15, 23, 42, 0.55);
                border: 1px solid #4F46E5;
                border-radius: 10px;
                margin-top: 8px;
                padding: 6px 8px 4px 8px;
                color: #E0E7FF;
                font-size: 13px;
                font-weight: 600;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #818CF8;
            }
            QLabel {
                color: #C7D2FE;
                font-size: 12px;
            }
            QLineEdit, QDateEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background-color: #0B1120;
                border: 1px solid #4F46E5;
                border-radius: 6px;
                color: #E0E7FF;
                padding: 3px 8px;
                min-height: 20px;
                selection-background-color: #6366F1;
            }
            QLineEdit:focus, QDateEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #22D3EE;
            }
            QComboBox::drop-down, QDateEdit::drop-down {
                border: none;
                width: 20px;
            }
            QCheckBox {
                color: #C7D2FE;
                font-size: 12px;
                spacing: 6px;
            }
            QCheckBox::indicator {
                width: 14px;
                height: 14px;
            }
            QCheckBox::indicator:unchecked {
                border: 1px solid #4F46E5;
                border-radius: 3px;
                background-color: #0B1120;
            }
            QCheckBox::indicator:checked {
                border: 1px solid #22D3EE;
                border-radius: 3px;
                background-color: #22D3EE;
            }
            QPushButton {
                background-color: rgba(79, 70, 229, 0.15);
                border: 1px solid #4F46E5;
                border-radius: 6px;
                color: #A5B4FC;
                padding: 6px 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                border: 1px solid #22D3EE;
                color: #E0E7FF;
            }
            QPushButton#btnRun {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #22D3EE, stop:1 #818CF8);
                color: #0B1120;
                font-weight: 700;
                border: none;
            }
            QPushButton#btnRun:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #38E3F5, stop:1 #9AA2FF);
            }
            QPushButton#btnCrossRun {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #8B5CF6, stop:1 #A78BFA);
                color: #0B1120;
                font-weight: 700;
                border: none;
            }
            QPushButton#btnCrossRun:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #A78BFA, stop:1 #C4B5FD);
            }
            QTabWidget::pane {
                border: 1px solid #4F46E5;
                border-radius: 10px;
                background-color: rgba(15, 23, 42, 0.45);
                top: -1px;
            }
            QTabBar::tab {
                background-color: rgba(79, 70, 229, 0.08);
                border: 1px solid #4F46E5;
                border-bottom: none;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                color: #A5B4FC;
                padding: 7px 18px;
                font-size: 12px;
                margin-right: 3px;
            }
            QTabBar::tab:selected {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #22D3EE, stop:1 #818CF8);
                color: #0B1120;
                font-weight: 700;
            }
            QTabBar::tab:hover:!selected {
                border: 1px solid #22D3EE;
                color: #E0E7FF;
            }
            QPlainTextEdit#progressView {
                background-color: rgba(2, 6, 23, 0.88);
                border: 1px solid #4F46E5;
                border-radius: 8px;
                color: #A5F3FC;
                font-family: Consolas, "Microsoft YaHei", monospace;
                font-size: 12px;
                selection-background-color: #6366F1;
            }
            QPlainTextEdit#progressView:focus {
                border: 1px solid #22D3EE;
            }
            QPlainTextEdit#crosspoolView {
                background-color: rgba(2, 6, 23, 0.88);
                border: 1px solid #8B5CF6;
                border-radius: 8px;
                color: #A5F3FC;
                font-family: Consolas, "Microsoft YaHei", monospace;
                font-size: 12px;
                selection-background-color: #6366F1;
            }
            QPlainTextEdit#crosspoolView:focus {
                border: 1px solid #A78BFA;
            }
            QCalendarWidget QWidget {
                background-color: #0F172A;
                color: #C7D2FE;
            }
            QCalendarWidget QToolButton {
                background-color: #1E1B4B;
                border: 1px solid #4F46E5;
                border-radius: 4px;
                color: #E0E7FF;
            }
            QToolButton#titleMin, QToolButton#titleClose {
                background-color: rgba(255, 255, 255, 0.06);
                border: 1px solid #4F46E5;
                border-radius: 4px;
                color: #C7D2FE;
                font-size: 12px;
                padding: 0;
            }
            QToolButton#titleMin:hover {
                background-color: rgba(34, 211, 238, 0.15);
                color: #22D3EE;
            }
            QToolButton#titleClose:hover {
                background-color: rgba(239, 68, 68, 0.25);
                color: #F87171;
            }
        """)
    # ── UI 构建 ──
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        # ===== 自绘标题栏（2026-09-10：标题高亮方案，替代系统原生标题栏） =====
        title_bar = QWidget()
        tb_title = QHBoxLayout(title_bar)
        tb_title.setContentsMargins(2, 2, 4, 2)
        self.title_label = QLabel('鲲鹏量化交易系统-回测配置')
        self.title_label.setStyleSheet(
            'color: #22D3EE; font-size: 15px; font-weight: 700; background: transparent;'
        )
        _glow = QGraphicsDropShadowEffect(self.title_label)
        _glow.setBlurRadius(14)
        _glow.setColor(QColor(34, 211, 238, 150))
        _glow.setOffset(0, 0)
        self.title_label.setGraphicsEffect(_glow)
        # 标题区域鼠标事件穿透，便于拖动窗口
        self.title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        tb_title.addWidget(self.title_label)
        tb_title.addStretch()
        self.btn_title_min = QToolButton()
        self.btn_title_min.setText('—')
        self.btn_title_min.setObjectName('titleMin')
        self.btn_title_min.setFixedSize(26, 22)
        self.btn_title_min.setToolTip('最小化')
        self.btn_title_min.clicked.connect(self.showMinimized)
        self.btn_title_close = QToolButton()
        self.btn_title_close.setText('✕')
        self.btn_title_close.setObjectName('titleClose')
        self.btn_title_close.setFixedSize(26, 22)
        self.btn_title_close.setToolTip('关闭')
        self.btn_title_close.clicked.connect(self.close)
        tb_title.addWidget(self.btn_title_min)
        tb_title.addWidget(self.btn_title_close)
        layout.addWidget(title_bar)
        # ===== 科技渐变风 · 标签页结构（方案三 2026-09-05） =====
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        # ── Tab1：基础设置 ──
        tab_basic = QWidget()
        tb = QVBoxLayout(tab_basic)
        box_backtest = QGroupBox("回测设置")
        form = QFormLayout(box_backtest)
        self.date_start = QDateEdit(QDate(2024, 1, 1))
        self.date_start.setCalendarPopup(True)
        # ★ 2026-09-13 默认结束日期改为当天（与「看板生成」页一致）
        self.date_end = QDateEdit(QDate.currentDate())
        self.date_end.setCalendarPopup(True)
        form.addRow("开始日期", self.date_start)
        form.addRow("结束日期", self.date_end)
        self.capital = QDoubleSpinBox()
        self.capital.setRange(10000, 10_000_000_000)
        self.capital.setDecimals(0)
        self.capital.setSingleStep(100_000)
        self.capital.setValue(1_000_000)
        self.capital.setSuffix(" 元")
        form.addRow("初始资金", self.capital)
        self.benchmark = QLineEdit("000300.SH")
        form.addRow("基准指数", self.benchmark)
        self.strategy = QComboBox()
        self.strategy.addItems(load_registered_strategies())
        # ★ 默认选中 rsrs 策略
        rsrs_index = self.strategy.findText("rsrs")
        if rsrs_index >= 0:
            self.strategy.setCurrentIndex(rsrs_index)
        form.addRow("策略", self.strategy)
        tb.addWidget(box_backtest)
        # ★ 2026-09-10 Tab 合并（第一步）：策略参数（RSRS）+ 仓位管理 并入基础设置
        #   （控件/信号/默认值全部原样保留，仅重组容器；原「策略与仓位」Tab 已删除）
        box_strategy = QGroupBox('策略参数（RSRS）')
        form_s = QFormLayout(box_strategy)
        self.spin_rsrs_period = QSpinBox()
        self.spin_rsrs_period.setRange(5, 100)
        self.spin_rsrs_period.setValue(18)
        self.spin_rsrs_period.setSuffix(' 根')
        form_s.addRow('RSRS 周期', self.spin_rsrs_period)
        self.spin_norm_window = QSpinBox()
        self.spin_norm_window.setRange(10, 500)
        self.spin_norm_window.setValue(60)
        self.spin_norm_window.setSuffix(' 根')
        form_s.addRow('标准化窗口', self.spin_norm_window)
        self.spin_buy_threshold = QDoubleSpinBox()
        self.spin_buy_threshold.setRange(-3.0, 3.0)
        self.spin_buy_threshold.setDecimals(2)
        self.spin_buy_threshold.setSingleStep(0.1)
        self.spin_buy_threshold.setValue(0.7)
        form_s.addRow('买入阈值', self.spin_buy_threshold)
        self.spin_sell_threshold = QDoubleSpinBox()
        self.spin_sell_threshold.setRange(-3.0, 3.0)
        self.spin_sell_threshold.setDecimals(2)
        self.spin_sell_threshold.setSingleStep(0.1)
        self.spin_sell_threshold.setValue(-0.7)
        form_s.addRow('卖出阈值', self.spin_sell_threshold)
        self.spin_take_profit = QDoubleSpinBox()
        self.spin_take_profit.setRange(0, 100)
        self.spin_take_profit.setDecimals(1)
        self.spin_take_profit.setSingleStep(5)
        self.spin_take_profit.setValue(20)
        self.spin_take_profit.setSuffix(' %')
        form_s.addRow('止盈', self.spin_take_profit)
        self.spin_stop_loss = QDoubleSpinBox()
        self.spin_stop_loss.setRange(0, 100)
        self.spin_stop_loss.setDecimals(1)
        self.spin_stop_loss.setSingleStep(5)
        self.spin_stop_loss.setValue(15)
        self.spin_stop_loss.setSuffix(' %')
        form_s.addRow('止损', self.spin_stop_loss)
        box_position = QGroupBox('仓位管理')
        form_p = QFormLayout(box_position)
        self.spin_max_single_pos = QDoubleSpinBox()
        self.spin_max_single_pos.setRange(1, 100)
        self.spin_max_single_pos.setDecimals(1)
        self.spin_max_single_pos.setSingleStep(5)
        self.spin_max_single_pos.setValue(20)
        self.spin_max_single_pos.setSuffix(' %')
        form_p.addRow('单票最大仓位', self.spin_max_single_pos)
        self.spin_max_positions = QSpinBox()
        self.spin_max_positions.setRange(1, 50)
        self.spin_max_positions.setValue(5)
        self.spin_max_positions.setSuffix(' 只')
        form_p.addRow('最大持仓数', self.spin_max_positions)
        self.spin_max_total_pos = QDoubleSpinBox()
        self.spin_max_total_pos.setRange(10, 100)
        self.spin_max_total_pos.setDecimals(1)
        self.spin_max_total_pos.setSingleStep(5)
        self.spin_max_total_pos.setValue(95)
        self.spin_max_total_pos.setSuffix(' %')
        form_p.addRow('总仓位上限', self.spin_max_total_pos)
        row_s = QHBoxLayout()
        row_s.addWidget(box_strategy, 1)
        row_s.addWidget(box_position, 1)
        tb.addLayout(row_s)
        tb.addStretch()
        self._tabs.addTab(tab_basic, "基础设置")
        # ── Tab2：股票池（过滤 + 预筛选 并排） ──
        tab_pool = QWidget()
        tp = QVBoxLayout(tab_pool)
        box_filter = QGroupBox("股票池过滤（回测用）")
        vf = QVBoxLayout(box_filter)
        hint = QLabel(
            "勾选要剔除的板块，或在下框直接填写要排除的代码前缀。\n"
            "例：不选 30、92 开头的票 → 勾选创业板/北交所，或在自定义框填 “30,92”。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #A5B4FC;")
        vf.addWidget(hint)
        self.filter_checks: List[Tuple[str, QCheckBox]] = []
        for label, _prefixes in PRESET_FILTERS.items():
            cb = QCheckBox(label)
            cb.toggled.connect(self._refresh_filter_preview)
            self.filter_checks.append((label, cb))
            vf.addWidget(cb)
        row_custom = QHBoxLayout()
        row_custom.addWidget(QLabel("额外排除前缀（逗号分隔）："))
        self.custom_prefixes = QLineEdit()
        self.custom_prefixes.setPlaceholderText("例如：30,92")
        self.custom_prefixes.textChanged.connect(self._refresh_filter_preview)
        row_custom.addWidget(self.custom_prefixes)
        vf.addLayout(row_custom)
        # 过滤规则实时预览
        self.filter_preview = QLabel()
        self.filter_preview.setWordWrap(True)
        self.filter_preview.setStyleSheet(
            "background: #0B1120; border: 1px solid #4F46E5; border-radius: 6px;"
            " padding: 6px; color: #E0E7FF;"
        )
        vf.addWidget(self.filter_preview)
        note = QLabel(
            "说明：前缀过滤将写入配置 stock_pool.exclude_prefixes，"
            "与策略层股票池过滤共用；流通股本等实时条件属实盘过滤，后续版本加入。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748B; font-size: 11px;")
        vf.addWidget(note)
        box_prefilter = QGroupBox("标的预筛选（可选，从全市场筛选可交易池）")
        vpf = QVBoxLayout(box_prefilter)
        self.chk_prefilter = QCheckBox("启用标的预筛选（回测只跑筛选后的标的）")
        self.chk_prefilter.setChecked(False)
        vpf.addWidget(self.chk_prefilter)
        # 筛选条件行1：上市年限 + 日均成交额
        row_pf1 = QHBoxLayout()
        row_pf1.addWidget(QLabel("上市满"))
        self.spin_list_years = QDoubleSpinBox()
        self.spin_list_years.setRange(0, 20)
        self.spin_list_years.setDecimals(1)
        self.spin_list_years.setSingleStep(0.5)
        self.spin_list_years.setValue(1.0)
        self.spin_list_years.setSuffix(" 年")
        row_pf1.addWidget(self.spin_list_years)
        row_pf1.addSpacing(20)
        row_pf1.addWidget(QLabel("日均成交额 ≥"))
        self.spin_min_amount = QDoubleSpinBox()
        self.spin_min_amount.setRange(0, 1_000_000)
        self.spin_min_amount.setDecimals(0)
        self.spin_min_amount.setSingleStep(1000)
        self.spin_min_amount.setValue(5000)
        self.spin_min_amount.setSuffix(" 万")
        row_pf1.addWidget(self.spin_min_amount)
        row_pf1.addStretch()
        vpf.addLayout(row_pf1)
        # 筛选条件行2：日均换手率 + 回看天数
        row_pf2 = QHBoxLayout()
        row_pf2.addWidget(QLabel("日均换手率 ≥"))
        self.spin_min_turnover = QDoubleSpinBox()
        self.spin_min_turnover.setRange(0, 50)
        self.spin_min_turnover.setDecimals(2)
        self.spin_min_turnover.setSingleStep(0.5)
        self.spin_min_turnover.setValue(1.0)
        self.spin_min_turnover.setSuffix(" %")
        row_pf2.addWidget(self.spin_min_turnover)
        row_pf2.addSpacing(20)
        row_pf2.addWidget(QLabel("流动性回看"))
        self.spin_lookback = QSpinBox()
        self.spin_lookback.setRange(30, 1000)
        self.spin_lookback.setSingleStep(30)
        self.spin_lookback.setValue(365)
        self.spin_lookback.setSuffix(" 天")
        row_pf2.addWidget(self.spin_lookback)
        row_pf2.addStretch()
        vpf.addLayout(row_pf2)
        turnover_note = QLabel(
            "注：换手率过滤需数据库 turnover 字段有数据；若全为空则自动跳过，仅按成交额过滤。"
        )
        turnover_note.setWordWrap(True)
        turnover_note.setStyleSheet("color: #64748B; font-size: 11px;")
        vpf.addWidget(turnover_note)
        # 预筛选按钮 + 结果预览
        row_pf_btn = QHBoxLayout()
        self.btn_prefilter = QPushButton("▶ 预筛选标的")
        self.btn_prefilter.clicked.connect(self._on_run_prefilter)
        row_pf_btn.addWidget(self.btn_prefilter)
        self.label_prefilter_result = QLabel("结果：未筛选")
        self.label_prefilter_result.setStyleSheet("color: #94A3B8;")
        row_pf_btn.addWidget(self.label_prefilter_result)
        row_pf_btn.addStretch()
        vpf.addLayout(row_pf_btn)
        # ★ 股票池过滤 + 标的预筛选 左右并排
        row_filter_prefilter = QHBoxLayout()
        row_filter_prefilter.addWidget(box_filter, 1)
        row_filter_prefilter.addWidget(box_prefilter, 1)
        tp.addLayout(row_filter_prefilter)
        self._tabs.addTab(tab_pool, "股票池")
        # ── Tab3：引擎与性能（引擎 + 防未来 + 内存 + 交易成本 + 输出） ──
        tab_engine = QWidget()
        te = QVBoxLayout(tab_engine)
        box_perf = QGroupBox("引擎与性能")
        form_perf = QFormLayout(box_perf)
        self.combo_engine_mode = QComboBox()
        self.combo_engine_mode.addItems(["auto", "event", "mixed", "vectorized"])
        self.combo_engine_mode.setCurrentText("auto")
        self.combo_engine_mode.setToolTip(
            "auto=按策略能力自动选择（推荐）\n"
            "vectorized=纯向量化\nmixed=混合引擎（生产主力）\nevent=事件驱动（真值基准）"
        )
        form_perf.addRow("引擎模式", self.combo_engine_mode)
        self.chk_parallel = QCheckBox("并行治理（I/O线程池+计算进程池）")
        self.chk_parallel.setChecked(True)
        form_perf.addRow(self.chk_parallel)
        self.spin_cpu_workers = QSpinBox()
        self.spin_cpu_workers.setRange(1, 64)
        self.spin_cpu_workers.setSpecialValueText("自动")
        self.spin_cpu_workers.setValue(1)
        form_perf.addRow("计算进程数", self.spin_cpu_workers)
        self.chk_check_pickle = QCheckBox("进程池提交前 pickle 预检")
        self.chk_check_pickle.setChecked(True)
        form_perf.addRow(self.chk_check_pickle)
        self.chk_bt_logging = QCheckBox("回测日志模式（降级+异步队列）")
        self.chk_bt_logging.setChecked(True)
        form_perf.addRow(self.chk_bt_logging)
        self.combo_log_level = QComboBox()
        self.combo_log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self.combo_log_level.setCurrentText("INFO")
        form_perf.addRow("日志级别", self.combo_log_level)
        self.chk_timer = QCheckBox("分阶段计时")
        self.chk_timer.setChecked(True)
        form_perf.addRow(self.chk_timer)
        box_guard = QGroupBox("轻量防未来函数 / 蒙特卡洛 / 增量")
        form_guard = QFormLayout(box_guard)
        self.chk_light_guard = QCheckBox("轻量防未来函数校验（逐bar运行时）")
        self.chk_light_guard.setChecked(True)
        form_guard.addRow(self.chk_light_guard)
        self.chk_light_strict = QCheckBox("严格模式（违规立即抛错）")
        self.chk_light_strict.setChecked(False)
        form_guard.addRow(self.chk_light_strict)
        self.chk_mc_skip = QCheckBox("跳过蒙特卡洛模拟（省算力）")
        self.chk_mc_skip.setChecked(False)
        form_guard.addRow(self.chk_mc_skip)
        self.chk_incremental = QCheckBox("增量回测（数据指纹识别变更标的）")
        self.chk_incremental.setChecked(True)
        form_guard.addRow(self.chk_incremental)
        self.chk_force_full = QCheckBox("强制全量重算（忽略指纹差异）")
        self.chk_force_full.setChecked(False)
        form_guard.addRow(self.chk_force_full)
        row_perf = QHBoxLayout()
        row_perf.addWidget(box_perf, 1)
        row_perf.addWidget(box_guard, 1)
        te.addLayout(row_perf)
        # 内存自动降级（OOM防护）
        box_mem = QGroupBox("内存自动降级（OOM防护）")
        form_mem = QFormLayout(box_mem)
        self.chk_memory = QCheckBox("启用内存自动降级")
        self.chk_memory.setChecked(True)
        form_mem.addRow(self.chk_memory)
        self.spin_mem_warning = QSpinBox()
        self.spin_mem_warning.setRange(10, 100)
        self.spin_mem_warning.setSuffix(" %")
        self.spin_mem_warning.setValue(70)
        form_mem.addRow("告警阈值", self.spin_mem_warning)
        self.spin_mem_critical = QSpinBox()
        self.spin_mem_critical.setRange(10, 100)
        self.spin_mem_critical.setSuffix(" %")
        self.spin_mem_critical.setValue(85)
        form_mem.addRow("严重阈值", self.spin_mem_critical)
        # ★ 2026-09-10 Tab 合并（第二步）：交易成本 + 输出 并入引擎与性能页
        #   （控件/信号/默认值全部原样保留；内存降级|交易成本 等高并排，输出全宽）
        box_cost = QGroupBox('交易成本')
        form_c = QFormLayout(box_cost)
        self.spin_commission = QDoubleSpinBox()
        self.spin_commission.setRange(0, 1.0)
        self.spin_commission.setDecimals(3)
        self.spin_commission.setSingleStep(0.01)
        self.spin_commission.setValue(0.02)  # 万2 = 0.02%
        self.spin_commission.setSuffix(' %')
        form_c.addRow('佣金率', self.spin_commission)
        self.spin_stamp_tax = QDoubleSpinBox()
        self.spin_stamp_tax.setRange(0, 1.0)
        self.spin_stamp_tax.setDecimals(3)
        self.spin_stamp_tax.setSingleStep(0.01)
        self.spin_stamp_tax.setValue(0.1)  # 千1 = 0.1%
        self.spin_stamp_tax.setSuffix(' %')
        form_c.addRow('印花税率（卖出）', self.spin_stamp_tax)
        self.spin_slippage = QDoubleSpinBox()
        self.spin_slippage.setRange(0, 1.0)
        self.spin_slippage.setDecimals(3)
        self.spin_slippage.setSingleStep(0.01)
        self.spin_slippage.setValue(0.02)  # 万2 = 0.02%
        self.spin_slippage.setSuffix(' %')
        form_c.addRow('滑点', self.spin_slippage)
        row_mem_cost = QHBoxLayout()
        row_mem_cost.addWidget(box_mem, 1)
        row_mem_cost.addWidget(box_cost, 1)
        te.addLayout(row_mem_cost)
        # 输出（全宽）
        box_out = QGroupBox('输出')
        form_out = QFormLayout(box_out)
        self.output_dir = QLineEdit('./outputs')
        form_out.addRow('输出目录', self.output_dir)
        te.addWidget(box_out)
        te.addStretch()
        self._tabs.addTab(tab_engine, "引擎与性能")
        # ── Tab4：批量回测（后台直调批量引擎；run_批量回测.py 零改动）──
        self.batch_tab = BatchBacktestTab(
            project_root=self.project_root,
            default_start=self.date_start.date().toString("yyyy-MM-dd"),
            default_end=self.date_end.date().toString("yyyy-MM-dd"),
        )
        self._tabs.addTab(self.batch_tab, "批量回测")
        # ── Tab5：候选交叉池（双策略交叉验证候选池；页内参数 + 页内结果区，不动原面板结构） ──
        self.crosspool_tab = self._build_crosspool_tab()
        self._tabs.addTab(self.crosspool_tab, "候选交叉池")
        # ── Tab6：看板生成（标的直接输入 → 一键回测+生成看板；取代 看板快速生成.py） ──
        self.dashboard_tab = self._build_dashboard_tab()
        self._tabs.addTab(self.dashboard_tab, "看板生成")
        layout.addWidget(self._tabs)
        # ===== 回测进度（方案A：管道 → 面板滚动显示） =====
        self._progress_box = QGroupBox("回测进度")
        v_progress = QVBoxLayout(self._progress_box)
        self.progress_view = QPlainTextEdit()
        self.progress_view.setObjectName("progressView")
        self.progress_view.setReadOnly(True)
        self.progress_view.setMaximumBlockCount(PROGRESS_MAX_BLOCKS)
        self.progress_view.setPlaceholderText(PROGRESS_PLACEHOLDER)
        self.progress_view.setMinimumHeight(PROGRESS_MIN_HEIGHT)
        self.progress_view.setMaximumHeight(PROGRESS_MAX_HEIGHT)
        v_progress.addWidget(self.progress_view)
        layout.addWidget(self._progress_box)
        # ===== 底部按钮 =====
        buttons = QHBoxLayout()
        self.btn_generate = QPushButton("生成配置文件")
        self.btn_run = QPushButton("生成并启动回测")
        self.btn_cancel = QPushButton("取消")
        self.btn_run.setObjectName("btnRun")  # ★ 主按钮渐变高亮
        self.btn_generate.clicked.connect(self._on_generate)
        self.btn_run.clicked.connect(self._on_generate_and_run)
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_generate)
        buttons.addWidget(self.btn_run)
        buttons.addWidget(self.btn_cancel)
        layout.addLayout(buttons)
        # ★ 切到批量回测页时隐藏单次回测的进度面板与操作按钮（避免界面出现两个进度框）
        self._tabs.currentChanged.connect(self._on_tab_changed)
    def _on_tab_changed(self, index: int) -> None:
        """标签页切换：批量回测页隐藏单次回测进度面板与操作按钮，避免双进度框。

        Args:
            index: 当前标签页索引。
        """
        is_batch = index == self._tabs.indexOf(self.batch_tab)
        # ★ 候选交叉池页也隐藏回测进度面板（预览图一致）；底部按钮保留
        is_cross = index == self._tabs.indexOf(self.crosspool_tab)
        self._progress_box.setVisible(not (is_batch or is_cross))
        self.btn_generate.setVisible(not is_batch)
        self.btn_run.setVisible(not is_batch)

    # ── 过滤前缀收集 ──
    def collect_exclude_prefixes(self) -> List[str]:
        """汇总勾选板块 + 自定义前缀，去重排序"""
        prefixes: List[str] = []
        for label, cb in self.filter_checks:
            if cb.isChecked():
                prefixes.extend(PRESET_FILTERS[label])
        # 自定义输入：按逗号/空格/中文逗号拆分
        custom_text = self.custom_prefixes.text().strip()
        for part in custom_text.replace("，", ",").replace(" ", ",").split(","):
            part = part.strip()
            if part:
                prefixes.append(part)
        return sorted(set(prefixes))

    def _refresh_filter_preview(self) -> None:
        prefixes = self.collect_exclude_prefixes()
        if prefixes:
            text = "最终排除前缀： " + "、".join(prefixes)
        else:
            text = "当前不排除任何前缀（全市场）"
        self.filter_preview.setText(text)

    # ── 标的预筛选 ──
    def _on_run_prefilter(self) -> None:
        """点击「预筛选标的」按钮：调用筛选脚本，读取结果并显示"""
        if not self.chk_prefilter.isChecked():
            QMessageBox.warning(
                self, "未启用",
                "请先勾选「启用标的预筛选」，再点击预筛选按钮。"
            )
            return

        start = self.date_start.date().toString("yyyy-MM-dd")
        end = self.date_end.date().toString("yyyy-MM-dd")
        exclude = ",".join(self.collect_exclude_prefixes())

        # 构造命令行参数
        script = self.project_root / "scripts" / "标的预筛选.py"
        if not script.exists():
            QMessageBox.critical(self, "脚本缺失", f"找不到筛选脚本：\n{script}")
            return

        cmd = [
            sys.executable, str(script),
            "--start", start,
            "--end", end,
            "--exclude-prefixes", exclude if exclude else "none",
            "--min-list-years", str(self.spin_list_years.value()),
            "--min-avg-amount", str(int(self.spin_min_amount.value() * 10000)),  # 万→元
            "--min-avg-turnover", str(self.spin_min_turnover.value()),
            "--liquidity-lookback-days", str(self.spin_lookback.value()),
            "--output", "config/stock_pool.yaml",
        ]

        self.btn_prefilter.setEnabled(False)
        self.label_prefilter_result.setText("结果：筛选中，请稍候...")
        QApplication.processEvents()

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8",
                cwd=str(self.project_root), timeout=300,
            )
            if result.returncode != 0:
                self.label_prefilter_result.setText("结果：筛选失败")
                QMessageBox.critical(
                    self, "筛选失败",
                    f"筛选脚本执行失败：\n{result.stderr or result.stdout}"
                )
                return

            # 读取筛选结果
            pool_path = self.project_root / "config" / "stock_pool.yaml"
            if not pool_path.exists():
                self.label_prefilter_result.setText("结果：未生成清单")
                QMessageBox.warning(self, "无结果", "筛选完成但未生成标的清单文件。")
                return

            with open(pool_path, "r", encoding="utf-8") as f:
                pool_data = yaml.safe_load(f)

            self._preset_codes = pool_data.get("stock_pool", {}).get("codes", [])
            self._preset_meta = pool_data.get("filter_meta", {})

            count = len(self._preset_codes)
            preview = "、".join(self._preset_codes[:5])
            if count > 5:
                preview += f" ...等{count}只"
            turnover_ok = pool_data.get("filter_conditions", {}).get("turnover_data_available", True)
            turnover_note = "" if turnover_ok else "（换手率数据缺失，已自动跳过）"

            self.label_prefilter_result.setText(
                f"结果：{count} 只 | {preview} {turnover_note}"
            )
            self.label_prefilter_result.setStyleSheet("color: #2e7d32; font-weight: bold;")

        except subprocess.TimeoutExpired:
            self.label_prefilter_result.setText("结果：超时")
            QMessageBox.critical(self, "超时", "筛选脚本执行超时（>300秒）。")
        except Exception as e:
            self.label_prefilter_result.setText("结果：异常")
            QMessageBox.critical(self, "异常", f"筛选过程出错：\n{e}")
        finally:
            self.btn_prefilter.setEnabled(True)

    # ── 生成配置 ──
    def _gather_values(self) -> Dict:
        values = {
            "start_date": self.date_start.date().toString("yyyy-MM-dd"),
            "end_date": self.date_end.date().toString("yyyy-MM-dd"),
            "initial_capital": self.capital.value(),
            "benchmark": self.benchmark.text().strip() or "000300.SH",
            "strategy_name": self.strategy.currentText(),
            "exclude_prefixes": self.collect_exclude_prefixes(),
            "output_dir": self.output_dir.text().strip() or "./outputs",
            # ★ 策略参数
            "rsrs_period": self.spin_rsrs_period.value(),
            "rsrs_norm_window": self.spin_norm_window.value(),
            "buy_threshold": self.spin_buy_threshold.value(),
            "sell_threshold": self.spin_sell_threshold.value(),
            "take_profit_pct": self.spin_take_profit.value() / 100.0,  # 百分比→比率
            "stop_loss_pct": self.spin_stop_loss.value() / 100.0,      # 百分比→比率
            # ★ 仓位管理
            "max_single_position": self.spin_max_single_pos.value() / 100.0,  # 百分比→比率
            "max_total_positions": self.spin_max_positions.value(),
            "max_total_position_pct": self.spin_max_total_pos.value() / 100.0,  # 百分比→比率
            # ★ 交易成本（百分比→比率）
            "commission_rate": self.spin_commission.value() / 100.0,
            "stamp_tax_rate": self.spin_stamp_tax.value() / 100.0,
            "slippage_value": self.spin_slippage.value() / 100.0,
            # ★ 引擎与性能优化（P2 全套开关）
            "engine_mode": self.combo_engine_mode.currentText(),
            "parallel_enabled": self.chk_parallel.isChecked(),
            "cpu_workers": None if self.spin_cpu_workers.value() == 1 else self.spin_cpu_workers.value(),
            "check_pickle": self.chk_check_pickle.isChecked(),
            "backtest_logging_enabled": self.chk_bt_logging.isChecked(),
            "backtest_logging_level": self.combo_log_level.currentText(),
            "timer_enabled": self.chk_timer.isChecked(),
            "light_guard_enabled": self.chk_light_guard.isChecked(),
            "light_guard_strict": self.chk_light_strict.isChecked(),
            "mc_skip": self.chk_mc_skip.isChecked(),
            "incremental_enabled": self.chk_incremental.isChecked(),
            "incremental_force_full": self.chk_force_full.isChecked(),
            "memory_enabled": self.chk_memory.isChecked(),
            "memory_warning_pct": float(self.spin_mem_warning.value()),
            "memory_critical_pct": float(self.spin_mem_critical.value()),
        }
        # 标的预筛选：启用且已筛选时，把结果传给 build_config_dict
        if self.chk_prefilter.isChecked() and self._preset_codes:
            values["preset_codes"] = self._preset_codes
        else:
            # ★ 未启用预筛选时，自动从数据目录扫描全市场标的
            all_symbols = self._scan_all_symbols()
            if all_symbols:
                values["preset_codes"] = all_symbols
            else:
                values["preset_codes"] = None
        return values

    def _scan_all_symbols(self) -> List[str]:
        """
        从 datasets/processed 目录扫描所有可用标的（parquet/csv 文件）。
        未启用标的预筛选时，自动用全市场标的填充股票池。
        """
        data_dir = self.project_root / "datasets" / "processed"
        if not data_dir.exists():
            return []
        symbols = []
        for f in data_dir.iterdir():
            if f.is_file() and f.suffix.lower() in (".parquet", ".csv"):
                symbols.append(f.stem)  # 文件名即股票代码，如 000001.SZ
        return sorted(symbols)

    def _config_path(self) -> Path:
        return self.project_root / "config" / "backtest_config.yaml"

    def generate_config(self) -> Path:
        """生成 backtest_config.yaml，返回配置路径"""
        import yaml

        values = self._gather_values()
        config = build_config_dict(project_root=self.project_root, **values)
        out_path = self._config_path()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return out_path

    # ── 按钮事件 ──
    def _on_generate(self) -> None:
        try:
            path = self.generate_config()
            QMessageBox.information(
                self,
                "配置已生成",
                f"已生成回测配置：\n{path}\n\n"
                f"排除前缀：{', '.join(self.collect_exclude_prefixes()) or '无'}",
            )
        except Exception as e:
            QMessageBox.critical(self, "生成失败", f"生成配置失败：{e}")

    def _on_generate_and_run(self) -> None:
        try:
            path = self.generate_config()
        except Exception as e:
            QMessageBox.critical(self, "生成失败", f"生成配置失败：{e}")
            return

        # 统计实际使用的标的数量
        values = self._gather_values()
        symbol_count = len(values.get("preset_codes") or [])
        prefilter_enabled = self.chk_prefilter.isChecked() and self._preset_codes
        source = "预筛选结果" if prefilter_enabled else "全市场扫描"

        # ★ 方案A：清空进度面板并写入启动信息（不再新开命令行窗口）
        self._reset_progress_panel()
        self._append_progress(f"生成配置文件：{path.name}")
        self._append_progress(f"标的来源：{source}（{symbol_count} 只）")

        entry = self.project_root / "run_回测入口.py"
        cmd = [sys.executable, str(entry), "--config", str(path)]
        try:
            # 子进程 stdout/stderr 走管道（不新开窗口），由 reader 线程转发到面板
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"  # 保证日志行实时刷新，不积压在缓冲区
            env["PYTHONUTF8"] = "1"        # 强制子进程 UTF-8 输出，避免 GBK 乱码
            popen_kwargs: Dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
                "cwd": str(self.project_root),
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **popen_kwargs)
            self._backtest_proc = proc
            # 监视回测进程，结束后弹提示（成功/失败+报告路径）
            self._backtest_watcher = _ProcWatcher(proc)
            self._backtest_watcher.finished.connect(self._on_backtest_finished)
            self._backtest_watcher.start()
            # 后台线程逐行读 stdout → Signal → 面板滚动区
            threading.Thread(
                target=self._read_backtest_stdout,
                args=(proc,),
                daemon=True,
            ).start()
            self._append_progress("回测进程已启动，以下为实时进度：")
            QMessageBox.information(
                self,
                "回测已启动",
                f"回测已启动，进度将实时显示在窗口下方「回测进度」区域。\n\n"
                f"标的来源：{source}\n"
                f"标的数量：{symbol_count} 只\n\n"
                f"回测完成后会自动弹出结果提示，报告保存在输出目录下。",
            )
        except Exception as e:
            self._append_progress(f"启动失败：{e}")
            QMessageBox.critical(self, "启动失败", f"启动回测失败：{e}")


    # ── 回测完成提示 ──
    def _on_backtest_finished(self, exit_code: int) -> None:
        """回测子进程结束后弹提示（成功含报告路径，失败给退出码）"""
        output_dir = self.project_root / "outputs"
        report_files: List[Path] = []
        if output_dir.exists():
            for pattern in (
                "report_*.pdf", "report_*.html",
                "equity_*.csv", "trades_*.csv", "result_*.json",
            ):
                matches = sorted(
                    output_dir.glob(pattern),
                    key=lambda f: f.stat().st_mtime,
                    reverse=True,
                )
                if matches:
                    report_files.append(matches[0])

        # ★ 方案A：进度面板追加结束状态
        if exit_code == 0:
            self._append_progress("回测正常完成 ✓（详情见弹窗）")
        else:
            self._append_progress(f"回测异常退出（退出码 {exit_code}）")

        if exit_code == 0:
            lines = ["回测已正常完成！", ""]
            if report_files:
                lines.append("本次已生成：")
                for f in report_files[:5]:
                    lines.append(f"  • {f.name}")
                lines.append("")
                lines.append(f"输出目录：{output_dir}")
            else:
                lines.append("未找到报告文件，请查看回测命令行窗口输出。")
            QMessageBox.information(self, "回测完成", "\n".join(lines))
        else:
            QMessageBox.critical(
                self,
                "回测异常",
                f"回测进程异常退出（退出码 {exit_code}）。\n\n"
                f"请查看日志：logs\\myquant_{datetime.now():%Y%m%d}.log\n"
                f"或重新运行并查看「回测进度」面板输出。",
            )

    # ── 回测进度面板（方案A：子进程管道 → 滚动显示） ──
    def _reset_progress_panel(self) -> None:
        """清空进度面板，恢复占位提示"""
        self.progress_view.clear()
        self.progress_view.setPlaceholderText(PROGRESS_PLACEHOLDER)

    def _append_progress(self, text: str) -> None:
        """把一行进度文本追加到进度面板（带时间戳，自动滚动到底部）"""
        ts = datetime.now().strftime("%H:%M:%S")
        self.progress_view.appendPlainText(f"[{ts}] {text}")
        scrollbar = self.progress_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _read_backtest_stdout(self, proc: "subprocess.Popen") -> None:
        """后台线程：逐行读取子进程 stdout，经 Signal 转发到 GUI 线程"""
        try:
            for raw_line in proc.stdout:  # type: ignore[union-attr]
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    continue
                self._progress_bridge.line_received.emit(line)
        except Exception:
            pass  # 进程已结束或管道关闭，静默退出


    # ─────────────────────────────────────────────
    # 候选交叉池（Tab5）：双策略交叉验证
    # ─────────────────────────────────────────────
    def _build_crosspool_tab(self) -> QWidget:
        """构建「候选交叉池」标签页：缓存与输出 + 过滤条件（自定义可调）+ 执行 + 结果区。"""
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setSpacing(8)

        # 左：双策略缓存与输出
        box_cache = QGroupBox("双策略缓存与输出")
        form_cache = QFormLayout(box_cache)
        self.edit_rsrs_cache = QLineEdit("results/batch_backtest/rsrs")
        self.edit_ma_cache = QLineEdit("results/batch_backtest/ma_cross")
        self.edit_cross_out = QLineEdit()
        self.edit_cross_out.setPlaceholderText("留空 = 自动 双强候选池/双强候选池_<时间戳>.csv")
        form_cache.addRow("rsrs 缓存目录", self.edit_rsrs_cache)
        form_cache.addRow("ma_cross 缓存", self.edit_ma_cache)
        form_cache.addRow("输出 CSV 路径", self.edit_cross_out)

        # 右：过滤条件（自定义可调）
        box_filter = QGroupBox("过滤条件（自定义可调）")
        form_filter = QFormLayout(box_filter)
        self.spin_min_sharpe = QDoubleSpinBox()
        self.spin_min_sharpe.setRange(0.0, 5.0)
        self.spin_min_sharpe.setDecimals(2)
        self.spin_min_sharpe.setSingleStep(0.1)
        self.spin_min_sharpe.setValue(0.5)
        form_filter.addRow("最小夏普", self.spin_min_sharpe)
        self.spin_min_trades = QSpinBox()
        self.spin_min_trades.setRange(1, 10000)
        self.spin_min_trades.setValue(10)
        form_filter.addRow("最小交易数", self.spin_min_trades)
        self.spin_max_drawdown = QDoubleSpinBox()
        self.spin_max_drawdown.setRange(0.0, 100.0)
        self.spin_max_drawdown.setDecimals(1)
        self.spin_max_drawdown.setSingleStep(1.0)
        self.spin_max_drawdown.setValue(10.0)
        self.spin_max_drawdown.setSuffix(" %")
        form_filter.addRow("最大回撤", self.spin_max_drawdown)
        self.spin_top_n = QSpinBox()
        self.spin_top_n.setRange(1, 1000)
        self.spin_top_n.setValue(30)
        form_filter.addRow("Top N", self.spin_top_n)

        row = QHBoxLayout()
        row.addWidget(box_cache, 1)
        row.addWidget(box_filter, 1)
        root.addLayout(row)

        # 执行按钮 + 状态
        op = QHBoxLayout()
        self.btn_crosspool_run = QPushButton("▶ 开始交叉验证")
        self.btn_crosspool_run.setObjectName("btnCrossRun")
        self.btn_crosspool_run.clicked.connect(self._on_run_crosspool)
        op.addWidget(self.btn_crosspool_run)
        self.label_crosspool_result = QLabel("结果：未验证")
        self.label_crosspool_result.setStyleSheet("color: #94A3B8;")
        op.addWidget(self.label_crosspool_result)
        op.addStretch()
        root.addLayout(op)

        # 结果区（页内，独立于回测进度面板）
        box_result = QGroupBox("验证结果")
        v_result = QVBoxLayout(box_result)
        self.crosspool_view = QPlainTextEdit()
        self.crosspool_view.setObjectName("crosspoolView")
        self.crosspool_view.setReadOnly(True)
        self.crosspool_view.setMaximumBlockCount(PROGRESS_MAX_BLOCKS)
        self.crosspool_view.setPlaceholderText(
            "点击「开始交叉验证」后，双强候选池（rsrs×ma_cross 交集）将在此显示；"
            "CSV 产物写入输出目录。"
        )
        self.crosspool_view.setMinimumHeight(200)
        v_result.addWidget(self.crosspool_view)
        root.addWidget(box_result, 1)

        note = QLabel(
            "前置条件：需先在「批量回测」页分别运行 rsrs 与 ma_cross 两个策略生成 "
            "results.json 缓存；缓存缺失时点击将弹窗提示。"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #64748B; font-size: 11px;")
        root.addWidget(note)
        return tab

    def _on_run_crosspool(self) -> None:
        """点击「开始交叉验证」：检查缓存 → 子进程调脚本 → 结果区显示。"""
        if self._crosspool_proc is not None:
            return
        rsrs_text = self.edit_rsrs_cache.text().strip() or "results/batch_backtest/rsrs"
        ma_text = self.edit_ma_cache.text().strip() or "results/batch_backtest/ma_cross"
        rsrs_dir = self.project_root / rsrs_text
        ma_dir = self.project_root / ma_text

        # 缓存完整性检查
        missing = []
        if not (rsrs_dir / "results.json").exists():
            missing.append(str(rsrs_dir))
        if not (ma_dir / "results.json").exists():
            missing.append(str(ma_dir))
        if missing:
            QMessageBox.warning(
                self,
                "缓存缺失",
                "以下策略缓存不存在（results.json）：\n\n"
                + "\n".join(f"  • {m}" for m in missing)
                + "\n\n请先在「批量回测」页分别运行 rsrs 与 ma_cross 两个策略生成缓存。",
            )
            return

        script = self.project_root / "scripts" / "候选池交叉验证.py"
        if not script.exists():
            QMessageBox.critical(self, "脚本缺失", f"找不到交叉验证脚本：\n{script}")
            return

        cmd = [
            sys.executable, str(script),
            "--rsrs-cache", rsrs_text,
            "--ma-cache", ma_text,
            "--min-sharpe", str(self.spin_min_sharpe.value()),
            "--min-trades", str(self.spin_min_trades.value()),
            "--max-drawdown", str(round(self.spin_max_drawdown.value() / 100.0, 4)),
            "--top-n", str(self.spin_top_n.value()),
        ]
        out_text = self.edit_cross_out.text().strip()
        if out_text:
            cmd += ["--out", out_text]

        # 清空结果区并写入启动信息
        self.crosspool_view.clear()
        self._crosspool_lines.clear()
        self._append_crosspool_output("候选池交叉验证已启动（子进程 scripts/候选池交叉验证.py）")
        self._append_crosspool_output(
            f"过滤: 夏普≥{self.spin_min_sharpe.value()} | 交易≥{self.spin_min_trades.value()} "
            f"| 回撤≤{self.spin_max_drawdown.value()}% | Top N: {self.spin_top_n.value()}"
        )
        self.btn_crosspool_run.setEnabled(False)
        self.label_crosspool_result.setText("结果：验证中，请稍候...")
        self.label_crosspool_result.setStyleSheet("color: #FDE68A;")
        QApplication.processEvents()

        try:
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONUTF8"] = "1"
            popen_kwargs: Dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
                "cwd": str(self.project_root),
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **popen_kwargs)
            self._crosspool_proc = proc
            # 完成监视 + stdout 逐行转发
            watcher = _ProcWatcher(proc)
            watcher.finished.connect(self._on_crosspool_finished)
            watcher.start()
            threading.Thread(
                target=self._read_crosspool_stdout,
                args=(proc,),
                daemon=True,
            ).start()
        except Exception as e:
            self._append_crosspool_output(f"启动失败：{e}")
            self.label_crosspool_result.setText("结果：异常")
            self.label_crosspool_result.setStyleSheet("color: #F87171;")
            self.btn_crosspool_run.setEnabled(True)
            self._crosspool_proc = None
            QMessageBox.critical(self, "启动失败", f"启动交叉验证失败：{e}")

    def _read_crosspool_stdout(self, proc: "subprocess.Popen") -> None:
        """后台线程：逐行读取交叉验证子进程 stdout，经 Signal 转发到 GUI 线程。"""
        try:
            for raw_line in proc.stdout:  # type: ignore[union-attr]
                line = raw_line.rstrip("\r\n")
                if not line.strip():
                    continue
                self._crosspool_bridge.line_received.emit(line)
        except Exception:
            pass  # 进程已结束或管道关闭，静默退出

    def _append_crosspool_output(self, text: str) -> None:
        """把一行文本追加到候选交叉池结果区（带时间戳，自动滚动到底部）。"""
        ts = datetime.now().strftime("%H:%M:%S")
        self._crosspool_lines.append(text)
        self.crosspool_view.appendPlainText(f"[{ts}] {text}")
        scrollbar = self.crosspool_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_crosspool_finished(self, exit_code: int) -> None:
        """交叉验证子进程结束后：恢复按钮，解析输出并弹窗（含 CSV 路径与打开按钮）。"""
        self._crosspool_proc = None
        self.btn_crosspool_run.setEnabled(True)
        if exit_code == 0:
            self.label_crosspool_result.setText("结果：完成")
            self.label_crosspool_result.setStyleSheet("color: #6EE7B7; font-weight: bold;")
            # 解析输出行
            pool_line = next((ln for ln in self._crosspool_lines if "双强候选池:" in ln), "")
            csv_line = next((ln for ln in self._crosspool_lines if ln.strip().startswith("产物:")), "")
            csv_path = csv_line.split("产物:", 1)[1].strip() if csv_line else ""
            report_line = next((ln for ln in self._crosspool_lines if ln.strip().startswith("报告文档:")), "")
            report_text = report_line.split("报告文档:", 1)[1].strip() if report_line else ""
            detail = pool_line.strip() if pool_line else "交叉验证完成，详见结果区。"
            box = QMessageBox(self)
            box.setWindowTitle("候选池交叉验证完成")
            box.setIcon(QMessageBox.Information)
            report_note = f"\n报告文档：{report_text}" if report_text else ""
            box.setText(f"✓ {detail}\n\n产物：{csv_path or '（未解析到路径，详见结果区）'}{report_note}")
            open_btn = box.addButton("打开 CSV 目录", QMessageBox.AcceptRole)
            box.addButton("关闭", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is open_btn and csv_path:
                csv_file = Path(csv_path)
                target = csv_file if csv_file.is_dir() else csv_file.parent
                if target.exists():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        else:
            self.label_crosspool_result.setText("结果：失败")
            self.label_crosspool_result.setStyleSheet("color: #F87171;")
            QMessageBox.critical(
                self,
                "交叉验证失败",
                f"候选池交叉验证异常退出（退出码 {exit_code}），详见结果区。",
            )


    # ─────────────────────────────────────────────
    # 看板生成（Tab6）：标的直接输入 → 一键回测+生成看板
    # ─────────────────────────────────────────────
    def _build_dashboard_tab(self) -> QWidget:
        """构建「看板生成」标签页：标的直接输入 + 区间/策略 + 看板输出与打开。"""
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setSpacing(8)

        # 左：标的与回测区间
        box_symbols = QGroupBox("标的与回测区间")
        form_s = QFormLayout(box_symbols)
        self.dash_symbols = QLineEdit()
        self.dash_symbols.setPlaceholderText("600519.SH,000001.SZ（逗号分隔多只）")
        self.dash_symbols.setClearButtonEnabled(True)
        form_s.addRow("标的代码", self.dash_symbols)
        hint_symbols = QLabel("直接输入股票代码，支持多只、逗号分隔；不再从标的列表选择。")
        hint_symbols.setWordWrap(True)
        hint_symbols.setStyleSheet("color: #64748B; font-size: 11px;")
        form_s.addRow("", hint_symbols)
        self.dash_start = QDateEdit(QDate(2024, 1, 2))
        self.dash_start.setCalendarPopup(True)
        form_s.addRow("开始日期", self.dash_start)
        self.dash_end = QDateEdit(QDate.currentDate())
        self.dash_end.setCalendarPopup(True)
        form_s.addRow("结束日期", self.dash_end)
        hint_end = QLabel("默认 = 当天，可手动修改。")
        hint_end.setWordWrap(True)
        hint_end.setStyleSheet("color: #64748B; font-size: 11px;")
        form_s.addRow("", hint_end)
        self.dash_strategy = QComboBox()
        self.dash_strategy.addItems(load_registered_strategies())
        rsrs_index = self.dash_strategy.findText("rsrs")
        if rsrs_index >= 0:
            self.dash_strategy.setCurrentIndex(rsrs_index)
        form_s.addRow("策略", self.dash_strategy)

        # 右：看板输出
        box_out = QGroupBox("看板输出")
        form_o = QFormLayout(box_out)
        self.dash_output_dir = QLineEdit("./outputs")
        form_o.addRow("输出目录", self.dash_output_dir)
        self.dash_keep = QSpinBox()
        self.dash_keep.setRange(0, 100)
        self.dash_keep.setValue(3)
        self.dash_keep.setSuffix(" 个")
        self.dash_keep.setSpecialValueText("不清理")
        form_o.addRow("保留看板数", self.dash_keep)
        hint_name = QLabel("看板文件名：回测看板_<代码>_<起止日期>.html")
        hint_name.setStyleSheet("color: #64748B; font-size: 11px;")
        form_o.addRow("", hint_name)
        hint_clean = QLabel("生成成功后自动清理旧看板，仅保留最新 N 个。")
        hint_clean.setStyleSheet("color: #64748B; font-size: 11px;")
        form_o.addRow("", hint_clean)
        row_btns = QHBoxLayout()
        self.btn_dash_generate = QPushButton("生成看板")
        self.btn_dash_generate.setObjectName("btnRun")
        self.btn_dash_generate.clicked.connect(self._on_dash_generate)
        row_btns.addWidget(self.btn_dash_generate)
        self.btn_dash_open = QPushButton("打开看板")
        self.btn_dash_open.setToolTip("在浏览器中打开最近生成的看板 HTML")
        self.btn_dash_open.clicked.connect(self._on_dash_open)
        row_btns.addWidget(self.btn_dash_open)
        row_btns.addStretch()
        form_o.addRow(row_btns)
        self.label_dash_status = QLabel("状态：未生成")
        self.label_dash_status.setStyleSheet("color: #94A3B8;")
        form_o.addRow("", self.label_dash_status)
        hint_open = QLabel("「打开看板」= 在浏览器中打开最近生成的看板 HTML。")
        hint_open.setStyleSheet("color: #64748B; font-size: 11px;")
        form_o.addRow("", hint_open)

        row = QHBoxLayout()
        row.addWidget(box_symbols, 1)
        row.addWidget(box_out, 1)
        root.addLayout(row)
        root.addStretch()
        return tab

    def _dashboard_output_path(self) -> str:
        """构造本次看板输出路径：<输出目录>/回测看板_<代码>_<起止日期>.html。"""
        start = self.dash_start.date().toString("yyyy-MM-dd")
        end = self.dash_end.date().toString("yyyy-MM-dd")
        symbols = self.dash_symbols.text().strip()
        out_dir = self.dash_output_dir.text().strip() or "./outputs"
        date_stamp = f"{start.replace('-', '')}_{end.replace('-', '')}"
        return os.path.join(out_dir, f"回测看板_{symbols}_{date_stamp}.html")

    def _latest_dashboard(self, output_dir: str) -> Optional[Path]:
        """返回输出目录中最新生成的看板文件（按修改时间倒序）。"""
        out = Path(output_dir)
        if not out.is_dir():
            return None
        matches = sorted(
            out.glob("回测看板_*.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    def _open_dashboard_file(self, path: str) -> None:
        """用系统默认浏览器打开看板 HTML；文件不存在时弹提示。"""
        p = Path(path)
        if not p.exists():
            QMessageBox.warning(self, "文件不存在", f"看板文件不存在：\n{p}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _cleanup_old_dashboards(self, output_dir: str, keep: int = 3) -> Tuple[List[str], List[str]]:
        """清理输出目录中的旧看板，仅保留最新 keep 个。

        匹配 回测看板_*.html（本页生成的看板文件），按文件修改时间从新到旧排序，
        删除最旧的超出部分；只清理看板文件，不影响 result_*.json 等其它产物。

        Args:
            output_dir: 看板输出目录。
            keep: 保留的最新文件数量；<=0 表示不清理。

        Returns:
            (deleted, kept): 删除的文件列表与保留的文件列表。
        """
        if keep <= 0:
            return [], []
        out_dir = Path(output_dir)
        if not out_dir.is_dir():
            return [], []
        dashboards = sorted(
            out_dir.glob("回测看板_*.html"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if len(dashboards) <= keep:
            return [], [str(p) for p in dashboards]
        to_delete = dashboards[keep:]
        deleted: List[str] = []
        for p in to_delete:
            try:
                p.unlink()
                deleted.append(str(p))
            except OSError as e:
                self._append_progress(f"[警告] 删除旧看板失败: {p} ({e})")
        kept = [str(p) for p in dashboards[:keep]]
        return deleted, kept

    def _on_dash_generate(self) -> None:
        """点击「生成看板」：校验输入 → 生成配置 → 子进程跑回测+渲染看板。"""
        symbols = self.dash_symbols.text().strip()
        if not symbols:
            QMessageBox.warning(self, "标的为空", "请先在「标的代码」输入股票代码（可逗号分隔多只）。")
            return
        if self._dash_proc is not None:
            self._append_progress("已有看板生成任务在运行，请等待其完成。")
            return
        # 生成回测配置（与主流程共用窗口当前设置），再以命令行参数覆盖标的/区间/策略
        try:
            config_path = self.generate_config()
        except Exception as e:
            QMessageBox.critical(self, "生成失败", f"生成配置文件失败：{e}")
            return

        start = self.dash_start.date().toString("yyyy-MM-dd")
        end = self.dash_end.date().toString("yyyy-MM-dd")
        strategy = self.dash_strategy.currentText()
        out_path = self._dashboard_output_path()
        self._dash_out_path = out_path

        self._reset_progress_panel()
        self._append_progress(f"生成配置文件：{config_path.name}")
        self._append_progress(f"标的：{symbols} | 区间：{start} ~ {end} | 策略：{strategy}")
        self._append_progress(f"看板输出：{out_path}")
        self.label_dash_status.setText("状态：生成中...")
        self.label_dash_status.setStyleSheet("color: #FDE68A;")

        entry = self.project_root / "run_回测入口.py"
        cmd = [
            sys.executable, str(entry),
            "--config", str(config_path),
            "--symbols", symbols,
            "--start", start,
            "--end", end,
            "--strategy", strategy,
            "--dashboard", out_path,
            "--no-report",
        ]
        try:
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONUTF8"] = "1"
            popen_kwargs: Dict = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "env": env,
                "cwd": str(self.project_root),
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            proc = subprocess.Popen(cmd, **popen_kwargs)
            self._dash_proc = proc
            watcher = _ProcWatcher(proc)
            watcher.finished.connect(self._on_dash_finished)
            watcher.start()
            threading.Thread(
                target=self._read_backtest_stdout,
                args=(proc,),
                daemon=True,
            ).start()
            self._append_progress("回测进程已启动，以下为实时进度：")
        except Exception as e:
            self._dash_proc = None
            self.label_dash_status.setText("状态：启动失败")
            self.label_dash_status.setStyleSheet("color: #F87171;")
            QMessageBox.critical(self, "启动失败", f"启动回测失败：{e}")

    def _on_dash_finished(self, exit_code: int) -> None:
        """看板生成子进程结束后：成功则清理旧看板并弹窗（可打开看板），失败提示。"""
        self._dash_proc = None
        out_dir = self.dash_output_dir.text().strip() or "./outputs"
        if exit_code == 0:
            keep = self.dash_keep.value()
            deleted, kept = self._cleanup_old_dashboards(out_dir, keep)
            if deleted:
                self._append_progress(f"已清理 {len(deleted)} 个旧看板，保留最新 {len(kept)} 个")
            dashboard_path = self._dash_out_path or self._dashboard_output_path()
            self.label_dash_status.setText("状态：成功")
            self.label_dash_status.setStyleSheet("color: #6EE7B7; font-weight: bold;")
            self._append_progress(f"[成功] 看板已生成: {dashboard_path}")
            box = QMessageBox(self)
            box.setWindowTitle("看板生成完成")
            box.setIcon(QMessageBox.Information)
            text = f"看板已生成：\n{dashboard_path}"
            if deleted:
                text += f"\n\n已清理 {len(deleted)} 个旧看板，保留最新 {len(kept)} 个。"
            box.setText(text)
            open_btn = box.addButton("打开看板", QMessageBox.AcceptRole)
            box.addButton("关闭", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is open_btn:
                self._open_dashboard_file(dashboard_path)
        else:
            self.label_dash_status.setText("状态：生成失败")
            self.label_dash_status.setStyleSheet("color: #F87171;")
            QMessageBox.critical(
                self,
                "看板生成失败",
                f"回测进程异常退出（退出码 {exit_code}），详见「回测进度」面板。",
            )

    def _on_dash_open(self) -> None:
        """点击「打开看板」：在浏览器中打开最近生成的看板 HTML。"""
        out_dir = self.dash_output_dir.text().strip() or "./outputs"
        latest = self._latest_dashboard(out_dir)
        if latest is None:
            QMessageBox.warning(
                self,
                "未找到看板",
                f"输出目录中未找到「回测看板_*.html」：\n{out_dir}\n\n请先生成看板。",
            )
            return
        self._open_dashboard_file(str(latest))


def run_dialog(project_root: Path) -> int:
    """启动配置窗口（供入口脚本调用）"""
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = BacktestConfigDialog(project_root)
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    root = find_project_root()
    sys.exit(run_dialog(root))
