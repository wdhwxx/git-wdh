# 文件名：myquant/live/acceptance.py
"""仿真模拟盘绩效准入评估器（任务 T10，表5 阈值判定）。

把仿真运行产出的指标（metrics JSON）对照「实盘准入阈值（表5）」逐项判定，
输出综合结论（通过 / 不通过 / 待补充），供「量化清单 + 审批」双控使用。

设计要点：
  - 阈值**可配置**（DEFAULT_THRESHOLDS 为表5 示例值，可由阈值文件/参数覆盖，
    因为表5 标注"可按策略类型调整"）；
  - 数据驱动：metrics 为标准 JSON schema，缺失字段判为「待补充(NA)」，
    绝不把缺数据的项当作通过（避免误准入）；
  - 防未来函数：本模块只对已发生的运行指标做判定，不预测未来绩效。

与实盘链路关系（蓝图 myquant/live/ 段）：
  run_实盘入口.py --stats-json 导出运行统计 → scripts/仿真准入评估.py
  读取 metrics JSON + 可选回测基线 → SimulationAcceptanceEvaluator.evaluate()
  → 准入结论。
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("live.acceptance")

# 运行模式允许值（与 run_mode.RunMode 保持一致，避免循环导入）
_ACCEPTABLE_MODES = frozenset({"paper", "sim"})


class AcceptanceStatus(str, enum.Enum):
    """单项判定状态。"""

    PASS = "pass"  # 达标
    FAIL = "fail"  # 未达标
    NA = "na"  # 数据不足，未评估（不判通过）


# ── 阈值默认值（表5 示例，可按策略类型调整）──
# key 含义见 DEFAULT_THRESHOLDS 注释；所有值均可被 thresholds 参数覆盖。
DEFAULT_THRESHOLDS: Dict[str, Any] = {
    # 仿真运行时长（自然日，表5：≥1个月）
    "run_days_min": 30,
    # 年化收益率下限（表5：满足策略目标收益；0 表示不劣于零，可覆盖）
    "annualized_return_min": 0.0,
    # 年化收益允许低于回测的幅度（表5：不显著劣于回测）
    "annualized_return_gap": 0.05,
    # 最大回撤≤回测回撤倍数（表5：≤回测×1.3）
    "drawdown_ratio_limit": 1.3,
    # 回撤绝对上限（表5：或 ≤策略风险上限）
    "drawdown_cap": 0.15,
    # 夏普比率下限（表5：如 ≥1.0）
    "sharpe_min": 1.0,
    # 平均滑点上限（表5：滑点在可接受区间，示例万5）
    "slippage_max": 0.0005,
    # 异常漏单上限（表5：无异常漏单，示例 0）
    "missed_orders_max": 0,
    # 重大风控违规上限（表5：零重大违规，示例 0）
    "major_risk_violations_max": 0,
    # 系统中断次数上限（表5：≤设定阈值，示例 3）
    "interruptions_max": 3,
}


@dataclass
class ItemResult:
    """单项指标判定结果。"""

    key: str  # 指标键
    name: str  # 指标名
    status: AcceptanceStatus  # 判定状态
    actual: str  # 实际值（可读字符串）
    threshold: str  # 阈值（可读字符串）
    detail: str = ""  # 补充说明

    def to_dict(self) -> Dict[str, str]:
        """序列化为字典。"""
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status.value,
            "actual": self.actual,
            "threshold": self.threshold,
            "detail": self.detail,
        }


@dataclass
class AcceptanceReport:
    """综合准入评估报告。"""

    items: List[ItemResult] = field(default_factory=list)
    run_mode: str = "paper"
    overall: bool = False
    passed: int = 0
    failed: int = 0
    pending: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "run_mode": self.run_mode,
            "overall": self.overall,
            "passed": self.passed,
            "failed": self.failed,
            "pending": self.pending,
            "items": [i.to_dict() for i in self.items],
        }

    def to_text(self) -> str:
        """格式化为可打印文本报告。"""
        lines = [
            f"仿真模拟盘绩效准入评估（运行模式: {self.run_mode}）",
            "-" * 56,
        ]
        for item in self.items:
            marker = {"pass": "✅", "fail": "❌", "na": "⏸"}[item.status.value]
            lines.append(
                f"{marker} [{item.name}] 实际={item.actual} 阈值={item.threshold}"
            )
            if item.detail:
                lines.append(f"     {item.detail}")
        lines.append("-" * 56)
        lines.append(
            f"结论：{'✅ 通过，可纳入实盘准入清单' if self.overall else '❌ 不通过 / 数据不足'} "
            f"（通过={self.passed} 未达标={self.failed} 待补充={self.pending}）"
        )
        return "\n".join(lines)


# ── 数值提取工具 ──

def _num(
    metrics: Dict[str, Any],
    key: str,
    *nested_keys: str,
) -> Optional[float]:
    """从 metrics 提取数值，顶层与嵌套（如 stats）均可，缺失返回 None。

    防未来函数说明：只读取已提供的运行指标，不生成或推算未来数据。
    """
    for keys in [(key,)] + [(key, *nested_keys)]:
        cur: Any = metrics
        try:
            for k in keys:
                if not isinstance(cur, dict):
                    break
                cur = cur.get(k)
            else:
                if cur is not None:
                    return float(cur)
        except (TypeError, ValueError):
            continue
    return None


def _fmt(value: Optional[float], nd: int = 4) -> str:
    """数值格式化；None 显示为「缺失」。"""
    if value is None:
        return "缺失"
    return f"{value:.{nd}f}"


# ── 单项判定函数（key -> callable）──
# 每个 callable: (metrics, thresholds, backtest) -> ItemResult


def _eval_run_days(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "run_days")
    limit = float(th["run_days_min"])
    if actual is None:
        return ItemResult("run_days", "仿真运行时长", AcceptanceStatus.NA,
                          "缺失", f"≥{limit:.0f}天", "需绩效分析补充 run_days")
    ok = actual >= limit
    return ItemResult("run_days", "仿真运行时长",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.0f}天", f"≥{limit:.0f}天")


def _eval_annualized(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "annualized_return")
    if actual is None:
        return ItemResult("annualized_return", "年化收益率", AcceptanceStatus.NA,
                          "缺失", "≥目标收益且不显著劣于回测", "需绩效分析补充 annualized_return")
    min_val = float(th["annualized_return_min"])
    gap = float(th["annualized_return_gap"])
    bt_val = _num(backtest or {}, "annualized_return")
    threshold = max(min_val, (bt_val - gap) if bt_val is not None else min_val)
    ok = actual >= threshold
    return ItemResult("annualized_return", "年化收益率",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.2%}", f"≥{threshold:.2%}")


def _eval_drawdown(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "max_drawdown")
    if actual is None:
        return ItemResult("max_drawdown", "最大回撤", AcceptanceStatus.NA,
                          "缺失", "≤回测×1.3 或 ≤风险上限", "需绩效分析补充 max_drawdown")
    cap = float(th["drawdown_cap"])
    ratio = float(th["drawdown_ratio_limit"])
    bt_dd = _num(backtest or {}, "max_drawdown")
    threshold = min((bt_dd * ratio) if bt_dd is not None else cap, cap)
    ok = actual <= threshold
    return ItemResult("max_drawdown", "最大回撤",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.2%}", f"≤{threshold:.2%}")


def _eval_sharpe(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "sharpe_ratio")
    limit = float(th["sharpe_min"])
    if actual is None:
        return ItemResult("sharpe_ratio", "夏普比率", AcceptanceStatus.NA,
                          "缺失", f"≥{limit:.2f}", "需绩效分析补充 sharpe_ratio")
    ok = actual >= limit
    return ItemResult("sharpe_ratio", "夏普比率",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.2f}", f"≥{limit:.2f}")


def _eval_slippage(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "avg_slippage")
    limit = float(th["slippage_max"])
    if actual is None:
        return ItemResult("avg_slippage", "成交一致性(平均滑点)", AcceptanceStatus.NA,
                          "缺失", f"≤{limit:.4f}", "需绩效分析补充 avg_slippage")
    ok = actual <= limit
    return ItemResult("avg_slippage", "成交一致性(平均滑点)",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.4f}", f"≤{limit:.4f}")


def _eval_missed_orders(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "missed_orders")
    limit = float(th["missed_orders_max"])
    if actual is None:
        return ItemResult("missed_orders", "异常漏单", AcceptanceStatus.NA,
                          "缺失", f"≤{limit:.0f}", "需绩效分析补充 missed_orders")
    ok = actual <= limit
    return ItemResult("missed_orders", "异常漏单",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.0f}", f"≤{limit:.0f}")


def _eval_risk_violations(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "major_risk_violations")
    limit = float(th["major_risk_violations_max"])
    if actual is None:
        return ItemResult("major_risk_violations", "重大风控违规", AcceptanceStatus.NA,
                          "缺失", f"≤{limit:.0f}", "需风控记录补充 major_risk_violations")
    ok = actual <= limit
    return ItemResult("major_risk_violations", "重大风控违规",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.0f}", f"≤{limit:.0f}")


def _eval_interruptions(
    metrics: Dict[str, Any], th: Dict[str, Any], backtest: Optional[Dict[str, Any]]
) -> ItemResult:
    actual = _num(metrics, "interruptions")
    limit = float(th["interruptions_max"])
    if actual is None:
        return ItemResult("interruptions", "系统稳定性(中断次数)", AcceptanceStatus.NA,
                          "缺失", f"≤{limit:.0f}", "需运维记录补充 interruptions")
    ok = actual <= limit
    return ItemResult("interruptions", "系统稳定性(中断次数)",
                      AcceptanceStatus.PASS if ok else AcceptanceStatus.FAIL,
                      f"{actual:.0f}", f"≤{limit:.0f}")


# 指标判定注册表（顺序即报告展示顺序）
_EVALUATORS: List[Callable[[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]], ItemResult]] = [
    _eval_run_days,
    _eval_annualized,
    _eval_drawdown,
    _eval_sharpe,
    _eval_slippage,
    _eval_missed_orders,
    _eval_risk_violations,
    _eval_interruptions,
]


class SimulationAcceptanceEvaluator:
    """仿真模拟盘绩效准入评估器。

    Args:
        metrics: 仿真运行指标字典（标准 schema，含表5 各字段）。
        thresholds: 阈值覆盖字典（与 DEFAULT_THRESHOLDS 合并，默认用表5 示例）。
        backtest: 可选回测基线字典（用于年化/回撤的「不显著劣于回测」比较）。
        run_mode: 运行模式标识（默认 paper）。
    """

    def __init__(
        self,
        metrics: Dict[str, Any],
        thresholds: Optional[Dict[str, Any]] = None,
        backtest: Optional[Dict[str, Any]] = None,
        run_mode: str = "paper",
    ) -> None:
        if not isinstance(metrics, dict) or not metrics:
            raise ValueError("metrics 必须为非空字典")
        self._metrics = metrics
        self._thresholds = dict(DEFAULT_THRESHOLDS)
        if thresholds:
            if not isinstance(thresholds, dict):
                raise ValueError("thresholds 必须为字典")
            self._thresholds.update(thresholds)
        self._backtest = backtest if isinstance(backtest, dict) else None
        self._run_mode = str(run_mode).lower()

    def evaluate(self) -> AcceptanceReport:
        """执行逐项判定并生成综合报告。

        Returns:
            AcceptanceReport：综合结论（overall 通过要求全部 PASS 且无 FAIL）。
        """
        items: List[ItemResult] = []
        for fn in _EVALUATORS:
            items.append(fn(self._metrics, self._thresholds, self._backtest))

        # P3-16：准入仅允许 paper/sim；LIVE 等模式不得凭 stats JSON 判"准入通过"
        if self._run_mode not in _ACCEPTABLE_MODES:
            items.append(ItemResult(
                key="run_mode", name="运行模式", status=AcceptanceStatus.FAIL,
                actual=self._run_mode, threshold="/".join(sorted(_ACCEPTABLE_MODES)),
                detail=f"仅允许 {sorted(_ACCEPTABLE_MODES)}，拒绝 {self._run_mode} 的准入判定",
            ))

        passed = sum(1 for i in items if i.status is AcceptanceStatus.PASS)
        failed = sum(1 for i in items if i.status is AcceptanceStatus.FAIL)
        pending = sum(1 for i in items if i.status is AcceptanceStatus.NA)
        # 综合通过：全部项均有结论且无未达标（passed>0 且无 FAIL 且无 NA）
        overall = failed == 0 and pending == 0 and passed > 0
        return AcceptanceReport(
            items=items,
            run_mode=self._run_mode,
            overall=overall,
            passed=passed,
            failed=failed,
            pending=pending,
        )


def _demo() -> None:
    """命令行自检 demo（纯内存，无业务副作用）。"""
    sample = {
        "run_days": 45,
        "annualized_return": 0.12,
        "max_drawdown": 0.08,
        "sharpe_ratio": 1.4,
        "avg_slippage": 0.0002,
        "missed_orders": 0,
        "major_risk_violations": 0,
        "interruptions": 1,
    }
    backtest = {"annualized_return": 0.15, "max_drawdown": 0.06}
    report = SimulationAcceptanceEvaluator(
        sample, backtest=backtest, run_mode="paper"
    ).evaluate()
    print(report.to_text())


if __name__ == "__main__":
    _demo()
