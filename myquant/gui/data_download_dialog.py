# 文件名：myquant/gui/data_download_dialog.py
"""
数据下载管理器弹窗（PySide6）v2
==============================
在原 scripts/数据下载.py 命令行工具之上提供的图形化下载管理器，
布局与用户指定参考图（数据下载管理器v2）保持一致：
- 顶部标题栏：应用名 + 环境/路径
- 信息区：数据源状态 + 数据概览（只读）
- 勾选区：8 项功能多选（原6项 + 新增2项），可自由组合，
  "新增"项（通达信/网页）带橙色徽标
- 进度区：live 指示器 + 十行滚动实时进度，格式 [序号/总数] 代码 √/×/⏯（条数）
- 操作区：开始执行 / 取消 + 总进度条 + 百分比 + 防未来函数提示

新增 2 项：
  7. 通达信单独下载 —— 强制仅用通达信本地源（D:\\TDX）全量下载，零联网
  8. 网页抓取全量   —— akshare 网页源全量抓取，降级链 东财→新浪→腾讯；
                        代码框留空=全量，填入代码=单只抓取

任务执行放后台线程（QThread），UI 不卡顿；原命令行功能保持不变。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QDateEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from myquant.core.logger import get_logger
from myquant.data_access import (
    DataFetcher,
    DataManager,
    create_default_fetcher,
    create_fetcher,
)
from myquant.data_access.sources.web_source import WebSourceDataSource

logger = get_logger(__name__)


# ════════════════════════════════════════════════════════════════
# 常量：选项定义 / 显示顺序 / 执行顺序 / 互斥规则
# ════════════════════════════════════════════════════════════════

# key -> (显示名, 提示, 分组)
OPTIONS: Dict[str, tuple] = {
    "full":        ("全量下载日线数据",           "约5000只,30-60分钟",           "常规"),
    "incremental": ("增量更新日线",               "盘后每日:1-5分钟",             "常规"),
    "finance":     ("下载财务数据",               "PE/PB/业绩报表",               "常规"),
    "all":         ("全部更新",                   "日线增量+财务",                "常规"),
    "validate":    ("数据质量校验",               "完整性/异常检测",              "常规"),
    "status":      ("查看同步状态报告",           "仅展示,不下载",                "常规"),
    "tdx":         ("通达信单独下载",             "强制仅用D:\\TDX本地源,全量,零联网", ""),
    "web":         ("网页抓取全量",               "akshare网页源 东财→新浪→腾讯降级", ""),
}

# 勾选区显示顺序（与参考图 v2 一致：常规项在前，新增项沉底）
DISPLAY_ORDER: List[str] = [
    "full", "incremental", "finance", "all",
    "validate", "status", "tdx", "web",
]

# 固定执行顺序（勾选后按此顺序串行执行）
EXEC_ORDER: List[str] = [
    "full", "incremental", "finance", "all",
    "tdx", "web", "validate", "status",
]

# 互斥组：同组同时勾选时弹提示二选一
MUTEX_GROUPS: List[List[str]] = [["full", "incremental", "all"]]


# ════════════════════════════════════════════════════════════════
# 底层任务函数（复刻 scripts/数据下载.py 逻辑，薄入口只做参数/调度）
# ════════════════════════════════════════════════════════════════

def _make_manager(fetcher, project_root: Path):
    """创建 DataManager（每个任务独立连接，任务结束关闭）"""
    from myquant.database import get_connection
    from myquant.database.schema import table_exists, TABLE_NAMES
    from myquant.database.migrations import migrate_to_latest

    db_path = project_root / "database" / "quant.db"
    conn = get_connection(db_path)
    # 防御：数据库为空/被删时自动建表，避免 "no such table: sync_status"
    if not table_exists(conn, TABLE_NAMES["sync_status"]):
        migrate_to_latest(conn)
    manager = DataManager(fetcher, db_connection=conn)
    return manager, conn


def _save_parquet(result, project_root: Path) -> Path:
    """保存行情数据到 processed（Parquet）"""
    from myquant.utils.file_utils import ensure_dir, write_parquet
    processed_dir = ensure_dir(project_root / "datasets" / "processed")
    output_path = processed_dir / f"{result.symbol}.parquet"
    df = result.to_dataframe()
    if not df.empty:
        write_parquet(output_path, df)
    return output_path


def _save_financial_csv(data_list: List[dict], project_root: Path, is_history: bool = False) -> Path:
    """保存财务数据到 raw/financial（CSV），is_history=True 时文件名加 _history"""
    from myquant.utils.file_utils import ensure_dir, write_csv
    from datetime import datetime as _dt
    fin_dir = ensure_dir(project_root / "datasets" / "raw" / "financial")
    prefix = "financial_history" if is_history else "financial"
    output_path = fin_dir / f"{prefix}_{_dt.now().strftime('%Y%m%d')}.csv"
    if data_list:
        write_csv(output_path, data_list)
    return output_path


def run_daily(fetcher, manager, project_root: Path, progress_callback=None, force_full: bool = False) -> dict:
    """
    下载日线数据
    force_full=True=全量重下（忽略数据库同步状态，从头拉取全部历史）；
    force_full=False=增量（只补缺失，默认，DataManager 依据 sync_status 自动判断）。
    【防未来函数】DataManager 内部严格截止到当前日期
    """
    from myquant.utils.file_utils import ensure_dir
    ensure_dir(project_root / "datasets" / "raw" / "daily")
    ensure_dir(project_root / "datasets" / "processed")

    stocks = fetcher.get_stock_list()
    symbols = [s["symbol"] for s in stocks if s.get("symbol")]
    if not symbols:
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "parquet_saved": 0}

    results = manager.update_symbols_batch(
        symbols,
        period="daily",
        adjust="hfq",
        progress_callback=progress_callback,
        force_full=force_full,
    )

    success_count = 0
    for r in results:
        if r["status"] == "completed" and r.get("record_count", 0) > 0:
            result = fetcher.get_market_data(
                r["symbol"],
                start_date=r.get("start_date"),
                end_date=r.get("end_date"),
            )
            if result and result.count > 0:
                _save_parquet(result, project_root)
                success_count += 1

    return {
        "total": len(results),
        "success": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "parquet_saved": success_count,
    }


def run_finance(fetcher, project_root: Path, progress_callback=None, finance_start_date: str = "", finance_end_date: str = "") -> dict:
    """下载财务数据（PE/PB/业绩报表）。finance_start_date/end_date 非空时进入历史多期模式"""
    stocks = fetcher.get_stock_list()
    symbols = [s["symbol"] for s in stocks if s.get("symbol")]
    if not symbols:
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "records": 0}

    # 历史模式判断：start_date 或 end_date 非空即为历史多期模式
    is_history = bool(finance_start_date or finance_end_date)
    if is_history:
        logger.info(f"财务历史模式: {finance_start_date} ~ {finance_end_date}")
    all_financial = []
    success = failed = skipped = 0
    for i, symbol in enumerate(symbols, 1):
        try:
            if is_history:
                data = fetcher.get_financial_history(symbol, finance_start_date, finance_end_date)
            else:
                data = fetcher.get_financial_data(symbol)
            if data:
                all_financial.extend(data)
                success += 1
                status = "completed"
            else:
                skipped += 1
                status = "skipped"
            if progress_callback:
                progress_callback(i, len(symbols), {
                    "symbol": symbol, "status": status,
                    "record_count": len(data) if data else 0,
                })
        except Exception as e:
            failed += 1
            if progress_callback:
                progress_callback(i, len(symbols), {
                    "symbol": symbol, "status": "failed", "record_count": 0,
                })
            logger.debug(f"财务获取失败 [{symbol}]: {e}")
        time.sleep(0.3)

    if all_financial:
        _save_financial_csv(all_financial, project_root, is_history=is_history)

    return {
        "total": len(symbols), "success": success,
        "failed": failed, "skipped": skipped, "records": len(all_financial),
        "is_history": is_history,
        "date_range": f"{finance_start_date}~{finance_end_date}" if is_history else "latest",
    }


def run_validate(manager, project_root: Path, progress_callback=None) -> List[dict]:
    """数据质量校验（抽查 processed 前20只）"""
    processed_dir = project_root / "datasets" / "processed"
    if not processed_dir.exists():
        return []
    parquet_files = list(processed_dir.glob("*.parquet"))
    symbols = [f.stem for f in parquet_files[:20]]
    reports = []
    for i, symbol in enumerate(symbols, 1):
        report = manager.validate_data(symbol)
        reports.append(report)
        healthy = report.get("overall_status") == "healthy"
        if progress_callback:
            progress_callback(i, len(symbols), {
                "symbol": symbol,
                "status": "completed" if healthy else "skipped",
                "record_count": report.get("total_records", 0),
            })
    return reports


def run_status(manager, progress_callback=None) -> dict:
    """查看同步状态报告"""
    report = manager.get_sync_report()
    if progress_callback:
        progress_callback(1, 1, {
            "symbol": "汇总",
            "status": "completed",
            "record_count": report.get("total_symbols", 0),
        })
    return report


# ── 各入口任务（统一签名 fn(project_root, progress_callback)）──

def _entry_full(project_root: Path, progress_callback=None) -> dict:
    """全量下载日线数据：force_full=True 强制全量重下（忽略已同步状态）"""
    fetcher = create_default_fetcher()
    manager, conn = _make_manager(fetcher, project_root)
    try:
        return run_daily(fetcher, manager, project_root, progress_callback, force_full=True)
    finally:
        conn.close()


def _entry_incremental(project_root: Path, progress_callback=None) -> dict:
    """增量更新日线：force_full=False，只补缺失数据"""
    fetcher = create_default_fetcher()
    manager, conn = _make_manager(fetcher, project_root)
    try:
        return run_daily(fetcher, manager, project_root, progress_callback, force_full=False)
    finally:
        conn.close()


def _entry_finance(project_root: Path, progress_callback=None, finance_start_date: str = "", finance_end_date: str = "") -> dict:
    fetcher = create_default_fetcher()
    return run_finance(fetcher, project_root, progress_callback, finance_start_date, finance_end_date)


def _entry_all(project_root: Path, progress_callback=None) -> dict:
    """全部更新：日线增量 + 财务数据（合并汇总到顶层，兼容 summary_total 统计）"""
    daily = _entry_incremental(project_root, progress_callback)
    finance = _entry_finance(project_root, progress_callback)
    return {
        "total": daily.get("total", 0) + finance.get("total", 0),
        "success": daily.get("success", 0) + finance.get("success", 0),
        "failed": daily.get("failed", 0) + finance.get("failed", 0),
        "skipped": daily.get("skipped", 0) + finance.get("skipped", 0),
        "parquet_saved": daily.get("parquet_saved", 0),
        "records": finance.get("records", 0),
        "daily": daily,
        "finance": finance,
    }


def _entry_tdx(project_root: Path, progress_callback=None) -> dict:
    """通达信数据：强制仅用通达信本地源全量（force_full=True）"""
    fetcher = create_fetcher("tdx", enable_cache=False)
    manager, conn = _make_manager(fetcher, project_root)
    try:
        return run_daily(fetcher, manager, project_root, progress_callback, force_full=True)
    finally:
        conn.close()


def _entry_web(project_root: Path, progress_callback=None, symbol: Optional[str] = None) -> dict:
    """
    网页抓取全量：akshare 网页源，降级链 东财→新浪→腾讯
    symbol 为空=全量；非空=单只抓取
    """
    fetcher = DataFetcher(enable_cache=False)
    web_src = WebSourceDataSource()
    fetcher.add_source(web_src)

    def _with_web_info(summary: dict) -> dict:
        summary["_web"] = {
            "last_source": web_src.last_source or "—",
            "degrade_count": web_src.degrade_count,
        }
        return summary

    if symbol:
        # 单只抓取
        try:
            result = fetcher.get_market_data(symbol)
            ok = result is not None and result.count > 0
            if ok:
                _save_parquet(result, project_root)
            if progress_callback:
                progress_callback(1, 1, {
                    "symbol": symbol,
                    "status": "completed" if ok else "failed",
                    "record_count": result.count if ok else 0,
                })
            return _with_web_info({
                "total": 1, "success": 1 if ok else 0,
                "failed": 0 if ok else 1, "skipped": 0,
                "parquet_saved": 1 if ok else 0,
            })
        except Exception as e:
            if progress_callback:
                progress_callback(1, 1, {"symbol": symbol, "status": "failed", "record_count": 0})
            logger.error(f"网源单只抓取失败 [{symbol}]: {e}")
            return _with_web_info({"total": 1, "success": 0, "failed": 1, "skipped": 0, "parquet_saved": 0})
    # 全量抓取（force_full=True 强制全量）
    manager, conn = _make_manager(fetcher, project_root)
    try:
        return _with_web_info(run_daily(fetcher, manager, project_root, progress_callback, force_full=True))
    finally:
        conn.close()


def _entry_validate(project_root: Path, progress_callback=None) -> dict:
    fetcher = create_default_fetcher()
    manager, conn = _make_manager(fetcher, project_root)
    try:
        reports = run_validate(manager, project_root, progress_callback)
        return {"total": len(reports), "success": len(reports), "failed": 0, "skipped": 0}
    finally:
        conn.close()


def _entry_status(project_root: Path, progress_callback=None) -> dict:
    fetcher = create_default_fetcher()
    manager, conn = _make_manager(fetcher, project_root)
    try:
        return run_status(manager, progress_callback)
    finally:
        conn.close()


# 任务注册表：key -> (标签, 执行函数, 是否需要代码输入参数)
TASKS: Dict[str, tuple] = {
    "full":        (OPTIONS["full"][0], _entry_full, False),
    "incremental": (OPTIONS["incremental"][0], _entry_incremental, False),
    "finance":     (OPTIONS["finance"][0], _entry_finance, False),
    "all":         (OPTIONS["all"][0], _entry_all, False),
    "validate":    (OPTIONS["validate"][0], _entry_validate, False),
    "status":      (OPTIONS["status"][0], _entry_status, False),
    "tdx":         (OPTIONS["tdx"][0], _entry_tdx, False),
    "web":         (OPTIONS["web"][0], _entry_web, True),
}


# ════════════════════════════════════════════════════════════════
# 工作线程
# ════════════════════════════════════════════════════════════════

class DownloadWorker(QThread):
    """后台执行勾选的下载任务，逐条回传进度"""

    task_started = Signal(str)            # 任务名
    progress = Signal(int, int, dict)     # current, total, result
    task_finished = Signal(str, dict)     # 任务名, 汇总
    all_finished = Signal(dict)           # 总汇总
    log = Signal(str)                     # 普通状态行

    def __init__(self, project_root: Path, selected: List[str], web_symbol: str = "", finance_start_date: str = "", finance_end_date: str = "", parent=None):
        super().__init__(parent)
        self._project_root = project_root
        self._selected = selected
        self._web_symbol = web_symbol.strip()
        self._finance_start_date = finance_start_date.strip()
        self._finance_end_date = finance_end_date.strip()
        self._cancel = False

    def cancel(self) -> None:
        """请求取消：当前任务跑完后停止后续任务"""
        self._cancel = True

    def run(self) -> None:
        summary_total = {"tasks": 0, "success": 0, "failed": 0, "parquet": 0}
        for key in EXEC_ORDER:
            if key not in self._selected:
                continue
            if self._cancel:
                self.log.emit("已请求取消,停止后续任务")
                break
            label, fn, need_symbol = TASKS[key]
            self.task_started.emit(label)

            def cb(current: int, total: int, result: dict):
                self.progress.emit(current, total, result)

            try:
                if need_symbol:
                    summary = fn(self._project_root, cb, symbol=self._web_symbol or None)
                else:
                    if key == "finance":
                        summary = fn(self._project_root, cb, finance_start_date=self._finance_start_date, finance_end_date=self._finance_end_date)
                    else:
                        summary = fn(self._project_root, cb)
            except Exception as e:
                logger.error(f"任务 [{label}] 执行异常: {e}")
                summary = {"error": str(e)}

            self.task_finished.emit(label, summary)
            summary_total["tasks"] += 1
            summary_total["success"] += summary.get("success", 0)
            summary_total["failed"] += summary.get("failed", 0)
            summary_total["parquet"] += summary.get("parquet_saved", 0)

        self.all_finished.emit(summary_total)


# ════════════════════════════════════════════════════════════════
# 深色主题 QSS（贴合参考图：深蓝黑底 + 青色高亮 + 红色主按钮）
# ════════════════════════════════════════════════════════════════

DARK_QSS = """
QDialog { background: #131720; }
QLabel { color: #cdd6e6; }
QGroupBox {
    color: #cdd6e6; font-weight: 600;
    border: 1px solid #232a3a; border-radius: 8px;
    margin-top: 14px; padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 14px; padding: 0 6px;
    color: #2fd4a7; font-weight: 700;
}
QLabel#cardTitle { color: #7b8498; font-size: 11px; letter-spacing: 1px; }
QLabel#infoName { color: #8b93a7; }
QLabel#infoVal  { color: #e8edf7; font-family: Consolas; }
QLabel#okMark   { color: #2fd4a7; font-weight: 700; }
QLabel#offMark  { color: #5d667a; }
QLabel#taskState { color: #8fb8ff; font-family: Consolas; font-size: 12px; }
QCheckBox {
    color: #dbe2ef; background: #0f1420;
    border: 1px solid #232a3a; border-radius: 7px;
    padding: 8px 12px; spacing: 8px;
}
QCheckBox:hover { border-color: #2fd4a7; }
QCheckBox::indicator {
    width: 15px; height: 15px; border-radius: 4px;
    border: 1.5px solid #4a5468; background: #1a2130;
}
QCheckBox::indicator:checked { background: #2fd4a7; border-color: #2fd4a7; }
QCheckBox#webCheck { border-color: #4a4258; }
QCheckBox#webCheck:checked { border-color: #ffb84d; }
QCheckBox#webCheck::indicator:checked { background: #ffb84d; border-color: #ffb84d; }
QLineEdit {
    background: #0a0e16; color: #e8edf7;
    border: 1px solid #2b3346; border-radius: 6px;
    padding: 4px 8px; font-family: Consolas;
}
QLineEdit:disabled { color: #5d667a; background: #141a26; }
QPlainTextEdit {
    background: #0a0e16; color: #c6cede;
    border: 1px solid #232a3a; border-radius: 8px;
    font-family: Consolas; font-size: 12px;
}
QProgressBar {
    background: #0a0e16; border: 1px solid #232a3a; border-radius: 5px;
    height: 8px; text-align: center; color: #c6cede; font-size: 10px;
}
QProgressBar::chunk { background: #2fd4a7; border-radius: 5px; }
QPushButton#startBtn {
    background: #d93026; color: white; font-weight: 700;
    border: none; border-radius: 7px; padding: 9px 26px; font-size: 13px;
}
QPushButton#startBtn:hover { background: #e8483c; }
QPushButton#startBtn:disabled { background: #5c2a26; color: #9a8f8f; }
QPushButton#cancelBtn {
    background: #1a2130; color: #8b93a7;
    border: 1px solid #2b3346; border-radius: 7px; padding: 8px 18px;
}
QPushButton#cancelBtn:hover { border-color: #5d667a; color: #cdd6e6; }
QPushButton#cancelBtn:disabled { color: #4a5468; }
QLabel#appTitle { color: #e8edf7; font-size: 16px; font-weight: 700; }
QLabel#appPath  { color: #7b8498; font-size: 12px; font-family: Consolas; }
QLabel#newBadge {
    color: #ffb84d; background: #2a2115;
    border: 1px solid #ffb84d; border-radius: 3px;
    padding: 1px 6px; font-size: 10px; font-weight: 700;
}
QLabel#liveLabel { color: #ff5c5c; font-size: 11px; font-weight: 700; }
QLabel#pctLabel { color: #2fd4a7; font-size: 15px; font-weight: 700; font-family: Consolas; }
QLabel#noteLabel { color: #7b8498; font-size: 11px; }
"""


# ════════════════════════════════════════════════════════════════
# 主弹窗
# ════════════════════════════════════════════════════════════════

class DataDownloadDialog(QDialog):
    """数据下载管理器主弹窗（v2，贴合参考图布局）"""

    # 进度滚动区保留最近 N 行（实时滚动显示，保留全部历史；5000 行足够一次任务全程回看）
    PROGRESS_LINES = 5000

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Pythonwdhmm 鲲鹏量化交易系统-数据下载管理器v2")
        self.setMinimumSize(940, 760)
        self.setStyleSheet(DARK_QSS)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinimizeButtonHint)  # 保留关闭按钮，追加最小化按钮

        self._checkboxes: Dict[str, QCheckBox] = {}
        self._recent_lines: deque = deque(maxlen=self.PROGRESS_LINES)
        self._worker: Optional[DownloadWorker] = None
        self._pq_before = 0
        self._finance_mode: str = "latest"  # latest=最新一期, history=自定义范围
        self._finance_start: str = ""
        self._finance_end: str = ""

        self._build_ui()
        self._refresh_info()
        self._update_web_input_state()

    # ───────────────────────────────
    # UI 构建
    # ───────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        root.addLayout(self._build_info_area())
        root.addWidget(self._build_check_area())
        root.addWidget(self._build_progress_area())
        root.addLayout(self._build_action_area())

    def _build_header(self) -> QFrame:
        """顶部标题栏：应用名 + 环境/路径 + 最小化/关闭按钮"""
        header = QFrame()
        header.setStyleSheet(
            "QFrame { background: rgba(129,140,248,0.08); border:1px solid rgba(129,140,248,0.2); border-radius:10px; }"
        )
        lay = QHBoxLayout(header)
        lay.setContentsMargins(16, 10, 12, 10)
        # 左侧：指示灯 + 标题
        title_box = QHBoxLayout()
        title_box.setSpacing(10)
        indicator = QLabel("●")
        indicator.setStyleSheet("color:#818CF8; font-size:14px;")
        title_box.addWidget(indicator)
        title = QLabel("Pythonwdhmm 鲲鹏量化交易系统 · 数据下载管理器 v2")
        title.setObjectName("appTitle")
        title_box.addWidget(title)
        lay.addLayout(title_box)
        lay.addStretch()
        # 中间：环境路径
        path = QLabel(f"quant · {self._find_project_root() / 'datasets'}")
        path.setObjectName("appPath")
        lay.addWidget(path)
        lay.addSpacing(16)
        # 右侧：最小化 + 关闭按钮
        self.btn_minimize = QPushButton("—")
        self.btn_minimize.setFixedSize(28, 24)
        self.btn_minimize.setStyleSheet(
            "QPushButton { background: rgba(148,163,184,0.1); color:#94A3B8; border:none; border-radius:4px; font-size:14px; }"
            "QPushButton:hover { background: rgba(148,163,184,0.2); color:#E2E8F0; }"
        )
        self.btn_minimize.clicked.connect(self.showMinimized)
        lay.addWidget(self.btn_minimize)
        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 24)
        self.btn_close.setStyleSheet(
            "QPushButton { background: rgba(239,68,68,0.15); color:#F87171; border:none; border-radius:4px; font-size:12px; }"
            "QPushButton:hover { background: rgba(239,68,68,0.3); color:#FCA5A5; }"
        )
        self.btn_close.clicked.connect(self.close)
        lay.addWidget(self.btn_close)
        return header

    def _make_card(self, title: str) -> tuple:
        """返回 (容器, 内容布局)，浅色卡片样式"""
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background:#0f1420; border:1px solid #232a3a; border-radius:8px; }"
        )
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(6)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        lay.addWidget(t)
        return card, lay

    def _build_info_area(self) -> QHBoxLayout:
        """信息条：单行紧凑显示数据源状态 + 数据概览"""
        lay = QHBoxLayout()
        lay.setSpacing(12)

        # 数据源状态（单行）
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: rgba(15,23,42,0.6); border:1px solid rgba(34,211,238,0.15); border-radius:10px; }"
        )
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 10, 14, 10)
        cl.setSpacing(16)
        t = QLabel("数据源")
        t.setStyleSheet("color:#22D3EE; font-size:13px; font-weight:600;")
        cl.addWidget(t)
        self.lbl_tdx = QLabel("—"); self.lbl_web = QLabel("—"); self.lbl_broker = QLabel("—")
        self._marks: Dict[str, QLabel] = {}
        for key, name, val, mark in (
            ("tdx", "通达信", "D:\\TDX", "okMark"),
            ("web", "网页", "东财→新浪→腾讯", "okMark"),
            ("broker", "券商", "未配置", "offMark"),
        ):
            row = QHBoxLayout(); row.setSpacing(6)
            mk = QLabel("●" if mark == "okMark" else "○")
            mk.setObjectName(mark)
            mk.setStyleSheet(f"color:{'#22C55E' if mark == 'okMark' else '#64748B'}; font-size:10px;")
            nm = QLabel(name); nm.setObjectName("infoName")
            nm.setStyleSheet("color:#CBD5E1; font-size:12px;")
            vl = QLabel(val); vl.setObjectName("infoVal")
            vl.setStyleSheet("color:#64748B; font-size:11px;")
            row.addWidget(mk); row.addWidget(nm); row.addWidget(vl)
            cl.addLayout(row)
            self._marks[key] = mk
        cl.addStretch()
        lay.addWidget(card, 1)

        # 数据概览（单行）
        card2 = QFrame()
        card2.setStyleSheet(
            "QFrame { background: rgba(15,23,42,0.6); border:1px solid rgba(129,140,248,0.15); border-radius:10px; }"
        )
        cl2 = QHBoxLayout(card2)
        cl2.setContentsMargins(14, 10, 14, 10)
        cl2.setSpacing(20)
        t2 = QLabel("数据概览")
        t2.setStyleSheet("color:#818CF8; font-size:13px; font-weight:600;")
        cl2.addWidget(t2)
        self.lbl_pq = QLabel("—"); self.lbl_raw = QLabel("—"); self.lbl_db = QLabel("—")
        for name, obj in (
            ("Parquet", self.lbl_pq),
            ("CSV", self.lbl_raw),
            ("DB", self.lbl_db),
        ):
            row = QHBoxLayout(); row.setSpacing(6)
            nm = QLabel(name); nm.setObjectName("infoName")
            nm.setStyleSheet("color:#94A3B8; font-size:12px;")
            obj.setObjectName("infoVal")
            obj.setStyleSheet("color:#22D3EE; font-size:15px; font-weight:600; font-family:Consolas;")
            row.addWidget(nm); row.addWidget(obj)
            cl2.addLayout(row)
        cl2.addStretch()
        lay.addWidget(card2, 1)
        return lay

    def _build_check_area(self) -> QTabWidget:
        """功能勾选区：QTabWidget 标签页分区（行情数据/财务数据/数据管理）"""
        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabWidget::pane { background: rgba(15,23,42,0.4); border:1px solid rgba(34,211,238,0.2); border-radius:0 10px 10px 10px; }"
            "QTabBar::tab { background: rgba(15,23,42,0.4); color:#94A3B8; padding:8px 18px; border:1px solid rgba(148,163,184,0.1); border-bottom:none; border-radius:8px 8px 0 0; margin-right:4px; font-size:12px; }"
            "QTabBar::tab:selected { background: rgba(34,211,238,0.15); color:#22D3EE; border:1px solid rgba(34,211,238,0.3); border-bottom:none; font-weight:600; }"
        )

        # 标签页分组定义
        tab_defs = [
            ("行情数据", ["full", "incremental", "tdx", "web"]),
            ("财务数据", ["finance", "all"]),
            ("数据管理", ["validate", "status"]),
        ]

        for tab_name, keys in tab_defs:
            tab_widget = QWidget()
            tab_layout = QVBoxLayout(tab_widget)
            tab_layout.setContentsMargins(14, 14, 14, 14)
            tab_layout.setSpacing(10)

            grid = QGridLayout()
            grid.setSpacing(10)
            for idx, key in enumerate(keys):
                label, hint, group = OPTIONS[key]
                cb = QCheckBox(f"{label}\n{hint}")
                cb.setObjectName("webCheck" if key == "web" else "")
                self._checkboxes[key] = cb
                r, c = divmod(idx, 2)

                cell = QWidget()
                wl = QHBoxLayout(cell)
                wl.setContentsMargins(0, 0, 0, 0)
                wl.setSpacing(8)
                wl.setAlignment(Qt.AlignVCenter)

                if key == "finance":
                    # 财务数据卡片：两行布局（勾选框+单选 / 日期输入）
                    fin_widget = QWidget()
                    fin_layout = QVBoxLayout(fin_widget)
                    fin_layout.setContentsMargins(0, 0, 0, 0)
                    fin_layout.setSpacing(6)
                    # 第一行
                    row1 = QHBoxLayout()
                    row1.setSpacing(12)
                    cb.setMinimumWidth(180)
                    row1.addWidget(cb)
                    self._radio_finance_latest = QRadioButton("最新一期")
                    self._radio_finance_latest.setChecked(True)
                    self._radio_finance_latest.setStyleSheet("color:#E2E8F0; font-size:12px;")
                    self._radio_finance_latest.toggled.connect(self._on_finance_mode_changed)
                    row1.addWidget(self._radio_finance_latest)
                    self._radio_finance_history = QRadioButton("自定义范围")
                    self._radio_finance_history.setStyleSheet("color:#94A3B8; font-size:12px;")
                    row1.addWidget(self._radio_finance_history)
                    row1.addStretch()
                    fin_layout.addLayout(row1)
                    # 第二行（缩进）
                    row2 = QHBoxLayout()
                    row2.setSpacing(8)
                    row2.addSpacing(24)
                    self._label_finance_start = QLabel("开始")
                    self._label_finance_start.setStyleSheet("color:#64748B; font-size:11px;")
                    row2.addWidget(self._label_finance_start)
                    self._date_finance_start = QDateEdit()
                    self._date_finance_start.setDisplayFormat("yyyy-MM-dd")
                    self._date_finance_start.setDate(datetime.now() - timedelta(days=365*3))
                    self._date_finance_start.setCalendarPopup(True)
                    self._date_finance_start.setEnabled(False)
                    self._date_finance_start.setStyleSheet("QDateEdit { background:#0a0e16; color:#E8EDF7; border:1px solid #2b3346; border-radius:4px; padding:3px 8px; font-family:Consolas; font-size:11px; }")
                    row2.addWidget(self._date_finance_start)
                    self._label_finance_end = QLabel("结束")
                    self._label_finance_end.setStyleSheet("color:#64748B; font-size:11px;")
                    row2.addWidget(self._label_finance_end)
                    self._date_finance_end = QDateEdit()
                    self._date_finance_end.setDisplayFormat("yyyy-MM-dd")
                    self._date_finance_end.setDate(datetime.now())
                    self._date_finance_end.setCalendarPopup(True)
                    self._date_finance_end.setEnabled(False)
                    self._date_finance_end.setStyleSheet("QDateEdit { background:#0a0e16; color:#E8EDF7; border:1px solid #2b3346; border-radius:4px; padding:3px 8px; font-family:Consolas; font-size:11px; }")
                    row2.addWidget(self._date_finance_end)
                    row2.addStretch()
                    fin_layout.addLayout(row2)
                    cell = fin_widget
                else:
                    wl.addWidget(cb, 1)
                    if key == "web":
                        self.edit_web_symbol = QLineEdit()
                        self.edit_web_symbol.setPlaceholderText("留空=全量;或填代码如600519.SH")
                        self.edit_web_symbol.setMaximumWidth(200)
                        self.edit_web_symbol.setEnabled(False)
                        wl.addWidget(self.edit_web_symbol)
                        cb.toggled.connect(self._update_web_input_state)

                # 卡片样式包装
                card = QFrame()
                card.setStyleSheet(
                    "QFrame { background: rgba(30,41,59,0.5); border:1px solid rgba(148,163,184,0.15); border-radius:8px; }"
                )
                card_layout = QVBoxLayout(card)
                card_layout.setContentsMargins(12, 10, 12, 10)
                card_layout.addWidget(cell)
                grid.addWidget(card, r, c)

            tab_layout.addLayout(grid)
            tab_layout.addStretch()
            tabs.addTab(tab_widget, tab_name)

        return tabs

    def _build_progress_area(self) -> QGroupBox:
        box = QGroupBox()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 6, 12, 12)
        lay.setSpacing(6)
        # 标题行：下载进度 + live 指示器
        hdr = QHBoxLayout()
        t = QLabel("下载进度(实时滚动,完整保留)")
        t.setStyleSheet("color:#2fd4a7; font-weight:700; font-size:13px;")
        self.lbl_live = QLabel("○ idle")
        self.lbl_live.setObjectName("liveLabel")
        hdr.addWidget(t); hdr.addStretch(); hdr.addWidget(self.lbl_live)
        lay.addLayout(hdr)
        self.txt_progress = QPlainTextEdit()
        self.txt_progress.setReadOnly(True)
        self.txt_progress.setMaximumBlockCount(0)
        font = QFont("Consolas", 12)
        self.txt_progress.setFont(font)
        self.txt_progress.setFixedHeight(250)
        lay.addWidget(self.txt_progress)
        return box

    def _build_action_area(self) -> QHBoxLayout:
        lay = QHBoxLayout()
        lay.setSpacing(12)

        self.btn_start = QPushButton("开始执行")
        self.btn_start.setObjectName("startBtn")
        self.btn_start.clicked.connect(self._on_start)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setObjectName("cancelBtn")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(18)

        self.lbl_pct = QLabel("0%")
        self.lbl_pct.setObjectName("pctLabel")

        self.lbl_note = QLabel("防未来函数:下载严格截止当前日期")
        self.lbl_note.setObjectName("noteLabel")

        lay.addWidget(self.btn_start)
        lay.addWidget(self.btn_cancel)
        lay.addWidget(self.progress_bar, 1)
        lay.addWidget(self.lbl_pct)
        lay.addWidget(self.lbl_note)
        return lay

    # ───────────────────────────────
    # 信息刷新
    # ───────────────────────────────

    def _refresh_info(self) -> None:
        """刷新数据源状态 + 数据概览（只读，从磁盘/数据源探测）"""
        try:
            from myquant.data_access.factory import detect_tdx_available
            tdx_ok, tdx_path = detect_tdx_available()
            self.lbl_tdx.setText(tdx_path if tdx_ok else "未检测到")
            self._marks["tdx"].setText("✔" if tdx_ok else "×")
        except Exception as e:
            logger.debug(f"通达信检测失败: {e}")
            self.lbl_tdx.setText("—")

        # 网页源健康
        try:
            web = WebSourceDataSource()
            st = web.health_check()
            self.lbl_web.setText("可用" if st.value == "healthy" else "不可用")
        except Exception:
            self.lbl_web.setText("—")

        # 数据概览
        project_root = self._find_project_root()
        processed_dir = project_root / "datasets" / "processed"
        raw_daily_dir = project_root / "datasets" / "raw" / "daily"
        db_path = project_root / "database" / "quant.db"
        try:
            pq = len(list(processed_dir.glob("*.parquet"))) if processed_dir.exists() else 0
            raw = len(list(raw_daily_dir.glob("*.csv"))) if raw_daily_dir.exists() else 0
            db = db_path.stat().st_size if db_path.exists() else 0
            self.lbl_pq.setText(f"{pq}只")
            self.lbl_raw.setText(f"{raw}只")
            self.lbl_db.setText(f"quant.db {db / 1024:.1f} KB")
        except Exception as e:
            logger.debug(f"数据概览刷新失败: {e}")

    @staticmethod
    def _find_project_root() -> Path:
        """自动探测项目根目录（向上查找 pyproject.toml）"""
        current = Path(__file__).resolve().parent
        for _ in range(20):
            if (current / "pyproject.toml").exists():
                return current
            if current == current.parent:
                break
            current = current.parent
        return Path.cwd()

    def _count_parquet(self) -> int:
        """统计 processed 目录 Parquet 数量"""
        d = self._find_project_root() / "datasets" / "processed"
        return len(list(d.glob("*.parquet"))) if d.exists() else 0

    def _update_web_input_state(self) -> None:
        """勾选网页抓取全量时启用代码输入框"""
        if hasattr(self, "edit_web_symbol"):
            checked = self._checkboxes.get("web", QCheckBox()).isChecked()
            self.edit_web_symbol.setEnabled(checked)

    # ───────────────────────────────
    # 进度滚动区
    # ───────────────────────────────

    def _on_finance_mode_changed(self, checked: bool) -> None:
        """财务数据模式切换：最新一期/自定义范围，联动启用/禁用日期输入"""
        is_history = self._radio_finance_history.isChecked()
        self._date_finance_start.setEnabled(is_history)
        self._date_finance_end.setEnabled(is_history)
        self._label_finance_start.setStyleSheet(f"color: {'#E2E8F0' if is_history else '#64748B'}; font-size: 10px;")
        self._label_finance_end.setStyleSheet(f"color: {'#E2E8F0' if is_history else '#64748B'}; font-size: 10px;")
        self._radio_finance_latest.setStyleSheet(f"color: {'#94A3B8' if is_history else '#E2E8F0'}; font-size: 11px;")
        self._radio_finance_history.setStyleSheet(f"color: {'#E2E8F0' if is_history else '#94A3B8'}; font-size: 11px;")

    def _append_progress(self, current: int, total: int, result: dict) -> None:
        symbol = str(result.get("symbol", ""))
        status = result.get("status", "unknown")
        cnt = result.get("record_count", 0) or 0
        icon = "√" if status == "completed" else ("⏯" if status == "skipped" else "×")
        line = f"[{current}/{total}] {symbol:<12s} {icon} ({cnt}条)"
        self._recent_lines.append(line)
        self.txt_progress.setPlainText("\n".join(self._recent_lines))
        self._scroll_progress_bottom()

    def _append_log(self, text: str) -> None:
        self._recent_lines.append(text)
        self.txt_progress.setPlainText("\n".join(self._recent_lines))
        self._scroll_progress_bottom()

    def _scroll_progress_bottom(self) -> None:
        """进度文本更新后自动滚动到底部（实时滚动跟随最新行）"""
        scroll = self.txt_progress.verticalScrollBar()
        scroll.setValue(scroll.maximum())

    # ───────────────────────────────
    # 事件处理
    # ───────────────────────────────

    def _on_start(self) -> None:
        selected = [k for k, cb in self._checkboxes.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "提示", "请至少勾选一项功能")
            return

        # 互斥校验
        for group in MUTEX_GROUPS:
            hit = [OPTIONS[k][0] for k in group if k in selected]
            if len(hit) > 1:
                QMessageBox.warning(
                    self, "选项冲突",
                    f"「{'」和「'.join(hit)}」互斥，请二选一后重试",
                )
                return

        web_symbol = ""
        if "web" in selected:
            web_symbol = self.edit_web_symbol.text().strip()
            if web_symbol:
                web_symbol = self._normalize_symbol(web_symbol)

        self._pq_before = self._count_parquet()
        project_root = self._find_project_root()
        # 读取财务时间选项
        finance_start = ""
        finance_end = ""
        if "finance" in selected and hasattr(self, "_radio_finance_history") and self._radio_finance_history.isChecked():
            finance_start = self._date_finance_start.date().toString("yyyy-MM-dd")
            finance_end = self._date_finance_end.date().toString("yyyy-MM-dd")
        self._worker = DownloadWorker(project_root, selected, web_symbol, finance_start, finance_end, parent=self)
        self._worker.task_started.connect(self._on_task_started)
        self._worker.progress.connect(self._on_progress)
        self._worker.task_finished.connect(self._on_task_finished)
        self._worker.all_finished.connect(self._on_all_finished)
        self._worker.log.connect(self._append_log)

        # 锁定控件
        self._set_running(True)
        self.txt_progress.clear()
        self._recent_lines.clear()
        self.lbl_pct.setText("0%")
        self._append_log(f"开始执行:{', '.join(OPTIONS[k][0] for k in selected)}")
        self._worker.start()

    def _normalize_symbol(self, raw: str) -> str:
        """把 600519 / 600519.SH / sh600519 规范为 600519.SH"""
        s = raw.strip().upper()
        s = s.replace("SH600", "600").replace("SZ000", "000").replace("SZ300", "300")
        if "." in s:
            return s
        if s.startswith(("60", "68", "90")):
            return f"{s}.SH"
        if s.startswith(("00", "30", "20")):
            return f"{s}.SZ"
        if s.startswith(("43", "83", "87", "88")):
            return f"{s}.BJ"
        return s

    def _on_cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self.btn_cancel.setEnabled(False)
            self._append_log("已请求取消,当前任务完成后停止…")

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        for cb in self._checkboxes.values():
            cb.setEnabled(not running)
        if hasattr(self, "edit_web_symbol"):
            self.edit_web_symbol.setEnabled(not running and self._checkboxes["web"].isChecked())
        self.lbl_live.setText("● live" if running else "○ idle")

    # ── 信号槽 ──

    def _on_task_started(self, label: str) -> None:
        self._append_log(f"[任务]{label}")
        self.progress_bar.setRange(0, 0)  # 忙碌态
        self.lbl_pct.setText("…")

    def _on_progress(self, current: int, total: int, result: dict) -> None:
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.lbl_pct.setText(f"{int(current / total * 100)}%")
        self._append_progress(current, total, result)

    def _on_task_finished(self, label: str, summary: dict) -> None:
        if "error" in summary:
            self._append_log(f"× {label} 异常:{summary['error']}")
            self.lbl_pct.setText("异常")
            return
        s = summary.get("success", 0)
        f = summary.get("failed", 0)
        sk = summary.get("skipped", 0)
        ps = summary.get("parquet_saved", 0)
        if "daily" in summary:  # 全部更新任务
            d = summary["daily"]; fi = summary["finance"]
            self._append_log(
                f"✔ {label} 完成:日线 成功{d.get('success',0)}/失败{d.get('failed',0)}/跳过{d.get('skipped',0)}"
                f";财务 成功{fi.get('success',0)}/失败{fi.get('failed',0)}/共{fi.get('records',0)}条"
            )
        else:
            pq_now = self._count_parquet()
            added = pq_now - self._pq_before
            total = summary.get("total", s + f + sk)
            self._append_log(
                f"✔ {label} 完成:已完成{s + f + sk}/{total}·成功{s}·失败{f}·跳过{sk}"
                f"·本次共写入parquet {self._pq_before}→新增{added}个"
            )
        # 网页任务：显示最终使用的数据源与降级次数
        web_info = summary.get("_web")
        if web_info:
            src = web_info.get("last_source", "—")
            deg = web_info.get("degrade_count", 0)
            self._append_log(f"  源:{src}·已降级{deg}次")

    def _on_all_finished(self, summary_total: dict) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.lbl_pct.setText("100%")
        self.lbl_live.setText("○ idle")
        self._append_log(
            f"全部完成:任务{summary_total['tasks']}个 | 成功{summary_total['success']} | "
            f"失败{summary_total['failed']} | parquet {summary_total['parquet']}"
        )
        self._set_running(False)

    def closeEvent(self, event) -> None:  # noqa: N802
        """窗口关闭时尽量停止工作线程"""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)