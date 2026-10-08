# 文件名：myquant/utils/performance_config.py
"""
回测性能配置应用器（P2 第10项：全部优化开关落地）
==================================================
把 config/backtest_config.example.yaml 新增的 engine.mode + performance 段
解析为可执行的开关配置，并实际应用到运行环境。

结构（与配置模板保持一致）:
  engine:
    mode: "auto"                 # auto / event / mixed / vectorized（三模式调度）
  performance:
    parallel:                    # 并行治理（P2-2）：进程池提交前 pickle 预检
      enabled / cpu_workers / check_pickle
    backtest_logging:            # 回测日志模式（P2-3）：日志降级 + 异步队列 + 采样
      enabled / level / sampling
    timer:                       # 分阶段计时（P2-4）
      enabled
    light_lookahead_guard:       # 轻量防未来函数（P2-5）
      enabled / strict
    monte_carlo:                 # 蒙特卡洛 skip（P2-6）
      skip
    incremental:                 # 增量回测（P2-8/9）
      enabled / force_full
    memory:                      # 内存自动降级（P2-9）
      enabled / warning_pct / critical_pct

实际应用:
  - engine.mode    -> 由入口传给 BacktestScheduler / run_backtest（三模式调度）
  - backtest_logging -> enable_backtest_logging（root logger 降级 + 异步队列，防日志风暴）
  - timer          -> reset_stage_timers（分阶段计时就绪）
  - light_lookahead_guard -> 单例 enabled/strict + 同步环境变量（P2-D7-002 整改：当前仅同步开关、未接入引擎，属独立可选模块；接入需在事件/混合引擎逐 bar 检查点挂载 on_bar_close/check_*）
  - parallel / monte_carlo / incremental / memory -> 解析为配置值，由各执行器消费
    （并行上下文由 parallel_factor_context 包裹时生效、MC skip 由分析器构造时传入、
     增量/内存阈值由批量与增量调度器消费）

设计原则:
  - 缺省值即安全默认，字段缺失向前兼容（旧配置可直接运行）
  - 防御式校验：非法枚举 / 非法阈值 / 非法级别抛出 ValueError 带清晰信息
  - 本模块不做时间序列计算，无未来函数风险
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from myquant.utils.memory_watcher import get_memory_watcher

logger = logging.getLogger("myquant.utils.performance_config")

# ── 常量（禁魔法数字） ──────────────────────────────────────────────
DEFAULT_ENGINE_MODE: str = "auto"
SUPPORTED_ENGINE_MODES: List[str] = ["auto", "vectorized", "mixed", "event"]
ENGINE_PRIORITY: List[str] = ["vectorized", "mixed", "event"]

DEFAULT_BT_LOG_LEVEL: str = "WARNING"
SUPPORTED_LOG_LEVELS: List[str] = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_MEMORY_WARNING_PCT: float = 70.0
DEFAULT_MEMORY_CRITICAL_PCT: float = 85.0

# 轻量防未来函数环境变量（与 light_lookahead_guard 模块约定一致）
ENV_LIGHT_GUARD_ENABLED: str = "LIGHT_LOOKAHEAD_GUARD_ENABLED"
ENV_LIGHT_GUARD_STRICT: str = "LIGHT_LOOKAHEAD_GUARD_STRICT"

_LOG_LEVEL_MAP: Dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


# ── 配置值对象 ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class PerformanceConfig:
    """解析后的性能配置值对象（不可变，带默认值）。

    Attributes:
        engine_mode: 引擎模式（auto/event/mixed/vectorized）。
        parallel_enabled: 是否启用并行治理。
        cpu_workers: 计算进程池大小（None=自动）。
        check_pickle: 进程池提交前是否做 pickle 预检。
        backtest_logging_enabled: 是否启用回测日志模式。
        backtest_logging_level: 回测日志级别（字符串）。
        backtest_logging_sampling: 是否启用采样输出。
        timer_enabled: 是否启用分阶段计时。
        light_guard_enabled: 是否启用轻量防未来函数校验。
        light_guard_strict: 轻量校验是否严格模式。
        mc_skip: 是否跳过蒙特卡洛模拟。
        incremental_enabled: 是否启用增量回测。
        incremental_force_full: 增量回测是否强制全量。
        memory_enabled: 是否启用内存自动降级。
        memory_warning_pct: 内存告警阈值（系统内存百分比）。
        memory_critical_pct: 内存严重阈值（系统内存百分比）。
    """

    engine_mode: str = DEFAULT_ENGINE_MODE
    parallel_enabled: bool = True
    cpu_workers: Optional[int] = None
    check_pickle: bool = True
    backtest_logging_enabled: bool = True
    backtest_logging_level: str = DEFAULT_BT_LOG_LEVEL
    backtest_logging_sampling: bool = True
    timer_enabled: bool = True
    light_guard_enabled: bool = True
    light_guard_strict: bool = False
    mc_skip: bool = False
    incremental_enabled: bool = True
    incremental_force_full: bool = False
    memory_enabled: bool = True
    memory_warning_pct: float = DEFAULT_MEMORY_WARNING_PCT
    memory_critical_pct: float = DEFAULT_MEMORY_CRITICAL_PCT

    def __post_init__(self) -> None:
        """防御式校验：枚举/级别/阈值合法性。"""
        if self.engine_mode not in SUPPORTED_ENGINE_MODES:
            raise ValueError(
                f"非法引擎模式: {self.engine_mode!r}，可选: {SUPPORTED_ENGINE_MODES}"
            )
        level = self.backtest_logging_level.upper()
        if level not in SUPPORTED_LOG_LEVELS:
            raise ValueError(
                f"非法日志级别: {self.backtest_logging_level!r}，可选: {SUPPORTED_LOG_LEVELS}"
            )
        if not (0 < self.memory_warning_pct <= 100):
            raise ValueError(
                f"memory_warning_pct 必须在 (0, 100] 内，收到: {self.memory_warning_pct}"
            )
        if not (0 < self.memory_critical_pct <= 100):
            raise ValueError(
                f"memory_critical_pct 必须在 (0, 100] 内，收到: {self.memory_critical_pct}"
            )
        if self.memory_warning_pct >= self.memory_critical_pct:
            raise ValueError(
                f"memory_warning_pct ({self.memory_warning_pct}) 必须小于 "
                f"memory_critical_pct ({self.memory_critical_pct})"
            )
        if self.cpu_workers is not None and (
            not isinstance(self.cpu_workers, int) or self.cpu_workers < 1
        ):
            raise ValueError(f"cpu_workers 必须为正整数或 None，收到: {self.cpu_workers!r}")
        # 规范化级别为大写
        object.__setattr__(self, "backtest_logging_level", level)

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "PerformanceConfig":
        """从配置字典（yaml 顶层）解析性能配置。

        Args:
            config_dict: 完整配置字典（含 engine / performance 段）。

        Returns:
            PerformanceConfig 实例（缺省字段用安全默认值）。

        Raises:
            ValueError: 任一字段非法。
        """
        engine_cfg = config_dict.get("engine", {}) or {}
        perf_cfg = config_dict.get("performance", {}) or {}

        parallel = perf_cfg.get("parallel", {}) or {}
        bt_log = perf_cfg.get("backtest_logging", {}) or {}
        timer = perf_cfg.get("timer", {}) or {}
        light_guard = perf_cfg.get("light_lookahead_guard", {}) or {}
        monte_carlo = perf_cfg.get("monte_carlo", {}) or {}
        incremental = perf_cfg.get("incremental", {}) or {}
        memory = perf_cfg.get("memory", {}) or {}

        return cls(
            engine_mode=str(engine_cfg.get("mode", DEFAULT_ENGINE_MODE)),
            parallel_enabled=_as_bool(parallel.get("enabled", True), "performance.parallel.enabled"),
            cpu_workers=parallel.get("cpu_workers"),
            check_pickle=_as_bool(parallel.get("check_pickle", True), "performance.parallel.check_pickle"),
            backtest_logging_enabled=_as_bool(bt_log.get("enabled", True), "performance.backtest_logging.enabled"),
            backtest_logging_level=str(bt_log.get("level", DEFAULT_BT_LOG_LEVEL)),
            backtest_logging_sampling=_as_bool(bt_log.get("sampling", True), "performance.backtest_logging.sampling"),
            timer_enabled=_as_bool(timer.get("enabled", True), "performance.timer.enabled"),
            light_guard_enabled=_as_bool(light_guard.get("enabled", True), "performance.light_lookahead_guard.enabled"),
            light_guard_strict=_as_bool(light_guard.get("strict", False), "performance.light_lookahead_guard.strict"),
            mc_skip=_as_bool(monte_carlo.get("skip", False), "performance.monte_carlo.skip"),
            incremental_enabled=_as_bool(incremental.get("enabled", True), "performance.incremental.enabled"),
            incremental_force_full=_as_bool(incremental.get("force_full", False), "performance.incremental.force_full"),
            memory_enabled=_as_bool(memory.get("enabled", True), "performance.memory.enabled"),
            memory_warning_pct=float(memory.get("warning_pct", DEFAULT_MEMORY_WARNING_PCT)),
            memory_critical_pct=float(memory.get("critical_pct", DEFAULT_MEMORY_CRITICAL_PCT)),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可读字典（供报告/日志输出）。"""
        return {
            "engine_mode": self.engine_mode,
            "parallel": {
                "enabled": self.parallel_enabled,
                "cpu_workers": self.cpu_workers,
                "check_pickle": self.check_pickle,
            },
            "backtest_logging": {
                "enabled": self.backtest_logging_enabled,
                "level": self.backtest_logging_level,
                "sampling": self.backtest_logging_sampling,
            },
            "timer": {"enabled": self.timer_enabled},
            "light_lookahead_guard": {
                "enabled": self.light_guard_enabled,
                "strict": self.light_guard_strict,
            },
            "monte_carlo": {"skip": self.mc_skip},
            "incremental": {
                "enabled": self.incremental_enabled,
                "force_full": self.incremental_force_full,
            },
            "memory": {
                "enabled": self.memory_enabled,
                "warning_pct": self.memory_warning_pct,
                "critical_pct": self.memory_critical_pct,
            },
        }


def _as_bool(value: Any, name: str) -> bool:
    """把 yaml 布尔/字符串值规范化为 bool（防御式）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    raise ValueError(f"{name} 必须为布尔值，收到: {value!r}")


# ── 引擎模式解析 ─────────────────────────────────────────────────────
def resolve_engine_mode(config: PerformanceConfig, strategy: Any = None) -> str:
    """解析最终引擎模式：auto 时按策略能力优先级选择。

    Args:
        config: 性能配置。
        strategy: 策略实例（engine_mode=auto 时用于探测能力）。

    Returns:
        具体引擎模式（vectorized/mixed/event）。

    Raises:
        ValueError: auto 且策略不支持任何引擎模式。
    """
    if config.engine_mode != "auto":
        return config.engine_mode
    if strategy is None or not hasattr(strategy, "engine_modes"):
        raise ValueError("engine_mode=auto 需要策略实例以探测引擎能力")
    modes = set(strategy.engine_modes())
    for candidate in ENGINE_PRIORITY:
        if candidate in modes:
            return candidate
    raise ValueError(f"策略不支持任何可用引擎模式: {sorted(modes)}")


# ── 开关应用 ─────────────────────────────────────────────────────────
def apply_performance(config: PerformanceConfig) -> Dict[str, Any]:
    """把性能开关实际应用到运行环境（有副作用的开关落地）。

    应用的开关:
      - backtest_logging.enabled  -> enable_backtest_logging（root logger 降级+异步队列）
      - timer.enabled             -> reset_stage_timers（分阶段计时就绪）
      - light_lookahead_guard     -> 单例 enabled/strict + 环境变量（引擎逐 bar 校验）

    返回:
        实际应用的开关摘要字典（含 memory 阈值初始化，供批量调度器复用单例）。

    Raises:
        RuntimeError: 应用回测日志模式失败（如队列已启动）。
    """
    applied: Dict[str, Any] = {"engine_mode": config.engine_mode}

    # 1. 回测日志模式（日志降级 + 异步队列 + 采样，防日志风暴阻塞主线程）
    if config.backtest_logging_enabled:
        from myquant.core.logger import enable_backtest_logging

        enable_backtest_logging(
            level=_LOG_LEVEL_MAP[config.backtest_logging_level],
            async_queue=True,
        )
        applied["backtest_logging"] = {
            "enabled": True,
            "level": config.backtest_logging_level,
            "sampling": config.backtest_logging_sampling,
        }
        logger.info("回测日志模式已启用（级别 %s）", config.backtest_logging_level)

    # 2. 分阶段计时就绪（清零历史阶段统计）
    if config.timer_enabled:
        from myquant.utils.timer import reset_stage_timers

        reset_stage_timers()
        applied["timer"] = {"enabled": True}

    # 3. 轻量防未来函数校验（引擎逐 bar 校验；strict 立即抛错）
    if config.light_guard_enabled:
        from myquant.core.light_lookahead_guard import get_light_lookahead_guard

        guard = get_light_lookahead_guard()
        guard.enabled = True
        guard.strict = config.light_guard_strict
        os.environ[ENV_LIGHT_GUARD_ENABLED] = "true"
        os.environ[ENV_LIGHT_GUARD_STRICT] = (
            "true" if config.light_guard_strict else "false"
        )
        applied["light_lookahead_guard"] = {
            "enabled": True,
            "strict": config.light_guard_strict,
        }
    else:
        from myquant.core.light_lookahead_guard import get_light_lookahead_guard

        get_light_lookahead_guard().enabled = False
        os.environ[ENV_LIGHT_GUARD_ENABLED] = "false"
        applied["light_lookahead_guard"] = {"enabled": False}

    # 4. 内存监控单例按阈值初始化（供批量/增量调度器消费；首建后阈值固定）
    if config.memory_enabled:
        get_memory_watcher(
            warning_pct=config.memory_warning_pct,
            critical_pct=config.memory_critical_pct,
        )
        applied["memory"] = {
            "enabled": True,
            "warning_pct": config.memory_warning_pct,
            "critical_pct": config.memory_critical_pct,
        }

    return applied


# ── 演示 / 自测入口 ───────────────────────────────────────────────────
def _main() -> int:
    """模块自测：解析 + 校验 + 应用。"""
    print("=" * 64)
    print("  performance_config.py — 性能配置应用器 自测 Demo")
    print("=" * 64)

    # 1. 从完整配置字典解析（含 engine + performance 全段）
    sample = {
        "engine": {"mode": "mixed"},
        "performance": {
            "parallel": {"enabled": True, "cpu_workers": 4, "check_pickle": True},
            "backtest_logging": {"enabled": True, "level": "WARNING", "sampling": True},
            "timer": {"enabled": True},
            "light_lookahead_guard": {"enabled": True, "strict": True},
            "monte_carlo": {"skip": False},
            "incremental": {"enabled": True, "force_full": False},
            "memory": {"enabled": True, "warning_pct": 60.0, "critical_pct": 80.0},
        },
    }
    cfg = PerformanceConfig.from_dict(sample)
    print(f"[1] 解析: mode={cfg.engine_mode}, log={cfg.backtest_logging_level}, "
          f"mem={cfg.memory_warning_pct}/{cfg.memory_critical_pct}")
    assert cfg.engine_mode == "mixed"
    assert cfg.cpu_workers == 4
    assert cfg.light_guard_strict is True
    print("    ✓ 完整配置解析正确")

    # 2. 缺省向前兼容：空配置 -> 安全默认
    cfg_default = PerformanceConfig.from_dict({})
    print(f"[2] 空配置默认: mode={cfg_default.engine_mode}, "
          f"cpu={cfg_default.cpu_workers}, strict={cfg_default.light_guard_strict}")
    assert cfg_default.engine_mode == "auto"
    assert cfg_default.cpu_workers is None
    assert cfg_default.memory_warning_pct == 70.0
    print("    ✓ 缺省向前兼容")

    # 3. 防御校验
    print("[3] 非法配置校验")
    for bad, desc in [
        ({"engine": {"mode": "nope"}}, "非法引擎模式"),
        ({"performance": {"backtest_logging": {"level": "TRACE"}}}, "非法日志级别"),
        ({"performance": {"memory": {"warning_pct": 90, "critical_pct": 80}}},
         "warning>=critical"),
        ({"performance": {"memory": {"warning_pct": 0}}}, "warning<=0"),
        ({"performance": {"parallel": {"cpu_workers": 0}}}, "cpu_workers=0"),
    ]:
        try:
            PerformanceConfig.from_dict(bad)
            print(f"    FAIL(未拦截): {desc}")
        except ValueError:
            print(f"    ✓ 拦截: {desc}")

    # 4. 引擎模式解析（auto + 策略能力）
    # P0-1（回写#255）：自测改用本地能力桩（duck-type），消除 L0→L3 反向依赖；
    #       resolve_engine_mode 仅依赖 strategy.engine_modes() 能力接口，不依赖具体策略类
    class _EngineCapableStub:
        """本地自测桩：模拟策略引擎能力（仅实现 resolve_engine_mode 所需接口）。"""

        def engine_modes(self) -> List[str]:
            return ["event", "mixed", "vectorized"]

    auto_cfg = PerformanceConfig.from_dict({"engine": {"mode": "auto"}})
    resolved = resolve_engine_mode(auto_cfg, _EngineCapableStub())
    print(f"[4] auto 解析: {resolved}")
    assert resolved in ("vectorized", "mixed", "event")
    assert resolve_engine_mode(cfg, None) == "mixed"
    print("    ✓ auto 按策略能力解析 / 显式模式直通")

    # 5. 应用开关（回测日志/计时/轻量防未来函数）
    print("[5] 应用开关")
    applied = apply_performance(cfg)
    print(f"    应用摘要: {sorted(applied.keys())}")
    assert "backtest_logging" in applied
    assert "light_lookahead_guard" in applied and applied["light_lookahead_guard"]["strict"]
    assert "timer" in applied
    assert "memory" in applied
    from myquant.core.light_lookahead_guard import get_light_lookahead_guard
    assert get_light_lookahead_guard().enabled is True
    assert get_light_lookahead_guard().strict is True
    print("    ✓ 回测日志/计时/轻量防未来函数已实际应用")
    from myquant.core.logger import disable_backtest_logging
    disable_backtest_logging()

    print("\n" + "=" * 64)
    print("  自测通过: 解析/默认兼容/5项校验/引擎解析/开关应用")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main())
