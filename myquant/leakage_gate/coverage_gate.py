# -*- coding: utf-8 -*-
"""覆盖率门禁业务逻辑（P1，v1.2 §5/§6.2 P1-4）。

背景
----
测试覆盖率在项目里曾经是"无人执行的数字"：R-19（引用级近似）长期漂移、R-20（测试
计数）可被删测试刷量。本模块把覆盖率做成**可重复判定的门禁**：读 pytest-cov 产出的
coverage.json，按**资金安全代价分层**（T0 阻断级 / T1 严格级 / T2 常规级 / T3 报告级），
执行 C-1~C-7 判据，输出可读报告与通过/失败结论。薄入口见 scripts/覆盖率门禁.py。

口径契约（v1.2 §5.4，P1-9）
----------------------------
- 采集：`pytest-cov --cov=myquant --cov-report=json`（pyproject [tool.coverage.run] 固定 source/omit）。
- 覆盖集：tests/unit + tests/leakage_gate + tests/realism_gate + tests/scripts（**排除 tests/benchmarks**）。
- 权威平台：**CI ubuntu-latest / Python 3.12 腿**（Windows 腿只跑测试不产覆盖率）。
- 阻断主口径：**行覆盖**（num_statements / covered_lines）；分支仅报告，不入阻断。
- 与既有口径分工（不混用，数字禁止相互引用）：
  - R-19：引用级近似（廉价快照，保留）；本门禁为执行级，两数字互不引用。
  - R-20：测试计数（辅助兜底，保留）；本门禁不依赖测试数量。
  - R-24：只管 >1500 行文件的行数；本门禁的 C-5 语句数棘轮覆盖其余文件，二者互补。

基线治理规则（v1.2 §5.3）
--------------------------
1. 改基线（COVERAGE_BASELINE / COVERED_LINES_BASELINE / TIER_FLOOR / FILE_FLOOR /
   FILE_STMT_CEILING / NEW_FILE_FLOOR / SOURCE_ASSERT_ONLY_MAX / SKIP_BUDGET）
   **须独立 commit 并写明理由**，禁止与业务改动混在同一提交里。
2. 每季度必须至少上调一次，否则门禁输出告警。
3. 基线常量历史取值在下方 docstring 变更记录中留存（同 test_architecture.py 做法）。

基线变更记录
------------
- v1.2 §8.2 初值（2026-10-15，P1 占位）：COVERAGE_BASELINE=62.0 / COVERED_LINES_BASELINE=38300
  （本地实测 62.99% / 38,864 行，留 ~1pp / ~564 行学习期余量；CI 实测后替换）。

★★★ 防未来函数 ★★★：本模块只读 coverage.json 与 tests/ 源码，不参与回测/实盘数据流，
无未来函数风险。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── 路径常量 ─────────────────────────────────────────────
DEFAULT_COVERAGE_JSON: str = "coverage.json"
DEFAULT_JUNIT_XML: str = "test-results.xml"
SOURCE_ROOT: str = "myquant"

# ── 分层表（v1.2 §5.1，按资金安全代价分层，不按架构层）──────
# T0 阻断级：资金链路核心
_T0_DIR_PREFIXES: tuple[str, ...] = (
    "leakage_gate/",
    "realism_gate/",
    "risk/",
    "execution/",
    "live/",
    "engine/",
)
_T0_EXACT_FILES: frozenset[str] = frozenset({
    "backtest/engine.py",
    "backtest/mixed_engine.py",
    "backtest/vectorized_engine.py",
    "strategies/kunpeng_strategy.py",
})
# T1 严格级：正确性敏感
_T1_DIR_PREFIXES: tuple[str, ...] = (
    "core/",
    "data_access/",
    "monitoring/",
    "models/",
    "indicators/",
    "allocation/",
    "ml/",
    "strategies/",
    "backtest/",
)
# T2 常规级：外围/工具
_T2_DIR_PREFIXES: tuple[str, ...] = ("api/", "web/", "gui/", "utils/", "acceptance/")
# T3 报告级：database/migrations/、*_selftest.py、性能基准（仅报告 + 迁移冒烟门禁 P1-7）

# ── 基线常量（v1.2 §5.3/§8.2；改基线须独立 commit，见模块 docstring）────
# C-1b 总覆盖率下限（v1 学习期额度；CI 实测后替换）
COVERAGE_BASELINE: float = 62.0
# C-1a 绝对覆盖行数下限（CI 口径；实测 38,864 → 占位 38,300，留 ~564 行余量）
COVERED_LINES_BASELINE: int = 38300
# C-2 分层下限（实测 71.2/62.9/55.3 − 3~4pp 余量）
TIER_FLOOR: dict[str, float] = {"T0": 68.0, "T1": 60.0, "T2": 52.0}
# C-3 关键文件覆盖率下限（v1.2 §5.2 表，只增不减；取实测就近下取整）。
# 注：qmt_broker 显示 61.0% 为四舍五入，底层实测 60.95% < 61.0 会浮点边界误报，
# 故取 60.0（低于实测 1 个点）；其余按 v1.2 表，CI 首轮冻结时整体校准（§5.3）。
FILE_FLOOR: dict[str, float] = {
    "live/data_feed.py": 28.0,
    "core/light_lookahead_guard.py": 34.0,
    "indicators/rsrs.py": 43.0,
    "backtest/fee_table.py": 44.0,
    "backtest/order_matcher.py": 48.0,
    "data_access/manager.py": 48.0,
    "core/event_bus.py": 52.0,
    "risk/risk_manager.py": 56.0,
    "live/scheduler.py": 59.0,
    "live/brokers/qmt_broker.py": 60.0,
    "monitoring/alerting.py": 68.0,
    "backtest/vectorized_engine.py": 69.0,
    "strategies/kunpeng_strategy.py": 85.0,
    "backtest/engine.py": 85.0,
}
# C-5 存量 T0/T1 文件语句数棘轮（v1.2 §5.3：按现状冻结；语句数超 ceiling 即红，
# 封堵"把新代码塞进旧文件绕开 C-4"）。初值由本地实测回填（CI 实测后独立 commit 更新）。
FILE_STMT_CEILING: dict[str, int] = {
    "allocation/__init__.py": 5,
    "allocation/base.py": 130,
    "allocation/equal_weight.py": 29,
    "allocation/risk_parity.py": 439,
    "allocation/target_volatility.py": 25,
    "backtest/__init__.py": 14,
    "backtest/assembly.py": 13,
    "backtest/async_monte_carlo.py": 249,
    "backtest/attribution.py": 206,
    "backtest/baseline_archive.py": 243,
    "backtest/batch_backtest.py": 417,
    "backtest/batch_report.py": 261,
    "backtest/candidate_pool.py": 74,
    "backtest/candidate_report.py": 205,
    "backtest/config_builder.py": 68,
    "backtest/consistency_check.py": 285,
    "backtest/dashboard_serializer.py": 238,
    "backtest/data_loader.py": 5,
    "backtest/engine.py": 711,
    "backtest/engine_benchmark.py": 149,
    "backtest/exempt_registry.py": 10,
    "backtest/fee_common.py": 289,
    "backtest/fee_table.py": 234,
    "backtest/incremental_backtest.py": 270,
    "backtest/kunpeng_param_scanner.py": 175,
    "backtest/metrics.py": 290,
    "backtest/mixed_engine.py": 621,
    "backtest/monte_carlo.py": 112,
    "backtest/order_matcher.py": 401,
    "backtest/param_optimizer.py": 448,
    "backtest/pdf_report.py": 333,
    "backtest/recorder.py": 124,
    "backtest/report.py": 179,
    "backtest/survivorship_bias.py": 167,
    "backtest/vectorized_engine.py": 499,
    "core/__init__.py": 13,
    "core/alert_store.py": 199,
    "core/async_runtime.py": 125,
    "core/audit_log.py": 223,
    "core/cache.py": 147,
    "core/config_loader.py": 145,
    "core/constants.py": 265,
    "core/date_utils.py": 133,
    "core/event_bus.py": 834,
    "core/exceptions.py": 188,
    "core/field_crypto.py": 101,
    "core/health_check.py": 548,
    "core/light_lookahead_guard.py": 321,
    "core/logger.py": 457,
    "core/lookahead_guard.py": 200,
    "core/measurability.py": 149,
    "core/metrics_history.py": 599,
    "core/registry.py": 12,
    "core/settings.py": 240,
    "core/transports/__init__.py": 9,
    "core/transports/base.py": 71,
    "core/transports/rabbitmq_bus.py": 154,
    "core/transports/redis_bus.py": 81,
    "data_access/__init__.py": 9,
    "data_access/cleaner/__init__.py": 7,
    "data_access/cleaner/bar_cleaner.py": 341,
    "data_access/cleaner/base_cleaner.py": 315,
    "data_access/cleaner/derived_metrics.py": 115,
    "data_access/cleaner/error_codes.py": 123,
    "data_access/cleaner/pipeline.py": 464,
    "data_access/cleaner/quality_reporter.py": 37,
    "data_access/cleaner/rules_loader.py": 286,
    "data_access/cleaner/tick_cleaner.py": 195,
    "data_access/factor_sync_service.py": 113,
    "data_access/factory.py": 75,
    "data_access/fetcher.py": 157,
    "data_access/financial_service.py": 315,
    "data_access/financial_sync_service.py": 161,
    "data_access/full_clean_service.py": 531,
    "data_access/loader.py": 461,
    "data_access/manager.py": 756,
    "data_access/provider.py": 381,
    "data_access/share_change_sync_service.py": 114,
    "data_access/sources/__init__.py": 5,
    "data_access/sources/akshare_source.py": 571,
    "data_access/sources/base.py": 204,
    "data_access/sources/broker_source.py": 89,
    "data_access/sources/composite_source.py": 230,
    "data_access/sources/tdx_source.py": 205,
    "data_access/sources/web_source.py": 249,
    "data_access/time_semantics.py": 43,
    "database/__init__.py": 6,
    "database/connection.py": 139,
    "database/queries.py": 505,
    "database/schema.py": 97,
    "database/trade_log.py": 54,
    "engine/__init__.py": 4,
    "engine/base.py": 63,
    "execution/__init__.py": 6,
    "execution/base.py": 44,
    "execution/sim_broker.py": 237,
    "execution/state_machine.py": 63,
    "indicators/__init__.py": 111,
    "indicators/factor_cache.py": 411,
    "indicators/factor_engine.py": 377,
    "indicators/factor_preprocessor.py": 217,
    "indicators/factor_validator.py": 256,
    "indicators/momentum.py": 209,
    "indicators/moving_average.py": 169,
    "indicators/rsrs.py": 289,
    "indicators/tradability.py": 95,
    "indicators/volatility.py": 370,
    "leakage_gate/__init__.py": 5,
    "leakage_gate/config_deadkey.py": 72,
    "leakage_gate/consumption.py": 84,
    "leakage_gate/contract.py": 99,
    "leakage_gate/coverage_gate.py": 223,
    "leakage_gate/cross_sectional.py": 138,
    "leakage_gate/exemption.py": 143,
    "leakage_gate/gate.py": 106,
    "leakage_gate/report.py": 63,
    "leakage_gate/sanity.py": 228,
    "leakage_gate/signal_words.py": 6,
    "leakage_gate/static_scan.py": 421,
    "live/__init__.py": 5,
    "live/acceptance.py": 228,
    "live/broker_factory.py": 13,
    "live/brokers/__init__.py": 8,
    "live/brokers/base_broker.py": 351,
    "live/brokers/manager.py": 475,
    "live/brokers/paper_broker.py": 347,
    "live/brokers/ptrade_broker.py": 58,
    "live/brokers/qmt_broker.py": 981,
    "live/data_feed.py": 882,
    "live/emergency_liquidation.py": 170,
    "live/engine.py": 1197,
    "live/kill_switch_signal.py": 147,
    "live/portfolio.py": 419,
    "live/run_mode.py": 114,
    "live/scheduler.py": 995,
    "live/state_store.py": 374,
    "ml/__init__.py": 6,
    "ml/feature_contract.py": 69,
    "ml/features.py": 447,
    "ml/inference_benchmark.py": 104,
    "ml/inference_service.py": 187,
    "ml/model_monitor.py": 214,
    "ml/model_registry.py": 518,
    "ml/training_pipeline.py": 624,
    "models/__init__.py": 5,
    "models/backtest.py": 224,
    "models/market_data.py": 280,
    "models/trade.py": 260,
    "monitoring/__init__.py": 4,
    "monitoring/account_reconciler.py": 278,
    "monitoring/action_engine.py": 968,
    "monitoring/alerting.py": 1510,
    "monitoring/capacity.py": 384,
    "monitoring/center_provider.py": 201,
    "monitoring/circuit_breaker.py": 338,
    "monitoring/config_as_code.py": 175,
    "monitoring/data_quality_explorer.py": 340,
    "monitoring/dynamic_baseline.py": 728,
    "monitoring/execution_stats.py": 496,
    "monitoring/incident_context.py": 316,
    "monitoring/margin_monitor.py": 288,
    "monitoring/meta_monitor.py": 572,
    "monitoring/metrics_collector.py": 866,
    "monitoring/rate_limit_monitor.py": 256,
    "monitoring/regime_matcher.py": 238,
    "monitoring/rolling_performance.py": 335,
    "monitoring/security_monitor.py": 161,
    "monitoring/signal_consistency.py": 381,
    "monitoring/signal_decay.py": 346,
    "monitoring/watchdog.py": 351,
    "monitoring/weekly_review.py": 223,
    "realism_gate/__init__.py": 4,
    "realism_gate/checks/__init__.py": 5,
    "realism_gate/checks/causality.py": 19,
    "realism_gate/checks/cost_model.py": 16,
    "realism_gate/checks/data_pit.py": 26,
    "realism_gate/checks/market_rules.py": 16,
    "realism_gate/checks/sample_pool.py": 10,
    "realism_gate/gate.py": 72,
    "realism_gate/invariants.py": 137,
    "realism_gate/manifest.py": 105,
    "realism_gate/report.py": 36,
    "realism_gate/signature_guard.py": 43,
    "risk/__init__.py": 4,
    "risk/guardian.py": 254,
    "risk/risk_manager.py": 574,
    "risk/rules.py": 670,
    "strategies/__init__.py": 11,
    "strategies/base.py": 439,
    "strategies/grid_strategy.py": 210,
    "strategies/kunpeng_strategy.py": 370,
    "strategies/ma_strategy.py": 331,
    "strategies/mean_reversion_strategy.py": 214,
    "strategies/momentum_strategy.py": 257,
    "strategies/multi_factor_strategy.py": 596,
    "strategies/param_tuner.py": 196,
    "strategies/registry.py": 105,
}
# C-4 新增文件门槛：T0/T1 新增须 ≥ 60%、T2 新增须 ≥ 30%，否则红。
# "新增"定义 = 不在存量注册表（FILE_STMT_CEILING 的 T0/T1、T2_FILES 的 T2）中的文件。
NEW_FILE_FLOOR: dict[str, float] = {"T0": 60.0, "T1": 60.0, "T2": 30.0}
# C-4 的 T2 存量注册表（T2 全部文件；初值由本地实测回填）
T2_FILES: frozenset[str] = frozenset({
    "acceptance/__init__.py",
    "acceptance/b_acceptance.py",
    "acceptance/c_acceptance.py",
    "acceptance/chaos_drill.py",
    "acceptance/d_acceptance.py",
    "acceptance/disaster_drill.py",
    "acceptance/live_safety_gate.py",
    "acceptance/m1_acceptance.py",
    "api/__init__.py",
    "api/cli.py",
    "api/dependencies/__init__.py",
    "api/dependencies/get_services.py",
    "api/middleware/__init__.py",
    "api/middleware/auth.py",
    "api/middleware/rate_limit.py",
    "api/models/__init__.py",
    "api/models/schemas.py",
    "api/routes.py",
    "gui/__init__.py",
    "gui/backtest_config_dialog.py",
    "gui/batch_backtest_tab.py",
    "gui/cleanup_dialog.py",
    "gui/data_clean_dialog.py",
    "gui/data_download_dialog.py",
    "gui/main_window.py",
    "gui/paper_run_dialog.py",
    "gui/widgets/__init__.py",
    "gui/widgets/kline_item.py",
    "gui/widgets/plot_helper.py",
    "gui/widgets/side_panel.py",
    "utils/__init__.py",
    "utils/cache_sync.py",
    "utils/cleanup_service.py",
    "utils/csv_utils.py",
    "utils/data_fingerprint.py",
    "utils/data_versioner.py",
    "utils/file_utils.py",
    "utils/math_utils.py",
    "utils/memory_pool.py",
    "utils/memory_watcher.py",
    "utils/parallel.py",
    "utils/performance_config.py",
    "utils/pickle_checker.py",
    "utils/time_utils.py",
    "utils/timer.py",
    "utils/trade_calendar.py",
    "web/__init__.py",
    "web/dashboard.py",
})
# C-6 仅源码断言守护的模块数上限（只减不增；替代不成立的 --cov-omit 方案，保持缺口可见）
SOURCE_ASSERT_ONLY_MAX: int = 4
# C-7 运行时跳过预算（pytest.skip 实际触发数，只减不增；防 fail-open 回潮）
SKIP_BUDGET: int = 4


def tier_of(rel_path: str) -> str | None:
    """返回文件所属分层（T0/T1/T2/T3）；无法归层返回 None。

    Args:
        rel_path: 相对 SOURCE_ROOT 的 posix 风格路径（如 "backtest/engine.py"）。

    Returns:
        分层名（"T0"/"T1"/"T2"/"T3"）；未映射返回 None（V-9 口径自洽检查暴露）。
    """
    name = rel_path.rsplit("/", 1)[-1]
    # T3：迁移脚本与自测模块（仅报告 + 迁移冒烟门禁）
    if name.endswith("_selftest.py") or rel_path.startswith("database/migrations/"):
        return "T3"
    # 顶层包元数据（myquant/__init__.py）归报告级
    if rel_path == "__init__.py":
        return "T3"
    # 子包 __init__.py 归其所在目录分层（leakage_gate/__init__.py → T0）
    if name == "__init__.py":
        dir_prefix = rel_path[: -len("__init__.py")]
        if dir_prefix.startswith(_T0_DIR_PREFIXES):
            return "T0"
        if dir_prefix.startswith("database/"):
            return "T1"
        if dir_prefix.startswith(_T1_DIR_PREFIXES):
            return "T1"
        if dir_prefix.startswith(_T2_DIR_PREFIXES):
            return "T2"
        return None
    # T0：精确文件 + 目录前缀
    if rel_path in _T0_EXACT_FILES:
        return "T0"
    if rel_path.startswith(_T0_DIR_PREFIXES):
        return "T0"
    # T1：database/（非 migrations，已在上方排除）
    if rel_path.startswith("database/"):
        return "T1"
    if rel_path.startswith(_T1_DIR_PREFIXES):
        return "T1"
    # T2
    if rel_path.startswith(_T2_DIR_PREFIXES):
        return "T2"
    # 未知目录：显式暴露（V-9 红），不静默兜底
    return None


def build_tier_manifest(project_root: Path) -> dict[str, str]:
    """构建全量 myquant 文件分层清单（V-9 口径自洽的数据源）。

    Args:
        project_root: 项目根目录。

    Returns:
        {相对 SOURCE_ROOT 的 posix 路径: 分层名}；未映射文件分层为 "UNMAPPED"。
    """
    src = project_root / SOURCE_ROOT
    manifest: dict[str, str] = {}
    if not src.is_dir():
        return manifest
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        tier = tier_of(rel)
        manifest[rel] = tier if tier is not None else "UNMAPPED"
    return manifest


@dataclass
class TierStat:
    """单层覆盖率聚合统计。

    Attributes:
        tier: 分层名（T0/T1/T2）。
        statements: 该层语句总数。
        covered: 该层已覆盖语句数。
        percent: 该层行覆盖率（百分比）。
        file_count: 该层文件数。
    """

    tier: str
    statements: int = 0
    covered: int = 0
    percent: float = 0.0
    file_count: int = 0


@dataclass
class CoverageGateReport:
    """覆盖率门禁判定报告。

    Attributes:
        json_path: 被读取的 coverage.json 路径。
        total_statements: 总语句数。
        total_covered: 总已覆盖语句数。
        total_percent: 总行覆盖率（百分比）。
        tier_stats: 分层聚合统计（T0/T1/T2）。
        violations: 违反的判据消息清单（空 = 通过）。
        unmapped_files: 未分层文件清单（V-9 口径漏洞）。
        skipped: 运行时跳过数（C-7，来自 junit.xml）。
        report_only: 是否报告模式（True 不阻断，仅输出）。
        ok: 判据是否全部通过（report_only 时恒为 True 语义为"可阻断性待冻结"）。
    """

    json_path: str
    total_statements: int = 0
    total_covered: int = 0
    total_percent: float = 0.0
    tier_stats: list[TierStat] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    unmapped_files: list[str] = field(default_factory=list)
    skipped: int = 0
    assert_only_count: int = 0
    report_only: bool = False

    @property
    def ok(self) -> bool:
        """判据是否全部通过（报告模式下恒 True，阻断性由冻结后的常量决定）。"""
        return not self.violations

    def summary(self) -> str:
        """多行可读摘要（判据预演输出，含口径标注，禁裸报数字）。"""
        lines = [
            "覆盖率门禁（口径: 覆盖集=unit+leakage_gate+realism_gate+scripts，排除benchmarks；"
            "主口径=行覆盖；权威平台=CI ubuntu/Py3.12）",
            f"总量: {self.total_percent:.1f}% ({self.total_covered}/{self.total_statements} 语句) | "
            f"C-1a 下限 {COVERED_LINES_BASELINE} 行 / C-1b 下限 {COVERAGE_BASELINE}%",
        ]
        for ts in self.tier_stats:
            lines.append(
                f"  {ts.tier}: {ts.percent:.1f}% ({ts.covered}/{ts.statements}) "
                f"/ {ts.file_count} 文件 | 下限 {TIER_FLOOR.get(ts.tier, 0.0):.1f}%"
            )
        if self.unmapped_files:
            lines.append(f"  ⚠ 未分层文件({len(self.unmapped_files)}): {', '.join(self.unmapped_files)}")
        if self.assert_only_count > SOURCE_ASSERT_ONLY_MAX:
            lines.append(
                f"  ⚠ C-6 仅源码断言守护模块 {self.assert_only_count} 个 > 上限 "
                f"{SOURCE_ASSERT_ONLY_MAX}（P2-4 精化口径后启用阻断）"
            )
        if self.violations:
            lines.append(f"  ❌ 违反判据 {len(self.violations)} 项:")
            for v in self.violations:
                lines.append(f"      - {v}")
        else:
            lines.append("  ✓ 全部判据通过")
        if self.report_only:
            lines.append("  （报告模式：基线待 CI 冻结后转阻断）")
        return "\n".join(lines)


def _parse_skipped(junit_xml: Path | None) -> int:
    """从 junit.xml 统计运行时跳过数（C-7 数据源）。

    Args:
        junit_xml: pytest --junitxml 产物路径；不存在返回 0（C-7 不参与判定）。

    Returns:
        skipped 总数（testsuite.skipped 属性优先；否则累计 testcase skipped 标签数）。
    """
    if junit_xml is None or not junit_xml.is_file():
        return 0
    text = junit_xml.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<testsuite[^>]*\bskipped="(\d+)"', text)
    if m:
        return int(m.group(1))
    return len(re.findall(r"<testcase[^>]*>\s*<skipped", text))


def _find_source_assert_only_modules(project_root: Path) -> list[str]:
    """扫描"仅源码断言守护"的模块（C-6）：tests/ 中只有字符串引用（read_text 断言）、
    无 import 引用的 myquant 模块。

    Args:
        project_root: 项目根目录。

    Returns:
        满足"仅字符串引用、零 import 引用"的模块相对路径清单。
    """
    tests_root = project_root / "tests"
    if not tests_root.is_dir():
        return []
    test_texts: list[str] = []
    for py in tests_root.rglob("*.py"):
        if "benchmarks" in py.parts:
            continue  # 覆盖集排除 benchmarks，其断言不计入守护
        try:
            test_texts.append(py.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    blob = "\n".join(test_texts)
    src = project_root / SOURCE_ROOT
    if not src.is_dir():
        return []
    assert_only: list[str] = []
    for py in sorted(src.rglob("*.py")):
        rel = py.relative_to(src).as_posix()
        mod = rel[:-3].replace("/", ".")
        # import 引用（import myquant.X / from myquant import X / from myquant.X import Y）
        import_pattern = (
            rf"^\s*(import\s+myquant\.{re.escape(mod)}(?:\s|\.|$)|"
            rf"from\s+myquant(?:\.{re.escape(mod)})?\s+import\s+)"
        )
        has_import = re.search(import_pattern, blob, flags=re.MULTILINE) is not None
        if has_import:
            continue
        # 字符串引用（read_text 断言 / f"..." 路径）
        str_pattern = re.escape(f"myquant/{rel}")
        if re.search(str_pattern, blob):
            assert_only.append(rel)
    return assert_only


def _load_coverage(json_path: Path) -> dict[str, Any] | None:
    """读取 coverage.json 并做结构校验。

    Args:
        json_path: pytest-cov --cov-report=json 产物路径。

    Returns:
        json 内容 dict；文件缺失或结构非法返回 None。
    """
    if not json_path.is_file():
        logger.error("coverage.json 不存在: %s", json_path)
        return None
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:  # noqa: BLE001
        logger.error("coverage.json 解析失败: %s", exc)
        return None
    if "totals" not in data or "files" not in data:
        logger.error("coverage.json 缺少 totals/files 键（非 pytest-cov json 产物？）")
        return None
    return data


def _normalize_file_key(raw_key: str) -> str:
    """把 coverage.json 的 files 键归一化为相对 SOURCE_ROOT 的 posix 路径。

    coverage.py 的 files 键随平台/调用方式变化：CI（ubuntu）为相对正斜杠
    （"myquant/backtest/engine.py"），本地 Windows 可能为反斜杠绝对路径
    （"C:\\...\\myquant\\backtest\\engine.py"）。统一转成
    "backtest/engine.py" 形式的相对 posix 路径，保证 C-2 分层聚合与
    C-4 新增文件判定在任意平台一致。

    Args:
        raw_key: coverage.json files 键。

    Returns:
        相对 SOURCE_ROOT 的 posix 路径。
    """
    key = raw_key.replace("\\", "/")
    marker = SOURCE_ROOT + "/"
    if marker in key:
        return key.split(marker, 1)[-1]
    return key.lstrip("/")


def evaluate(
    coverage_json: Path,
    junit_xml: Path | None = None,
    project_root: Path | None = None,
    report_only: bool = False,
) -> CoverageGateReport:
    """执行覆盖率门禁全部判据（C-1~C-7 + V-9 口径自洽）。

    Args:
        coverage_json: coverage.json 路径。
        junit_xml: junit.xml 路径（可选，C-7 数据源）。
        project_root: 项目根目录（默认 coverage_json 的父目录）。
        report_only: True=报告模式（输出预演、不阻断）；False=阻断模式。

    Returns:
        判定报告（violations 为空即通过）。
    """
    root = project_root if project_root is not None else coverage_json.parent
    report = CoverageGateReport(
        json_path=str(coverage_json),
        report_only=report_only,
    )
    data = _load_coverage(coverage_json)
    if data is None:
        report.violations.append("coverage.json 缺失或非法，门禁无法判定（fail-closed）")
        return report

    totals = data["totals"]
    report.total_statements = int(totals.get("num_statements", 0))
    report.total_covered = int(totals.get("covered_lines", 0))
    report.total_percent = float(totals.get("percent_covered", 0.0))

    files: dict[str, Any] = data.get("files", {})

    # ── V-9 口径自洽：分层清单 vs 全量清单 diff 为空 ──
    manifest = build_tier_manifest(root)
    unmapped = sorted(rel for rel, tier in manifest.items() if tier == "UNMAPPED")
    report.unmapped_files = unmapped
    if unmapped:
        report.violations.append(
            f"V-9 分层口径不自洽：{len(unmapped)} 个 myquant 文件未归层"
            f"（{', '.join(unmapped[:8])}）"
        )

    # ── 分层聚合（T0/T1/T2 用行覆盖；T3 仅报告）──
    # 判层用纯函数 tier_of（对任意输入一致）；manifest 只服务 V-9 口径自洽。
    tier_stat: dict[str, TierStat] = {}
    file_stats: dict[str, tuple[int, int, float]] = {}
    for rel, raw in files.items():
        key = _normalize_file_key(rel)
        summary = raw.get("summary", {})
        stmts = int(summary.get("num_statements", 0))
        covered = int(summary.get("covered_lines", 0))
        percent = float(summary.get("percent_covered", 0.0))
        file_stats[key] = (stmts, covered, percent)
        tier = tier_of(key)
        if tier in ("T0", "T1", "T2"):
            ts = tier_stat.setdefault(tier, TierStat(tier=tier))
            ts.statements += stmts
            ts.covered += covered
            ts.file_count += 1

    for tier in ("T0", "T1", "T2"):
        ts = tier_stat.get(tier)
        if ts is None:
            continue
        if ts.statements > 0:
            ts.percent = ts.covered * 100.0 / ts.statements
        report.tier_stats.append(ts)

    # ── C-1a 总量棘轮·绝对行（防删测试）──
    if report.total_covered < COVERED_LINES_BASELINE:
        report.violations.append(
            f"C-1a 已覆盖行 {report.total_covered} < 基线 {COVERED_LINES_BASELINE}"
            f"（疑似删测试/删已覆盖代码，绝对行只增不减）"
        )
    # ── C-1b 总量棘轮·百分比（防堆未测代码）──
    if report.total_percent < COVERAGE_BASELINE:
        report.violations.append(
            f"C-1b 总覆盖率 {report.total_percent:.1f}% < 基线 {COVERAGE_BASELINE}%"
            f"（疑似堆未测代码）"
        )
    # ── C-2 分层下限（防外围刷量、红线烂内核）──
    for ts in report.tier_stats:
        floor = TIER_FLOOR.get(ts.tier, 0.0)
        if ts.percent < floor:
            report.violations.append(
                f"C-2 {ts.tier} 覆盖率 {ts.percent:.1f}% < 下限 {floor:.1f}%"
            )
    # ── C-3 关键文件覆盖率下限（只增不减）──
    for rel, floor in FILE_FLOOR.items():
        stmts, covered, percent = file_stats.get(rel, (0, 0, 0.0))
        if stmts > 0 and percent < floor:
            report.violations.append(
                f"C-3 {rel} 覆盖率 {percent:.1f}% < floor {floor:.1f}%"
            )
    # ── C-4 新增文件门槛（不在存量注册表 → 新增；T0/T1 ≥60%、T2 ≥30%）──
    for rel, (stmts, covered, percent) in file_stats.items():
        tier = tier_of(rel)
        if tier not in ("T0", "T1", "T2") or stmts == 0:
            continue
        if tier in ("T0", "T1") and rel in FILE_STMT_CEILING:
            continue  # 存量
        if tier == "T2" and rel in T2_FILES:
            continue  # 存量
        floor = NEW_FILE_FLOOR.get(tier, 0.0)
        if percent < floor:
            report.violations.append(
                f"C-4 新增文件 {rel}（{tier}）覆盖率 {percent:.1f}% < 门槛 {floor:.1f}%"
            )
    # ── C-5 存量 T0/T1 文件语句数棘轮（封堵塞旧文件绕开 C-4）──
    for rel, ceiling in FILE_STMT_CEILING.items():
        stmts, _covered, _percent = file_stats.get(rel, (0, 0, 0.0))
        if stmts > ceiling:
            report.violations.append(
                f"C-5 {rel} 语句数 {stmts} > 冻结值 {ceiling}（存量语句只增不减）"
            )
    # ── C-6 仅源码断言守护的模块数（P2-4 精化口径后启用阻断；首版仅报告）──
    # v1.2 §6.3 P2-4：C-6 冻结为 SOURCE_ASSERT_ONLY_MAX 且只减不增；P1 首版先报告
    # 实际计数（避免未精化口径导致首发即红），P2-4 给缺口模块补行为级测试后转阻断。
    assert_only = _find_source_assert_only_modules(root)
    report.assert_only_count = len(assert_only)
    # ── C-7 运行时跳过预算（只减不增）──
    report.skipped = _parse_skipped(junit_xml)
    if report.skipped > SKIP_BUDGET:
        report.violations.append(
            f"C-7 运行时跳过 {report.skipped} 个 > 预算 {SKIP_BUDGET}（fail-open 回潮）"
        )

    return report
