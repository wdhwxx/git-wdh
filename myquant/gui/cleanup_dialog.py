# -*- coding: utf-8 -*-
# 文件名：myquant/gui/cleanup_dialog.py
"""
项目清理对话框（方案一 · 上下分区）
==================================
在清理服务（myquant/utils/cleanup_service.py）之上提供图形化清理弹窗：
勾选要清理的项目、设置回测输出保留期数、选择执行模式，
下半部滚动区实时展示 dry-run 预览 / 执行结果。

布局（方案一 · 上下分区）：
  1. 清理项目分组：A 测试残留 / B 测试备份 / C Python 缓存 /
                    D 轮转日志 / E 旧格式 CSV 子目录（勾选网格）
  2. 回测输出分组：启用开关 + 保留最近 N 期（N ≥ 1）
  3. 执行模式：仅预览（默认） / 直接清理（单选）
  4. 保护清单只读提示（quant.db / processed / 报告族 / .gitkeep / 代码备份）
  5. 预览结果滚动区（QPlainTextEdit，只读，自动滚底）
  6. 底部按钮：预览 / 执行清理 / 取消

安全设计：
  - 默认仅预览：扫描只收集计划，不删除任何文件。
  - 「执行清理」仅在「直接清理」模式下可用，且执行前弹确认框。
  - 扫描与删除均在后台线程执行，不阻塞 GUI。

设计原则（蓝图纪律）：窗口只做「收集选项 + 展示结果」，业务逻辑全部在
myquant/utils/cleanup_service.py。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from myquant.utils.cleanup_service import (
    CATEGORY_NAMES,
    CleanupOptions,
    CleanupPlan,
    execute_plan,
    format_plan_summary,
    run_cleanup_scan,
)

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 滚动区最大保留行数（防内存膨胀，超出自动丢弃最旧行）
PROGRESS_MAX_BLOCKS: int = 5000
# 滚动区最小/最大高度（保持窗口观感，不随内容撑开）
PROGRESS_MIN_HEIGHT: int = 150
PROGRESS_MAX_HEIGHT: int = 260
# 保留期数输入范围
KEEP_MIN: int = 1
KEEP_MAX: int = 10
# 滚动区占位提示
PROGRESS_PLACEHOLDER: str = (
    "点击「预览」扫描全部清理项（DRY-RUN，不删除）；"
    "或勾选「直接清理」后点击「执行清理」。"
)
# 保护清单提示（只读展示）
PROTECT_HINT: str = (
    "自动跳过（永不删除）：quant.db / datasets/processed / 保留期内的报告族 / "
    "批量回测 fingerprints.json 与 results.json 增量缓存 / "
    "所有 .gitkeep / backups 代码备份（*.bak_*.py）"
)


# ─────────────────────────────────────────────
# 跨线程桥
# ─────────────────────────────────────────────

class _CleanupBridge(QObject):
    """后台扫描/执行线程 → GUI 线程的信号桥（跨线程安全）。

    Attributes:
        scan_done: 扫描完成，携带 CleanupPlan。
        execute_done: 删除完成，携带 (plan, 成功数, 失败数, 释放字节)。
        failed: 执行失败，携带错误信息字符串。
    """
    scan_done = Signal(object)
    execute_done = Signal(object, int, int, int)
    failed = Signal(str)


# ─────────────────────────────────────────────
# 主对话框
# ─────────────────────────────────────────────

class CleanupDialog(QDialog):
    """项目清理对话框（方案一 · 上下分区）。"""

    def __init__(self, project_root: Path, parent=None) -> None:
        """初始化对话框。

        Args:
            project_root: 项目根目录（绝对路径）。
            parent: 父窗口（默认 None）。
        """
        super().__init__(parent)
        self.project_root = project_root
        # 后台任务桥（扫描/删除在子线程执行）
        self._bridge = _CleanupBridge(self)
        self._bridge.scan_done.connect(self._on_scan_done)
        self._bridge.execute_done.connect(self._on_execute_done)
        self._bridge.failed.connect(self._on_failed)
        # 是否在扫描完成后进入「执行确认」流程
        self._pending_execute: bool = False
        # 最近一次扫描计划（供执行复用，避免二次扫描）
        self._last_plan: Optional[CleanupPlan] = None

        self.setWindowTitle("鲲鹏量化交易系统 · 项目清理")
        self.setMinimumWidth(860)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinimizeButtonHint)
        self._apply_tech_style()
        self._build_ui()

    # ── 样式（与回测配置对话框一致的深紫科技渐变风） ──
    def _apply_tech_style(self) -> None:
        """深紫渐变 + 霓虹青紫强调；滚动区深色等宽字体。"""
        self.setStyleSheet("""
            QDialog {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0B1120, stop:1 #1E1B4B);
            }
            QGroupBox {
                background-color: rgba(15, 23, 42, 0.55);
                border: 1px solid #4F46E5;
                border-radius: 10px;
                margin-top: 12px;
                padding: 8px 12px 6px 12px;
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
            QRadioButton {
                color: #C7D2FE;
                font-size: 12px;
                spacing: 6px;
            }
            QRadioButton::indicator {
                width: 14px;
                height: 14px;
            }
            QRadioButton::indicator:unchecked {
                border: 1px solid #4F46E5;
                border-radius: 7px;
                background-color: #0B1120;
            }
            QRadioButton::indicator:checked {
                border: 1px solid #22D3EE;
                border-radius: 7px;
                background-color: #22D3EE;
            }
            QSpinBox {
                background-color: #0B1120;
                border: 1px solid #4F46E5;
                border-radius: 6px;
                color: #E0E7FF;
                padding: 3px 8px;
                min-height: 20px;
                selection-background-color: #6366F1;
            }
            QSpinBox:focus {
                border: 1px solid #22D3EE;
            }
            QPlainTextEdit#cleanupView {
                background-color: rgba(2, 6, 23, 0.88);
                border: 1px solid #4F46E5;
                border-radius: 8px;
                color: #A5F3FC;
                font-family: Consolas, "Microsoft YaHei", monospace;
                font-size: 12px;
                selection-background-color: #6366F1;
            }
            QPlainTextEdit#cleanupView:focus {
                border: 1px solid #22D3EE;
            }
            QPushButton {
                background-color: #0B1120;
                border: 1px solid #4F46E5;
                border-radius: 6px;
                color: #E0E7FF;
                padding: 6px 16px;
                font-size: 13px;
            }
            QPushButton:hover {
                border: 1px solid #22D3EE;
                color: #A5F3FC;
            }
            QPushButton#btnRun {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #22D3EE, stop:1 #4F46E5);
                border: none;
                color: #FFFFFF;
                font-weight: 600;
            }
            QPushButton#btnRun:disabled {
                background: rgba(79, 70, 229, 0.4);
                color: #94A3B8;
            }
            QPushButton:disabled {
                color: #64748B;
                border: 1px solid rgba(79, 70, 229, 0.4);
            }
        """)

    # ── UI 构建（方案一 · 上下分区） ──
    def _build_ui(self) -> None:
        """构建上下分区布局：清理项目 / 回测输出 / 执行模式 / 滚动区 / 按钮。"""
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ===== 1. 清理项目（勾选网格，2 列） =====
        box_junk = QGroupBox("清理项目")
        grid = QVBoxLayout(box_junk)
        self.chk_test = QCheckBox(f"A · {CATEGORY_NAMES['A']}（测试命名 / 孤儿 bad / clean 报告）")
        self.chk_test.setChecked(True)
        self.chk_backup = QCheckBox(f"B · {CATEGORY_NAMES['B']}（backup_*_quant_before_clean.db）")
        self.chk_backup.setChecked(True)
        self.chk_pycache = QCheckBox(f"C · {CATEGORY_NAMES['C']}（__pycache__，删除后自动重建）")
        self.chk_pycache.setChecked(True)
        self.chk_rotlog = QCheckBox(f"D · {CATEGORY_NAMES['D']}（logs/*.log.1）")
        self.chk_rotlog.setChecked(True)
        self.chk_legacy = QCheckBox(f"E · {CATEGORY_NAMES['E']}（datasets/processed 子目录，默认关闭）")
        self.chk_legacy.setChecked(False)
        self.chk_artifacts = QCheckBox(f"F · {CATEGORY_NAMES['F']}（outputs 回测产物：equity/report/summary/trades/result/回测看板）")
        self.chk_artifacts.setChecked(False)
        self.chk_bad = QCheckBox(f"G · {CATEGORY_NAMES['G']}（datasets/bad 脏数据归档，默认关闭）")
        self.chk_bad.setChecked(False)
        self.chk_batch = QCheckBox(f"H · {CATEGORY_NAMES['H']}（results/batch_backtest 批量回测汇总/双强候选池，按时间戳归期）")
        self.chk_batch.setChecked(False)
        grid.addWidget(self.chk_test)
        grid.addWidget(self.chk_backup)
        grid.addWidget(self.chk_pycache)
        grid.addWidget(self.chk_rotlog)
        grid.addWidget(self.chk_legacy)
        grid.addWidget(self.chk_artifacts)
        grid.addWidget(self.chk_bad)
        grid.addWidget(self.chk_batch)
        layout.addWidget(box_junk)

        # ===== 2. 回测输出（启用开关 + 保留期数） =====
        box_outputs = QGroupBox("回测输出")
        row_outputs = QHBoxLayout(box_outputs)
        self.chk_outputs = QCheckBox("清理过期回测产物（report/equity/trades/summary/result 按时间戳归期）")
        self.chk_outputs.setChecked(True)
        row_outputs.addWidget(self.chk_outputs)
        row_outputs.addSpacing(8)
        row_outputs.addWidget(QLabel("保留最近"))
        self.spin_keep = QSpinBox()
        self.spin_keep.setRange(KEEP_MIN, KEEP_MAX)
        self.spin_keep.setValue(KEEP_MIN)
        self.spin_keep.setSuffix(" 期")
        row_outputs.addWidget(self.spin_keep)
        row_outputs.addStretch()
        layout.addWidget(box_outputs)

        # ===== 3. 执行模式 =====
        row_mode = QHBoxLayout()
        row_mode.addWidget(QLabel("执行模式："))
        self.radio_preview = QRadioButton("仅预览（DRY-RUN，推荐）")
        self.radio_preview.setChecked(True)
        self.radio_execute = QRadioButton("直接清理")
        row_mode.addWidget(self.radio_preview)
        row_mode.addWidget(self.radio_execute)
        row_mode.addStretch()
        layout.addLayout(row_mode)

        # ===== 4. 保护清单提示 =====
        label_protect = QLabel(PROTECT_HINT)
        label_protect.setWordWrap(True)
        label_protect.setStyleSheet("color: #64748B; font-size: 11px;")
        layout.addWidget(label_protect)

        # ===== 5. 预览结果滚动区 =====
        box_result = QGroupBox("预览结果")
        v_result = QVBoxLayout(box_result)
        self.progress_view = QPlainTextEdit()
        self.progress_view.setObjectName("cleanupView")
        self.progress_view.setReadOnly(True)
        self.progress_view.setMaximumBlockCount(PROGRESS_MAX_BLOCKS)
        self.progress_view.setPlaceholderText(PROGRESS_PLACEHOLDER)
        self.progress_view.setMinimumHeight(PROGRESS_MIN_HEIGHT)
        self.progress_view.setMaximumHeight(PROGRESS_MAX_HEIGHT)
        v_result.addWidget(self.progress_view)
        layout.addWidget(box_result)

        # ===== 6. 底部按钮 =====
        buttons = QHBoxLayout()
        self.btn_preview = QPushButton("预览（DRY-RUN）")
        self.btn_run = QPushButton("执行清理")
        self.btn_cancel = QPushButton("取消")
        self.btn_run.setObjectName("btnRun")
        self.btn_preview.clicked.connect(self._on_preview)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_preview)
        buttons.addWidget(self.btn_run)
        buttons.addWidget(self.btn_cancel)
        layout.addLayout(buttons)

    # ── 选项收集 ──
    def _gather_options(self, execute: bool) -> CleanupOptions:
        """把勾选状态映射为清理选项。

        Args:
            execute: 是否执行删除。

        Returns:
            清理选项。

        Raises:
            ValueError: 保留期数非法时抛出。
        """
        return CleanupOptions(
            include_legacy=self.chk_legacy.isChecked(),
            clean_artifacts=self.chk_artifacts.isChecked(),
            clean_bad=self.chk_bad.isChecked(),
            clean_batch_results=self.chk_batch.isChecked(),
            clean_outputs=self.chk_outputs.isChecked(),
            keep=self.spin_keep.value(),
            execute=execute,
        )

    # ── 按钮事件 ──
    def _on_preview(self) -> None:
        """预览：后台扫描，滚动区展示 DRY-RUN 计划（不删除）。"""
        try:
            options = self._gather_options(execute=False)
        except ValueError as exc:
            QMessageBox.critical(self, "参数错误", f"参数校验失败：{exc}")
            return
        self._set_busy(True)
        self._pending_execute = False
        self._append_line("正在扫描（DRY-RUN）...")
        threading.Thread(
            target=self._scan_worker, args=(options,), daemon=True
        ).start()

    def _on_run(self) -> None:
        """执行清理：直接清理模式下扫描→确认→删除；仅预览模式下提示切换。"""
        if not self.radio_execute.isChecked():
            QMessageBox.information(
                self,
                "当前为仅预览模式",
                "请先勾选「直接清理」，再点击执行清理。\n\n"
                "或者点击「预览」先查看 DRY-RUN 结果，确认无误后再切换执行。",
            )
            return
        try:
            options = self._gather_options(execute=True)
        except ValueError as exc:
            QMessageBox.critical(self, "参数错误", f"参数校验失败：{exc}")
            return
        self._set_busy(True)
        self._pending_execute = True
        self._append_line("正在扫描（执行前预览）...")
        threading.Thread(
            target=self._scan_worker, args=(options,), daemon=True
        ).start()

    # ── 后台工作线程 ──
    def _scan_worker(self, options: CleanupOptions) -> None:
        """后台线程：执行一次完整扫描，结果经信号回传 GUI 线程。

        Args:
            options: 清理选项。
        """
        try:
            plan = run_cleanup_scan(self.project_root, options)
            self._bridge.scan_done.emit(plan)
        except Exception as exc:  # noqa: BLE001 扫描失败统一上报，避免线程静默死亡
            self._bridge.failed.emit(f"扫描失败：{exc}")

    def _execute_worker(self, plan: CleanupPlan) -> None:
        """后台线程：执行计划中的删除，结果经信号回传 GUI 线程。

        Args:
            plan: 清理计划。
        """
        try:
            deleted, failed, freed = execute_plan(plan)
            self._bridge.execute_done.emit(plan, deleted, failed, freed)
        except Exception as exc:  # noqa: BLE001 删除异常统一上报
            self._bridge.failed.emit(f"执行失败：{exc}")

    # ── 信号处理（GUI 线程） ──
    def _on_scan_done(self, plan: CleanupPlan) -> None:
        """扫描完成：渲染计划；若处于执行确认流程则弹确认框。

        Args:
            plan: 清理计划。
        """
        self._last_plan = plan
        self._set_busy(False)
        self._render_plan(plan)
        if self._pending_execute:
            self._pending_execute = False
            candidates = plan.delete_candidates()
            if not candidates:
                QMessageBox.information(self, "无可清理项", "扫描未发现可清理的项。")
                return
            total_mb = plan.delete_total_size() / 1024 / 1024
            answer = QMessageBox.question(
                self,
                "确认执行清理",
                f"将删除 {len(candidates)} 项，释放约 {total_mb:.1f} MB。\n\n"
                f"保护清单内的文件（quant.db / processed / 保留期报告等）不会受影响。\n\n"
                f"是否确认执行？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._append_line("已取消执行，未删除任何文件。")
                return
            self._set_busy(True)
            self._append_line(f"开始删除 {len(candidates)} 项...")
            threading.Thread(
                target=self._execute_worker, args=(plan,), daemon=True
            ).start()

    def _on_execute_done(self, plan: CleanupPlan, deleted: int, failed: int, freed: int) -> None:
        """删除完成：追加执行结果行。

        Args:
            plan: 清理计划。
            deleted: 成功删除数。
            failed: 失败数。
            freed: 释放字节数。
        """
        self._set_busy(False)
        self._append_line(
            f"执行完成：成功 {deleted}，失败 {failed}，释放 {freed / 1024 / 1024:.1f} MB"
        )
        if failed:
            QMessageBox.warning(
                self, "部分失败",
                f"清理完成，但 {failed} 项删除失败（详见滚动区上方日志）。",
            )
        else:
            QMessageBox.information(
                self, "清理完成",
                f"已删除 {deleted} 项，释放 {freed / 1024 / 1024:.1f} MB。",
            )

    def _on_failed(self, message: str) -> None:
        """后台任务失败：恢复按钮并提示。

        Args:
            message: 错误信息。
        """
        self._set_busy(False)
        self._append_line(f"[错误] {message}")
        QMessageBox.critical(self, "操作失败", message)

    # ── 渲染与状态 ──
    def _render_plan(self, plan: CleanupPlan) -> None:
        """把清理计划渲染进滚动区（清空后重写）。

        Args:
            plan: 清理计划。
        """
        self.progress_view.clear()
        for line in format_plan_summary(plan):
            self._append_line(line)

    def _append_line(self, text: str) -> None:
        """追加一行到滚动区，并自动滚动到底部。

        Args:
            text: 文本行内容。
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.progress_view.appendPlainText(f"[{timestamp}] {text}")
        scrollbar = self.progress_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _set_busy(self, busy: bool) -> None:
        """切换按钮忙状态（扫描/执行期间禁用操作按钮）。

        Args:
            busy: True 表示后台任务进行中。
        """
        self.btn_preview.setEnabled(not busy)
        self.btn_run.setEnabled(not busy)


def run_cleanup_dialog(project_root: Path) -> int:
    """启动清理对话框（供入口脚本调用）。

    Args:
        project_root: 项目根目录。

    Returns:
        应用退出码。
    """
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = CleanupDialog(project_root)
    dialog.show()
    return app.exec()


if __name__ == "__main__":
    # 可运行 demo：启动清理对话框（项目根自动探测）
    script_dir = Path(__file__).resolve().parent
    demo_root = script_dir.parent.parent
    if not (demo_root / "pyproject.toml").exists():
        raise RuntimeError(f"未找到项目根（pyproject.toml）：{demo_root}")
    sys.exit(run_cleanup_dialog(demo_root))
