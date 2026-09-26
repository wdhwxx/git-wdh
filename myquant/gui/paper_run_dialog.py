# -*- coding: utf-8 -*-
# 文件名：myquant/gui/paper_run_dialog.py
"""
鲲鹏仿真运测 - Paper 仿真运行控制台（#62 / #64 / #65）
=====================================================
启动后直接拉起 run_实盘入口.py --mode paper --duration 0，
在本窗口内实时显示 bar/信号/订单/成交/盈亏，关闭窗口优雅停止引擎。

设计原则：
  - 窗口本身不写回测/实盘业务逻辑，只负责「拉起子进程 + 轮询状态展示」。
  - 视觉风格对齐 backtest_config_dialog（深紫渐变 + 霓虹青紫 + 自绘标题栏）。
  - 盈亏颜色按 A 股习惯：红涨 #F87171、绿跌 #34D399。

依赖: PySide6（可选依赖）。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from myquant.core.logger import get_logger

logger = get_logger(__name__)

from PySide6.QtCore import QObject, QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────
LIVE_STATUS_FILENAME = "live_status.json"
POLL_INTERVAL_MS = 2000
INITIAL_CAPITAL = 2_000_000  # 与 create_live_engine 注入一致（#60）

# logo 路径（与本文件同目录）
_LOGO_PATH = Path(__file__).resolve().parent / "kunpeng_logo.png"


# ─────────────────────────────────────────────
# 子进程 stdout 读取线程（跨线程安全转发）
# ─────────────────────────────────────────────
class _StdoutReader(QObject):
    """后台线程逐行读取子进程 stdout，通过 Signal 转发到 GUI 线程。"""

    line_received = Signal(str)

    def __init__(self, proc: subprocess.Popen) -> None:
        super().__init__()
        self._proc = proc

    def run(self) -> None:
        """逐行读取 stdout，EOF 后退出。"""
        try:
            assert self._proc.stdout is not None
            for raw in iter(self._proc.stdout.readline, ""):
                line = raw.rstrip("\r\n")
                if line:
                    self.line_received.emit(line)
        except Exception:
            pass


# ─────────────────────────────────────────────
# 主弹窗
# ─────────────────────────────────────────────
class PaperRunDialog(QDialog):
    """鲲鹏仿真运测 - Paper 仿真运行控制台。

    Args:
        project_root: 项目根目录（用于定位 run_实盘入口.py 与 outputs/）。
        parent: 父控件。
    """

    def __init__(self, project_root: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[QThread] = None
        self._reader: Optional[_StdoutReader] = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_live_status)
        self._drag_offset: Optional[Any] = None

        self.setWindowTitle("鲲鹏仿真运测")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setMinimumWidth(860)
        self._apply_style()
        self._build_ui()
        self._update_panel_state()
        self._center_on_screen()

    def _apply_style(self) -> None:
        """深紫渐变 + 霓虹青紫（对齐主对话框）。"""
        self.setStyleSheet("""
            QDialog {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0B1120, stop:1 #1E1B4B);
            }
            QLabel { color: #C7D2FE; font-size: 12px; background: transparent; }
            QPushButton {
                border-radius: 6px; padding: 6px 18px; font-size: 12px; font-weight: 600;
            }
            QPushButton#btnPrimary { background-color: #22D3EE; color: #0B1120; border: none; }
            QPushButton#btnPrimary:hover { background-color: #67E8F9; }
            QPushButton#btnStop { background-color: #EF4444; color: #fff; border: none; }
            QPushButton#btnStop:hover { background-color: #F87171; }
            QPushButton#btnCancel {
                background-color: transparent; color: #94A3B8; border: 1px solid #4F46E5;
            }
            QPushButton#btnCancel:hover { background-color: rgba(79,70,229,0.2); }
            QPlainTextEdit {
                background-color: #0F172A; border: 1px solid #1E293B;
                border-radius: 8px; color: #94A3B8;
                font-family: Consolas, monospace; font-size: 14px; padding: 6px;
            }
            QToolButton#titleMin:hover {
                background-color: rgba(34,211,238,0.2); color: #22D3EE;
            }
            QToolButton#titleClose:hover {
                background-color: rgba(239,68,68,0.25); color: #F87171;
            }
        """)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 10)
        layout.setSpacing(8)

        # ── 自绘标题栏 ──
        title_bar = QWidget()
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(4, 0, 4, 0)
        tb.setSpacing(8)

        # 绿色呼吸灯
        self._led = QLabel("●")
        self._led.setStyleSheet("color: #10B981; font-size: 12px;")
        tb.addWidget(self._led)

        self.title_label = QLabel("鲲鹏仿真运测")
        self.title_label.setStyleSheet("color: #22D3EE; font-size: 19px; font-weight: 700;")
        glow = QGraphicsDropShadowEffect(self.title_label)
        glow.setBlurRadius(14)
        glow.setColor(QColor(34, 211, 238, 150))
        glow.setOffset(0, 0)
        self.title_label.setGraphicsEffect(glow)
        tb.addWidget(self.title_label)
        tb.addStretch()

        # 最小化
        self.btn_title_min = QToolButton()
        self.btn_title_min.setText("—")
        self.btn_title_min.setObjectName("titleMin")
        self.btn_title_min.setFixedSize(26, 22)
        self.btn_title_min.setToolTip("最小化")
        self.btn_title_min.clicked.connect(self.showMinimized)
        tb.addWidget(self.btn_title_min)

        # 关闭
        self.btn_title_close = QToolButton()
        self.btn_title_close.setText("✕")
        self.btn_title_close.setObjectName("titleClose")
        self.btn_title_close.setFixedSize(26, 22)
        self.btn_title_close.setToolTip("关闭")
        self.btn_title_close.clicked.connect(self.close)
        tb.addWidget(self.btn_title_close)
        layout.addWidget(title_bar)

        # ── 引擎状态条（logo + 名称 + 运行中绿标）──
        engine_bar = QFrame()
        engine_bar.setStyleSheet(
            "background-color: rgba(34,211,238,0.06);"
            "border: 1px solid rgba(34,211,238,0.25); border-radius: 8px;"
        )
        eb = QHBoxLayout(engine_bar)
        eb.setContentsMargins(10, 8, 10, 8)
        eb.setSpacing(10)

        # logo
        self._logo_label = QLabel()
        self._logo_label.setFixedSize(49, 49)
        self._logo_label.setAlignment(Qt.AlignCenter)
        if _LOGO_PATH.exists():
            pix = QPixmap(str(_LOGO_PATH))
            self._logo_label.setPixmap(pix.scaled(49, 49, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        eb.addWidget(self._logo_label)

        # 名称 + 子状态
        info_box = QVBoxLayout()
        info_box.setSpacing(2)
        self._engine_name = QLabel("实盘仿真运测")
        self._engine_name.setStyleSheet("color: #E0E7FF; font-size: 17px; font-weight: 600;")
        self._engine_sub = QLabel("paper · duration=0 · tdx本地")
        self._engine_sub.setStyleSheet("color: #64748B; font-size: 12px;")
        info_box.addWidget(self._engine_name)
        info_box.addWidget(self._engine_sub)
        eb.addLayout(info_box)
        eb.addStretch()

        # 运行中 badge
        self._status_badge = QLabel("● 未启动")
        self._status_badge.setStyleSheet(
            "padding: 6px 14px; border-radius: 12px; font-size: 14px; font-weight: 600;"
            "color: #FBBF24; background-color: rgba(251,191,36,0.1);"
            "border: 1px solid rgba(251,191,36,0.3);"
        )
        eb.addWidget(self._status_badge)
        layout.addWidget(engine_bar)

        # ── 6 指标卡一行 ──
        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self._stat_labels: Dict[str, QLabel] = {}
        stat_defs = [
            ("bar", "当前Bar"),
            ("signal", "信号买/卖"),
            ("orders", "待成交订单"),
            ("trades", "今日成交"),
            ("day_pnl", "当日盈亏"),
            ("total_pnl", "总盈亏"),
        ]
        for key, label_text in stat_defs:
            card = QFrame()
            card.setStyleSheet("background-color: #0F172A; border: 1px solid #1E293B; border-radius: 8px;")
            cv = QVBoxLayout(card)
            cv.setContentsMargins(8, 16, 8, 16)
            cv.setSpacing(9)
            lab = QLabel(label_text)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("color: #64748B; font-size: 19px;")
            val = QLabel("--")
            val.setAlignment(Qt.AlignCenter)
            val.setTextFormat(Qt.RichText)
            val.setStyleSheet(
                "color: #E0E7FF; font-size: 17px; font-weight: 700;"
                "font-family: Consolas, monospace;"
            )
            cv.addWidget(lab)
            cv.addWidget(val)
            stats_row.addWidget(card)
            self._stat_labels[key] = val
        layout.addLayout(stats_row)

        # ── 日志区（占满剩余高度）──
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlainText("[GUI] 点击「启动常驻引擎」开始 Paper 仿真运行\n")
        layout.addWidget(self._log, stretch=1)

        # ── 底部按钮 ──
        footer = QHBoxLayout()
        self._foot_hint = QLabel("数据源：本地 processed(tdx) · 结束后自动导出 JSON+HTML+PDF+CSV")
        self._foot_hint.setStyleSheet("color: #475569; font-size: 10px;")
        footer.addWidget(self._foot_hint)
        footer.addStretch()
        self.btn_cancel = QPushButton("关闭")
        self.btn_cancel.setObjectName("btnCancel")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_primary = QPushButton("启动常驻引擎")
        self.btn_primary.setObjectName("btnPrimary")
        self.btn_primary.clicked.connect(self._on_primary_clicked)
        footer.addWidget(self.btn_cancel)
        footer.addWidget(self.btn_primary)
        layout.addLayout(footer)

    # 标题栏拖动
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and event.position().y() <= 32:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _center_on_screen(self) -> None:
        screen = self.screen().availableGeometry() if self.screen() else QRect(0, 0, 1920, 1080)
        self.resize(860, 520)
        self.move(screen.center() - self.rect().center())

    def _update_panel_state(self) -> None:
        """根据引擎运行状态更新 badge 与按钮文案。"""
        if self._proc is None:
            self._status_badge.setText("● 未启动")
            self._status_badge.setStyleSheet(
                "padding: 6px 14px; border-radius: 12px; font-size: 14px; font-weight: 600;"
                "color: #FBBF24; background-color: rgba(251,191,36,0.1);"
                "border: 1px solid rgba(251,191,36,0.3);"
            )
            self.btn_primary.setText("启动常驻引擎")
            self.btn_primary.setObjectName("btnPrimary")
            self._led.setStyleSheet("color: #FBBF24; font-size: 12px;")
        else:
            self._status_badge.setText("● 运行中")
            self._status_badge.setStyleSheet(
                "padding: 6px 14px; border-radius: 12px; font-size: 14px; font-weight: 600;"
                "color: #10B981; background-color: rgba(16,185,129,0.1);"
                "border: 1px solid rgba(16,185,129,0.3);"
            )
            self.btn_primary.setText("停止引擎")
            self.btn_primary.setObjectName("btnStop")
            self._led.setStyleSheet("color: #10B981; font-size: 12px;")
        self.btn_primary.style().unpolish(self.btn_primary)
        self.btn_primary.style().polish(self.btn_primary)

    def _on_primary_clicked(self) -> None:
        if self._proc is None:
            self._start_engine()
        else:
            self._stop_engine()

    def _start_engine(self) -> None:
        """subprocess 启动 run_实盘入口.py --mode paper --duration 0。"""
        entry = self._project_root / "run_实盘入口.py"
        if not entry.exists():
            self._append_log(f"[错误] 找不到入口脚本: {entry}")
            return
        stats_path = f"outputs/paper_stats/paper_{datetime.now().strftime('%Y%m%d')}.json"
        cmd = [
            sys.executable, str(entry),
            "--mode", "paper",
            "--duration", "0",
            "--stats-json", stats_path,
        ]
        self._append_log(f"[GUI] 启动子进程: {' '.join(cmd)}")
        self._append_log(f"[GUI] 结束后自动导出: JSON + HTML + PDF + CSV → outputs/paper_stats/")
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(self._project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as e:
            self._append_log(f"[错误] 子进程启动失败: {e}")
            self._proc = None
            return

        self._reader = _StdoutReader(self._proc)
        self._reader_thread = QThread(self)
        self._reader.moveToThread(self._reader_thread)
        self._reader.line_received.connect(self._append_log)
        self._reader_thread.started.connect(self._reader.run)
        self._reader_thread.start()
        self._timer.start()
        self._update_panel_state()

    def _stop_engine(self) -> None:
        """优雅停止子进程。"""
        if self._proc is None:
            return
        self._append_log("[GUI] 发送停止信号，等待引擎优雅退出...")
        try:
            if sys.platform == "win32":
                os.kill(self._proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._append_log("[GUI] 子进程未在 10s 内退出，强制 terminate")
                self._proc.terminate()
        except Exception as e:
            self._append_log(f"[GUI] 停止信号发送异常: {e}")
            try:
                self._proc.kill()
            except Exception:
                pass
        self._cleanup_proc()
        self._update_panel_state()

    def _cleanup_proc(self) -> None:
        self._timer.stop()
        if self._reader_thread is not None:
            self._reader_thread.quit()
            self._reader_thread.wait(2000)
            self._reader_thread = None
        self._reader = None
        self._proc = None

    def _poll_live_status(self) -> None:
        """每 2 秒读取 live_status.json 更新指标卡。"""
        live_path = self._project_root / "outputs" / "paper_stats" / LIVE_STATUS_FILENAME
        if not live_path.exists():
            return
        try:
            with open(live_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        st = data.get("stats", {})
        self._stat_labels["bar"].setText(str(st.get("total_bars", 0)))
        buy = st.get("buy_signals", 0)
        sell = st.get("sell_signals", 0)
        self._stat_labels["signal"].setText(
            f'<span style="color:#F87171">{buy}</span>/'
            f'<span style="color:#34D399">{sell}</span>'
        )
        self._stat_labels["orders"].setText(str(st.get("total_orders", 0)))
        self._stat_labels["trades"].setText(str(st.get("total_trades", 0)))

        total_assets = float(data.get("total_assets", 0) or 0)
        day_start = float(data.get("day_start_assets", 0) or 0)
        initial = float(data.get("initial_capital", INITIAL_CAPITAL))
        total_pnl = total_assets - initial
        day_pnl = total_assets - day_start if day_start > 0 else 0.0
        self._set_pnl_label("day_pnl", day_pnl)
        self._set_pnl_label("total_pnl", total_pnl)

    def _set_pnl_label(self, key: str, value: float) -> None:
        label = self._stat_labels[key]
        if value > 0:
            color = "#F87171"
            sign = "+"
        elif value < 0:
            color = "#34D399"
            sign = ""
        else:
            color = "#C7D2FE"
            sign = ""
        label.setText(f"{sign}{value:,.0f}")
        label.setStyleSheet(
            f"color: {color}; font-size: 17px; font-weight: 700;"
            "font-family: Consolas, monospace;"
        )

    def _append_log(self, text: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.appendPlainText(f"[{ts}] {text}")

    def closeEvent(self, event: Any) -> None:
        if self._proc is not None:
            self._stop_engine()
        super().closeEvent(event)


def run_paper_dialog(project_root: Path) -> int:
    """启动鲲鹏仿真运测控制台。

    Args:
        project_root: 项目根目录。

    Returns:
        QDialog.exec() 返回值。
    """
    app = QApplication.instance() or QApplication(sys.argv)
    dlg = PaperRunDialog(project_root)
    return dlg.exec()
