# 文件名：tests/unit/test_config_live_check.py
"""
test_config_live_check.py - 配置模板完整性 + 第4批配置死项修复验证
================================================================================
覆盖范围:
  [配置模板完整性]
  - 4 个 example 配置存在且 YAML 可解析（backtest_config/live_config/strategy_params/data_clean）
  - risk_params.example.yaml 已删除（第4批BUG-09：整文件无消费，防死配置回归）
  - live_config / strategy_params 关键段结构完整性

  [第4批修复验证]
  - BUG-07: pipeline 消费 log_bad_records/log_quality_report 开关（False 时不写盘/不生成报告）
  - BUG-08: live_config.example.yaml signal_output/intraday_monitor/notification 标「预留·未消费」
  - BUG-10: strategy_params.example.yaml active_strategy/common/three_factor_pool 标「预留·未消费」

运行方式:
  pytest tests/unit/test_config_live_check.py -v

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（配置模板可解析即实盘/回测可消费）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 测试数据使用历史日期(2024-01)，无未来日期
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from myquant.data_access.cleaner.pipeline import run_clean_pipeline

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


def _load_yaml(name: str) -> Dict[str, Any]:
    """加载 config/ 下 YAML 模板"""
    path = CONFIG_DIR / name
    assert path.exists(), f"{name} 不存在"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert data is not None, f"{name} 内容为空"
    return data


# ═══════════════════════════════════════════════
#  配置模板完整性
# ═══════════════════════════════════════════════

class TestConfigTemplateIntegrity:
    """配置模板存在性 + 可解析性"""

    @pytest.mark.parametrize(
        "name",
        [
            "backtest_config.example.yaml",
            "live_config.example.yaml",
            "strategy_params.example.yaml",
            "data_clean.example.yaml",
        ],
    )
    def test_example_config_parsable(self, name: str) -> None:
        """example 配置存在且 YAML 可解析"""
        data = _load_yaml(name)
        assert isinstance(data, dict)

    def test_risk_params_deleted(self) -> None:
        """第4批BUG-09：risk_params.example.yaml 已删除（整文件无消费，风控实际读 live_config.risk_control）"""
        assert not (CONFIG_DIR / "risk_params.example.yaml").exists(), (
            "risk_params.example.yaml 不应存在（BUG-09 已删除），若重新引入需恢复消费"
        )

    @pytest.mark.skipif(
        not (CONFIG_DIR / "live_config.yaml").exists(),
        reason="生产真实配置 live_config.yaml 未入库（.gitignore 忽略），CI 环境跳过；本地存在时即红",
    )
    def test_live_config_structure(self) -> None:
        """live_config 真实配置关键段完整（M0-17：guard 断言从 .example 改为真实配置，改真实配置即红）"""
        live = _load_yaml("live_config.yaml")
        for section in [
            "live", "broker", "execution", "market_data", "stock_pool",
            "strategy", "position", "risk_control", "lookahead_guard",
            "state_store", "scheduler", "leakage_gate",
        ]:
            assert section in live, f"live_config 缺少 {section} 段"
        assert live["lookahead_guard"]["enabled"] is True, "防未来函数必须开启"
        assert live["lookahead_guard"]["strict_mode"] is True, "防未来严格模式必须开启"
        assert live["leakage_gate"]["enabled"] is True, "防泄漏门禁必须开启（M0-16 整合）"
        assert live["leakage_gate"]["audit_violations"] is True, "防泄漏门禁违规必须落审计"
        assert "name" in live["strategy"]

    # ═══════════════════════════════════════════════
    #  M0-16 配置整合断言（leakage_gate 段 + adjust_strict）
    # ═══════════════════════════════════════════════

    @pytest.mark.skipif(
        not (CONFIG_DIR / "backtest_config.yaml").exists() or not (CONFIG_DIR / "backtest_kunpeng.yaml").exists(),
        reason="生产真实回测配置未入库（.gitignore 忽略），CI 环境跳过；本地存在时即红",
    )
    def test_backtest_real_config_adjust_strict(self) -> None:
        """M0-16：真实回测配置 adjust=hfq 必须显式 adjust_strict: true（消除静默降级 raw 口径不一致）"""
        for name in ["backtest_config.yaml", "backtest_kunpeng.yaml"]:
            cfg = _load_yaml(name)
            assert cfg["data"]["adjust"] == "hfq", f"{name} adjust 应为 hfq"
            assert cfg["data"]["adjust_strict"] is True, f"{name} 缺 adjust_strict: true（M0-16）"
            assert cfg["leakage_gate"]["enabled"] is True, f"{name} 缺 leakage_gate 段（M0-16）"
            assert cfg["leakage_gate"]["audit_violations"] is True, f"{name} leakage_gate.audit_violations 应为 true"

    def test_backtest_example_leakage_gate(self) -> None:
        """M0-16：backtest_config.example.yaml 含 leakage_gate 段（入库模板与真实配置一致）"""
        cfg = _load_yaml("backtest_config.example.yaml")
        assert cfg["leakage_gate"]["enabled"] is True
        assert cfg["leakage_gate"]["audit_violations"] is True

    def test_live_example_leakage_gate(self) -> None:
        """M0-16：live_config.example.yaml 含 leakage_gate 段（入库模板与真实配置一致）"""
        cfg = _load_yaml("live_config.example.yaml")
        assert cfg["leakage_gate"]["enabled"] is True
        assert cfg["leakage_gate"]["audit_violations"] is True

    def test_strategy_params_structure(self) -> None:
        """strategy_params 全部策略块 + 顶层键存在"""
        sp = _load_yaml("strategy_params.example.yaml")
        for block in [
            "rsrs", "grid", "momentum", "mean_reversion", "multi_factor",
            "kunpeng", "three_factor_pool", "ma_cross", "rsi", "macd",
            "bollinger", "common",
        ]:
            assert block in sp, f"strategy_params 缺少 {block} 块"
        assert "active_strategy" in sp

    def test_data_clean_structure(self) -> None:
        """data_clean 关键键完整（含 BUG-07 开关）"""
        dc = _load_yaml("data_clean.example.yaml")
        for key in ["enabled", "log_bad_records", "log_quality_report", "bar", "tick", "bad_data", "quality_report"]:
            assert key in dc, f"data_clean 缺少 {key} 键"


# ═══════════════════════════════════════════════
#  第4批 BUG-07：pipeline 消费 log_bad_records/log_quality_report
# ═══════════════════════════════════════════════

class TestBug07SwitchConsumed:
    """清洗开关 log_bad_records/log_quality_report 应被 pipeline 消费"""

    @pytest.fixture()
    def bad_records(self) -> list:
        """构造含负价格（必进 bad）的脏 bar 记录"""
        return [
            {"symbol": "000001.SZ", "date": "2024-01-02", "open": 10.0, "high": 10.5,
             "low": 9.8, "close": 10.0, "volume": 100000},
            {"symbol": "000001.SZ", "date": "2024-01-03", "open": -1.0, "high": 10.8,
             "low": 10.1, "close": 10.6, "volume": 120000},  # 负开盘价 → bad
        ]

    def _run(self, tmp_path, bad_records, log_bad, log_report) -> Dict[str, Any]:
        """带开关配置跑 pipeline，返回结果 dict"""
        config = {
            "log_bad_records": log_bad,
            "log_quality_report": log_report,
            "bad_data": {"output_dir": str(tmp_path)},
            "quality_report": {"output_dir": str(tmp_path)},
            "bar": {"max_price_jump_pct": 0.22},
        }
        return run_clean_pipeline(
            records=bad_records,
            data_type="bar",
            symbol="B7.SZ",
            config=config,
            save_bad=True,
            save_report=True,
            publish_events=False,
        )

    def test_log_bad_records_false_no_bad_file(self, tmp_path, bad_records) -> None:
        """log_bad_records=False 时脏数据不写盘（bad_file=None）"""
        result = self._run(tmp_path, bad_records, log_bad=False, log_report=True)
        assert len(result["bad"]) >= 1, "应检测到脏数据"
        assert result["bad_file"] is None, "log_bad_records=False 不应写脏数据文件"

    def test_log_bad_records_true_writes_bad_file(self, tmp_path, bad_records) -> None:
        """log_bad_records=True（默认）时脏数据写盘"""
        result = self._run(tmp_path, bad_records, log_bad=True, log_report=False)
        assert len(result["bad"]) >= 1
        assert result["bad_file"] is not None
        assert Path(result["bad_file"]).exists()

    def test_log_quality_report_false_no_report(self, tmp_path, bad_records) -> None:
        """log_quality_report=False 时不生成质量报告（report_file=None）"""
        result = self._run(tmp_path, bad_records, log_bad=True, log_report=False)
        assert result["report_file"] is None, "log_quality_report=False 不应生成质量报告"

    def test_log_quality_report_true_generates_report(self, tmp_path, bad_records) -> None:
        """log_quality_report=True（默认）时生成质量报告"""
        result = self._run(tmp_path, bad_records, log_bad=False, log_report=True)
        assert result["report_file"] is not None
        assert Path(result["report_file"]).exists()

    def test_no_config_defaults_on(self, tmp_path, bad_records) -> None:
        """无 config 传入（旧调用方）时行为不变：默认写盘+生成报告"""
        result = run_clean_pipeline(
            records=bad_records,
            data_type="bar",
            symbol="B7NO.SZ",
            save_bad=True,
            save_report=True,
            bad_output_dir=str(tmp_path),
            report_output_dir=str(tmp_path),
            publish_events=False,
        )
        assert len(result["bad"]) >= 1
        assert result["bad_file"] is not None
        assert result["report_file"] is not None


# ═══════════════════════════════════════════════
#  第4批 BUG-08 / BUG-10：预留段标注存在
# ═══════════════════════════════════════════════

class TestReservedMarkers:
    """未消费配置段必须带「预留·未消费」标注（文本断言，注释不进 yaml 数据）"""

    def test_live_config_reserved_markers(self) -> None:
        """live_config 三段（signal_output/intraday_monitor/notification）标注存在"""
        text = (CONFIG_DIR / "live_config.example.yaml").read_text(encoding="utf-8")
        for section in ["signal_output:", "intraday_monitor:", "notification:"]:
            assert section in text, f"live_config 缺少 {section} 段"
        assert text.count("【预留·未消费】") >= 3, "live_config 三段均需「预留·未消费」标注"

    def test_strategy_params_reserved_markers(self) -> None:
        """strategy_params 三键（active_strategy/common/three_factor_pool）标注存在"""
        text = (CONFIG_DIR / "strategy_params.example.yaml").read_text(encoding="utf-8")
        for key in ["active_strategy:", "common:", "three_factor_pool:"]:
            assert key in text, f"strategy_params 缺少 {key} 键"
        assert text.count("【预留·未消费】") >= 3, "strategy_params 三键均需「预留·未消费」标注"



# ═══════════════════════════════════════════════
#  T-45 KP-P1-08：文档声明与代码实现一致性（对拍）
# ═══════════════════════════════════════════════

class TestKP1_08DocImplConsistency:
    """KP-P1-08：文档声明与代码实现一致性（three_factor / run_mode / P2 Gate 状态）"""

    def test_backtest_example_type_matches_impl(self) -> None:
        """backtest_config 示例 type 用实际支持的 preset_filtered，three_factor 标预留未接线"""
        text = (CONFIG_DIR / "backtest_config.example.yaml").read_text(encoding="utf-8")
        assert 'type: "preset_filtered"' in text
        assert "预留·未接线" in text

    def test_config_builder_three_factor_raises(self) -> None:
        """config_builder 对 three_factor 显式 raise，不再静默回退 codes"""
        src = (PROJECT_ROOT / "myquant" / "backtest" / "config_builder.py").read_text(encoding="utf-8")
        assert 'pool_type == "three_factor"' in src
        assert "raise ValueError" in src
        assert "尚未接线" in src
        assert "不再静默回退" in src

    def test_live_example_mode_consistent(self) -> None:
        """live_config broker.mode=SIM 与 trade_mode=paper 一致（避免 LIVE 降级矛盾）"""
        text = (CONFIG_DIR / "live_config.example.yaml").read_text(encoding="utf-8")
        assert 'mode: "SIM"' in text
        assert 'trade_mode: "paper"' in text

    def test_p2_gate_status_accurate(self) -> None:
        """P2整改任务清单 Gate 3 状态改准确：附-A 待闭环，观察期未进入"""
        p = PROJECT_ROOT / "docs" / "因子与机器学习档案" / "P2整改任务清单.md"
        text = p.read_text(encoding="utf-8")
        assert "Gate 3 状态" in text
        assert "尚未进入" in text
