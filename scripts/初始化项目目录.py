# 文件名：scripts/初始化项目目录.py
"""
初始化项目目录脚本（部署用）
==============================

功能：
    自动创建项目运行时所需的全部目录骨架（含 .gitkeep），确保首次部署或
    新环境克隆后目录结构完整，避免因目录缺失导致的运行时错误。

    本脚本幂等：已存在的目录不会报错，已存在的 .gitkeep 不会被覆盖。

覆盖目录：
    - cache/          运行时缓存（V5新增）：factor / fingerprint / backtest / tmp / benchmark
    - logs/           运行时日志
    - outputs/        回测结果输出：daily_reports
    - datasets/       数据存储：raw / processed / features / bad / live_state
    - database/       SQLite 数据库文件
    - backups/        结果备份归档
    - notebooks/      Jupyter 探索性分析

使用方法：
    # 创建全部目录（默认）
    python scripts/初始化项目目录.py

    # 仅预览，不实际创建
    python scripts/初始化项目目录.py --dry-run

    # 详细输出每个目录的创建状态
    python scripts/初始化项目目录.py --verbose

    # 仅创建 cache 相关目录
    python scripts/初始化项目目录.py --only cache

设计原则：
    - 幂等：重复执行安全，不报错、不覆盖已有文件
    - 防御式：路径校验、异常捕获、清晰错误信息
    - 可审计：--dry-run 预览、--verbose 详细输出、返回创建/跳过计数
    - 零第三方依赖：仅使用标准库
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("init_dirs")

# 目录分组定义：{组名: [相对路径列表]}
# 所有路径相对于项目根目录
DIRECTORY_GROUPS: Dict[str, List[str]] = {
    "cache": [
        "cache",
        "cache/factor",
        "cache/fingerprint",
        "cache/backtest",
        "cache/tmp",
        "cache/benchmark",
    ],
    "logs": [
        "logs",
    ],
    "outputs": [
        "outputs",
        "outputs/daily_reports",
    ],
    "datasets": [
        "datasets",
        "datasets/raw",
        "datasets/processed",
        "datasets/features",
        "datasets/bad",
        "datasets/live_state",
    ],
    "database": [
        "database",
    ],
    "backups": [
        "backups",
    ],
    "notebooks": [
        "notebooks",
    ],
}

# 所有组名列表（用于 --only 参数校验和帮助信息）
ALL_GROUP_NAMES: Tuple[str, ...] = tuple(DIRECTORY_GROUPS.keys())

# .gitkeep 文件名
GITKEEP_FILENAME: str = ".gitkeep"


# ─────────────────────────────────────────────
# 核心函数
# ─────────────────────────────────────────────

def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。

    Returns:
        项目根目录的绝对路径。

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


def get_directories_for_groups(
    groups: Optional[List[str]] = None,
) -> List[str]:
    """根据组名筛选需要创建的目录列表。

    Args:
        groups: 要创建的目录组名列表；None 表示创建全部组。

    Returns:
        去重后的相对路径列表（保持定义顺序）。

    Raises:
        ValueError: 包含未定义的组名。
    """
    if groups is None:
        selected_groups = ALL_GROUP_NAMES
    else:
        # 校验组名
        unknown = [g for g in groups if g not in DIRECTORY_GROUPS]
        if unknown:
            raise ValueError(
                f"未知的目录组: {unknown}. "
                f"可用组: {list(ALL_GROUP_NAMES)}"
            )
        selected_groups = tuple(groups)

    # 按组顺序收集目录，去重（保持首次出现顺序）
    seen: set = set()
    result: List[str] = []
    for group_name in selected_groups:
        for rel_path in DIRECTORY_GROUPS[group_name]:
            if rel_path not in seen:
                seen.add(rel_path)
                result.append(rel_path)
    return result


def create_directory_with_gitkeep(
    project_root: Path,
    rel_path: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[str, bool]:
    """创建单个目录并确保 .gitkeep 存在。

    Args:
        project_root: 项目根目录绝对路径。
        rel_path: 相对于项目根的目录路径。
        dry_run: True 时仅预览不实际创建。
        verbose: True 时输出详细日志。

    Returns:
        (相对路径, 是否为新建) 元组；已存在返回 (路径, False)。

    Raises:
        OSError: 目录创建失败（权限不足等）。
    """
    target = project_root / rel_path
    gitkeep_path = target / GITKEEP_FILENAME

    if target.exists() and target.is_dir():
        if verbose:
            logger.info("已存在，跳过: %s", rel_path)
        # 确保 .gitkeep 存在（目录已存在但可能缺少 .gitkeep）
        if not dry_run and not gitkeep_path.exists():
            try:
                gitkeep_path.touch()
                if verbose:
                    logger.info("补充 .gitkeep: %s/%s", rel_path, GITKEEP_FILENAME)
            except OSError as e:
                logger.error("创建 .gitkeep 失败: %s, 错误: %s", gitkeep_path, e)
                raise
        return (rel_path, False)

    if dry_run:
        logger.info("[预览] 将创建: %s", rel_path)
        return (rel_path, True)

    # 实际创建
    try:
        target.mkdir(parents=True, exist_ok=True)
        gitkeep_path.touch()
        logger.info("已创建: %s", rel_path)
        return (rel_path, True)
    except OSError as e:
        logger.error("创建目录失败: %s, 错误: %s", target, e)
        raise


def initialize_directories(
    project_root: Path,
    groups: Optional[List[str]] = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Dict[str, int]:
    """批量初始化项目目录。

    Args:
        project_root: 项目根目录绝对路径。
        groups: 要创建的目录组名列表；None 表示全部。
        dry_run: True 时仅预览。
        verbose: True 时详细输出。

    Returns:
        统计字典: {"total": 总数, "created": 新建数, "skipped": 已存在数}。

    Raises:
        ValueError: 组名无效。
        OSError: 目录创建失败。
    """
    directories = get_directories_for_groups(groups)
    total = len(directories)
    created = 0
    skipped = 0

    logger.info(
        "开始初始化目录: 项目根=%s, 目录数=%d, dry_run=%s",
        project_root, total, dry_run,
    )

    for rel_path in directories:
        _, is_new = create_directory_with_gitkeep(
            project_root=project_root,
            rel_path=rel_path,
            dry_run=dry_run,
            verbose=verbose,
        )
        if is_new:
            created += 1
        else:
            skipped += 1

    stats = {"total": total, "created": created, "skipped": skipped}
    logger.info(
        "目录初始化完成: 总计=%d, 新建=%d, 已存在跳过=%d",
        stats["total"], stats["created"], stats["skipped"],
    )
    return stats


def verify_directories(
    project_root: Path,
    groups: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """校验目录是否全部存在。

    Args:
        project_root: 项目根目录绝对路径。
        groups: 要校验的目录组名列表；None 表示全部。

    Returns:
        (存在的目录列表, 缺失的目录列表) 元组。
    """
    directories = get_directories_for_groups(groups)
    existing: List[str] = []
    missing: List[str] = []
    for rel_path in directories:
        target = project_root / rel_path
        if target.exists() and target.is_dir():
            existing.append(rel_path)
        else:
            missing.append(rel_path)
    return existing, missing


# ─────────────────────────────────────────────
# 命令行接口
# ─────────────────────────────────────────────

def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。

    Returns:
        配置好的 ArgumentParser 实例。
    """
    parser = argparse.ArgumentParser(
        prog="初始化项目目录",
        description="Pythonwdhmm 鲲鹏量化交易系统 — 运行时目录初始化（部署用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python scripts/初始化项目目录.py              # 创建全部目录\n"
            "  python scripts/初始化项目目录.py --dry-run    # 仅预览\n"
            "  python scripts/初始化项目目录.py --only cache # 仅创建缓存目录\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅预览，不实际创建目录",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出每个目录的创建状态",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        choices=list(ALL_GROUP_NAMES),
        metavar="GROUP",
        help=f"仅创建指定目录组，可选: {list(ALL_GROUP_NAMES)}",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="仅校验目录是否存在，不创建",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="运行内置自测 Demo（使用临时目录，不影响实际项目）",
    )
    return parser


def main() -> int:
    """主入口函数。

    Returns:
        退出码: 0=成功, 1=失败。
    """
    parser = setup_argument_parser()
    args = parser.parse_args()

    try:
        project_root = find_project_root()
    except RuntimeError as e:
        logger.error("%s", e)
        return 1

    groups = args.only

    # --verify 模式：仅校验不创建
    if args.verify:
        existing, missing = verify_directories(project_root, groups)
        logger.info("校验结果: 存在=%d, 缺失=%d", len(existing), len(missing))
        if missing:
            logger.warning("缺失的目录:")
            for m in missing:
                logger.warning("  - %s", m)
            return 1
        logger.info("全部目录已存在，校验通过。")
        return 0

    # 正常模式：创建目录
    try:
        initialize_directories(
            project_root=project_root,
            groups=groups,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except (ValueError, OSError) as e:
        logger.error("目录初始化失败: %s", e)
        return 1

    # 非 dry-run 时做一次校验
    if not args.dry_run:
        _, missing = verify_directories(project_root, groups)
        if missing:
            logger.error("初始化后仍有缺失目录: %s", missing)
            return 1

    if args.dry_run:
        logger.info("[预览模式] 未实际创建任何目录。")
    else:
        logger.info("目录初始化成功完成。")

    return 0


# ─────────────────────────────────────────────
# 自测 Demo（--self-test 触发，使用临时目录）
# ─────────────────────────────────────────────

def run_self_test() -> None:
    """运行内置自测 Demo。

    使用临时目录测试目录创建功能，不影响实际项目结构。
    find_project_root() 测试使用实际项目（只读，无副作用）。
    """
    print("=" * 60)
    print("  初始化项目目录.py — 自测 Demo")
    print("=" * 60)

    # 1. 探测项目根目录（只读测试）
    print("\n[1] 探测项目根目录")
    root = find_project_root()
    print(f"    项目根: {root}")
    assert (root / "pyproject.toml").exists(), "pyproject.toml 应存在"
    print("    ✓ pyproject.toml 存在")

    # 2. 获取全部目录列表（纯函数测试）
    print("\n[2] 获取全部目录列表")
    all_dirs = get_directories_for_groups()
    print(f"    共 {len(all_dirs)} 个目录")
    assert len(all_dirs) > 0, "目录列表不应为空"
    assert "cache/factor" in all_dirs
    assert "cache/fingerprint" in all_dirs
    print("    ✓ 目录列表非空，包含 cache 子目录")

    # 3. 组筛选
    print("\n[3] 组筛选 (仅 cache 组)")
    cache_dirs = get_directories_for_groups(["cache"])
    print(f"    cache 组共 {len(cache_dirs)} 个目录")
    assert len(cache_dirs) == 6
    assert "logs" not in cache_dirs, "cache 组不应包含 logs"
    print("    ✓ 组筛选正确")

    # 4. 未知组名校验
    print("\n[4] 未知组名校验")
    try:
        get_directories_for_groups(["nonexistent_group"])
        assert False, "应抛出 ValueError"
    except ValueError:
        print("    ✓ 正确抛出 ValueError")

    # 5. 使用临时目录测试实际创建
    print("\n[5] 临时目录测试 — 创建 cache 组")
    tmp_root = Path(tempfile.mkdtemp(prefix="init_dirs_test_"))
    print(f"    临时根目录: {tmp_root}")
    try:
        stats1 = initialize_directories(tmp_root, groups=["cache"], dry_run=False)
        print(f"    首次统计: {stats1}")
        assert stats1["created"] == 6, f"应新建 6 个目录, 实际 {stats1['created']}"

        # 验证目录和 .gitkeep
        for d in cache_dirs:
            assert (tmp_root / d).is_dir(), f"目录应存在: {d}"
            assert (tmp_root / d / ".gitkeep").exists(), f".gitkeep 应存在: {d}"
        print("    ✓ 6 个目录及 .gitkeep 全部创建")

        # 6. 幂等验证
        print("\n[6] 幂等验证（重复执行）")
        stats2 = initialize_directories(tmp_root, groups=["cache"], dry_run=False)
        print(f"    重复统计: {stats2}")
        assert stats2["created"] == 0, "重复执行不应有新建目录"
        assert stats2["skipped"] == 6, "重复执行应全部跳过"
        print("    ✓ 幂等性验证通过（新建数=0）")

        # 7. dry-run 验证
        print("\n[7] dry-run 验证（不实际创建）")
        new_dir = "cache/test_dry_run"
        # 临时添加一个测试目录到 DIRECTORY_GROUPS 不可行，直接测试单目录函数
        _, is_new = create_directory_with_gitkeep(
            tmp_root, new_dir, dry_run=True, verbose=False
        )
        assert is_new is True, "dry-run 应报告为新建"
        assert not (tmp_root / new_dir).exists(), "dry-run 不应实际创建目录"
        print("    ✓ dry-run 不实际创建目录")

        # 8. 校验功能
        print("\n[8] 校验功能")
        existing, missing = verify_directories(tmp_root, groups=["cache"])
        print(f"    存在: {len(existing)}, 缺失: {len(missing)}")
        assert len(missing) == 0, f"不应有缺失目录: {missing}"
        print("    ✓ 全部 cache 目录校验通过")

        # 9. 校验缺失检测
        print("\n[9] 缺失目录检测")
        existing2, missing2 = verify_directories(tmp_root, groups=["logs"])
        assert len(missing2) == 1, "logs 目录应缺失"
        assert "logs" in missing2
        print(f"    ✓ 正确检测到缺失目录: {missing2}")

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_root, ignore_errors=True)
        print(f"\n[10] 临时目录已清理: {tmp_root}")

    print("\n" + "=" * 60)
    print("  ✓ 初始化项目目录.py 全部自测通过")
    print("=" * 60)


if __name__ == "__main__":
    # --self-test 模式：运行内置自测（使用临时目录，不影响实际项目）
    if "--self-test" in sys.argv:
        run_self_test()
        sys.exit(0)
    # 默认模式：执行目录初始化
    sys.exit(main())
