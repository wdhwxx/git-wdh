# 文件名：scripts/验收_B包保钱包.py
"""
验收_B包保钱包.py - B-10 B 包（保钱包）验收脚本（中文入口，薄入口）
================================================================
按《量化系统监控模块施工总清单_V1.2》B-10 项，一键运行 B 包保钱包九项验收：

  - B-1 对账差异=0 可展示
  - B-2 PnL 可信
  - B-3 限额闭环
  - B-4/5 执行质量（四段延迟 P99 + ACK P2 + 拒单率 P2）
  - B-6 熔断闭环
  - B-7 新鲜度
  - B-8 驾驶舱四视图
  - B-9 保证金（PENDING_CAL 语义验收）

业务逻辑位于 myquant.acceptance.b_acceptance（BAcceptanceRunner），
本脚本只做：参数解析 → 项目根探测 → 调用 → 输出报告（控制台 + JSON）。

运行方式:
    python scripts/验收_B包保钱包.py
    python scripts/验收_B包保钱包.py --out docs/监控档案/B包保钱包验收_20261006.json
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


# 顶部 sys.path 注入：CLI 直跑免设 PYTHONPATH（与监控 M1 薄入口对齐）
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

# 默认输出为项目根绝对路径（修复：原相对路径从 scripts/ 目录启动时
# 会把报告写到 scripts/docs/监控档案/，与 docs/监控档案/ 正式落档分叉）
DEFAULT_OUT_PATH = _PROJECT_ROOT / "docs" / "监控档案" / "B包保钱包验收_20261006.json"

from myquant.acceptance.b_acceptance import BAcceptanceRunner  # noqa: E402  # 业务模块

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("验收_B包保钱包")


def _print_report(report: Dict[str, Any]) -> None:
    """打印验收报告摘要。"""
    summary: Dict[str, Any] = report["summary"]

    print("\n" + "=" * 64)
    print(f"B-10  B 包（保钱包）验收  mode={report['mode']}")
    print("=" * 64)
    print(f"  总计: {summary['total']} 项 | 通过: {summary['passed']} | 失败: {summary['failed']}")
    for it in report["items"]:
        status = "✅ 通过" if it["pass"] else "❌ 未通过"
        print(f"    [{it['item']}] {status}")
        print(f"        详情: {it['detail']}")
        print(f"        阈值真源: {it['threshold_source']}")
    if report.get("caveats"):
        print("  注记:")
        for c in report["caveats"]:
            print(f"    - {c}")
    overall = "✅ 全部通过" if summary["failed"] == 0 else "❌ 存在失败项"
    print(f"  总体判定: {overall}")
    print("=" * 64)


def main() -> int:
    """薄入口主流程：argparse → 调用业务模块 → 输出报告。"""
    parser = argparse.ArgumentParser(
        description="B-10 B 包（保钱包）验收（B-1~B-9 逐项）"
    )
    parser.add_argument(
        "--mode", choices=("full",), default="full",
        help="验收档位：当前仅支持 full（B-1~B-9 全量）",
    )
    parser.add_argument(
        "--out", metavar="FILE",
        default=str(DEFAULT_OUT_PATH),
        help=f"验收报告 JSON 输出路径（默认 {DEFAULT_OUT_PATH}）",
    )
    parser.add_argument(
        "--db", metavar="FILE",
        help="告警/指标临时落库目录（默认临时目录，自动清理；一般不传）",
    )
    args = parser.parse_args()

    runner = BAcceptanceRunner()
    try:
        report = runner.run(args.mode)
    finally:
        runner.close()

    _print_report(report)

    bundle: Dict[str, Any] = {
        "source": "验收_B包保钱包",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "report": report,
        "overall_pass": report["summary"]["failed"] == 0,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    logger.info("验收报告已写入: %s", out_path)

    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
