# 文件名：tests/unit/test_realism_gate_e2e.py
"""
tests/unit/test_realism_gate_e2e.py - P0-01 端到端验收（#345）
================================================================

验收判据（P0-01 卡 L61，施工方案 v2.2 §4.3.1/§4.7）：
  1. **阻断路径**：enabled=True 且回测真实性违规 → run 收尾阻断，独立产出
     ``outputs/realism_gate_blocked_<ts>_<strategy_id>.json``（§4.3.1：被门禁
     拦下时无 result 对象，独立落盘；含完整 manifest + 违规明细 + blocked=True）。
  2. **正常路径**：合规回测 run 完成后 results.json 含完整清单
     （``realism_manifest`` 键 = CRITICAL_FIELDS 全量快照）。
  3. **审计档**：enabled=False（分期启用期）时违规不阻断，清单仍挂载落盘。

注入方式（真实回测路径，非单元级 mock）：违规策略在 on_bar 篡改
``context._slicing_enabled=False`` 并 ``get_data`` 读全量（含未来行）→
访问器层记账 ``accessed_future=True``（P1-07② 判据，子项⑥）+ 收尾
``context_sliced=false`` 且未声明全量（判据①）→ run 收尾
``_refresh_causality_manifest``（assessed_at_run_end=True）→ checks/causality
判 critical → 阻断落盘 + RealismGateError（判据正式生效点 #344 的端到端闭环）。

事实来源：本次落地；被测：myquant/realism_gate/report.py（write_blocked_artifact /
write_manifest_to_results 接线）、三引擎 _refresh catch（#345 阻断落盘）、
BacktestResult.realism_manifest（#345 挂载）、BacktestConfig.realism_gate（#345 透传）。
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

import pandas as pd
import pytest

from myquant.backtest.engine import BacktestScheduler
from myquant.models.backtest import BacktestConfig
from myquant.models.market_data import Bar
from myquant.realism_gate.manifest import CRITICAL_FIELDS, RealismGateError
from myquant.realism_gate.report import write_manifest_to_results
from myquant.strategies.base import StrategyBase, StrategyContext

SYMBOL: str = "000001.SZ"


@pytest.fixture(autouse=True)
def _causality_only_checks() -> object:
    """隔离：仅注册 causality 检查器（本卡聚焦 causality 判据端到端阻断）。

    其余 4 检查器（market_rules/cost_model/data_pit/sample_pool）当前会把入口
    诚实保守值（lot_size.enforced=False、survivorship.discount=None、
    pool.exclusion_basis="today" 等）判为 critical——属 P0-03/P0-05 真实生效值
    采集对齐范围（P0-01 卡 L63 后续卡），非本卡引入；本卡只验收 causality 判据。
    teardown 恢复全量注册，避免污染后续测试。
    """
    from myquant.realism_gate.checks.causality import check_causality
    from myquant.realism_gate.gate import (
        register_realism_check,
        reset_realism_checks,
    )

    reset_realism_checks()
    register_realism_check("causality", check_causality)
    yield
    # 恢复全量注册（4 检查器）
    reset_realism_checks()
    import myquant.realism_gate.checks.cost_model as _cm
    import myquant.realism_gate.checks.data_pit as _dp
    import myquant.realism_gate.checks.market_rules as _mr
    import myquant.realism_gate.checks.sample_pool as _sp

    from myquant.realism_gate.gate import register_realism_check as _reg

    _reg("market_rules", _mr.check_market_rules)
    _reg("cost_model", _cm.check_cost_model)
    _reg("data_pit", _dp.check_data_pit)
    _reg("sample_pool", _sp.check_sample_pool)


def _make_data() -> Dict[str, pd.DataFrame]:
    """构造 6 个交易日的合成行情（英文列名由引擎统一映射为中文）。"""
    idx = pd.date_range("2024-01-01", periods=6, freq="B")
    close = [10.0 + 0.1 * i for i in range(6)]
    return {
        SYMBOL: pd.DataFrame(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": close,
                "volume": 1_000_000.0,
            },
            index=idx,
        )
    }


class _LeakyStrategy(StrategyBase):
    """违规探针：on_bar 篡改切片状态并读全量（含未来行）→ accessed_future。"""

    name: str = "leaky_probe"

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[object]:
        # 篡改引擎逐 bar 置位：未封门路径 get_data 返回全量（含未来）→ 记账
        context._slicing_enabled = False
        context.get_data(bar.symbol)
        return []

    def generate_signal(self, bar: Bar, context: StrategyContext) -> object:
        return None


class _CompliantStrategy(StrategyBase):
    """合规探针：on_bar 不触数据访问（已封门路径无记账、无违规）。"""

    name: str = "compliant_probe"

    def on_bar(self, bar: Bar, context: StrategyContext) -> List[object]:
        return []

    def generate_signal(self, bar: Bar, context: StrategyContext) -> object:
        return None


def _run_backtest(
    strategy: StrategyBase, enabled: bool, tmp_path: object
) -> object:
    """走真实 create_engine 出口（入口断言 seal）+ run 的完整链路。

    Args:
        strategy: 探针策略（合规/违规）。
        enabled: 门禁 enabled 值（True=阻断档 / False=审计档）。
        tmp_path: pytest tmp_path（cwd 切换到隔离目录，blocked 产物落其 outputs/）。

    Returns:
        引擎实例（run 已执行；阻断时抛 RealismGateError 前先落 blocked 产物）。
    """
    cfg = BacktestConfig(
        strategy_name=strategy.name,
        symbols=[SYMBOL],
        start_date="2024-01-01",
        end_date="2024-01-20",
        frequency="1d",
        initial_capital=1_000_000.0,
        realism_gate={"enabled": enabled, "audit_violations": False},
    )
    scheduler = BacktestScheduler(mode="event")
    engine = scheduler.create_engine(
        config=cfg, strategy=strategy, data=_make_data()
    )
    # 两阶段握手⑤（#344）：create_engine 已 seal，run 入口放行
    assert engine._realism_manifest.is_sealed()
    engine.run()
    return engine


def _find_blocked_files(tmp_path: object, strategy_id: str) -> List[str]:
    """在隔离 outputs/ 下查找阻断产物。"""
    outputs = os.path.join(str(tmp_path), "outputs")
    if not os.path.isdir(outputs):
        return []
    return [
        os.path.join(outputs, f)
        for f in os.listdir(outputs)
        if f.startswith(f"realism_gate_blocked_") and strategy_id in f
    ]


def test_blocked_artifact_written_when_leak_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """判据 1（阻断路径）：enabled=True + 违规策略 → run 抛 RealismGateError，
    且 blocked 产物落盘（blocked=True、manifest 完整、violations 含 causality）。"""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RealismGateError) as exc_info:
        _run_backtest(_LeakyStrategy(), enabled=True, tmp_path=tmp_path)

    # 违规明细含 causality 判据（accessed_future 或 context_sliced 至少一条）
    field_paths = [v.field_path for v in exc_info.value.violations]
    assert any(p.startswith("causality.") for p in field_paths), field_paths

    blocked = _find_blocked_files(tmp_path, "leaky_probe")
    assert len(blocked) == 1, f"阻断产物未落盘: {blocked}"
    with open(blocked[0], "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["blocked"] is True
    assert payload["strategy_id"] == "leaky_probe"
    # 完整 manifest：causality 组 6 字段 + 运行期标记已生效（#344）
    causality = payload["manifest"]["fields"]["causality"]
    assert causality["assessed_at_run_end"] is True
    for key in (
        "context_sliced",
        "current_bar_ts_enforced",
        "full_sample_declared",
        "guard_injected",
        "context_data_scoped",
    ):
        assert key in causality
    # 违规明细落盘（causality 判据反红）
    v_paths = [v["field_path"] for v in payload["violations"]]
    assert any(p.startswith("causality.") for p in v_paths), v_paths


def test_results_manifest_attached_when_compliant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """判据 2（正常路径）：enabled=True + 合规策略 → run 成功，
    result.realism_manifest 完整（CRITICAL_FIELDS 全覆盖），
    write_manifest_to_results 后 results.json 含 realism_manifest 键。"""
    monkeypatch.chdir(tmp_path)
    engine = _run_backtest(_CompliantStrategy(), enabled=True, tmp_path=tmp_path)

    assert engine.result.is_success
    assert engine.result.realism_manifest is not None
    fields = engine.result.realism_manifest["fields"]
    # 完整清单：CRITICAL_FIELDS 点号路径全部可命中（扁平 + 分组嵌套）
    for path in CRITICAL_FIELDS:
        group, _, leaf = path.partition(".")
        if leaf:
            assert group in fields and leaf in fields[group], f"缺 {path}"
        else:
            assert path in fields, f"缺 {path}"

    # results.json 落盘（P0-01 卡判据：真实回测 results.json 含完整清单）
    results_path = os.path.join(str(tmp_path), "results.json")
    write_manifest_to_results(results_path, engine._realism_manifest)
    with open(results_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert "realism_manifest" in saved
    assert saved["realism_manifest"]["sealed"] is True
    assert saved["realism_manifest"]["fields"]["causality"]["context_sliced"] is True


def test_manifest_attached_when_audit_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """判据 3（审计档）：enabled=False + 违规策略 → run 不抛（软接线审计），
    清单仍挂载落盘（enabled:false 时清单仍落盘——验收判据原文）。"""
    monkeypatch.chdir(tmp_path)
    engine = _run_backtest(_LeakyStrategy(), enabled=False, tmp_path=tmp_path)

    # 审计档不阻断：run 正常返回，但清单完整挂载（真实采集仍在）
    assert engine.result is not None
    assert engine.result.realism_manifest is not None
    fields = engine.result.realism_manifest["fields"]
    assert fields["causality"]["assessed_at_run_end"] is True
    # 审计档下阻断产物不落盘（无违规阻断 → 无 blocked 文件）
    assert _find_blocked_files(tmp_path, "leaky_probe") == []
