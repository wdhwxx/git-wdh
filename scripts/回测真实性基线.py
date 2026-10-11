#!/usr/bin/env python3
# 文件名：scripts/回测真实性基线.py
"""
scripts/回测真实性基线.py - 回测真实性基线建档薄入口（P0-00 子步骤 B）
================================================================
建立 / 校验"策略 × 引擎 × 配置"的基线净值档案（v2.2 §4.8）：
  - 默认动作：对注册策略跑真实回测 → 输出
    outputs/realism_baseline/{strategy}_{engine}_{config_hash}.json
    （净值曲线哈希 + 关键指标 + 成交明细指纹）；
  - --verify：重跑同配置回测，与已存档基线对拍（净值 max abs diff ≤0.5%
    且成交笔数差 ≤5%），超阈即回归信号（退出码 2）。

本文件为薄入口：仅做参数解析、路径探测与委托 myquant 业务模块，
业务逻辑零写入脚本（架构门禁 R-23 / 治理规则 1.1 约束）。

Usage:
    python scripts/回测真实性基线.py --strategy rsrs --engine mixed
    python scripts/回测真实性基线.py --verify --strategy rsrs --engine mixed
    python scripts/回测真实性基线.py --list          # 列出注册策略
"""

import argparse
import sys
from pathlib import Path

# ── 项目根探测（与既有薄入口同模式：_REPO_ROOT + sys.path 注入）──
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        解析后的参数命名空间。
    """
    parser = argparse.ArgumentParser(
        prog="回测真实性基线",
        description="回测真实性基线建档/校验（P0-00 子步骤 B，v2.2 §4.8）",
    )
    parser.add_argument("--config", default=None, help="回测配置文件路径（默认 config/backtest_config.yaml）")
    parser.add_argument("--strategy", default=None, help="策略名（默认全部注册策略）")
    parser.add_argument("--engine", default=None, choices=["auto", "event", "mixed", "vectorized"],
                        help="引擎模式（默认 auto 按策略能力解析）")
    parser.add_argument("--out", default=None, help="基线输出目录（默认 outputs/realism_baseline）")
    parser.add_argument("--verify", action="store_true", help="重跑对拍模式：与已存档基线比较")
    parser.add_argument("--list", action="store_true", help="列出注册策略与引擎能力后退出")
    return parser.parse_args()


def _list_strategies() -> int:
    """列出注册策略及其支持的引擎模式。

    Returns:
        退出码（0=成功）。
    """
    from myquant.strategies.registry import create_strategy, discover_strategies, list_strategies

    discover_strategies()
    names = list_strategies()
    print("=" * 60)
    print("已注册策略（name → engine_modes）")
    print("=" * 60)
    for name in names:
        try:
            modes = create_strategy(name).engine_modes()
        except Exception as e:  # 实例化失败（如默认参数不合法）仅提示不阻断
            print(f"  {name:<16} → (实例化失败: {e})")
            continue
        print(f"  {name:<16} → {modes}")
    return 0


def _build_broker_factory():
    """构建撮合执行器工厂（SimBroker，与 engine.py 自测 Demo 同构）。

    装配责任在入口层（R-02 只查 myquant/ 包内，scripts/ 合法）；
    event 引擎路径必须注册工厂（G1 EX-09A），mixed/vectorized 不受影响。

    Returns:
        工厂函数 factory(config) -> SimBroker。
    """
    from myquant.execution.sim_broker import SimBroker

    def _factory(config):
        _bcfg = config.to_dict()
        return SimBroker(
            initial_capital=config.initial_capital,
            commission_model=config.commission,
            slippage_model=config.slippage,
            price_limit_pct=float(_bcfg.get("price_limit_pct", 0.10)),
            enforce_limit=bool(_bcfg.get("enforce_limit", False)),
            is_suspended=None,
        )

    return _factory


def _main() -> int:
    """入口主流程（薄委托）。

    Returns:
        退出码：0=成功；2=对拍判红/执行失败。
    """
    args = _parse_args()

    if args.list:
        return _list_strategies()

    try:
        from myquant.backtest.baseline_archive import BaselineArchiver

        archiver = BaselineArchiver(
            project_root=_PROJECT_ROOT,
            config_path=Path(args.config) if args.config else None,
            out_dir=Path(args.out) if args.out else None,
        )
        broker_factory = _build_broker_factory()
    except Exception as e:  # 初始化失败（如配置文件缺失）
        print(f"[错误] 基线档案器初始化失败: {e}")
        return 2

    # 收集目标（策略 × 引擎）
    targets = []
    if args.strategy:
        targets.append((args.strategy, args.engine))
    else:
        from myquant.strategies.registry import create_strategy, discover_strategies, list_strategies

        discover_strategies()
        for name in list_strategies():
            try:
                modes = create_strategy(name).engine_modes()
            except Exception as e:
                print(f"[WARN] {name} 实例化失败，跳过: {e}")
                continue
            for m in modes:
                targets.append((name, m))

    if args.verify:
        # ── 对拍模式 ──
        all_passed = True
        for name, engine_mode in targets:
            try:
                passed, report, path = archiver.verify(
                    strategy_name=name, engine_mode=engine_mode or "auto",
                    broker_factory=broker_factory,
                )
            except FileNotFoundError as e:
                print(f"[WARN] {e}")
                all_passed = False
                continue
            except Exception as e:
                print(f"[错误] {name}/{engine_mode} 对拍执行失败: {e}")
                all_passed = False
                continue
            status = "通过" if passed else "判红（回归信号）"
            print(f"[对拍] {name}/{engine_mode}: {status}  基线={path}")
            if not passed:
                all_passed = False
        return 0 if all_passed else 2

    # ── 建档模式 ──
    for name, engine_mode in targets:
        try:
            path = archiver.archive(
                strategy_name=name, engine_mode=engine_mode or "auto",
                broker_factory=broker_factory,
            )
            print(f"[建档] {name}/{engine_mode} → {path}")
        except Exception as e:
            print(f"[错误] {name}/{engine_mode} 建档失败: {e}")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
