# -*- coding: utf-8 -*-
# 文件名：scripts/垃圾数据清理.py
"""
项目垃圾数据清理工具（薄入口 + 扫描引擎）
==========================================
扫描 Pythonwdhmm 项目数据存储目录，识别并清理测试残留、孤儿文件、旧格式冗余、
缓存和轮转日志。默认 dry-run（只报告不删除），加 --execute 才真正删除。

垃圾分类（按安全等级）：
  A. 明确测试残留  — 文件名含 TEST/TEST_EXEC/TICK_TEST/EMPTY_TEST/HOOK_TEST/
                     unknown/_verify/_入口/test_，以及本次开发产生的孤儿文件
  B. 测试备份      — backups/backup_*_quant_before_clean.db（全量清洗测试产生）
  C. Python 缓存   — **/__pycache__/（删除后 Python 自动重建）
  D. 轮转日志      — logs/*.log.1
  E. 旧格式冗余    — datasets/processed/*/ 子目录（CSV 子目录格式，
                     当前回测用扁平 parquet，需 --include-legacy 才删）
  F. 疑似 API 残留 — outputs/result_*.json（需 --purge-results 才删）

保护清单（永不删除）：
  - database/quant.db（主数据库）
  - datasets/processed/*.parquet（回测主数据）
  - outputs/report_*/equity_*/trades_*/summary_*（真实回测报告）
  - datasets/bad/ 中非测试的真实脏数据归档
  - backups/backup_20260829_083316/（用户原有结果备份）
  - 所有 .gitkeep

用法：
  python scripts/垃圾数据清理.py                    # dry-run，只扫描报告
  python scripts/垃圾数据清理.py --execute          # 执行清理（A+B+C+D）
  python scripts/垃圾数据清理.py --execute --include-legacy   # 额外删旧格式 CSV
  python scripts/垃圾数据清理.py --execute --purge-results    # 额外删 result_*.json
  python scripts/垃圾数据清理.py --execute --all              # 全部清理
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# 垃圾识别规则
# ============================================================

# 文件名中包含这些关键词即判定为测试残留（A 类）
TEST_KEYWORDS = (
    "TEST_EXEC", "TICK_TEST", "EMPTY_TEST", "HOOK_TEST",
    "_入口测试", "_verify_full",
)
# 文件名精确匹配测试标识（更宽松，含 TEST 但排除真实标的如 600519）
TEST_NAME_PATTERNS = (
    "bad_bar_TEST", "bad_tick_TICK_TEST",
    "quality_report_bar_TEST", "quality_report_bar_EMPTY_TEST",
    "quality_report_bar_HOOK_TEST", "quality_report_bar_unknown",
    "quality_report_tick_TICK_TEST",
    "test_mc_report", "test_report",
    "_verify_equity", "_verify_mc_report", "_verify_no_mc",
    "_verify_report", "_verify_trades",
)

# 孤儿 bad 文件回退规则（仅当 clean_run_meta 数据库不可读时使用）
# 正常情况以 clean_run_meta 为准：bad 文件名未被引用 -> 孤儿 -> 可清理
ORPHAN_BAD_PATTERN = "bad_bar_*_20260830_225*.json"

# 全量清洗测试产生的备份和报告
# 注意：clean 报告文件名为 clean_all_full_clean_<batch>.json，不能用硬编码时间戳，否则新批次永远扫不到
TEST_BACKUP_PATTERN = "backup_*_quant_before_clean.db"
TEST_CLEAN_REPORT_PATTERN = "clean_all_full_clean_*.json"

# 保护：真实 bad 归档（非测试、非孤儿）
PROTECTED_BAD = {"bad_bar_000001.SZ_20260827_195958.json"}

# 保护：用户原有备份目录
PROTECTED_BACKUP_DIRS = {"backup_20260829_083316"}


@dataclass
class JunkItem:
    path: Path
    category: str  # A/B/C/D/E/F
    reason: str
    size: int = 0

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)


@dataclass
class ScanResult:
    items: List[JunkItem] = field(default_factory=list)

    def add(self, path: Path, category: str, reason: str):
        if path.is_file():
            self.items.append(JunkItem(path, category, reason, path.stat().st_size))
        elif path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            self.items.append(JunkItem(path, category, reason, total))

    def total_size(self) -> int:
        return sum(i.size for i in self.items)

    def by_category(self) -> dict:
        result = {}
        for i in self.items:
            result.setdefault(i.category, []).append(i)
        return result


# ============================================================
# 扫描引擎
# ============================================================

def _ts_of(name: str) -> str:
    """从文件名提取 YYYYMMDD_HHMMSS 时间戳，无则返回空串。"""
    import re
    m = re.search(r"_(\d{8}_\d{6})\.json$", name)
    return m.group(1) if m else ""


def _load_referenced_bad_names():
    """从 database/quant.db 的 clean_run_meta 读取被引用的 bad 文件基名集合（只读）。

    孤儿判定以该集合为准：bad 文件名不在集合中且不在保护名单 -> 孤儿测试残留。
    数据库不可用（被占用/损坏）时返回 None，调用方回退到 ORPHAN_BAD_PATTERN。
    """
    import sqlite3
    db_path = PROJECT_ROOT / "database" / "quant.db"
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute(
                "SELECT bad_file FROM clean_run_meta "
                "WHERE bad_file IS NOT NULL AND bad_file != ''"
            ).fetchall()
            return {Path(r[0]).name for r in rows}
        finally:
            con.close()
    except Exception:
        return None

def scan_test_residuals(result: ScanResult):
    """A 类：测试残留文件"""
    import fnmatch
    # 孤儿判定：以 clean_run_meta 为准（无记录且非保护 -> 孤儿）
    referenced_bad = _load_referenced_bad_names()
    orphan_ts = set()  # 孤儿 bad 的时间戳，用于关联同批 quality_report

    # datasets/bad/
    bad_dir = PROJECT_ROOT / "datasets" / "bad"
    if bad_dir.exists():
        for f in bad_dir.iterdir():
            if not f.is_file() or f.name == ".gitkeep":
                continue
            if f.name in PROTECTED_BAD:
                continue
            # 测试关键词
            if any(kw in f.name for kw in TEST_KEYWORDS):
                result.add(f, "A", f"bad 测试残留（含 {next(kw for kw in TEST_KEYWORDS if kw in f.name)}）")
                continue
            if any(pat in f.name for pat in TEST_NAME_PATTERNS):
                result.add(f, "A", "bad 测试残留（测试命名）")
                continue
            # 孤儿 bad：优先以 clean_run_meta 为准，DB 不可读时回退时间窗规则
            if referenced_bad is not None:
                if f.name not in referenced_bad:
                    orphan_ts.add(_ts_of(f.name))
                    result.add(f, "A", "bad 孤儿文件（clean_run_meta 无对应记录）")
            elif fnmatch.fnmatch(f.name, ORPHAN_BAD_PATTERN):
                result.add(f, "A", "bad 孤儿文件（副本测试产生，无对应元信息）")

    # outputs/
    out_dir = PROJECT_ROOT / "outputs"
    if out_dir.exists():
        for f in out_dir.iterdir():
            if f.is_file():
                if any(kw in f.name for kw in TEST_KEYWORDS):
                    result.add(f, "A", f"outputs 测试残留（含 {next(kw for kw in TEST_KEYWORDS if kw in f.name)}）")
                    continue
                if any(pat in f.name for pat in TEST_NAME_PATTERNS):
                    result.add(f, "A", "outputs 测试残留（测试命名）")
                    continue
                if fnmatch.fnmatch(f.name, TEST_CLEAN_REPORT_PATTERN):
                    result.add(f, "A", "全量清洗测试报告（开发测试产生）")
                    continue
                # 孤儿 quality_report：与孤儿 bad 同批（同时间戳）-> 一并清理
                if f.name.startswith("quality_report_bar_") and _ts_of(f.name) in orphan_ts:
                    result.add(f, "A", "quality_report 孤儿（与孤儿 bad 同批测试产生）")
            elif f.is_dir():
                if any(kw in f.name for kw in TEST_KEYWORDS):
                    result.add(f, "A", f"outputs 测试目录（{f.name}）")


def scan_test_backups(result: ScanResult):
    """B 类：测试备份（含 SQLite 伴生 -shm/-wal，避免删 .db 后残留孤儿文件）"""
    import fnmatch
    backup_dir = PROJECT_ROOT / "backups"
    if not backup_dir.exists():
        return
    for f in backup_dir.iterdir():
        if f.is_file() and fnmatch.fnmatch(f.name, TEST_BACKUP_PATTERN):
            result.add(f, "B", "全量清洗测试产生的数据库备份（副本测试，非用户数据）")
            for suffix in ("-shm", "-wal"):
                comp = Path(str(f) + suffix)
                if comp.exists() and comp.is_file():
                    result.add(comp, "B", "SQLite 伴生文件（随 .db 一并清理）")


def scan_pycache(result: ScanResult):
    """C 类：Python 缓存"""
    for d in PROJECT_ROOT.rglob("__pycache__"):
        if d.is_dir() and ".git" not in d.parts:
            result.add(d, "C", "Python 字节码缓存（删除后自动重建）")


def scan_rotated_logs(result: ScanResult):
    """D 类：轮转日志"""
    log_dir = PROJECT_ROOT / "logs"
    if not log_dir.exists():
        return
    for f in log_dir.glob("*.log.1"):
        result.add(f, "D", "日志轮转备份（.log.1）")


def scan_legacy_processed(result: ScanResult):
    """E 类：旧格式 processed 子目录（CSV 子目录格式）"""
    proc_dir = PROJECT_ROOT / "datasets" / "processed"
    if not proc_dir.exists():
        return
    for d in proc_dir.iterdir():
        if d.is_dir():
            result.add(d, "E", "旧格式 CSV 子目录（当前回测用扁平 parquet）")


def scan_result_json(result: ScanResult):
    """F 类：疑似 API 残留 result_*.json"""
    out_dir = PROJECT_ROOT / "outputs"
    if not out_dir.exists():
        return
    for f in out_dir.glob("result_*.json"):
        result.add(f, "F", "疑似 API 调用结果残留（result_<timestamp>.json）")


# ============================================================
# 报告与执行
# ============================================================

CATEGORY_NAMES = {
    "A": "测试残留",
    "B": "测试备份",
    "C": "Python 缓存",
    "D": "轮转日志",
    "E": "旧格式冗余",
    "F": "疑似 API 残留",
}


def print_report(result: ScanResult, execute: bool):
    print("\n" + "=" * 70)
    print(f"  垃圾数据扫描报告  {'【执行模式】' if execute else '【DRY-RUN 预览】'}")
    print("=" * 70)

    by_cat = result.by_category()
    grand_total = 0
    for cat in sorted(by_cat.keys()):
        items = by_cat[cat]
        cat_size = sum(i.size for i in items)
        grand_total += cat_size
        print(f"\n[{cat}] {CATEGORY_NAMES[cat]}  ({len(items)} 项, {cat_size/1024/1024:.1f} MB)")
        for item in items[:15]:
            rel = item.path.relative_to(PROJECT_ROOT)
            print(f"    {item.size_mb:8.2f} MB  {rel}")
        if len(items) > 15:
            print(f"    ... 还有 {len(items) - 15} 项")

    print(f"\n{'─' * 70}")
    print(f"  合计: {len(result.items)} 项, {grand_total / 1024 / 1024:.1f} MB ({grand_total / 1024 / 1024 / 1024:.2f} GB)")
    print(f"{'─' * 70}")

    if not execute:
        print("\n  当前为 DRY-RUN，未删除任何文件。")
        print("  加 --execute 执行清理（A+B+C+D）。")
        print("  加 --include-legacy 额外清理旧格式 CSV 子目录（E）。")
        print("  加 --purge-results 额外清理 result_*.json（F）。")
        print("  加 --all 清理全部类别。")


def execute_cleanup(result: ScanResult) -> tuple:
    """执行删除，返回 (成功数, 失败数, 释放字节)"""
    deleted = 0
    failed = 0
    freed = 0
    for item in result.items:
        try:
            if item.path.is_file():
                item.path.unlink()
            elif item.path.is_dir():
                import shutil
                shutil.rmtree(item.path)
            deleted += 1
            freed += item.size
        except Exception as e:
            failed += 1
            print(f"  [失败] {item.path}: {e}")
    return deleted, failed, freed


def parse_args():
    p = argparse.ArgumentParser(
        description="Pythonwdhmm 项目垃圾数据清理（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--execute", action="store_true", help="真正执行删除（默认只扫描报告）")
    p.add_argument("--include-legacy", action="store_true", help="包含旧格式 processed CSV 子目录（E 类）")
    p.add_argument("--purge-results", action="store_true", help="包含 outputs/result_*.json（F 类）")
    p.add_argument("--all", action="store_true", help="清理全部类别（A+B+C+D+E+F）")
    p.add_argument("--json", action="store_true", help="输出 JSON 格式报告")
    return p.parse_args()


def main():
    args = parse_args()
    include_legacy = args.include_legacy or args.all
    purge_results = args.purge_results or args.all

    result = ScanResult()

    # 始终扫描 A/B/C/D
    scan_test_residuals(result)
    scan_test_backups(result)
    scan_pycache(result)
    scan_rotated_logs(result)
    # 可选 E/F
    if include_legacy:
        scan_legacy_processed(result)
    if purge_results:
        scan_result_json(result)

    if args.json:
        report = {
            "scan_time": datetime.now().isoformat(timespec="seconds"),
            "execute": args.execute,
            "total_items": len(result.items),
            "total_size_mb": round(result.total_size() / 1024 / 1024, 2),
            "items": [
                {"path": str(i.path.relative_to(PROJECT_ROOT)),
                 "category": i.category,
                 "category_name": CATEGORY_NAMES[i.category],
                 "reason": i.reason,
                 "size_mb": round(i.size_mb, 4)}
                for i in result.items
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print_report(result, args.execute)

    if args.execute and result.items:
        print(f"\n开始删除 {len(result.items)} 项...")
        deleted, failed, freed = execute_cleanup(result)
        print(f"\n清理完成: 成功 {deleted}, 失败 {failed}, 释放 {freed / 1024 / 1024:.1f} MB")
    elif args.execute:
        print("\n没有可清理的垃圾数据。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
