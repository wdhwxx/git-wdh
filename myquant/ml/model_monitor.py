# 文件名：myquant/ml/model_monitor.py
"""
model_monitor.py - 模型失效监控与自动降级（F-05）
================================================
背景（问题链，审查确认）:
  - model_registry 的 status 仅 active/archived，无"失效"态：
    模型 IC 持续衰减/回撤超限后仍保持 active 被推理服务调用，
    无自动降级机制，失效模型持续产生低质量预测；
  - 任务卡验收：注入低 IC 序列后失效检出并降级时间 < 1 个监控周期；
    失效版本 0 次被调用。

本模块职责:
  1. 失效判定（内置规则）：
     a. 滚动窗口 IC 连续 N 个月 |IC| < 阈值（默认 N=3、阈值 0.02，
        按市场可校准）→ retired；
     b. 绩效最大回撤超限（默认 20%，读模型绩效 metrics）→ retired；
     c. 单月 IC 低但未达连续阈值 → degraded（观察态，不降级）。
  2. 自动降级：active 版本被判定失效 → mark_retired 自动切换至
     版本号最大的非失效版本并告警；无候选则 active 置空并告警。
  3. 屏蔽调用：失效版本由 model_registry.load_model(allow_retired=False)
     拒绝加载（失效版本 0 次被调用）。

防未来函数（铁律 11）:
  - 监控只消费"已观测历史绩效序列"（ic_series 由调用方提供，最新值=
    当前周期已观测值）与模型训练时绩效（meta.metrics），无预测性未来信息；
  - 本模块不读行情、不做预测，无任何未来数据引用。

监控周期参数外置（合规要点）:
  - 所有阈值/周期参数可注入（构造参数），默认值集中为模块常量 DEFAULT_*，
    禁止魔法数字散落业务逻辑；
  - from_config 支持 dict / JSON 文件路径注入（外部配置通道）。

实现:
  - 纯标准库 + numpy（Spearman 秩相关复用 training_pipeline._rank_ic 语义，
    本模块不重复实现）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from myquant.ml.model_registry import (
    ModelRegistry,
    ModelRegistryError,
    ModelVersion,
)

try:
    from myquant.core.logger import get_logger
except ImportError:  # pragma: no cover
    def get_logger(name: str) -> Any:
        return logging.getLogger(name)

logger = get_logger("ml.model_monitor")


# ═══════════════════════════════════════════════
#  常量（集中定义，禁止魔法数字；全部可经构造参数/from_config 覆盖）
# ═══════════════════════════════════════════════

# |IC| 失效阈值（任务卡初始建议 |IC|<0.02，按市场校准）
DEFAULT_IC_THRESHOLD: float = 0.02
# 连续低于阈值月数（滚动窗口）
DEFAULT_MONTHS_UNDER_THRESHOLD: int = 3
# 最大回撤限（0.20=20%，超限判失效；metrics.max_drawdown 为负值）
DEFAULT_DRAWDOWN_LIMIT: float = 0.20
# 监控周期（天），外置 config 通道（任务卡合规要点）
DEFAULT_MONITOR_INTERVAL_DAYS: int = 30

# 健康状态
STATUS_HEALTHY: str = "healthy"
STATUS_DEGRADED: str = "degraded"
STATUS_RETIRED: str = "retired"

# 绩效指标键
METRIC_IC: str = "ic"
METRIC_MAX_DRAWDOWN: str = "max_drawdown"


# ═══════════════════════════════════════════════
#  异常类
# ═══════════════════════════════════════════════

class ModelMonitorError(Exception):
    """模型监控通用错误"""


class ModelMonitorConfigError(ModelMonitorError):
    """监控配置非法"""


# ═══════════════════════════════════════════════
#  HealthReport 健康报告
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class HealthReport:
    """模型健康评估结果（可序列化入档/告警）。

    Attributes:
        model_name: 模型名称。
        version: 版本号。
        status: healthy / degraded / retired。
        ic_latest: 最新 IC（None=无 IC 序列）。
        months_below_threshold: 连续低于阈值月数。
        max_drawdown: 评估所用最大回撤（None=无绩效数据）。
        drawdown_breached: 是否回撤超限。
        reasons: 状态判定原因明细。
        degraded_to: 降级目标版本（None=未降级/无候选）。
        decided_at: 判定时间。
    """

    model_name: str
    version: int
    status: str = STATUS_HEALTHY
    ic_latest: Optional[float] = None
    months_below_threshold: int = 0
    max_drawdown: Optional[float] = None
    drawdown_breached: bool = False
    reasons: List[str] = field(default_factory=list)
    degraded_to: Optional[int] = None
    decided_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict。"""
        return {
            "model_name": self.model_name,
            "version": self.version,
            "status": self.status,
            "ic_latest": self.ic_latest,
            "months_below_threshold": self.months_below_threshold,
            "max_drawdown": self.max_drawdown,
            "drawdown_breached": self.drawdown_breached,
            "reasons": list(self.reasons),
            "degraded_to": self.degraded_to,
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthReport":
        """从 dict 反序列化（防御式校验）。"""
        if not isinstance(data, dict):
            raise ValueError(f"HealthReport 数据必须是 dict，got: {type(data).__name__}")
        if "model_name" not in data or "version" not in data:
            raise ValueError("HealthReport 缺少必填字段(model_name/version)")
        return cls(
            model_name=str(data["model_name"]),
            version=int(data["version"]),
            status=str(data.get("status", STATUS_HEALTHY)),
            ic_latest=data.get("ic_latest"),
            months_below_threshold=int(data.get("months_below_threshold", 0)),
            max_drawdown=data.get("max_drawdown"),
            drawdown_breached=bool(data.get("drawdown_breached", False)),
            reasons=[str(r) for r in (data.get("reasons") or [])],
            degraded_to=data.get("degraded_to"),
            decided_at=str(data.get("decided_at", "")),
        )


# ═══════════════════════════════════════════════
#  ModelMonitor 模型失效监控器
# ═══════════════════════════════════════════════

class ModelMonitor:
    """模型失效监控器（F-05）。

    用法（监控周期任务每次触发）:
      monitor = ModelMonitor()                    # 或 from_config(路径/dict)
      report = monitor.run("model_name", ic_series=..., registry=registry)

    判定规则（只消费历史绩效，防未来）:
      - IC 连续 N 个月 |IC| < 阈值 → retired
      - 绩效最大回撤超限 → retired
      - IC 低但未连续 / 回撤未超限 → degraded（观察）
      - 其余 → healthy
    """

    def __init__(
        self,
        ic_threshold: float = DEFAULT_IC_THRESHOLD,
        months_under_threshold: int = DEFAULT_MONTHS_UNDER_THRESHOLD,
        drawdown_limit: float = DEFAULT_DRAWDOWN_LIMIT,
        monitor_interval_days: int = DEFAULT_MONITOR_INTERVAL_DAYS,
        logger_name: str = "ml.model_monitor",
    ):
        """初始化监控器。

        Args:
            ic_threshold: |IC| 失效阈值（>0）。
            months_under_threshold: 连续低于阈值月数（>=1）。
            drawdown_limit: 最大回撤限（>0，0.20=20%）。
            monitor_interval_days: 监控周期（天，>0），外置 config 通道。
            logger_name: 监控日志名。

        Raises:
            ModelMonitorConfigError: 参数非法时抛出。
        """
        self._validate_params(
            ic_threshold, months_under_threshold,
            drawdown_limit, monitor_interval_days,
        )
        self.ic_threshold = ic_threshold
        self.months_under_threshold = months_under_threshold
        self.drawdown_limit = drawdown_limit
        self.monitor_interval_days = monitor_interval_days
        self.logger = get_logger(logger_name)

    @staticmethod
    def _validate_params(
        ic_threshold: float,
        months_under_threshold: int,
        drawdown_limit: float,
        monitor_interval_days: int,
    ) -> None:
        """参数合法性校验（防御式）。"""
        if not isinstance(ic_threshold, (int, float)) or ic_threshold <= 0:
            raise ModelMonitorConfigError(f"ic_threshold 必须 >0，got: {ic_threshold!r}")
        if not isinstance(months_under_threshold, int) or months_under_threshold < 1:
            raise ModelMonitorConfigError(
                f"months_under_threshold 必须 >=1 的整数，got: {months_under_threshold!r}")
        if not isinstance(drawdown_limit, (int, float)) or drawdown_limit <= 0:
            raise ModelMonitorConfigError(f"drawdown_limit 必须 >0，got: {drawdown_limit!r}")
        if not isinstance(monitor_interval_days, int) or monitor_interval_days < 1:
            raise ModelMonitorConfigError(
                f"monitor_interval_days 必须 >=1 的整数，got: {monitor_interval_days!r}")

    # ─────────────────────────────────────────────
    #  配置注入（外置 config 通道）
    # ─────────────────────────────────────────────

    @classmethod
    def from_config(cls, config: Any, logger_name: str = "ml.model_monitor") -> "ModelMonitor":
        """从 dict 或 JSON 文件路径加载监控参数。

        Args:
            config: dict，或 JSON 文件路径（Path/str）。
            logger_name: 监控日志名。

        Returns:
            ModelMonitor 实例。

        Raises:
            ModelMonitorConfigError: 配置不是 dict/文件不存在/参数非法时抛出。
        """
        if isinstance(config, (str, Path)):
            path = Path(config)
            if not path.is_file():
                raise ModelMonitorConfigError(f"监控配置文件不存在: {path}")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                raise ModelMonitorConfigError(f"监控配置文件解析失败: {path} ({exc})") from exc
        elif isinstance(config, dict):
            payload = dict(config)
        else:
            raise ModelMonitorConfigError(
                f"config 必须是 dict 或 JSON 路径，got: {type(config).__name__}")
        try:
            return cls(
                ic_threshold=float(payload.get("ic_threshold", DEFAULT_IC_THRESHOLD)),
                months_under_threshold=int(payload.get(
                    "months_under_threshold", DEFAULT_MONTHS_UNDER_THRESHOLD)),
                drawdown_limit=float(payload.get("drawdown_limit", DEFAULT_DRAWDOWN_LIMIT)),
                monitor_interval_days=int(payload.get(
                    "monitor_interval_days", DEFAULT_MONITOR_INTERVAL_DAYS)),
                logger_name=logger_name,
            )
        except (TypeError, ValueError) as exc:
            raise ModelMonitorConfigError(f"监控配置参数非法: {exc}") from exc

    def to_config(self) -> Dict[str, Any]:
        """导出当前参数（可回写外部 config）。"""
        return {
            "ic_threshold": self.ic_threshold,
            "months_under_threshold": self.months_under_threshold,
            "drawdown_limit": self.drawdown_limit,
            "monitor_interval_days": self.monitor_interval_days,
        }

    # ─────────────────────────────────────────────
    #  失效判定
    # ─────────────────────────────────────────────

    def _consecutive_below_count(self, ic_series: Sequence[float]) -> Tuple[int, Optional[float]]:
        """从序列末尾向前统计连续 |IC| < 阈值 的月数（滚动窗口）。

        本行使用的数据截至 t 时刻：仅消费已观测序列（末尾=最新已观测值），
        无未来数据。

        Args:
            ic_series: 月频 IC 序列（升序，最新在末尾）。

        Returns:
            (连续低于阈值月数, 最新 IC)。
        """
        count = 0
        latest: Optional[float] = None
        for raw in reversed(list(ic_series)):
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ModelMonitorError(f"IC 序列含非法数值: {raw!r}") from exc
            if latest is None:
                latest = value
            if abs(value) < self.ic_threshold:
                count += 1
            else:
                break
        return count, latest

    def _drawdown_breached(self, metrics: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
        """绩效最大回撤超限判定（metrics.max_drawdown 为负值，abs 与限比较）。"""
        raw = metrics.get(METRIC_MAX_DRAWDOWN)
        if raw is None:
            return False, None
        try:
            dd = float(raw)
        except (TypeError, ValueError):
            return False, None
        return (abs(dd) > self.drawdown_limit), dd

    def evaluate(
        self,
        meta: ModelVersion,
        ic_series: Sequence[float] = (),
        current_metrics: Optional[Dict[str, Any]] = None,
    ) -> HealthReport:
        """评估单个版本健康状态（只消费历史绩效，防未来）。

        Args:
            meta: 版本元数据（meta.metrics 为训练时绩效）。
            ic_series: 已观测月频 IC 序列（最新在末尾；缺省=无 IC 数据，
                仅按回撤判定）。
            current_metrics: 当前周期绩效（优先于 meta.metrics 使用）。

        Returns:
            HealthReport（status: retired/degraded/healthy + 原因明细）。

        Raises:
            ModelMonitorError: IC 序列含非法数值时抛出。
        """
        metrics = dict(current_metrics) if current_metrics else dict(meta.metrics or {})
        reasons: List[str] = []
        status = STATUS_HEALTHY

        # 判定①：IC 连续低
        below_count, ic_latest = self._consecutive_below_count(ic_series)
        if below_count >= self.months_under_threshold:
            status = STATUS_RETIRED
            reasons.append(
                f"IC 连续 {below_count} 个月 |IC|<{self.ic_threshold} "
                f"(最新 {ic_latest:.4f})"
            )
        elif ic_latest is not None and below_count > 0:
            status = STATUS_DEGRADED
            reasons.append(
                f"IC 低值进行中：连续 {below_count} 个月 |IC|<{self.ic_threshold}"
            )

        # 判定②：回撤超限
        dd_breached, dd_value = self._drawdown_breached(metrics)
        if dd_breached:
            status = STATUS_RETIRED
            reasons.append(
                f"绩效最大回撤 {abs(dd_value):.2%} 超过限 {self.drawdown_limit:.0%}"
            )

        if status == STATUS_HEALTHY:
            reasons.append("IC 与回撤均在阈值内，模型健康")
        elif status == STATUS_DEGRADED and not reasons:
            reasons.append("IC 低于阈值（未达连续失效条件），进入观察")

        return HealthReport(
            model_name=meta.model_name,
            version=meta.version,
            status=status,
            ic_latest=ic_latest,
            months_below_threshold=below_count,
            max_drawdown=dd_value,
            drawdown_breached=dd_breached,
            reasons=reasons,
        )

    # ─────────────────────────────────────────────
    #  监控执行（检出 → 降级 → 告警）
    # ─────────────────────────────────────────────

    def run(
        self,
        model_name: str,
        registry: ModelRegistry,
        ic_series: Sequence[float] = (),
        version: Optional[int] = None,
        current_metrics: Optional[Dict[str, Any]] = None,
        degrade_on_retire: bool = True,
    ) -> HealthReport:
        """执行一次监控：评估 active（或指定）版本，失效则自动降级并告警。

        验收口径：注入低 IC 序列后，单次 run() 内完成 检出→降级→告警，
        时间 < 1 个监控周期。

        Args:
            model_name: 模型名称。
            registry: ModelRegistry 实例（只读/状态变更均由其承载）。
            ic_series: 已观测月频 IC 序列（最新在末尾）。
            version: 指定版本（None=监控 active 版本）。
            current_metrics: 当前周期绩效（可选）。
            degrade_on_retire: 失效时是否自动降级（默认 True）。

        Returns:
            HealthReport（含降级目标 degraded_to）。

        Raises:
            ModelRegistryError: 模型/版本不存在时由 registry 抛出。
            ModelMonitorError: IC 序列非法时抛出。
        """
        meta = registry.get_model_info(model_name, version)
        report = self.evaluate(meta, ic_series=ic_series,
                               current_metrics=current_metrics)
        if report.status == STATUS_RETIRED and degrade_on_retire:
            retired_meta, degraded_to = registry.mark_retired(
                model_name, meta.version,
                reason="; ".join(report.reasons),
            )
            report = HealthReport(
                model_name=report.model_name,
                version=report.version,
                status=report.status,
                ic_latest=report.ic_latest,
                months_below_threshold=report.months_below_threshold,
                max_drawdown=report.max_drawdown,
                drawdown_breached=report.drawdown_breached,
                reasons=report.reasons,
                degraded_to=degraded_to,
            )
            if degraded_to is not None:
                self.logger.warning(
                    "[ml] 模型失效自动降级: %s v%d → v%d（%s）",
                    retired_meta.model_name, retired_meta.version, degraded_to,
                    "; ".join(report.reasons),
                )
            else:
                self.logger.warning(
                    "[ml] 模型失效且无可用降级版本: %s v%d 已 retired，active 置空（%s）",
                    retired_meta.model_name, retired_meta.version,
                    "; ".join(report.reasons),
                )
        else:
            self.logger.info(
                "[ml] 模型监控: %s v%d status=%s",
                report.model_name, report.version, report.status,
            )
        return report


__all__ = [
    "DEFAULT_IC_THRESHOLD",
    "DEFAULT_MONTHS_UNDER_THRESHOLD",
    "DEFAULT_DRAWDOWN_LIMIT",
    "DEFAULT_MONITOR_INTERVAL_DAYS",
    "STATUS_HEALTHY",
    "STATUS_DEGRADED",
    "STATUS_RETIRED",
    "HealthReport",
    "ModelMonitor",
    "ModelMonitorError",
    "ModelMonitorConfigError",
]
