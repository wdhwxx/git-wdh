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
BacktestResult.realism_manifest（#345 挂载）、BacktestConfig.realism_gate（#345 透传）、
4 检查器判据真实采集对齐（#346：lot_size.enforced/survivorship·invariants·
fingerprint_hash 待接线豁免、exclusion_basis="today" P0-06 真实口径）——
对齐后本验收在**全量 5 检查器注册**下跑（enabled=true 入口不再误伤，端到端阻断闭环）。
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
def _all_checks_registered() -> object:
    """确保 5 检查器全量注册（隔离 test_realism_gate 的 reset 污染 pytest 顺序）。

    #346 真实采集对齐后：4 检查器判据接受入口诚实披露值（lot_size.enforced=False
    撮合层真实事实 / survivorship·invariants·fingerprint_hash 待接线豁免 /
    exclusion_basis="today" P0-06 真实口径）→ enabled=true 入口不再误伤，
    本验收在全量检查器注册下跑（可真实创建引擎 + 端到端阻断闭环）。
    """
    from myquant.realism_gate.checks.causality import check_causality
    from myquant.realism_gate.checks.cost_model import check_cost_model
    from myquant.realism_gate.checks.data_pit import check_data_pit
    from myquant.realism_gate.checks.market_rules import check_market_rules
    from myquant.realism_gate.checks.sample_pool import check_sample_pool
    from myquant.realism_gate.gate import (
        register_realism_check,
        reset_realism_checks,
    )

    reset_realism_checks()
    register_realism_check("causality", check_causality)
    register_realism_check("market_rules", check_market_rules)
    register_realism_check("cost_model", check_cost_model)
    register_realism_check("data_pit", check_data_pit)
    register_realism_check("sample_pool", check_sample_pool)
    yield
    # 恢复默认全量注册（5 检查器），避免污染后续测试
    reset_realism_checks()
    from myquant.realism_gate.gate import register_realism_check as _reg

    _reg("causality", check_causality)
    _reg("market_rules", check_market_rules)
    _reg("cost_model", check_cost_model)
    _reg("data_pit", check_data_pit)
    _reg("sample_pool", check_sample_pool)


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


# ═══════════════════════════════════════════════════════════════
#  #349：from_data_provider 直构路径门禁收敛（真实端到端验收暴露
#  ——生产入口 scripts/生成回测报告.py 走 from_data_provider，此前无
#  manifest → 握手⑤跳过、判据完全不生效；本轮收敛到 attach_realism_manifest）
# ═══════════════════════════════════════════════════════════════

class _FakeProvider:
    """鸭子类型 DataProvider：返回合成 6 交易日行情（无复权降级）。"""

    def load_multiple(
        self,
        symbols: List[str],
        start_date: object = None,
        end_date: object = None,
        period: str = "1d",
    ) -> Dict[str, pd.DataFrame]:
        idx = pd.date_range("2024-01-01", periods=6, freq="B")
        close = [10.0 + 0.1 * i for i in range(6)]
        df = pd.DataFrame(
            {
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": close,
                "volume": 1_000_000.0,
            },
            index=idx,
        )
        return {s: df for s in symbols}

    def get_bars(
        self, symbol: str, start_date: object = None, end_date: object = None
    ) -> pd.DataFrame:
        return self.load_multiple([symbol])[symbol]


def _run_backtest_from_provider(
    strategy: StrategyBase, enabled: bool
) -> object:
    """走生产入口 from_data_provider 直构 + run（#349 门禁收敛验证）。"""
    cfg = BacktestConfig(
        strategy_name=strategy.name,
        symbols=[SYMBOL],
        start_date="2024-01-01",
        end_date="2024-01-20",
        frequency="1d",
        initial_capital=1_000_000.0,
        realism_gate={"enabled": enabled, "audit_violations": False},
    )
    from myquant.backtest.engine import BacktestEngine

    engine = BacktestEngine.from_data_provider(
        data_provider=_FakeProvider(),
        config=cfg,
        strategy=strategy,
    )
    # 握手⑤（#344）：attach 已在直构路径构建 + seal（此前 _realism_manifest 缺失）
    assert engine._realism_manifest is not None
    assert engine._realism_manifest.is_sealed()
    engine.run()
    return engine


def test_from_data_provider_blocked_on_degraded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """判据 4（#349 阻断）：from_data_provider + enabled=True，provider 加载后
    真实复权降级（monkeypatch 降级集合——入口空、收尾非空）→ 收尾回写 →
    data_pit critical → RealismGateError + blocked 产物（此前直构路径判据完全
    不生效）。"""
    monkeypatch.chdir(tmp_path)
    # 入口 attach 读取一次（空，入口不误伤）；run 收尾 _refresh 再读（非空）→
    # 收尾回写真实降级集合 → data_pit 判据在收尾 assert 真实生效（判据4闭环）
    _calls: Dict[str, int] = {"n": 0}

    def _degraded_after_entry() -> List[str]:
        _calls["n"] += 1
        return [SYMBOL] if _calls["n"] > 1 else []

    monkeypatch.setattr(
        "myquant.data_access.provider.get_adjust_degraded_symbols",
        _degraded_after_entry,
    )
    with pytest.raises(RealismGateError) as exc_info:
        _run_backtest_from_provider(_CompliantStrategy(), enabled=True)

    field_paths = [v.field_path for v in exc_info.value.violations]
    assert "adjust_degraded_symbols" in field_paths, field_paths

    blocked = _find_blocked_files(tmp_path, "compliant_probe")
    assert len(blocked) == 1, f"阻断产物未落盘: {blocked}"
    with open(blocked[0], "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["blocked"] is True
    # 收尾真实采集：assessed_at_run_end=True（causality 判据正式生效点）
    assert payload["manifest"]["fields"]["causality"]["assessed_at_run_end"] is True
    # 真实降级集合已回写进 manifest（data_pit 判据依据）
    assert payload["manifest"]["fields"]["adjust_degraded_symbols"] == [SYMBOL]
    v_paths = [v["field_path"] for v in payload["violations"]]
    assert "adjust_degraded_symbols" in v_paths, v_paths


def test_from_data_provider_compliant_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """判据 5（#349 合规）：from_data_provider + enabled=True + 无降级（合成
    数据）→ run 成功 + realism_manifest 完整挂载（CRITICAL_FIELDS 全覆盖）。"""
    monkeypatch.chdir(tmp_path)
    engine = _run_backtest_from_provider(_CompliantStrategy(), enabled=True)

    assert engine.result.is_success
    assert engine.result.realism_manifest is not None
    fields = engine.result.realism_manifest["fields"]
    for path in CRITICAL_FIELDS:
        group, _, leaf = path.partition(".")
        if leaf:
            assert group in fields and leaf in fields[group], f"缺 {path}"
        else:
            assert path in fields, f"缺 {path}"
    # 无降级 → data_pit 不判 critical → 阻断产物不落盘
    assert _find_blocked_files(tmp_path, "compliant_probe") == []
