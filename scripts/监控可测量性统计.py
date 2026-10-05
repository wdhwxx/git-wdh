# 文件名：scripts/监控可测量性统计.py
"""
监控可测量性统计 - 中文入口脚本（薄入口）
============================================
P0-8 可测量性与故障注入统计 CLI：
  - 打印 FI-01~07 标准故障注入用例库
  - 按 P0-8 口径自动统计 TP/FP/FN 与准确率/误报率/检出率
  - 按 P0-8 口径汇总 MTTD/MTTA/MTTR（系统时间戳，禁止人工估算）

业务逻辑位于 myquant.monitoring.measurability
（MttxCalculator / ClassificationStats / FI_CASES），
本脚本只做：参数解析 → 项目根探测 → 读取 JSON → 调用 → 输出报告。

运行方式:
    # 打印 FI-01~07 用例库
    python scripts/监控可测量性统计.py --list-fi

    # 统计告警记录 + 故障注入记录（输出 JSON 报告）
    python scripts/监控可测量性统计.py --alerts alerts.json --fi fi.json \
        --out docs/监控档案/可测量性统计_20261005.json

输入 JSON 格式（与 BASE-1 alerts 表 / P0-8 FI 记录对齐）:
    --alerts 告警记录列表，每条含：
        created_at / acked_at(可空) / resolved_at(可空) /
        acked_disposition("需处置"/"误报"/"噪声"/"已自愈无需动作") /
        auto_resolved / meta_confirmed_no_event
    --fi 故障注入记录列表，每条含：
        case_id(FI-01~07) / fault_ts / captured /
        matched_created_at(捕获时) / matched_resolved_at(恢复时)
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 顶部 sys.path 注入：CLI 直跑免设 PYTHONPATH（与推理延迟基准.py 等对齐）
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

from myquant.monitoring.measurability import (  # noqa: E402  # 业务模块
    ClassificationStats,
    MttxCalculator,
    list_fi_cases,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("监控可测量性统计")


def _load_json(path: str) -> List[Dict[str, Any]]:
    """读取 JSON 列表文件（防御式：不存在/非法/非列表 明确报错）。

    Args:
        path: JSON 文件路径。

    Returns:
        List[Dict[str, Any]]: 记录列表。

    Raises:
        SystemExit: 文件不存在、解析失败或根节点不是列表时退出。
    """
    p = Path(path)
    if not p.exists():
        logger.error("文件不存在: %s", p)
        raise SystemExit(f"文件不存在: {p}")
    try:
        # utf-8-sig：兼容 Windows 常见 UTF-8 BOM（Excel/记事本导出）与无 BOM 两种情形
        with open(p, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        logger.error("JSON 解析失败 %s: %s", p, exc)
        raise SystemExit(f"JSON 解析失败 {p}: {exc}")
    if not isinstance(data, list):
        logger.error("JSON 根节点必须是列表: %s", p)
        raise SystemExit(f"JSON 根节点必须是列表: {p}")
    return data


def _summarize_mttx(
    fi_records: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """按 P0-8 口径汇总 MTTD/MTTA/MTTR 样本。

    Args:
        fi_records: 故障注入记录（fault_ts / matched_created_at / matched_resolved_at）。
        alerts: 告警记录（created_at / acked_at）。

    Returns:
        dict: {"mttd": {...}, "mtta": {...}, "mttr": {...}}，空样本 avg/max 为 None。
    """
    mttd_samples: List[Dict[str, Any]] = []
    mttr_samples: List[Dict[str, Any]] = []
    for record in fi_records:
        fault_ts = record.get("fault_ts")
        if fault_ts is None:
            continue
        if record.get("captured") and record.get("matched_created_at") is not None:
            mttd_samples.append({
                "seconds": MttxCalculator.mttd(float(fault_ts), float(record["matched_created_at"])),
                "case_id": record.get("case_id"),
            })
        if record.get("matched_resolved_at") is not None:
            mttr_samples.append({
                "seconds": MttxCalculator.mttr(float(fault_ts), float(record["matched_resolved_at"])),
                "case_id": record.get("case_id"),
            })

    mtta_samples: List[Dict[str, Any]] = []
    for alert in alerts:
        created = alert.get("created_at")
        acked = alert.get("acked_at")
        if created is not None and acked is not None:
            mtta_samples.append({
                "seconds": MttxCalculator.mtta(float(created), float(acked)),
                "case_id": alert.get("alert_id"),
            })

    return {
        "mttd": MttxCalculator.summarize(mttd_samples, kind="mttd"),
        "mtta": MttxCalculator.summarize(mtta_samples, kind="mtta"),
        "mttr": MttxCalculator.summarize(mttr_samples, kind="mttr"),
    }


def main() -> int:
    """薄入口主流程：argparse → 读取 → 调用业务模块 → 输出报告。"""
    parser = argparse.ArgumentParser(
        description="P0-8 可测量性统计（MTTx / TPFPFN / FI 用例库）"
    )
    parser.add_argument("--alerts", metavar="FILE", help="告警记录 JSON（列表）")
    parser.add_argument("--fi", metavar="FILE", help="故障注入记录 JSON（列表）")
    parser.add_argument("--list-fi", action="store_true", help="打印 FI-01~07 用例库")
    parser.add_argument("--out", metavar="FILE", help="统计报告输出路径（JSON）")
    args = parser.parse_args()

    if args.list_fi:
        for case in list_fi_cases():
            print(json.dumps(case, ensure_ascii=False, indent=2))
        return 0

    if not args.alerts and not args.fi:
        parser.error("至少提供 --alerts 或 --fi 之一（或使用 --list-fi）")

    alerts: List[Dict[str, Any]] = _load_json(args.alerts) if args.alerts else []
    fi_records: List[Dict[str, Any]] = _load_json(args.fi) if args.fi else []

    report: Dict[str, Any] = {
        "source": "P0-8 可测量性统计",
        "inputs": {
            "alerts_count": len(alerts),
            "fi_count": len(fi_records),
        },
        "classification": ClassificationStats.compute(
            alerts,
            fi_records if fi_records else None,
        ),
        "mttx": _summarize_mttx(fi_records, alerts),
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        logger.info("统计报告已写入: %s", out_path)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
