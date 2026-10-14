# 文件名：scripts/验收_监控M1保命.py
"""
验收_监控M1保命.py - A-15 M1 保命验收脚本（中文入口，薄入口）
================================================================
按《量化系统监控模块施工总清单_V1.2》A-15-Min / A-15-Full 项，一键运行
M1 保命闭环验收：

  - A-15-Min  : 保命闭环四环覆盖率 100% + FI-01/03/04/05 注入检出且走事件路径
  - A-15-Full : FI-01~05 注入按 P0-8 口径计时（MTTD<1min / MTTA<5min /
                MTTR<30min）+ EVENT 项事件路径验证 + TP/FP/FN 分类统计

业务逻辑位于 myquant.acceptance.m1_acceptance（M1AcceptanceRunner），
本脚本只做：参数解析 → 项目根探测 → 调用 → 输出报告（控制台 + JSON）。

运行方式:
    # A-15-Min 子集验收
    python scripts/验收_监控M1保命.py --mode min

    # A-15-Full 完整验收（含 MTTx 计时 + 分类）
    python scripts/验收_监控M1保命.py --mode full

    # 两档都跑，并把 JSON 报告落到 docs/监控档案/
    python scripts/验收_监控M1保命.py --mode both --out docs/监控档案/M1保命验收_YYYYMMDD.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


# 顶部 sys.path 注入：CLI 直跑免设 PYTHONPATH（与监控可测量性统计.py 等对齐）
def _bootstrap_root() -> Path:
    """向上探测项目根（pyproject.toml），失败即明确报错。"""
    current = Path(__file__).resolve().parent
    for _ in range(20):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
        if current == current.parent:
            break
    raise RuntimeError(f"未找到项目根目录（pyproject.toml），起点: {Path(__file__)}")


_PROJECT_ROOT = _bootstrap_root()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from myquant.acceptance.m1_acceptance import (
    M1AcceptanceRunner,  # noqa: E402  # 业务模块
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("验收_监控M1保命")


def _run_one(mode: str, store_db_path: Any) -> Dict[str, Any]:
    """运行单档验收并返回报告（临时告警库，结束后清理）。

    Args:
        mode: min / full。
        store_db_path: 告警临时落库路径；None 时用临时目录自动清理。

    Returns:
        dict: M1AcceptanceRunner.run(mode) 的验收报告。
    """
    runner = M1AcceptanceRunner(store_db_path=store_db_path)
    try:
        report = runner.run(mode)
    finally:
        runner.close()
    return report


def _print_report(report: Dict[str, Any]) -> None:
    """打印验收报告摘要。"""
    mode: str = report["mode"]
    verdict: Dict[str, Any] = report["verdict"]
    coverage: Dict[str, Any] = report["coverage"]

    print("\n" + "=" * 64)
    print(f"A-15{'·Min' if mode == 'min' else '·Full'}  M1 保命验收")
    print("=" * 64)
    print(f"  保命闭环覆盖率: {coverage['coverage_pct'] * 100:.0f}% "
          f"({coverage['covered']}/{coverage['total']})")
    for ring in coverage["rings"]:
        print(f"    [{ring['item']}] {ring['ring']}: "
              f"{'✅ 覆盖' if ring['covered'] else '❌ 缺'}")
    print("  故障注入用例:")
    for r in report["fi"]:
        status = "✅ 检出" if r.get("captured") else "❌ 未检出"
        mttd = r.get("mttd_seconds")
        print(f"    {r['case_id']} [{r['expected_capture']}] {status}"
              + (f"  MTTD={mttd:.3f}s" if mttd is not None else ""))
    if mode == "full":
        cls: Dict[str, Any] = verdict["classification"]
        print(f"  分类(按 P0-8): TP={cls['tp']} FP={cls['fp']} FN={cls['fn']} "
              f"准确率={cls['accuracy']} 误报率={cls['fp_rate']} 检出率={cls['detection_rate']}")
        print(f"  事件路径验证(EVENT): {'✅ 通过' if verdict['event_path_ok'] else '❌ 未过'}")
    print(f"  判定: {'✅ 通过' if verdict['pass'] else '❌ 未通过'} — {verdict['reason']}")
    if report.get("caveats"):
        print("  注记:")
        for c in report["caveats"]:
            print(f"    - {c}")
    print("=" * 64)


def main() -> int:
    """薄入口主流程：argparse → 调用业务模块 → 输出报告。"""
    parser = argparse.ArgumentParser(
        description="A-15 M1 保命验收（Min/Full）"
    )
    parser.add_argument(
        "--mode", choices=("min", "full", "both"), default="both",
        help="验收档位：min=A-15-Min，full=A-15-Full，both=两档都跑",
    )
    parser.add_argument("--out", metavar="FILE", help="验收报告 JSON 输出路径")
    parser.add_argument(
        "--db", metavar="FILE",
        help="告警临时落库路径（默认临时目录，自动清理；一般不传）",
    )
    args = parser.parse_args()

    store_db_path: Any = args.db or None  # None → runner 用临时目录并自动清理

    modes: tuple = ("min", "full") if args.mode == "both" else (args.mode,)
    results: Dict[str, Any] = {}
    overall: bool = True
    for mode in modes:
        report = _run_one(mode, store_db_path)
        results[mode] = report
        _print_report(report)
        overall = overall and bool(report["verdict"]["pass"])

    bundle: Dict[str, Any] = {
        "source": "验收_监控M1保命",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
        "overall_pass": overall,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2)
        logger.info("验收报告已写入: %s", out_path)
    else:
        print(json.dumps(bundle, ensure_ascii=False, indent=2))

    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
