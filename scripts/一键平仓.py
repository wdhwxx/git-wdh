# 文件名：scripts/一键平仓.py
"""
一键平仓 - 紧急应急预案工具（薄入口）
========================================
系统崩溃 / 网络中断 / QMT 无法连接时，人工一键清空全部持仓：
  1. 连接券商 → 2. 撤销全部挂单 → 3. 逐仓市价全仓卖出。
默认【预览模式】（只输出平仓计划，不下单）；真实执行需加 --confirm。
报告输出为可打印文本（放于手边，配合《实盘前最终验收与硬性准备实施细则》
应急预案一节）。

【应急链独立（0.3/R-5）】本脚本与 myquant.live.emergency_liquidation /
myquant.live.broker_factory 组成应急链，**禁止 import 引擎/风控/guardian/
熔断/门禁等业务链模块**（引擎模块异常、装配被破坏时应急平仓仍须可用）；
业务逻辑位于 myquant.live.emergency_liquidation（真实模块），本脚本只做：
  参数解析 → 配置加载 → 券商装配 → 服务调用 → 打印 + 落盘报告。

运行方式:
    # 预览平仓计划（默认，安全）
    python scripts/一键平仓.py --config config/live_config.yaml

    # 真实平仓（LIVE 模式会真实卖出全部持仓！）
    python scripts/一键平仓.py --config config/live_config.yaml --confirm

    # 仅平仓指定标的 + 自定义报告输出
    python scripts/一键平仓.py --config config/live_config.yaml --symbol 600519.SH --confirm
    python scripts/一键平仓.py --config config/live_config.yaml --output D:\\backup\\平仓报告.txt

注意事项:
  - --confirm 为一次性参数：输入一次只执行一次，不记忆、不持久化。
  - LIVE 模式未加 --confirm 时仅预览并打印醒目警告。
  - 脚本不加载策略/引擎/风控，崩溃场景也能独立运行。
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


def find_project_root() -> Path:
    """自动探测项目根目录（向上查找 pyproject.toml）。"""
    script_dir = Path(__file__).resolve().parent
    current = script_dir
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    return script_dir.parent


def setup_argument_parser() -> argparse.ArgumentParser:
    """配置命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="一键平仓",
        description="Pythonwdhmm 量化交易系统 — 紧急一键平仓（应急预案）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  python scripts/一键平仓.py --config config/live_config.yaml            # 预览计划
  python scripts/一键平仓.py --config config/live_config.yaml --confirm   # 真实平仓
  python scripts/一键平仓.py --config config/live_config.yaml --symbol 600519.SH --confirm""",
    )
    parser.add_argument(
        "--config",
        default="config/live_config.yaml",
        help="实盘配置文件（live_config.yaml）路径，默认 config/live_config.yaml",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="仅平仓指定标的（如 600519.SH）；缺省平仓全部持仓",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="真实执行平仓（默认仅预览计划不下单）；LIVE 模式会真实卖出全部持仓！",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="报告输出路径；缺省自动生成 outputs/emergency/一键平仓_<时间戳>.txt",
    )
    return parser


def _load_config(config_path: Path) -> Dict[str, Any]:
    """加载实盘配置文件（yaml）。"""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - 环境缺依赖时给明确指引
        raise SystemExit(f"缺少依赖 pyyaml，请安装: pip install pyyaml（原始错误: {e}）")
    if not config_path.exists():
        raise SystemExit(
            f"配置文件不存在: {config_path}\n"
            f"请先复制模板并填写: copy config/live_config.example.yaml → {config_path}"
        )
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def _format_report(report: Dict[str, Any], config_path: Path) -> str:
    """把结构化报告格式化为可打印文本（人工应急核对用）。"""
    mode = report["mode"]
    lines = [
        "=" * 68,
        "  紧急一键平仓报告",
        "=" * 68,
        f"  执行时间   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  执行模式   : {mode}（{'真实下单' if mode == 'LIVE_EXEC' else '仅预览，未下单'}）",
        f"  真实执行   : {'是' if report.get('executed') else '否（仅生成计划/预览，未真实下单）'}",
        f"  配置文件   : {config_path}",
        f"  券商       : {report['connect'].get('broker', '')}",
        f"  连接状态   : {'正常' if report['connect'].get('ok') else '失败'}",
        f"  整体结果   : {'成功' if report.get('ok') else '存在异常（见下方明细）'}",
        "-" * 68,
        "  【挂单撤销】",
    ]
    if not report["cancelled_orders"]:
        lines.append("    无活跃挂单")
    for c in report["cancelled_orders"]:
        lines.append(
            f"    {'✓' if c.get('ok') else '✗'} {c.get('order_id', '')} "
            f"{c.get('symbol', '')}" + ("（预览）" if c.get("dry_run") else "")
        )
    lines.append("-" * 68)
    lines.append("  【平仓明细】")
    liq = report["liquidation"]
    if not liq["items"]:
        lines.append("    无持仓（空仓）")
    for it in liq["items"]:
        lines.append(
            f"    {it['symbol']:<12} 数量={it['quantity']:>10.0f}  "
            f"现价={it['price']:>10.2f}  市值={it['estimated_value']:>14,.2f}"
        )
        lines.append(
            f"        → 状态={it['status']:<10} "
            f"{it['reason'] or ''}"
            + (f"  委托号={it['order_id']}" if it.get("order_id") else "")
        )
    lines.append("-" * 68)
    lines.append("  【平仓汇总】")
    lines.append(f"    持仓标的数 : {liq['total_symbols']}")
    lines.append(f"    成功/失败  : {liq['success']} / {liq['failed']}")
    lines.append(f"    计划(预览) : {liq.get('planned', 0)}")
    lines.append(f"    跳过(不可卖): {liq['skipped']}")
    lines.append(f"    总卖出数量 : {liq['total_quantity']:,.0f}")
    lines.append(f"    估算卖出额 : {liq['estimated_value']:,.2f} 元")
    lines.append("-" * 68)
    lines.append("  【异常记录】")
    if not report["errors"]:
        lines.append("    无")
    for e in report["errors"]:
        lines.append(f"    ✗ {e}")
    lines.append("=" * 68)
    lines.append("  请人工核对以上明细；若存在失败项，按需手动处理。")
    lines.append("=" * 68)
    return "\n".join(lines)


def main() -> int:
    """一键平仓薄入口主函数。"""
    args = setup_argument_parser().parse_args()
    project_root = find_project_root()
    sys.path.insert(0, str(project_root))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = project_root / config_path
    cfg = _load_config(config_path)

    # 券商装配（应急链独立模块：broker_factory 只依赖 brokers，不依赖引擎/风控/门禁）
    from myquant.live.broker_factory import build_broker_from_config
    from myquant.live.emergency_liquidation import EmergencyLiquidationService

    broker_cfg = cfg.get("broker", {}) or {}
    execution_cfg = cfg.get("execution", {}) or {}
    live_cfg = cfg.get("live", {}) or {}
    initial_capital = float(live_cfg.get("initial_capital", 1_000_000))

    broker = build_broker_from_config(broker_cfg, execution_cfg, initial_capital)
    mode = str(broker_cfg.get("mode", "SIM")).upper()
    if mode == "LIVE" and not args.confirm:
        print(
            "\n⚠️  检测到 LIVE 模式但未加 --confirm：本次仅【预览】，不会真实下单。\n"
            "    确认要真实平仓时，请重新执行并携带 --confirm。\n"
        )
    elif mode == "LIVE" and args.confirm:
        print("\n🚨  LIVE 模式真实平仓确认！即将真实卖出全部（或指定）持仓。\n")

    service = EmergencyLiquidationService(broker)
    report = service.execute(confirm=args.confirm, symbol=args.symbol)

    # 打印 + 落盘可打印报告
    text = _format_report(report, config_path)
    print("\n" + text)

    output = Path(args.output) if args.output else (
        project_root / "outputs" / "emergency"
        / f"一键平仓_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    print(f"\n报告已保存: {output}")

    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
