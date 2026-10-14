# 文件名：myquant/leakage_gate/report.py
"""
leakage_gate/report.py - 泄漏审计报告产物（阶段 3 审计收口，M3-01）
====================================================================
每次回测/训练/参数优化产出 `leakage_report.json`，汇总本次实验的防泄漏审计事实：
  - 门禁统计（各门禁 severity 计数，gate.collect_gate_stats）
  - 违规明细（各门禁 Violation 列表，含 rule_id/severity/message）
  - 消费侧列引用清单与 severity 命中（consumption）
  - 截断重算 / sanity（模型层统计门禁）摘要
  - 数据版本 sidecar 摘要
  - lookahead_guard 统计（core.lookahead_guard.stats，阶段 3 起可用）
  - 复权降级标的清单（M1A-05，v3.0 §6.4.5-2）

存放位置/保留期/清理衔接：
  - 产物写入 `outputs/leakage_report_<YYYYMMDD_HHMMSS>.json`（与回测产物同目录）；
  - 保留期 = REPORT_RETENTION_DAYS（默认 30 天，写入产物 retention 段）；
  - 与 `scripts/清理历史回测输出.py` 衔接：cleanup_service 的
    BACKTEST_OUTPUT_PATTERNS 已纳入 `leakage_report_*.json`、PERIOD_FILE_PATTERN
    已含 `leakage_report` 前缀 → 清理脚本按回测期归组、`--keep` 期数保留、
    超期产物随其他回测产物一并清理（dry-run 默认只报告）。

分层：myquant.leakage_gate 登记 L0；本模块只依赖同包 gate（L0）与可选注入数据，
不 import data_access/ml/backtest（L0→L3/L4 反向被 R-02 拦截）——复权降级清单、
数据版本 sidecar、lookahead 统计等由 L3/L4 调用方显式注入。

防未来函数：报告只汇总本次实验"已发生"的门禁/消费/降级/版本事实，
created_at 取当前时点；不预测未来、不杜撰数据（缺失数据源记空并标注来源）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

logger = logging.getLogger("myquant.leakage_gate.report")

#: 泄漏审计报告保留期（天）：超过保留期的 leakage_report.json 由清理策略删除
REPORT_RETENTION_DAYS: int = 30
#: 报告文件名前缀（cleanup_service BACKTEST_OUTPUT_PATTERNS 已纳入）
_REPORT_PREFIX: str = "leakage_report_"
#: 默认输出目录（相对项目根 outputs/）
DEFAULT_OUTPUT_DIRNAME: str = "outputs"
#: 报告输出根 override（测试隔离用；None = 默认项目根/outputs）
_REPORT_OUTPUT_DIR_OVERRIDE: Optional[Path] = None

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def set_report_output_dir(path: Optional[Path]) -> None:
    """设置报告输出根（None 恢复默认项目根/outputs）。

    P1-11 测试隔离用：pytest 会话经 tests/conftest.py 指向会话临时目录，
    避免四出口接线后全量测试落盘污染项目 outputs；生产调用不设置，保持默认。

    Args:
        path: 输出根目录；None 恢复默认。
    """
    global _REPORT_OUTPUT_DIR_OVERRIDE
    _REPORT_OUTPUT_DIR_OVERRIDE = Path(path) if path is not None else None


def _default_output_dir() -> Path:
    """默认报告输出目录（override 优先；否则项目根/outputs）。"""
    if _REPORT_OUTPUT_DIR_OVERRIDE is not None:
        return _REPORT_OUTPUT_DIR_OVERRIDE
    return _PROJECT_ROOT / DEFAULT_OUTPUT_DIRNAME


def _timestamp_now() -> str:
    """当前时间戳 YYYYMMDD_HHMMSS（文件名用，与回测产物期归组一致）。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _iso_now() -> str:
    """当前 ISO 时点（报告 created_at）。"""
    return datetime.now().isoformat(timespec="seconds")


# ═══════════════════════════════════════════════
#  报告构建
# ═══════════════════════════════════════════════

def build_leakage_report(
    experiment_id: str,
    experiment_type: str,
    *,
    strategy_id: str = "",
    decision_time: str = "",
    gate_stats: Optional[Dict[str, Dict[str, int]]] = None,
    violations: Optional[Sequence[Any]] = None,
    consumption_hits: Optional[Sequence[Dict[str, str]]] = None,
    truncated_recompute: Optional[Dict[str, Any]] = None,
    sanity: Optional[Sequence[str]] = None,
    data_version: Optional[Dict[str, Any]] = None,
    lookahead_guard: Optional[Dict[str, Any]] = None,
    adjust_degradation: Optional[Sequence[str]] = None,
    retention_days: int = REPORT_RETENTION_DAYS,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构建一次实验的泄漏审计报告（JSON 可序列化字典）。

    数据源均由 L3/L4 调用方注入（避免 L0→L3/L4 跨层 import）；未提供的
    数据段以空/None 记录并保留来源标记，不杜撰。

    Args:
        experiment_id: 实验唯一 ID（如回测批次/训练版本/参数优化轮）。
        experiment_type: 实验类型（backtest/training/param_optimization）。
        strategy_id: 策略契约主键（name）。
        decision_time: 决策时点（T日，YYYY-MM-DD）。
        gate_stats: 门禁统计（gate.collect_gate_stats 产出）。
        violations: 本次实验门禁违规明细（Violation 或 dict，含 rule_id/severity）。
        consumption_hits: 消费侧列引用命中清单（{column, severity, strategy_id}）。
        truncated_recompute: 截断重算自检摘要（factor_engine）。
        sanity: 模型层统计门禁违规描述（sanity.py）。
        data_version: 数据版本 sidecar 摘要（data_versioner .version.json / manifest）。
        lookahead_guard: core.lookahead_guard 统计（阶段 3 起可用）。
        adjust_degradation: 复权降级标的清单（M1A-05，adjust_strict=False 降级 raw）。
        retention_days: 保留期（天，默认 REPORT_RETENTION_DAYS）。
        extra: 额外自由字段（透传入报告，JSON 安全）。

    Returns:
        Dict[str, Any]: 完整报告（含 meta 与各审计段）。
    """
    return {
        "meta": {
            "experiment_id": experiment_id,
            "experiment_type": experiment_type,
            "strategy_id": strategy_id,
            "decision_time": decision_time,
            "created_at": _iso_now(),
            "retention_days": int(retention_days),
        },
        "gates": _normalize_gate_stats(gate_stats),
        "violations": [
            _violation_to_dict(v) for v in (violations or [])
        ],
        "consumption": {
            "source": "consumption_gate",
            "hits": list(consumption_hits or []),
        },
        "truncated_recompute": dict(truncated_recompute or {}),
        "sanity": list(sanity or []),
        "data_version": dict(data_version or {}),
        "lookahead_guard": dict(lookahead_guard or {}),
        "adjust_degradation": {
            "source": "data_access.provider(_adjust_degraded_symbols)",
            "symbols": sorted(set(adjust_degradation or [])),
        },
        "extra": dict(extra or {}),
    }


def _normalize_gate_stats(
    gate_stats: Optional[Dict[str, Dict[str, int]]],
) -> Dict[str, Dict[str, int]]:
    """规整门禁统计（缺失段补 0，保证 JSON 稳定结构）。"""
    return {
        gate: {
            "blocked": int(s.get("blocked", 0)),
            "warning": int(s.get("warning", 0)),
            "total": int(s.get("total", 0)),
        }
        for gate, s in (gate_stats or {}).items()
    }


def _violation_to_dict(v: Any) -> Dict[str, str]:
    """Violation（或鸭子对象）→ dict；缺字段补空串（不抛错）。"""
    return {
        "gate": getattr(v, "gate", ""),
        "rule_id": getattr(v, "rule_id", ""),
        "severity": getattr(v, "severity", ""),
        "message": getattr(v, "message", ""),
        "strategy_id": getattr(v, "strategy_id", ""),
        "decision_time": getattr(v, "decision_time", ""),
    }


# ═══════════════════════════════════════════════
#  写入 / 读取 / 摘要
# ═══════════════════════════════════════════════

def write_leakage_report(
    report: Dict[str, Any],
    output_dir: Optional[Path] = None,
    *,
    timestamp: Optional[str] = None,
) -> Path:
    """写入 leakage_report.json（outputs/leakage_report_<ts>.json，原子写）。

    Args:
        report: build_leakage_report 产出的报告字典。
        output_dir: 输出目录；None 用项目根/outputs。
        timestamp: 文件名时间戳；None 用当前（YYYYMMDD_HHMMSS）。

    Returns:
        Path: 写入的报告文件路径。

    Raises:
        ValueError: report 缺少 experiment_id/meta。
    """
    if "meta" not in report or not report["meta"].get("experiment_id"):
        raise ValueError("leakage_report 缺 meta.experiment_id，无法落盘")
    out = Path(output_dir) if output_dir else _default_output_dir()
    out.mkdir(parents=True, exist_ok=True)
    ts = timestamp or _timestamp_now()
    path = out / f"{_REPORT_PREFIX}{ts}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    logger.info(f"泄漏审计报告已产出: {path}（保留 {report['meta'].get('retention_days')} 天）")
    return path


def load_leakage_report(path: str) -> Dict[str, Any]:
    """读取已产出的 leakage_report.json。

    Args:
        path: 报告文件路径。

    Returns:
        Dict[str, Any]: 报告字典。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件损坏/非 JSON。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"leakage_report 不存在: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"leakage_report 解析失败: {p}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"leakage_report 结构非法（非 dict）: {p}")
    return data


def summarize_report(report: Dict[str, Any]) -> str:
    """报告文本摘要（人读，供日志/脚本输出）。

    Args:
        report: build_leakage_report 产出报告。

    Returns:
        str: 多行摘要。
    """
    meta = report.get("meta", {})
    gates = report.get("gates", {})
    blocked = sum(int(s["blocked"]) for s in gates.values())
    warning = sum(int(s["warning"]) for s in gates.values())
    lines = [
        f"泄漏审计报告 [{meta.get('experiment_id','?')}/{meta.get('experiment_type','?')}]",
        f"  策略={meta.get('strategy_id','')} 决策日={meta.get('decision_time','')} "
        f"保留={meta.get('retention_days')}天",
        f"  门禁违规: 阻断 {blocked} / 警告 {warning}",
        f"  消费侧列命中: {len(report.get('consumption', {}).get('hits', []))}",
        f"  复权降级标的: {len(report.get('adjust_degradation', {}).get('symbols', []))}",
    ]
    if gates:
        lines.append(
            "  门禁明细: " + "; ".join(
                f"{g}(阻断{s['blocked']}/警告{s['warning']})" for g, s in gates.items())
        )
    return "\n".join(lines)


def write_experiment_report(
    experiment_id: str,
    experiment_type: str,
    *,
    output_dir: Optional[Path] = None,
    **sections: Any,
) -> Path:
    """构建并落盘一次实验的泄漏审计报告（便捷入口，M3-01 验收）。

    Args:
        experiment_id: 实验唯一 ID。
        experiment_type: backtest/training/param_optimization。
        output_dir: 输出目录；None 用默认。
        **sections: build_leakage_report 的关键字段（gate_stats/violations/
            consumption_hits/truncated_recompute/sanity/data_version/
            lookahead_guard/adjust_degradation/strategy_id/decision_time/extra 等）。

    Returns:
        Path: 落盘的报告文件路径。
    """
    report = build_leakage_report(
        experiment_id, experiment_type, **sections)
    return write_leakage_report(report, output_dir=output_dir)
