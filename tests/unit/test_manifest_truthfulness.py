# 文件名：tests/unit/test_manifest_truthfulness.py
"""
tests/unit/test_manifest_truthfulness.py - manifest 真实性反红测试（P0-08 子项③，用例⑪）
=====================================================================

覆盖（方案 §5.4.1 验收⑪ / S-11）：
  - create_engine 的 manifest 必须真正构建成功（修复此前恒软接线得 None 的洞：
    scheduler 无 self.data/self.config 属性 + engine.context 在 run 前未创建）
  - 7 组硬编码乐观值改真实实例读取：
    * t1_position_freeze.enabled ← config.enable_t1（真实配置，默认 True）
    * lot_size.enforced/value ← True/config.lot_size（#348 撮合层整手执行接线完成；
      #337 接线前为 False/None——撮合层无整手执行，读不到写 None 而非 True）
    * liquidity.participation_cap/declared_unlimited ← config.participation_rate（1.0 显式声明）
    * pool.exclusion_basis ← "today"（P0-06 真实口径，非 as_of 谎报）
    * survivorship.* 保留 None/"unknown"（已正确，登记待接线）
    * invariants.* 保留 {}（判定时机归子项⑥）
    * slippage.* ← config.slippage（SlippageModel 真实实例，替换被忽略的 YAML）
"""

import pytest

from myquant.backtest.engine import BacktestScheduler
from myquant.models.backtest import BacktestConfig, SlippageModel


def _build_manifest(**config_overrides):
    """走真实 create_engine（mode=event、无策略），返回构建成功的 manifest。"""
    cfg = BacktestConfig(
        initial_capital=1_000_000.0,
        symbols=["000001.SZ"],
        start_date="2024-01-01",
        end_date="2024-06-01",
        frequency="1d",
        **config_overrides,
    )
    scheduler = BacktestScheduler(mode="event")
    scheduler.create_engine(config=cfg, strategy=None, data=None)
    assert scheduler._realism_manifest is not None, (
        "manifest 构建失败（软接线掩盖），子项③修复前置未达成"
    )
    return scheduler._realism_manifest


class TestManifestBuilt:
    """修复前置：create_engine 的 manifest 必须真正构建成功（不再恒软接线 None）。"""

    def test_manifest_now_built_successfully(self):
        m = _build_manifest()
        assert m.get_field("run_id") is not None
        assert m.get_field("engine_mode_resolved") == "event"


class TestT1PositionFreeze:
    """t1_position_freeze.enabled ← config.enable_t1（真实配置）。"""

    def test_enable_t1_false_reports_false(self):
        m = _build_manifest(enable_t1=False)
        assert m.get_field("t1_position_freeze.enabled") is False

    def test_enable_t1_default_reports_true(self):
        m = _build_manifest()
        assert m.get_field("t1_position_freeze.enabled") is True


class TestLotSize:
    """撮合层整手执行（#348 接线完成）→ enforced=True、value=config.lot_size。"""

    def test_enforced_true_after_wiring(self):
        m = _build_manifest()
        assert m.get_field("lot_size.enforced") is True
        assert m.get_field("lot_size.value") == 100


class TestLiquidity:
    """liquidity.* ← config.participation_rate（1.0 显式声明为不限制）。"""

    def test_participation_rate_point_one(self):
        m = _build_manifest(participation_rate=0.1)
        assert m.get_field("liquidity.participation_cap") == 0.1
        assert m.get_field("liquidity.declared_unlimited") is False

    def test_participation_rate_default_full(self):
        m = _build_manifest()
        assert m.get_field("liquidity.participation_cap") == 1.0
        assert m.get_field("liquidity.declared_unlimited") is True


class TestPoolExclusionBasis:
    """pool.exclusion_basis ← "today"（P0-06 真实口径，as_of 为乐观谎报）。"""

    def test_reports_today_not_as_of(self):
        m = _build_manifest()
        assert m.get_field("pool.exclusion_basis") == "today"


class TestSlippage:
    """slippage.* ← config.slippage（SlippageModel 真实实例，替换被忽略的 YAML）。"""

    def test_none_type_reports_zero_cost(self):
        m = _build_manifest(slippage=SlippageModel(type="none"))
        assert m.get_field("slippage.type") == "none"
        assert m.get_field("slippage.value") == 0.0
        assert m.get_field("slippage.is_zero_cost") is True

    def test_fixed_bps_five_reports_value(self):
        m = _build_manifest(slippage=SlippageModel(fixed_bps=5.0))
        assert m.get_field("slippage.type") == "fixed_bps"
        assert m.get_field("slippage.value") == 5.0
        assert m.get_field("slippage.is_zero_cost") is False

    def test_default_model_reports_two_bps(self):
        m = _build_manifest()
        assert m.get_field("slippage.type") == "fixed_bps"
        assert m.get_field("slippage.value") == 2.0
        assert m.get_field("slippage.is_zero_cost") is False


class TestSurvivorshipInvariants:
    """survivorship.* 保留（已正确）；invariants.* 保留 {}（判定时机归子项⑥）。"""

    def test_survivorship_kept_unknown(self):
        m = _build_manifest()
        assert m.get_field("survivorship.discount") is None
        assert m.get_field("survivorship.status") == "unknown"

    def test_invariants_empty_until_subitem6(self):
        m = _build_manifest()
        assert m.get_field("invariants.price_bounds") == {}
        assert m.get_field("invariants.t1_consistency") == {}
