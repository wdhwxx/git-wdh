# -*- coding: utf-8 -*-
# 文件名：myquant/utils/cleanup_service.py
"""
项目清理服务（GUI 与 CLI 共用的业务核心）
==========================================
统一封装两类清理能力，供图形弹窗与命令行工具复用：

  1. 项目垃圾清理：A-E 五类
     A 测试残留（文件名含测试关键词 / 孤儿 bad / clean 测试报告）
     B 测试备份（backups/backup_*_quant_before_clean.db 及 -shm/-wal）
     C Python 缓存（**/__pycache__，删除后自动重建）
     D 轮转日志（logs/*.log.1）
     E 旧格式冗余（datasets/processed/*/ CSV 子目录，默认关闭）

  2. outputs 回测输出归期：按文件名时间戳只保留最近 N 期，
     result_<unix秒>.json 单独排序保留最近 N 个（统一归本模块处理，
     不再提供"删光"语义，避免与归期逻辑冲突）。

  3. 批量回测结果归期（H 类）：results/batch_backtest/ 下各策略子目录
     （ma_cross/rsrs/kunpeng 等）中的 批量回测汇总_YYYYMMDD_HHMMSS.*
     与 双强候选池_YYYYMMDD_HHMMSS.*，按时间戳分组每目录保留最新 N 批
     （csv+html+pdf 三件套同期）；不触碰 fingerprints.json/results.json
     增量缓存与非回测命名的人工产物。

安全设计：
  - 所有扫描默认只收集不动手；execute=True 才删除。
  - 保护清单（永不触碰）：
    database/quant.db、datasets/processed/*.parquet、
    outputs/report_*/equity_*/trades_*/summary_*（保留期内的真实报告）、
    results/batch_backtest/*/fingerprints.json 与 results.json（增量缓存）、
    非回测命名的人工产物、所有 .gitkeep、backups/ 代码备份（*.bak_*.py）、
    datasets/bad/ 真实脏数据归档（非孤儿）。
  - 时间戳无法解析 / 归属不明的文件一律保守保留。

设计原则（蓝图纪律）：业务逻辑归 myquant，脚本只做薄入口。
"""

from __future__ import annotations

import fnmatch
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量：垃圾识别规则
# ─────────────────────────────────────────────

# 文件名中包含这些关键词即判定为测试残留（A 类）
TEST_KEYWORDS: Tuple[str, ...] = (
    "TEST_EXEC", "TICK_TEST", "EMPTY_TEST", "HOOK_TEST",
    "_入口测试", "_verify_full",
)
# 文件名精确匹配测试标识（更宽松，含 TEST 但排除真实标的如 600519）
TEST_NAME_PATTERNS: Tuple[str, ...] = (
    "bad_bar_TEST", "bad_tick_TICK_TEST",
    "quality_report_bar_TEST", "quality_report_bar_EMPTY_TEST",
    "quality_report_bar_HOOK_TEST", "quality_report_bar_unknown",
    "quality_report_tick_TICK_TEST",
    "test_mc_report", "test_report",
    "_verify_equity", "_verify_mc_report", "_verify_no_mc",
    "_verify_report", "_verify_trades",
)

# 孤儿 bad 文件回退规则（仅当 quant.db 的 clean_run_meta 不可读时使用）
ORPHAN_BAD_PATTERN: str = "bad_bar_*_20260830_225*.json"

# 全量清洗测试产生的备份与报告
TEST_BACKUP_PATTERN: str = "backup_*_quant_before_clean.db"
TEST_CLEAN_REPORT_PATTERN: str = "clean_all_full_clean_*.json"

# 回测看板验收样品（一次性验收产物；正式看板由 回测配置GUI「看板生成」页产出，
# 命名规范为 回测看板_<代码>_<起止日期>.html，不匹配本模式）
DASHBOARD_ACCEPTANCE_PATTERN: str = "回测看板_验收*.html"

# F 类：回测产物输出（outputs 下正常回测产物，供用户随时一键清理）
BACKTEST_OUTPUT_PATTERNS: Tuple[str, ...] = (
    "equity_*.csv",
    "trades_*.csv",
    "report_*.html",
    "report_*.pdf",
    "summary_*.txt",
    "result_*.json",
    "回测看板_*.html",
)

# G 类：datasets/bad 脏数据归档（被 clean_run_meta 引用的真实脏数据，
# 默认保守保留；勾选 G 类后一键清理）
BAD_DATA_PATTERN: str = "bad_*.json"

# H 类：批量回测结果文件模式（results/batch_backtest/ 各策略子目录下）
BATCH_BACKTEST_RESULT_PATTERNS: Tuple[str, ...] = (
    "批量回测汇总_*.csv",
    "批量回测汇总_*.html",
    "批量回测汇总_*.pdf",
    "双强候选池_*.csv",
    "双强候选池_*.html",
    "双强候选池_*.pdf",
)
# 批量回测结果时间戳正则：从文件名提取 YYYYMMDD_HHMMSS
BATCH_RESULT_TS_PATTERN: re.Pattern = re.compile(r"_(\d{8}_\d{6})\.[A-Za-z]+$")

# 保护：真实 bad 归档（非测试、非孤儿）
PROTECTED_BAD: frozenset = frozenset({"bad_bar_000001.SZ_20260827_195958.json"})
# 保护：用户原有备份目录
PROTECTED_BACKUP_DIRS: frozenset = frozenset({"backup_20260829_083316"})

# 类别中文名
CATEGORY_NAMES: Dict[str, str] = {
    "A": "测试残留",
    "B": "测试备份",
    "C": "Python 缓存",
    "D": "轮转日志",
    "E": "旧格式冗余",
    "F": "回测产物输出",
    "G": "脏数据归档",
    "H": "批量回测结果",
}

# ─────────────────────────────────────────────
# 常量：outputs 归期规则
# ─────────────────────────────────────────────

# 默认保留的回测期数
DEFAULT_KEEP: int = 1
# 期时间戳正则：report_/equity_/trades_/summary_ 前缀 + YYYYMMDD_HHMMSS
PERIOD_FILE_PATTERN: re.Pattern = re.compile(
    r"^(report|equity|trades|summary)_(\d{8}_\d{6})\.[A-Za-z]+$"
)
# result 文件名：result_<unix秒>.json
RESULT_FILE_PATTERN: re.Pattern = re.compile(r"^result_(\d+)\.json$")
# .gitkeep 与任何目录均不参与清理
GITKEEP_FILENAME: str = ".gitkeep"


# ─────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────

@dataclass
class JunkItem:
    """一条待清理的垃圾项。

    Attributes:
        path: 文件或目录绝对路径。
        category: 类别标识（A/B/C/D/E）。
        reason: 判定原因（写入报告）。
        size: 占用的字节数（目录为递归求和）。
    """
    path: Path
    category: str
    reason: str
    size: int = 0

    @property
    def size_mb(self) -> float:
        """大小（MB，保留 2 位）。"""
        return self.size / (1024 * 1024)


@dataclass
class ScanResult:
    """垃圾扫描结果容器。"""
    items: List[JunkItem] = field(default_factory=list)

    def add(self, path: Path, category: str, reason: str) -> None:
        """追加一项；目录按递归文件大小求和。

        Args:
            path: 文件或目录路径。
            category: 类别标识。
            reason: 判定原因。
        """
        if path.is_file():
            self.items.append(JunkItem(path, category, reason, path.stat().st_size))
        elif path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            self.items.append(JunkItem(path, category, reason, total))

    def total_size(self) -> int:
        """全部垃圾项总字节数。"""
        return sum(i.size for i in self.items)

    def by_category(self) -> Dict[str, List[JunkItem]]:
        """按类别分组的垃圾项字典。"""
        result: Dict[str, List[JunkItem]] = {}
        for item in self.items:
            result.setdefault(item.category, []).append(item)
        return result


@dataclass
class CleanupOptions:
    """清理选项（GUI 勾选状态 / CLI 参数统一映射）。

    Attributes:
        include_legacy: 是否包含 E 类（旧格式 CSV 子目录）。
        clean_artifacts: 是否包含 F 类（outputs 回测产物：报告族/result/回测看板）。
        clean_bad: 是否包含 G 类（datasets/bad 脏数据归档）。
        clean_outputs: 是否执行 outputs 回测输出归期。
        clean_batch_results: 是否执行批量回测结果归期（results/batch_backtest/）。
        keep: 保留的最近回测期数（≥1），同时作用于 outputs 与批量回测结果。
        execute: 是否真正删除（False 表示仅扫描预览）。
    """
    include_legacy: bool = False
    clean_artifacts: bool = False
    clean_bad: bool = False
    clean_outputs: bool = True
    clean_batch_results: bool = False
    keep: int = DEFAULT_KEEP
    execute: bool = False

    def __post_init__(self) -> None:
        """防御式校验：keep 必须 ≥1。"""
        if self.keep < 1:
            raise ValueError(f"keep 必须 ≥ 1，实际为 {self.keep}")


@dataclass
class CleanupPlan:
    """一次清理扫描的完整计划（供 GUI/CLI 渲染）。

    Attributes:
        project_root: 项目根目录。
        junk: 项目垃圾扫描结果。
        outputs_keep: outputs 保留文件列表。
        outputs_delete: outputs 待删文件列表。
        scanned_at: 扫描时间。
        options: 本次扫描使用的选项。
    """
    project_root: Path
    junk: ScanResult
    outputs_keep: List[Path]
    outputs_delete: List[Path]
    scanned_at: datetime
    options: CleanupOptions

    def delete_candidates(self) -> List[Path]:
        """全部待删文件（项目垃圾 + outputs 过期产物），按路径去重。

        同一文件可能同时命中垃圾规则与 outputs 归期（如 F 类与归期重叠），
        执行时只删除一次，避免重复统计与重复删除。

        Returns:
            待删文件绝对路径列表（无重复）。
        """
        seen: set = set()
        candidates: List[Path] = []
        for path in [i.path for i in self.junk.items] + self.outputs_delete:
            if path not in seen:
                seen.add(path)
                candidates.append(path)
        return candidates

    def delete_total_size(self) -> int:
        """待删总字节数（垃圾项大小 + outputs 过期文件大小，按去重后候选统计）。"""
        return sum(p.stat().st_size for p in self.delete_candidates())


# ─────────────────────────────────────────────
# A 类：测试残留
# ─────────────────────────────────────────────

def _ts_of(name: str) -> str:
    """从文件名提取 YYYYMMDD_HHMMSS 时间戳，无则返回空串。

    Args:
        name: 文件名。

    Returns:
        时间戳字符串（如 20260830_225000），无匹配时为空串。
    """
    match = re.search(r"_(\d{8}_\d{6})\.json$", name)
    return match.group(1) if match else ""


def _load_referenced_bad_names(project_root: Path) -> Optional[set]:
    """只读读取 quant.db 的 clean_run_meta 中被引用的 bad 文件基名集合。

    孤儿判定以该集合为准：bad 文件名不在集合中且不在保护名单 -> 孤儿测试残留。
    数据库不可用（被占用/损坏）时返回 None，调用方回退到 ORPHAN_BAD_PATTERN。

    Args:
        project_root: 项目根目录。

    Returns:
        被引用 bad 文件基名集合；数据库不可读时返回 None。
    """
    db_path = project_root / "database" / "quant.db"
    if not db_path.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5
        )
        try:
            rows = connection.execute(
                "SELECT bad_file FROM clean_run_meta "
                "WHERE bad_file IS NOT NULL AND bad_file != ''"
            ).fetchall()
            return {Path(row[0]).name for row in rows}
        finally:
            connection.close()
    except sqlite3.Error as exc:
        logger.warning("quant.db 只读查询失败，回退时间窗孤儿规则: %s", exc)
        return None


def scan_test_residuals(project_root: Path, result: ScanResult) -> None:
    """扫描 A 类测试残留（bad 目录 + outputs 目录）。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    referenced_bad = _load_referenced_bad_names(project_root)
    orphan_ts: set = set()  # 孤儿 bad 的时间戳，用于关联同批 quality_report

    bad_dir = project_root / "datasets" / "bad"
    if bad_dir.exists():
        for entry in bad_dir.iterdir():
            if not entry.is_file() or entry.name == GITKEEP_FILENAME:
                continue
            if entry.name in PROTECTED_BAD:
                continue
            if any(kw in entry.name for kw in TEST_KEYWORDS):
                result.add(
                    entry, "A",
                    f"bad 测试残留（含 {next(kw for kw in TEST_KEYWORDS if kw in entry.name)}）",
                )
                continue
            if any(pattern in entry.name for pattern in TEST_NAME_PATTERNS):
                result.add(entry, "A", "bad 测试残留（测试命名）")
                continue
            # 孤儿 bad：优先以 clean_run_meta 为准，DB 不可读时回退时间窗规则
            if referenced_bad is not None:
                if entry.name not in referenced_bad:
                    orphan_ts.add(_ts_of(entry.name))
                    result.add(entry, "A", "bad 孤儿文件（clean_run_meta 无对应记录）")
            elif fnmatch.fnmatch(entry.name, ORPHAN_BAD_PATTERN):
                result.add(entry, "A", "bad 孤儿文件（副本测试产生，无对应元信息）")

    out_dir = project_root / "outputs"
    if out_dir.exists():
        for entry in out_dir.iterdir():
            if entry.is_file():
                if any(kw in entry.name for kw in TEST_KEYWORDS):
                    result.add(
                        entry, "A",
                        f"outputs 测试残留（含 {next(kw for kw in TEST_KEYWORDS if kw in entry.name)}）",
                    )
                    continue
                if any(pattern in entry.name for pattern in TEST_NAME_PATTERNS):
                    result.add(entry, "A", "outputs 测试残留（测试命名）")
                    continue
                if fnmatch.fnmatch(entry.name, TEST_CLEAN_REPORT_PATTERN):
                    result.add(entry, "A", "全量清洗测试报告（开发测试产生）")
                    continue
                # 回测看板验收样品（一次性验收产物）
                if fnmatch.fnmatch(entry.name, DASHBOARD_ACCEPTANCE_PATTERN):
                    result.add(entry, "A", "回测看板验收样品（一次性验收产物，正式看板走回测配置GUI「看板生成」页）")
                    continue
                # 孤儿 quality_report：与孤儿 bad 同批（同时间戳）-> 一并清理
                if entry.name.startswith("quality_report_bar_") and _ts_of(entry.name) in orphan_ts:
                    result.add(entry, "A", "quality_report 孤儿（与孤儿 bad 同批测试产生）")
            elif entry.is_dir():
                if any(kw in entry.name for kw in TEST_KEYWORDS):
                    result.add(entry, "A", f"outputs 测试目录（{entry.name}）")


# ─────────────────────────────────────────────
# B-E 类
# ─────────────────────────────────────────────

def scan_test_backups(project_root: Path, result: ScanResult) -> None:
    """扫描 B 类测试备份（含 SQLite 伴生 -shm/-wal）。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    backup_dir = project_root / "backups"
    if not backup_dir.exists():
        return
    for entry in backup_dir.iterdir():
        if entry.is_file() and fnmatch.fnmatch(entry.name, TEST_BACKUP_PATTERN):
            result.add(entry, "B", "全量清洗测试产生的数据库备份（副本测试，非用户数据）")
            for suffix in ("-shm", "-wal"):
                companion = Path(str(entry) + suffix)
                if companion.exists() and companion.is_file():
                    result.add(companion, "B", "SQLite 伴生文件（随 .db 一并清理）")


def scan_pycache(project_root: Path, result: ScanResult) -> None:
    """扫描 C 类 Python 缓存目录。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    for directory in project_root.rglob("__pycache__"):
        if directory.is_dir() and ".git" not in directory.parts:
            result.add(directory, "C", "Python 字节码缓存（删除后自动重建）")


def scan_rotated_logs(project_root: Path, result: ScanResult) -> None:
    """扫描 D 类轮转日志。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    log_dir = project_root / "logs"
    if not log_dir.exists():
        return
    for entry in log_dir.glob("*.log.1"):
        result.add(entry, "D", "日志轮转备份（.log.1）")


def scan_legacy_processed(project_root: Path, result: ScanResult) -> None:
    """扫描 E 类旧格式 processed 子目录（CSV 子目录格式）。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    proc_dir = project_root / "datasets" / "processed"
    if not proc_dir.exists():
        return
    for entry in proc_dir.iterdir():
        if entry.is_dir():
            result.add(entry, "E", "旧格式 CSV 子目录（当前回测用扁平 parquet）")


# ─────────────────────────────────────────────
# F 类：回测产物输出
# ─────────────────────────────────────────────

def scan_backtest_artifacts(project_root: Path, result: ScanResult) -> None:
    """扫描 F 类 outputs 回测产物（报告族 / result / 回测看板）。

    供用户随时一键清理正常回测产物；仅匹配已知产物命名模式，
    不触碰 .gitkeep、命令记录等非产物文件。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    out_dir = project_root / "outputs"
    if not out_dir.is_dir():
        return
    existing = {item.path for item in result.items}
    for entry in out_dir.iterdir():
        if not entry.is_file() or entry.name == GITKEEP_FILENAME:
            continue
        if entry in existing:
            # 已被 A-E 类命中（如验收样品），避免同一文件多类别重复展示
            continue
        for pattern in BACKTEST_OUTPUT_PATTERNS:
            if fnmatch.fnmatch(entry.name, pattern):
                result.add(entry, "F", f"回测产物输出（匹配 {pattern}）")
                break


# ─────────────────────────────────────────────
# G 类：脏数据归档
# ─────────────────────────────────────────────

def scan_bad_data(project_root: Path, result: ScanResult) -> None:
    """扫描 G 类 datasets/bad 脏数据归档（bad_*.json）。

    与 A 类互补：A 类只清测试残留/孤儿 bad；本类覆盖被 clean_run_meta
    引用的真实脏数据归档（默认保守保留，勾选后一键清理）。
    跳过 .gitkeep、保护清单（PROTECTED_BAD）及已被其他类别命中的文件。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
    """
    bad_dir = project_root / "datasets" / "bad"
    if not bad_dir.is_dir():
        return
    existing = {item.path for item in result.items}
    for entry in bad_dir.iterdir():
        if not entry.is_file() or entry.name == GITKEEP_FILENAME:
            continue
        if entry.name in PROTECTED_BAD:
            continue
        if entry in existing:
            # 已被 A 类命中（测试残留/孤儿），避免重复展示
            continue
        if fnmatch.fnmatch(entry.name, BAD_DATA_PATTERN):
            result.add(entry, "G", "脏数据归档（datasets/bad，用户勾选手动清理）")


# ─────────────────────────────────────────────
# H 类：批量回测结果归期
# ─────────────────────────────────────────────

def _scan_batch_dir(batch_dir: Path) -> Dict[str, List[Path]]:
    """扫描单个批量回测策略子目录，按时间戳归组回测结果文件。

    只匹配 BATCH_BACKTEST_RESULT_PATTERNS 中的已知命名模式，
    不触碰 fingerprints.json / results.json / 非回测命名文件。

    Args:
        batch_dir: 策略子目录绝对路径（如 results/batch_backtest/rsrs/）。

    Returns:
        期时间戳(YYYYMMDD_HHMMSS) → 该期文件列表 的字典；
        解析失败 / 不匹配模式的文件忽略。
    """
    periods: Dict[str, List[Path]] = {}
    if not batch_dir.is_dir():
        return periods
    for entry in batch_dir.iterdir():
        if not entry.is_file() or entry.name == GITKEEP_FILENAME:
            continue
        # 跳过增量缓存文件
        if entry.name in ("fingerprints.json", "results.json"):
            continue
        # 匹配已知回测结果命名模式
        matched = any(
            fnmatch.fnmatch(entry.name, pat)
            for pat in BATCH_BACKTEST_RESULT_PATTERNS
        )
        if not matched:
            continue
        ts_match = BATCH_RESULT_TS_PATTERN.search(entry.name)
        if not ts_match:
            logger.warning("批量回测结果文件时间戳解析失败，保守保留: %s", entry.name)
            continue
        periods.setdefault(ts_match.group(1), []).append(entry)
    return periods


def scan_batch_backtest_results(
    project_root: Path,
    result: ScanResult,
    keep: int = DEFAULT_KEEP,
) -> None:
    """扫描 H 类批量回测结果（results/batch_backtest/ 各策略子目录）。

    遍历 batch_backtest/ 下的直接子目录（含嵌套子目录如 kunpeng/refine/），
    每个目录内按时间戳归组，保留最新 keep 批，旧批次加入待删列表。
    根目录的 fingerprints.json / results.json 与各子目录的增量缓存均不触碰。

    Args:
        project_root: 项目根目录。
        result: 扫描结果容器（原地追加）。
        keep: 每个策略子目录保留的最近批次数，默认 1。
    """
    if keep < 1:
        raise ValueError(f"keep 必须 ≥ 1，实际为 {keep}")
    batch_root = project_root / "results" / "batch_backtest"
    if not batch_root.is_dir():
        logger.info("results/batch_backtest 目录不存在，跳过 H 类扫描")
        return
    # 递归扫描所有包含回测结果文件的子目录（含嵌套如 kunpeng/refine/）
    for sub_dir in sorted(batch_root.rglob("*")):
        if not sub_dir.is_dir():
            continue
        periods = _scan_batch_dir(sub_dir)
        if not periods:
            continue
        sorted_periods = sorted(periods.keys(), reverse=True)
        keep_periods = set(sorted_periods[:keep])
        for period_ts, files in periods.items():
            if period_ts in keep_periods:
                continue
            for fpath in files:
                relative = fpath.relative_to(project_root)
                result.add(
                    fpath, "H",
                    f"批量回测旧批次（{period_ts}，保留最近 {keep} 批）：{relative.parent.name}/",
                )


# ─────────────────────────────────────────────
# outputs 归期
# ─────────────────────────────────────────────

def scan_period_files(out_dir: Path) -> Dict[str, List[Path]]:
    """扫描 outputs 根目录，按回测期归组产物文件。

    Args:
        out_dir: outputs 目录绝对路径。

    Returns:
        期时间戳(YYYYMMDD_HHMMSS) → 该期文件列表 的字典；解析失败的文件忽略。
    """
    periods: Dict[str, List[Path]] = {}
    if not out_dir.is_dir():
        logger.warning("outputs 目录不存在: %s", out_dir)
        return periods
    for entry in out_dir.iterdir():
        if not entry.is_file() or entry.name == GITKEEP_FILENAME:
            continue
        match = PERIOD_FILE_PATTERN.match(entry.name)
        if match:
            periods.setdefault(match.group(2), []).append(entry)
    return periods


def scan_result_files(out_dir: Path) -> List[Tuple[str, Path]]:
    """扫描 result_*.json 文件，按文件名中的 unix 秒换算期时间戳。

    Args:
        out_dir: outputs 目录绝对路径。

    Returns:
        [(期时间戳, 文件路径), ...] 列表，按期时间戳升序；
        文件名数字无法换算时跳过（保守保留，不进入清理候选）。
    """
    results: List[Tuple[str, Path]] = []
    if not out_dir.is_dir():
        return results
    for entry in out_dir.iterdir():
        if not entry.is_file():
            continue
        match = RESULT_FILE_PATTERN.match(entry.name)
        if not match:
            continue
        unix_seconds = int(match.group(1))
        try:
            period_ts = datetime.fromtimestamp(unix_seconds).strftime("%Y%m%d_%H%M%S")
        except (OSError, OverflowError, ValueError) as exc:
            logger.warning("result 文件时间戳换算失败，保守保留: %s (%s)", entry.name, exc)
            continue
        results.append((period_ts, entry))
    results.sort(key=lambda item: item[0])
    return results


def plan_outputs_cleanup(
    out_dir: Path,
    keep: int,
) -> Tuple[List[Path], List[Path]]:
    """计算 outputs 保留与删除的文件清单。

    Args:
        out_dir: outputs 目录绝对路径。
        keep: 保留的最近期数，必须 ≥ 1。

    Returns:
        (保留文件列表, 删除文件列表) 元组。

    Raises:
        ValueError: keep 小于 1 时抛出。
    """
    if keep < 1:
        raise ValueError(f"keep 必须 ≥ 1，实际为 {keep}")
    periods = scan_period_files(out_dir)
    result_files = scan_result_files(out_dir)

    keep_files: List[Path] = []
    delete_files: List[Path] = []

    # 1) 回测期文件：按期时间戳倒序，保留前 keep 期
    sorted_periods = sorted(periods.keys(), reverse=True)
    keep_periods = set(sorted_periods[:keep])
    for period_ts, files in periods.items():
        if period_ts in keep_periods:
            keep_files.extend(files)
        else:
            delete_files.extend(files)

    # 2) result json：与回测期分开排序，保留最近 keep 个
    for idx, (_, path) in enumerate(result_files):
        if idx >= len(result_files) - keep:
            keep_files.append(path)
        else:
            delete_files.append(path)

    return keep_files, delete_files


# ─────────────────────────────────────────────
# 统一入口与执行
# ─────────────────────────────────────────────

def run_cleanup_scan(project_root: Path, options: CleanupOptions) -> CleanupPlan:
    """执行一次完整清理扫描（只收集计划，不删除）。

    Args:
        project_root: 项目根目录。
        options: 清理选项。

    Returns:
        清理计划（含垃圾项与 outputs 保留/删除清单）。

    Raises:
        ValueError: options 校验失败时抛出。
    """
    # 防御式校验：keep 下限
    if options.keep < 1:
        raise ValueError(f"keep 必须 ≥ 1，实际为 {options.keep}")

    result = ScanResult()
    # 始终扫描 A/B/C/D
    scan_test_residuals(project_root, result)
    scan_test_backups(project_root, result)
    scan_pycache(project_root, result)
    scan_rotated_logs(project_root, result)
    # 可选 E 类
    if options.include_legacy:
        scan_legacy_processed(project_root, result)
    # 可选 F 类
    if options.clean_artifacts:
        scan_backtest_artifacts(project_root, result)
    # 可选 G 类
    if options.clean_bad:
        scan_bad_data(project_root, result)
    # 可选 H 类（批量回测结果归期，复用 options.keep）
    if options.clean_batch_results:
        scan_batch_backtest_results(project_root, result, options.keep)

    keep_files: List[Path] = []
    delete_files: List[Path] = []
    if options.clean_outputs:
        out_dir = project_root / "outputs"
        keep_files, delete_files = plan_outputs_cleanup(out_dir, options.keep)

    return CleanupPlan(
        project_root=project_root,
        junk=result,
        outputs_keep=keep_files,
        outputs_delete=delete_files,
        scanned_at=datetime.now(),
        options=options,
    )


def execute_plan(plan: CleanupPlan) -> Tuple[int, int, int]:
    """执行计划中的删除操作。

    Args:
        plan: 清理计划。

    Returns:
        (成功数, 失败数, 释放字节数) 元组。
    """
    deleted = 0
    failed = 0
    freed = 0
    for path in plan.delete_candidates():
        try:
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                freed += size
            elif path.is_dir():
                total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
                shutil.rmtree(path)
                freed += total
            else:
                continue
            deleted += 1
        except FileNotFoundError:
            # 文件已被外部删除，视为成功
            deleted += 1
        except OSError as exc:
            failed += 1
            logger.error("删除失败: %s (%s)", path, exc)
    return deleted, failed, freed


def format_plan_summary(plan: CleanupPlan) -> List[str]:
    """把清理计划格式化为报告文本行（GUI 滚动区 / CLI 报告共用）。

    Args:
        plan: 清理计划。

    Returns:
        报告文本行列表。
    """
    lines: List[str] = []
    mode = "执行" if plan.options.execute else "DRY-RUN 预览"
    lines.append(f"扫描完成 · {plan.scanned_at:%Y-%m-%d %H:%M:%S}（{mode}）")

    for category in sorted(plan.junk.by_category().keys()):
        items = plan.junk.by_category()[category]
        cat_size = sum(item.size for item in items)
        lines.append(
            f"[{category}] {CATEGORY_NAMES[category]}：{len(items)} 项，{cat_size / 1024 / 1024:.1f} MB"
        )
        for item in items[:8]:
            relative = item.path.relative_to(plan.project_root)
            lines.append(f"    · {item.size_mb:7.2f} MB  {relative}  ({item.reason})")
        if len(items) > 8:
            lines.append(f"    · ... 其余 {len(items) - 8} 项")

    if plan.options.clean_outputs:
        lines.append(f"[回测输出] 保留最近 {plan.options.keep} 期：{len(plan.outputs_keep)} 个文件")
        if plan.outputs_delete:
            delete_mb = sum(p.stat().st_size for p in plan.outputs_delete) / 1024 / 1024
            lines.append(f"[回测输出] 待删过期产物：{len(plan.outputs_delete)} 个文件，{delete_mb:.1f} MB")
            for path in sorted(plan.outputs_delete)[:8]:
                lines.append(f"    · {path.relative_to(plan.project_root)}")
        else:
            lines.append("[回测输出] 无过期回测产物")

    total = plan.delete_total_size()
    lines.append(
        f"[合计] 待清理 {len(plan.delete_candidates())} 项，{total / 1024 / 1024:.1f} MB"
    )
    if not plan.options.execute:
        lines.append("[提示] 当前为 DRY-RUN 预览，未删除任何文件。")
    return lines


# ─────────────────────────────────────────────
# 可运行 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # 可运行测试 demo：对项目根执行一次 dry-run 扫描并打印摘要
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    demo_root = Path(__file__).resolve().parent.parent.parent
    try:
        demo_plan = run_cleanup_scan(demo_root, CleanupOptions(clean_outputs=True, keep=1))
    except ValueError as exc:
        logger.error("选项校验失败: %s", exc)
        raise SystemExit(1)
    print("\n".join(format_plan_summary(demo_plan)))
    print(f"\n（演示结束：dry-run 仅扫描，未删除任何文件）")
