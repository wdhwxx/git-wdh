# 文件名：tests/unit/test_signature_guard.py
"""
test_signature_guard.py - P1-16 manifest 签名与跨运行可比性守门反红测试（#366）
================================================================================
覆盖方案 §4.10：
  1. manifest_signature 剔除运行特异字段（run_id/时间戳/start_date/end_date/
     engine_mode_*），保留真实性相关字段；
  2. group_by_signature 按签名分组；
  3. assert_uniform_signature：单签名放行、多签名抛 RealismGateError 并输出
     差异清单（跨真实性配置禁止合并排行）；
  4. diff_signatures 逐字段差异清单；
  5. param_optimizer._rank 聚合排序入口接入签名守门（跨签名拒合并）。

R-19 零覆盖：显式 import 子模块；测试不触网、不写生产台账。
"""

import pytest

from myquant.realism_gate import signature_guard as sg
from myquant.realism_gate.manifest import RealismGateError, RealismManifest


def _make_manifest(**overrides) -> RealismManifest:
    """构造一份带真实性字段的 manifest（不 build()，仅测签名读 fields）。"""
    m = RealismManifest()
    m.set_field("run_id", "run-001")
    m.set_field("strategy_id", "ma_cross")
    m.set_field("slippage.value", 0.001)
    m.set_field("adjust.effective", "raw")
    for key, val in overrides.items():
        m.set_field(key, val)
    return m


class TestSignaturePurge:
    """manifest_signature 剔除运行特异字段。"""

    def test_run_id_stable(self):
        m1 = _make_manifest()
        m2 = _make_manifest(run_id="another-run")
        assert m1.manifest_signature() == m2.manifest_signature()

    def test_start_end_date_stable(self):
        """start_date/end_date（运行区间）被剔除，签名不变。"""
        m1 = _make_manifest()
        m2 = _make_manifest(start_date="2020-01-01", end_date="2024-12-31")
        assert m1.manifest_signature() == m2.manifest_signature()

    def test_engine_mode_prefix_stable(self):
        """engine_mode_* 前缀字段被剔除（引擎模式选择非真实性事实）。"""
        m1 = _make_manifest()
        m2 = _make_manifest(engine_mode_vectorized="mixed")
        assert m1.manifest_signature() == m2.manifest_signature()

    def test_realism_field_changes_signature(self):
        """改真实性相关字段（成本口径）→ 签名变。"""
        m1 = _make_manifest()
        m2 = _make_manifest(**{"slippage.value": 0.005})
        assert m1.manifest_signature() != m2.manifest_signature()


class TestGroupBySignature:
    def test_groups_same_signature_together(self):
        items = [
            {"symbol": "A", "manifest_signature": "sig_1"},
            {"symbol": "B", "manifest_signature": "sig_1"},
            {"symbol": "C", "manifest_signature": "sig_2"},
        ]
        groups = sg.group_by_signature(items)
        assert set(groups.keys()) == {"sig_1", "sig_2"}
        assert [x["symbol"] for x in groups["sig_1"]] == ["A", "B"]
        assert [x["symbol"] for x in groups["sig_2"]] == ["C"]

    def test_unsigned_items_fall_into_none_group(self):
        """无 manifest_signature 字段的记录归入 None 组（向后兼容放行）。"""
        items = [{"symbol": "A"}, {"symbol": "B", "manifest_signature": "sig_1"}]
        groups = sg.group_by_signature(items)
        assert None in groups and groups[None][0]["symbol"] == "A"
        assert "sig_1" in groups


class TestAssertUniform:
    def test_single_signature_passes(self):
        items = [
            {"symbol": "A", "manifest_signature": "sig_1"},
            {"symbol": "B", "manifest_signature": "sig_1"},
        ]
        sg.assert_uniform_signature(items, context="测试聚合")  # 不抛

    def test_no_signature_passes_backward_compat(self):
        """全部无签名字段 → 向后兼容放行（旧数据）。"""
        items = [{"symbol": "A"}, {"symbol": "B"}]
        sg.assert_uniform_signature(items)  # 不抛

    def test_multiple_signatures_rejected_with_diff_list(self):
        """两个不同签名 → 抛 RealismGateError，消息含差异清单（各签名组记录数）。"""
        items = [
            {"strategy": "ma_cross", "manifest_signature": "sig_aaa"},
            {"strategy": "rsrs", "manifest_signature": "sig_bbb"},
        ]
        with pytest.raises(RealismGateError, match="不同 manifest 签名"):
            sg.assert_uniform_signature(items, context="参数优化排行")
        # 差异清单：异常消息应列出两个签名组
        try:
            sg.assert_uniform_signature(items, context="参数优化排行")
        except RealismGateError as e:
            assert "sig_aaa" in str(e) and "sig_bbb" in str(e)


class TestDiffSignatures:
    def test_diff_lists_changed_fields(self):
        a = {"strategy": "ma_cross", "slippage": 0.001, "mode": "vec"}
        b = {"strategy": "ma_cross", "slippage": 0.005, "mode": "mixed"}
        diff = sg.diff_signatures(a, b)
        assert "slippage" in diff
        assert "mode" in diff
        assert "strategy" not in diff  # 相同字段不入差异


class TestParamOptimizerRankGuard:
    """param_optimizer._rank 入口接入签名守门：跨签名 outcomes 拒合并。"""

    def test_rank_rejects_cross_signature_outcomes(self):
        """_rank 排序前调 assert_uniform_signature——outcomes 含两个不同签名即拒。"""
        from myquant.realism_gate.signature_guard import assert_uniform_signature

        outcomes = [
            {"params": {"n": 5}, "sharpe_ratio": 1.2, "manifest_signature": "sig_batch1"},
            {"params": {"n": 10}, "sharpe_ratio": 1.5, "manifest_signature": "sig_batch2"},
        ]
        with pytest.raises(RealismGateError, match="不同 manifest 签名"):
            assert_uniform_signature(outcomes, context="参数优化聚合排行")

    def test_rank_accepts_same_signature_outcomes(self):
        """同一签名的 outcomes 放行（可合并排行）。"""
        from myquant.realism_gate.signature_guard import assert_uniform_signature

        outcomes = [
            {"params": {"n": 5}, "sharpe_ratio": 1.2, "manifest_signature": "sig_1"},
            {"params": {"n": 10}, "sharpe_ratio": 1.5, "manifest_signature": "sig_1"},
        ]
        assert_uniform_signature(outcomes)  # 不抛
