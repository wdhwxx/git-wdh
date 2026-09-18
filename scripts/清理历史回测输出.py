# -*- coding: utf-8 -*-
# 文件名：scripts/清理历史回测输出.py
"""
清理 outputs 历史回测输出（只保留最近 N 期）
=============================================
扫描 outputs/ 根目录下的回测产物文件，按回测期（文件名中的
YYYYMMDD_HHMMSS 时间戳）归组，仅保留时间戳最近的一期（或 --keep 指定期数），
删除其余期的全部产物。默认 dry-run（只报告不删除），加 --execute 才真正删除。

回测产物识别（仅匹配以下命名模式，其余文件/目录一律不碰）：
  - report_<YYYYMMDD_HHMMSS>.pdf / .html
  - equity_<YYYYMMDD_HHMMSS>.csv
  - trades_<YYYYMMDD_HHMMSS>.csv
  - summary_<YYYYMMDD_HHMMSS>.txt
  - result_<unix秒>.json   （run 结果，文件名数字≈回测完成时间，
                           与 report 时间戳差约 30 秒，故单独排序保留）

保护范围（永不删除）：
  - 所有子目录（daily_reports/、report_fixed_*/、backups/ 等）及 .gitkeep
  - 非上述命名模式的任意文件（人工产物、其他脚本输出）
  - result_*.json 中无法解析时间戳的文件（保守保留）

用法：
  python scripts/清理历史回测输出.py                    # dry-run，只报告
  python scripts/清理历史回测输出.py --execute          # 执行：只保留最近 1 期
  python scripts/清理历史回测输出.py --execute --keep 2 # 保留最近 2 期
  python scripts/清理历史回测输出.py --json             # 输出 JSON 报告

设计原则：
  - 薄入口工具：无业务逻辑，仅文件扫描与期归组，符合项目蓝图脚本层定位
  - 防御式：正则兜底、异常捕获并记录日志，绝不静默吞错
  - 保守优先：无法确认归属/时间戳的文件一律保留
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("clean_outputs")

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
# 核心函数
# ─────────────────────────────────────────────

def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。

    Returns:
        项目根目录绝对路径。

    Raises:
        RuntimeError: 向上搜索达到最大深度仍未找到 pyproject.toml。
    """
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    max_depth: int = 20
    depth: int = 0
    while current != current.parent and depth < max_depth:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        depth += 1
    raise RuntimeError(
        "无法自动探测项目根目录（找不到 pyproject.toml）。"
        f"搜索起点: {script_dir}"
    )


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


def plan_cleanup(
    periods: Dict[str, List[Path]],
    result_files: List[Tuple[str, Path]],
    keep: int,
) -> Tuple[List[Path], List[Path]]:
    """计算保留与删除的文件清单。

    Args:
        periods: 回测期 → 文件列表（来自 scan_period_files）。
        result_files: [(期时间戳, 文件路径)]（来自 scan_result_files）。
        keep: 保留的最近期数，必须 ≥ 1。

    Returns:
        (保留文件列表, 删除文件列表) 元组。

    Raises:
        ValueError: keep 小于 1 时抛出。
    """
    if keep < 1:
        raise ValueError(f"keep 必须 ≥ 1，实际为 {keep}")
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


def execute_deletion(paths: List[Path]) -> Tuple[int, int, int]:
    """执行文件删除。

    Args:
        paths: 待删除文件列表。

    Returns:
        (成功数, 失败数, 释放字节数) 元组。
    """
    deleted = 0
    failed = 0
    freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
            path.unlink()
            deleted += 1
            freed += size
        except FileNotFoundError:
            # 文件已被外部删除，视为成功
            deleted += 1
        except OSError as exc:
            failed += 1
            logger.error("删除失败: %s (%s)", path, exc)
    return deleted, failed, freed


def print_report(
    keep_files: List[Path],
    delete_files: List[Path],
    execute: bool,
    project_root: Path,
) -> None:
    """输出清理计划/结果报告（日志方式）。

    Args:
        keep_files: 保留文件列表。
        delete_files: 待删除文件列表。
        execute: 是否已执行删除。
        project_root: 项目根目录（用于相对路径展示）。
    """
    mode = "【执行结果】" if execute else "【DRY-RUN 预览】"
    logger.info("=" * 60)
    logger.info("outputs 历史回测清理 %s", mode)
    logger.info("=" * 60)
    if delete_files:
        logger.info("将删除 %d 个文件（共 %.1f MB）:",
                    len(delete_files),
                    sum(p.stat().st_size for p in delete_files) / 1024 / 1024)
        for path in sorted(delete_files):
            logger.info("  - %s", path.relative_to(project_root))
    else:
        logger.info("无过期回测产物，无需清理。")
    logger.info("保留 %d 个文件:", len(keep_files))
    for path in sorted(keep_files):
        logger.info("  + %s", path.relative_to(project_root))
    if not execute:
        logger.info("当前为 DRY-RUN，未删除任何文件；加 --execute 执行。")


def main() -> int:
    """主入口函数。

    Returns:
        退出码: 0=成功, 1=失败。
    """
    parser = argparse.ArgumentParser(
        prog="清理历史回测输出",
        description="Pythonwdhmm 鲲鹏量化交易系统 — outputs 历史回测清理（默认 dry-run）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/清理历史回测输出.py              # dry-run 预览\n"
            "  python scripts/清理历史回测输出.py --execute    # 只保留最近 1 期\n"
            "  python scripts/清理历史回测输出.py --execute --keep 2  # 保留最近 2 期\n"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="真正执行删除（默认只预览）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"保留的最近回测期数（默认 {DEFAULT_KEEP}）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式报告")
    args = parser.parse_args()

    try:
        project_root = find_project_root()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    out_dir = project_root / "outputs"

    # 防御：keep 参数校验
    try:
        keep_files, delete_files = plan_cleanup(
            periods=scan_period_files(out_dir),
            result_files=scan_result_files(out_dir),
            keep=args.keep,
        )
    except ValueError as exc:
        logger.error("参数错误: %s", exc)
        return 1

    if args.json:
        report = {
            "scan_time": datetime.now().isoformat(timespec="seconds"),
            "execute": args.execute,
            "keep": args.keep,
            "keep_count": len(keep_files),
            "delete_count": len(delete_files),
            "delete_size_mb": round(
                sum(p.stat().st_size for p in delete_files) / 1024 / 1024, 2),
            "kept": [str(p.relative_to(project_root)) for p in sorted(keep_files)],
            "deleted": [str(p.relative_to(project_root)) for p in sorted(delete_files)],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.execute and delete_files:
        logger.info("开始删除 %d 个文件...", len(delete_files))
        deleted, failed, freed = execute_deletion(delete_files)
        keep_files_after = keep_files + [p for p in delete_files
                                         if not p.exists()]
        logger.info("清理完成: 成功 %d, 失败 %d, 释放 %.1f MB",
                    deleted, failed, freed / 1024 / 1024)
        print_report(keep_files_after, [], execute=True, project_root=project_root)
        return 0 if failed == 0 else 1

    print_report(keep_files, delete_files, execute=False, project_root=project_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
