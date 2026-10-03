# 文件名：scripts/推理延迟基准.py
"""
推理延迟基准 - 中文入口脚本（薄入口）
====================================
F-06 推理延迟基准：对 ML 推理服务 predict_realtime 全链路做
随历史长度增长的延迟压测（P50/P95/P99/均值/最大），判定分钟级
策略延迟是否达标（P99 < 500ms，初始目标 < 200ms）。

业务逻辑位于 myquant.ml.inference_benchmark（InferenceBenchmark），
本脚本只做：参数解析 → 项目根探测 → 调用 measure → 报告入档
（JSON + Markdown 到 docs/因子与机器学习档案/）。

运行方式:
    # 默认：长度 100/250/500/1000/2000，重复 5 次
    python scripts/推理延迟基准.py

    # 自定义长度 / 重复次数 / 输出目录
    python scripts/推理延迟基准.py --lengths 500,1000,2000 --repeat 3 \
        --out docs/因子与机器学习档案
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List

# 顶部 sys.path 注入：CLI 直跑免设 PYTHONPATH（与同步股本变动.py 等对齐）
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

from myquant.ml.inference_benchmark import (  # noqa: E402  # 业务模块
    DEFAULT_LENGTHS,
    DEFAULT_REPEAT,
    InferenceBenchmark,
)

# 业务模块日志
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def parse_lengths(text: str) -> List[int]:
    """解析 --lengths 逗号分隔整数序列（升序校验交由 benchmark 层）。"""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError(f"--lengths 不能为空: {text!r}")
    try:
        return [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--lengths 必须为逗号分隔整数: {text!r}")


def write_markdown(report: dict, out_path: Path) -> None:
    """写 Markdown 摘要（人可读，与 JSON 同源）。"""
    lines = [
        "# 推理延迟基准报告（F-06）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 环境：python {report['environment']['python']} / {report['environment']['platform']}",
        f"- 模型：{report['model_name']}（训练 {report['train_rows']} 行合成数据）",
        f"- 口径：predict_realtime 全链路（compute_incremental+模型 predict），"
        f"每长度重复 {report['repeat']} 次",
        f"- 硬阈值：P99 < {report['hard_limit_ms']}ms（判定达标）；"
        f"初始目标：P99 < {report['target_ms']}ms",
        "",
        "| 历史长度 | P50(ms) | P95(ms) | P99(ms) | 均值(ms) | 最大(ms) | 达标 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in report["lengths"]:
        lines.append(
            f"| {r['length']} | {r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} "
            f"| {r['mean_ms']} | {r['max_ms']} | {'是' if r['passed'] else '否'} |")
    lines.append("")
    verdict = "达标" if report["overall_passed"] else "未达标"
    lines.append(f"**总体结论：{verdict}**（全部长度 P99 < {report['hard_limit_ms']}ms）。")
    lines.append("")
    lines.append("合规：合成随机游走数据（防未来），独立临时 registry，不连实盘。")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="推理延迟基准（F-06）：predict_realtime 全链路 P99 延迟压测")
    parser.add_argument(
        "--lengths", type=parse_lengths, default=None,
        help=f"压测历史长度，逗号分隔升序（默认 {' '.join(map(str, DEFAULT_LENGTHS))}）")
    parser.add_argument(
        "--repeat", type=int, default=DEFAULT_REPEAT,
        help=f"每长度重复次数（默认 {DEFAULT_REPEAT}）")
    parser.add_argument(
        "--model-name", type=str, default="benchmark_model",
        help="模型名（默认 benchmark_model）")
    parser.add_argument(
        "--out", type=str, default=None,
        help="报告输出目录（默认 docs/因子与机器学习档案/）")
    args = parser.parse_args()

    root = _PROJECT_ROOT
    out_dir = Path(args.out) if args.out else root / "docs" / "因子与机器学习档案"
    out_dir.mkdir(parents=True, exist_ok=True)

    bench = InferenceBenchmark(repeat=args.repeat)
    report = bench.measure(lengths=args.lengths, model_name=args.model_name)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"推理延迟基准_{stamp}.json"
    md_path = out_dir / f"推理延迟基准_{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    # 薄入口输出摘要（业务模块零 print，统一 logging）
    print(f"推理延迟基准完成：总体{'达标' if report['overall_passed'] else '未达标'}")
    for r in report["lengths"]:
        print(f"  len={r['length']}: P99={r['p99_ms']}ms "
              f"{'达标' if r['passed'] else '超限'}")
    print(f"报告：{json_path}")
    print(f"摘要：{md_path}")
    return 0 if report["overall_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
