# 文件名：tests/unit/test_c_acceptance.py
"""
test_c_acceptance.py - CAcceptanceRunner 单元测试（C-12）
==========================================================================
覆盖：
  1. run() 返回结构完整（items/false_positive/totals/overall_pass/generated_at）
  2. 逐项 item 结构（item/name/verdict/summary/measured/error 六键齐全）
  3. 隔离环境（固定时钟、不触 quant.db、FakeAlert 注入）
  4. P0-8 误报统计字段齐全（healthy_runs/false_positive_alerts/tp/fn）
  5. C-11 数据血缘决策项（磁盘不存在 data_lineage.py）
  6. 报告 JSON 可序列化
  7. write_report 落盘且可被 json.load 解析
  8. __main__ 自测返回 0 失败
"""

from __future__ import annotations

import json
from pathlib import Path

from myquant.monitoring.c_acceptance import (
    CLOCK_NOW,
    REPORT_DATE,
    CAcceptanceRunner,
    _FakeAlert,
)


# ═══════════════════════════════════════════════
#  1. run() 返回结构完整
# ═══════════════════════════════════════════════
class TestRunStructure:
    """run() 返回报告结构完整性。"""

    def setup_method(self) -> None:
        """每个用例新建 Runner 并跑一次。"""
        self.runner = CAcceptanceRunner()
        self.report = self.runner.run()

    def test_report_is_dict(self) -> None:
        """报告为 dict。"""
        assert isinstance(self.report, dict)

    def test_required_keys_present(self) -> None:
        """必需键齐全。"""
        for key in ("items", "false_positive", "totals", "overall_pass",
                    "generated_at", "report_date", "environment"):
            assert key in self.report, f"缺少键: {key}"

    def test_items_count_is_ten(self) -> None:
        """items 长度 = 10（C-1/C-2/C-4~C-11）。"""
        assert len(self.report["items"]) == 10

    def test_report_date(self) -> None:
        """报告日期为验收日。"""
        assert self.report["report_date"] == REPORT_DATE == "2026-10-06"

    def test_overall_pass_is_bool(self) -> None:
        """overall_pass 为 bool。"""
        assert isinstance(self.report["overall_pass"], bool)

    def test_totals_balanced(self) -> None:
        """totals 的 pass+fail == total。"""
        t = self.report["totals"]
        assert t["pass"] + t["fail"] == t["total"]
        assert t["total"] == 10


# ═══════════════════════════════════════════════
#  2. 逐项 item 结构
# ═══════════════════════════════════════════════
class TestItemStructure:
    """每项检查结果六键齐全。"""

    def setup_method(self) -> None:
        self.runner = CAcceptanceRunner()
        self.report = self.runner.run()

    def test_each_item_has_required_keys(self) -> None:
        for it in self.report["items"]:
            for key in ("item", "name", "verdict", "summary", "measured", "error"):
                assert key in it, f"缺键 {key}: {it}"
            assert it["verdict"] in ("pass", "fail")

    def test_item_ids_in_order(self) -> None:
        """item ID 顺序为 C-1,C-2,C-4,C-5,C-6,C-7,C-8,C-9,C-10,C-11。"""
        expected = ["C-1", "C-2", "C-4", "C-5", "C-6",
                    "C-7", "C-8", "C-9", "C-10", "C-11"]
        actual = [it["item"] for it in self.report["items"]]
        assert actual == expected


# ═══════════════════════════════════════════════
#  3. 隔离环境
# ═══════════════════════════════════════════════
class TestIsolation:
    """隔离环境属性。"""

    def test_environment_flags(self) -> None:
        report = CAcceptanceRunner().run()
        env = report["environment"]
        assert env["isolated"] is True
        assert env["fixed_clock_used"] is True
        assert env["quant_db_touched"] is False

    def test_fake_alert_capture(self) -> None:
        """_FakeAlert 正确捕获 alert() 调用。"""
        fa = _FakeAlert()
        assert fa.count == 0
        fa.alert(level="WARNING", message="x")
        fa.alert(level="ERROR", message="y")
        assert fa.count == 2

    def test_clock_is_fixed_past(self) -> None:
        """固定时钟远早于当前真实时间（防未来注入有效）。"""
        import time
        assert CLOCK_NOW < time.time()


# ═══════════════════════════════════════════════
#  4. P0-8 误报统计
# ═══════════════════════════════════════════════
class TestFalsePositiveStats:
    """P0-8 误报率口径字段。"""

    def setup_method(self) -> None:
        self.report = CAcceptanceRunner().run()

    def test_fields_present(self) -> None:
        fp = self.report["false_positive"]
        for key in ("healthy_runs", "false_positive_alerts",
                    "false_positive_rate", "tp", "fn", "positive_cases"):
            assert key in fp, f"缺键: {key}"

    def test_runs_positive(self) -> None:
        """健康输入电池至少跑了若干轮。"""
        assert self.report["false_positive"]["healthy_runs"] >= 1

    def test_tp_fn_consistent(self) -> None:
        """tp + fn == positive_cases。"""
        fp = self.report["false_positive"]
        assert fp["tp"] + fp["fn"] == fp["positive_cases"]


# ═══════════════════════════════════════════════
#  5. C-11 数据血缘决策项
# ═══════════════════════════════════════════════
class TestC11Lineage:
    """C-11：磁盘不存在 data_lineage.py。"""

    def test_lineage_file_absent(self) -> None:
        report = CAcceptanceRunner().run()
        c11 = next(it for it in report["items"] if it["item"] == "C-11")
        assert c11["measured"]["exists"] is False
        assert c11["verdict"] == "pass"


# ═══════════════════════════════════════════════
#  6. JSON 可序列化
# ═══════════════════════════════════════════════
class TestJsonSerializable:
    """报告可 json 序列化/反序列化。"""

    def test_report_roundtrip(self) -> None:
        report = CAcceptanceRunner().run()
        text = json.dumps(report, ensure_ascii=False, indent=2)
        assert isinstance(text, str)
        parsed = json.loads(text)
        assert parsed["totals"]["total"] == 10


# ═══════════════════════════════════════════════
#  7. write_report 落盘
# ═══════════════════════════════════════════════
class TestWriteReport:
    """write_report 写文件并可被 json.load 解析。"""

    def test_write_report(self, tmp_path: Path) -> None:
        out = tmp_path / "c_report.json"
        report = CAcceptanceRunner().write_report(out)
        assert out.exists()
        with open(out, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert loaded["report_date"] == "2026-10-06"
        assert loaded["totals"]["total"] == 10
        assert "overall_pass" in loaded
        assert report["totals"]["total"] == loaded["totals"]["total"]


# ═══════════════════════════════════════════════
#  8. __main__ 自测
# ═══════════════════════════════════════════════
class TestSelfTest:
    """模块 __main__ 自测返回 0 失败。"""

    def test_self_test_zero_failures(self) -> None:
        from myquant.monitoring.c_acceptance import _run_self_test
        assert _run_self_test() == 0
