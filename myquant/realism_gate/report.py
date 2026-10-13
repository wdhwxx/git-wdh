# 文件名：myquant/realism_gate/report.py
"""
realism_gate/report.py - RealismManifest 落盘（P0-01 第四刀）
================================================================

两类落盘产物（v2.2 §4.3.1 + §4.7）：
  1. **正常路径**：清单写入 results.json 的 ``realism_manifest`` 键（与回测结果同文件）
  2. **阻断路径**：``realism_gate_blocked_<YYYYMMDD_HHMMSS>_<strategy_id>.json``
     （被门禁拦下时没有 result 对象，故独立落盘；含完整 manifest + 违规明细 + 决策时点）

L0 纯构建器：数据由调用方注入，不 import L3/L4（R-02/R-16）。
原子写：先写 .tmp 再 rename（避免半截文件）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from myquant.realism_gate.manifest import RealismManifest, RealismViolation

logger = logging.getLogger("myquant.realism_gate.report")


# ═══════════════════════════════════════════════
#  正常路径：写入 results.json 的 realism_manifest 键
# ═══════════════════════════════════════════════

def write_manifest_to_results(
    results_path: str,
    manifest: Union[RealismManifest, Dict[str, Any]],
) -> str:
    """把 RealismManifest 写入 results.json 的 realism_manifest 键。

    若 results.json 已存在则合并（保留其他键）；不存在则新建。

    P1-03 收尾（#355）：兼容已序列化 dict——引擎收尾把 manifest 以
    ``to_dict()`` 产物挂到 ``BacktestResult.realism_manifest``（Dict），
    报告流程（backtest/report.py generate_full_report）落盘时直接传入
    该 Dict，无需重建实例（RealismManifest 实例与 to_dict 产物均可）。

    Args:
        results_path: results.json 绝对路径。
        manifest: 已 seal 的 RealismManifest 实例，或其实例的 to_dict() 产物。

    Returns:
        实际写入的文件路径。
    """
    existing: Dict[str, Any] = {}
    if os.path.exists(results_path):
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"results.json 读取失败（将覆盖重建）: {e}")

    existing["realism_manifest"] = (
        manifest.to_dict() if isinstance(manifest, RealismManifest) else manifest
    )

    tmp_path = results_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, results_path)
    logger.debug(f"真实性清单已写入 results.json: {results_path}")
    return results_path


# ═══════════════════════════════════════════════
#  阻断路径：realism_gate_blocked_*.json（v2.2 §4.3.1）
# ═══════════════════════════════════════════════

def write_blocked_artifact(
    output_dir: str,
    manifest: RealismManifest,
    violations: List[RealismViolation],
    decision_time: Optional[str] = None,
) -> str:
    """阻断路径产物落盘（v2.2 §4.3.1）。

    被门禁拦下时没有 result 对象，故独立落盘：
      outputs/realism_gate_blocked_<YYYYMMDD_HHMMSS>_<strategy_id>.json

    Args:
        output_dir: 输出目录（通常 outputs/）。
        manifest: 被阻断的 RealismManifest 实例。
        violations: 触发阻断的违规明细。
        decision_time: 决策时点（UTC ISO8601；默认当前时间）。

    Returns:
        实际写入的文件路径。
    """
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    strategy_id = manifest.get_field("strategy_id", "unknown")
    filename = f"realism_gate_blocked_{ts}_{strategy_id}.json"
    path = os.path.join(output_dir, filename)

    payload: Dict[str, Any] = {
        "blocked": True,
        "decision_time": decision_time or datetime.now().isoformat(),
        "run_id": manifest.get_field("run_id"),
        "strategy_id": strategy_id,
        "engine_mode_resolved": manifest.get_field("engine_mode_resolved"),
        "manifest": manifest.to_dict(),
        "violations": [
            {
                "field_path": v.field_path,
                "critical": v.critical,
                "message": v.message,
                "actual_value": repr(v.actual_value),
                "decision_time": v.decision_time,
            }
            for v in violations
        ],
    }

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, path)
    logger.warning(f"真实性门禁阻断产物已落盘: {path}")
    return path
