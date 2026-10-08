# 文件名：tests/unit/test_d_acceptance.py
"""
D-9 D 包验收 单测
=================
覆盖 DAcceptanceRunner 的 D-6（混沌演练）、D-7（配置版本化）、D-8（灾备演练）
集成验收项 + 验收项结果 JSON 可序列化。D-1~D-5 的专项单测套件由 D-9 run()
子进程 pytest 作为验收证据（此处不嵌套调用，避免递归跑全套件）。

隔离：全部在临时目录；不触碰工作区真实数据。防未来：只消费已发生事实。
"""
import json

from myquant.acceptance.d_acceptance import DAcceptanceRunner


class TestIntegrationItems:
    def test_check_d6_all_captured(self):
        """D-6 验收：P0-8 FI-01~07 演练全部被捕获（TP），通过。"""
        runner = DAcceptanceRunner()
        item = runner._check_d6()
        assert item.passed is True
        assert item.evidence["tp"] == 7
        assert item.evidence["fn"] == 0

    def test_check_d7_apply_rollback(self):
        """D-7 验收：配置 apply→回滚恢复上一版本内容，通过。"""
        runner = DAcceptanceRunner()
        item = runner._check_d7()
        assert item.passed is True
        assert item.evidence["applied_version"] == 2
        assert item.evidence["rolled_back"] is True
        assert item.evidence["restored_content"] is True
        assert item.evidence["history_len"] == 3  # init/apply/rollback

    def test_check_d8_all_passed(self):
        """D-8 验收：年度灾备演练四项全部通过。"""
        runner = DAcceptanceRunner()
        item = runner._check_d8()
        assert item.passed is True
        assert item.evidence["items_passed"] == 4


class TestSerializable:
    def test_item_to_dict_json(self):
        """验收项结果 JSON 可序列化。"""
        runner = DAcceptanceRunner()
        item = runner._check_d6()
        data = json.dumps(item.to_dict(), ensure_ascii=False)
        loaded = json.loads(data)
        assert loaded["item"] == "D-6"
        assert loaded["passed"] is True

    def test_item_schema(self):
        """验收项字段齐全（item/criterion/passed/detail/evidence）。"""
        runner = DAcceptanceRunner()
        item = runner._check_d7()
        d = item.to_dict()
        assert set(d) == {"item", "criterion", "passed", "detail", "evidence"}
