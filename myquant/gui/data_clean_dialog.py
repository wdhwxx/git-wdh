# -*- coding: utf-8 -*-
# 文件名：myquant/gui/data_clean_dialog.py
"""
数据全量清洗配置窗口
====================
在 数据全量清洗.py 之上提供一个图形化配置窗口：
  - 清洗范围选择（全量 / 指定标的）
  - 指数/基金过滤（自定义默认：默认排除上证/深证/板块指数与基金，规则可编辑）
  - 清洗参数（价格跳变阈值 / 并行进程数）
  - 选项（清洗前备份 / 清洗后重建 processed）

点击「开始清洗」后生成 config/data_clean_run.json，
并新开控制台运行 scripts/数据全量清洗.py --config ...。

使用方法：
    python run_全量清洗GUI.py

依赖：PySide6（quant 环境已安装）
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QMessageBox,
)

# 默认排除规则（指数/基金/板块，可自定义）:
#   000.SH = 上证指数(000001.SH上证指数, 000300.SH沪深300等)
#   399    = 深证/中证指数(399001.SZ深证成指等)
#   880    = 通达信板块指数
#   5.SH   = 沪市基金/ETF(511910.SH等)
#   1.SZ   = 深市基金/LOF(160137.SZ等)
#   30.SZ  = 创业板(300/301开头.SZ)
#   688.SH = 科创板(688开头.SH)
#   4/8/92 = 北交所/老三板(4开头老三板、8开头北交所82/83/87/88、92北交所920新段)
DEFAULT_EXCLUDE_RULES = "000.SH, 399, 880, 5.SH, 1.SZ, 30.SZ, 688.SH, 4, 8, 92, 68, 90, 20, 99"
# ST 风险警示股名单（离线数据源；由「刷新名单」联网生成或人工编辑，不入库）
ST_BLACKLIST_CONFIG = "config/st_blacklist.yaml"
DEFAULT_PRESETS = [
    ("上证指数（000.SH）", "000.SH"),
    ("深证/中证指数（399）", "399"),
    ("板块指数（880）", "880"),
    ("基金/ETF/LOF（5.SH, 1.SZ）", "5.SH, 1.SZ"),
    ("创业板（30.SZ）", "30.SZ"),
    ("科创板（688.SH）", "688.SH"),
    ("北交所/老三板（4, 8, 92）", "4, 8, 92"),
]
# 预设勾选区按此分行渲染（第一行：指数/基金；第二行：板块）
PRESET_ROW_INDEXES = [slice(0, 4), slice(4, 7)]


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）"""
    script_dir = Path(__file__).resolve().parent.parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    raise RuntimeError("无法自动探测项目根目录（找不到 pyproject.toml）。")


def load_all_symbols(db_path: Path):
    """读取全量 hfq 日线标的（用于预览排除数量）"""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT symbol FROM market_data "
            "WHERE period='daily' AND adjust='hfq' ORDER BY symbol"
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def match_rule(symbol: str, rule: str) -> bool:
    """判断 symbol 是否命中规则。规则支持 '000.SH'（前缀.后缀）或 '399'（前缀）"""
    rule = rule.strip()
    if not rule:
        return False
    code, _, market = symbol.partition(".")
    if "." in rule:
        prefix, suffix = rule.split(".", 1)
        return code.startswith(prefix) and market.upper() == suffix.strip().upper()
    return code.startswith(rule)


def split_rules(text: str):
    return [r.strip() for r in text.replace("，", ",").split(",") if r.strip()]


class DataCleanDialog(QDialog):
    """全量数据清洗配置弹窗"""

    def __init__(self, project_root: Path, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self.db_path = project_root / "database" / "quant.db"
        self.clean_script = project_root / "scripts" / "数据全量清洗.py"
        self.run_config_path = project_root / "config" / "data_clean_run.json"
        self.all_symbols = load_all_symbols(self.db_path)

        self.setWindowTitle("数据全量清洗")
        self.setMinimumWidth(560)
        self._build_ui()
        self._update_preview()

    # ─────────────────────────── UI 构建 ───────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ① 清洗范围
        grp_scope = QGroupBox("清洗范围")
        scope_form = QFormLayout(grp_scope)
        self.rb_all = QRadioButton("全量 hfq 日线标的（默认）")
        self.rb_all.setChecked(True)
        self.rb_custom = QRadioButton("仅指定标的（逗号分隔）")
        self.ed_symbols = QLineEdit()
        self.ed_symbols.setPlaceholderText("如 000001.SZ, 600519.SH, 601398.SH")
        self.ed_symbols.setEnabled(False)
        self.rb_custom.toggled.connect(lambda on: self.ed_symbols.setEnabled(on))
        scope_form.addRow(self.rb_all)
        scope_form.addRow(self.rb_custom, self.ed_symbols)
        root.addWidget(grp_scope)

        # ② 排除规则（指数/基金/板块，自定义默认）
        grp_filter = QGroupBox("排除规则（指数/基金/板块，自定义默认）")
        fv = QVBoxLayout(grp_filter)
        tip = QLabel("默认排除指数/基金与创业板/科创板/北交所等，仅清洗沪深主板可交易标的。可勾选预设或直接编辑下方规则：")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #666;")
        fv.addWidget(tip)

        # 预设勾选（分两行：第一行指数/基金，第二行板块）
        self.preset_checks: list[QCheckBox] = []
        for part in PRESET_ROW_INDEXES:
            preset_row = QHBoxLayout()
            for label, _rule in DEFAULT_PRESETS[part]:
                cb = QCheckBox(label)
                cb.setChecked(True)
                cb.toggled.connect(self._on_preset_changed)
                self.preset_checks.append(cb)
                preset_row.addWidget(cb)
            fv.addLayout(preset_row)

        # 规则输入
        rules_row = QHBoxLayout()
        rules_row.addWidget(QLabel("排除规则："))
        self.ed_rules = QLineEdit(DEFAULT_EXCLUDE_RULES)
        self.ed_rules.setToolTip(
            "逗号分隔的排除规则。支持两种形式：\n"
            "  '000.SH' = 代码以 000 开头且市场为 SH（后缀感知，不会误伤 000001.SZ 平安银行）\n"
            "  '399'    = 代码以 399 开头（不限市场）\n"
            "  '30.SZ'  = 创业板(300/301)；'688.SH' = 科创板；'4'/'8'/'92' = 北交所/老三板\n"
            "留空 = 不过滤，清洗全部标的。"
        )
        self.ed_rules.textChanged.connect(self._update_preview)
        rules_row.addWidget(self.ed_rules, 1)
        btn_default = QPushButton("恢复默认")
        btn_default.clicked.connect(self._on_restore_default)
        rules_row.addWidget(btn_default)
        fv.addLayout(rules_row)

        # ST 风险警示剔除（*ST/ST/S*ST/SST，按名称名单，与上方代码前缀规则正交串联）
        st_row = QHBoxLayout()
        self.cb_exclude_st = QCheckBox("剔除 ST / *ST / S*ST / SST 风险警示股")
        self.cb_exclude_st.setChecked(True)
        self.cb_exclude_st.toggled.connect(lambda _on: self._update_preview())
        self.cb_exclude_st.setToolTip("按名称匹配风险警示股（名单 config/st_blacklist.yaml），与上方代码前缀规则串联执行；不勾选则行为与之前完全一致")
        st_row.addWidget(self.cb_exclude_st)
        self.lbl_st_count = QLabel("")
        self.lbl_st_count.setStyleSheet("color: #666;")
        st_row.addWidget(self.lbl_st_count)
        btn_st_refresh = QPushButton("刷新名单")
        btn_st_refresh.setToolTip("联网拉取全市场股票名称，按 *ST/ST/S*ST/SST 前缀生成 config/st_blacklist.yaml")
        btn_st_refresh.clicked.connect(self._on_st_refresh)
        st_row.addWidget(btn_st_refresh)
        btn_st_edit = QPushButton("编辑名单")
        btn_st_edit.setToolTip("打开 config/st_blacklist.yaml 人工维护名单")
        btn_st_edit.clicked.connect(self._on_st_edit)
        st_row.addWidget(btn_st_edit)
        fv.addLayout(st_row)

        # 预览
        self.lbl_preview = QLabel("")
        self.lbl_preview.setStyleSheet("color: #1a7f37; font-weight: bold;")
        fv.addWidget(self.lbl_preview)
        root.addWidget(grp_filter)

        # ③ 清洗参数
        grp_param = QGroupBox("清洗参数")
        param_form = QFormLayout(grp_param)
        self.sp_jump = QDoubleSpinBox()
        self.sp_jump.setRange(0.01, 1.0)
        self.sp_jump.setSingleStep(0.01)
        self.sp_jump.setValue(0.22)
        self.sp_jump.setDecimals(2)
        self.sp_jump.setSuffix("  (跳变幅度)")
        self.sp_jump.setToolTip("价格跳变检测阈值 |open-prev_close|/prev_close，处理方式见下方下拉框")
        param_form.addRow("价格跳变阈值：", self.sp_jump)

        self.cb_jump_action = QComboBox()
        self.cb_jump_action.addItem("仅标记（保留数据）", "mark")
        self.cb_jump_action.addItem("整行剔除（进脏数据）", "drop")
        self.cb_jump_action.setCurrentIndex(0)
        self.cb_jump_action.setToolTip("价格跳变超过阈值时：仅标记 _warnings 保留，或整行剔除进脏数据")
        param_form.addRow("价格跳变处理：", self.cb_jump_action)

        self.cb_vol_action = QComboBox()
        self.cb_vol_action.addItem("仅标记（保留数据）", "mark")
        self.cb_vol_action.addItem("整行剔除（进脏数据）", "drop")
        self.cb_vol_action.setCurrentIndex(0)
        self.cb_vol_action.setToolTip("成交量异常放大超过阈值时：仅标记 _warnings 保留，或整行剔除进脏数据")
        param_form.addRow("成交量异常处理：", self.cb_vol_action)

        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(1, 32)
        self.sp_workers.setValue(8)
        param_form.addRow("并行进程数：", self.sp_workers)

        note = QLabel(
            "负成交量(NEGATIVE_VOLUME)：整行剔除\n"
            "价格跳变/成交量异常：默认仅标记 _warnings 保留；可选整行剔除\n"
            "重复时间戳：keep_last 去重；时间/序列异常：标记不删除\n"
            "写回方式：清洗前自动备份，事务内原地写回 quant.db"
        )
        note.setStyleSheet("color: #666;")
        note.setWordWrap(True)
        param_form.addRow(note)
        root.addWidget(grp_param)

        # ④ 选项
        grp_opt = QGroupBox("选项")
        opt_layout = QVBoxLayout(grp_opt)
        self.cb_backup = QCheckBox("清洗前自动备份数据库到 backups/")
        self.cb_backup.setChecked(True)
        self.cb_rebuild = QCheckBox("清洗后重建 processed（parquet，供回测读取）")
        self.cb_rebuild.setChecked(True)
        opt_layout.addWidget(self.cb_backup)
        opt_layout.addWidget(self.cb_rebuild)
        root.addWidget(grp_opt)

        # ⑤ 底部按钮
        btns = QHBoxLayout()
        btns.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        btn_start = QPushButton("开始清洗")
        btn_start.setDefault(True)
        btn_start.setStyleSheet(
            "background-color: #d93026; color: white; font-weight: bold; "
            "padding: 8px 24px;"
        )
        btn_start.clicked.connect(self._on_start)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_start)
        root.addLayout(btns)

    # ─────────────────────────── 逻辑 ───────────────────────────
    def _on_preset_changed(self) -> None:
        """预设勾选变化 → 同步预设规则；保留预设之外的自定义/板块规则（如 30.SZ/688.SH/4/8/92）"""
        preset_rules = {r for (_label, rule) in DEFAULT_PRESETS for r in split_rules(rule)}
        current = self._current_rules()
        custom = [r for r in current if r not in preset_rules]
        enabled = [
            r for cb, (_label, rule) in zip(self.preset_checks, DEFAULT_PRESETS)
            if cb.isChecked() for r in split_rules(rule)
        ]
        text = ", ".join(enabled + custom)
        self.ed_rules.setText(text)

    def _on_restore_default(self) -> None:
        for cb in self.preset_checks:
            cb.setChecked(True)
        self.ed_rules.setText(DEFAULT_EXCLUDE_RULES)

    def _current_rules(self):
        return split_rules(self.ed_rules.text())

    def _update_preview(self) -> None:
        if not self.all_symbols:
            self.lbl_preview.setText("⚠ 未找到 quant.db，无法预览（请先下载数据）")
            self.lbl_preview.setStyleSheet("color: #c0392b;")
            return
        rules = self._current_rules()
        excluded = [s for s in self.all_symbols if any(match_rule(s, r) for r in rules)]
        # ST 名单：勾选时并入排除统计（名单∩DB 存量，离线读文件）
        st_blacklist = self._load_st_blacklist()
        st_hit = [s for s in self.all_symbols if s in st_blacklist] if self.cb_exclude_st.isChecked() else []
        keep = len(self.all_symbols) - len(excluded) - len(st_hit)
        self.lbl_st_count.setText(
            f"名单 {len(st_blacklist)} 只（DB 内 {len(st_hit)} 只）"
            if st_blacklist else "名单为空（点击「刷新名单」生成）"
        )
        if st_hit:
            self.lbl_preview.setText(
                f"将清洗 {keep} 只标的，排除指数/基金/板块 {len(excluded)} 只 + "
                f"ST 风险警示 {len(st_hit)} 只（全量 {len(self.all_symbols)} 只）"
            )
        else:
            self.lbl_preview.setText(
                f"将清洗 {keep} 只标的，排除指数/基金/板块 {len(excluded)} 只（全量 {len(self.all_symbols)} 只）"
            )

    def _collect_config(self) -> dict:
        scope = "all" if self.rb_all.isChecked() else "custom"
        return {
            "run_id": f"clean_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "scope": scope,
            "symbols": self.ed_symbols.text().strip() if scope == "custom" else None,
            "exclude_rules": self._current_rules(),
            "exclude_st": self.cb_exclude_st.isChecked(),
            "st_blacklist_path": ST_BLACKLIST_CONFIG,
            "clean_overrides": {
                "max_price_jump_pct": self.sp_jump.value(),
                "price_jump_action": self.cb_jump_action.currentData(),
                "volume_anomaly_action": self.cb_vol_action.currentData(),
            },
            "workers": self.sp_workers.value(),
            "backup": self.cb_backup.isChecked(),
            "rebuild_processed": self.cb_rebuild.isChecked(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _load_st_blacklist(self) -> dict:
        """读取 ST 名单（离线文件）；缺失/损坏返回空 dict（不阻断预览）。"""
        from myquant.data_access.full_clean_service import load_st_blacklist
        return load_st_blacklist(self.project_root)

    def _on_st_refresh(self) -> None:
        """联网刷新 ST 名单（业务在 full_clean_service.refresh_st_blacklist）。"""
        from myquant.data_access.full_clean_service import refresh_st_blacklist
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            blacklist = refresh_st_blacklist(self.project_root)
        except Exception as e:
            QMessageBox.warning(
                self, "刷新失败",
                f"ST 名单刷新失败（网络/数据源错误）：\n{type(e).__name__}: {e}",
            )
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._update_preview()
        QMessageBox.information(
            self, "刷新完成",
            f"ST 风险警示名单已刷新：{len(blacklist)} 只\n保存至 config/st_blacklist.yaml",
        )

    def _on_st_edit(self) -> None:
        """打开 ST 名单文件人工维护。"""
        path = self.project_root / ST_BLACKLIST_CONFIG
        if not path.exists():
            QMessageBox.warning(
                self, "名单不存在",
                "config/st_blacklist.yaml 不存在，请先点击「刷新名单」生成。",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_start(self) -> None:
        if not self.clean_script.exists():
            QMessageBox.critical(self, "错误", f"未找到清洗脚本：\n{self.clean_script}")
            return
        if not self.db_path.exists():
            QMessageBox.critical(self, "错误", f"未找到数据库：\n{self.db_path}\n请先运行 数据下载.py")
            return

        config = self._collect_config()
        if config["scope"] == "custom" and not config["symbols"]:
            QMessageBox.warning(self, "提示", "已选择「仅指定标的」，请输入标的代码（逗号分隔）。")
            return

        # 写运行配置
        try:
            self.run_config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            QMessageBox.critical(self, "错误", f"写入运行配置失败：\n{e}")
            return

        # 新开控制台运行清洗脚本
        cmd = [
            sys.executable, str(self.clean_script),
            "--config", str(self.run_config_path),
        ]
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen(cmd, **kwargs)
        except OSError as e:
            QMessageBox.critical(self, "错误", f"启动清洗进程失败：\n{e}")
            return

        QMessageBox.information(
            self, "已启动",
            "已启动全量数据清洗（新控制台窗口）。\n"
            "清洗过程中会显示进度，结束后生成报告：\n"
            f"outputs/clean_all_{config['run_id']}.json",
        )
        self.accept()


def run_dialog(project_root: Path) -> int:
    """弹窗入口（参照 backtest_config_dialog.run_dialog）"""
    app = QApplication.instance() or QApplication(sys.argv)
    dialog = DataCleanDialog(project_root)
    dialog.show()
    return app.exec()
