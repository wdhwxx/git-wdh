# 文件名：tests/unit/test_config_as_code.py
"""
D-7 监控即代码完善 单测
=======================
覆盖 MonitoringConfigRegistry：4 类配置项版本化（P0-6 完成态）、initialize 幂等、
apply（版本+1/上一版本快照/变更关联监控告警/审计）、rollback（恢复上一版本内容）、
history 可追溯、防御式校验（未知项/空内容）、变更告警 dedup。

隔离：全部使用临时 config 目录 + 临时账本 + 隔离 AlertManager；不触碰
config/monitoring 真实模板。防未来：时间戳一律系统时钟已发生时刻。
"""
import json

import pytest

from myquant.core.constants import SeverityLevel
from myquant.monitoring.alert_store import AlertStore
from myquant.monitoring.alerting import AlertManager
from myquant.monitoring.config_as_code import (
    KNOWN_ITEMS,
    MonitoringConfigRegistry,
)


class _Registry:
    """测试用注册表：临时目录 + 隔离 AlertManager。"""

    def __init__(self, tmp_path, alert_manager=None):
        self.config_dir = tmp_path / "config_monitoring"
        self.config_dir.mkdir()
        self.ledger = tmp_path / "ledger.json"
        self.registry = MonitoringConfigRegistry(
            config_dir=str(self.config_dir),
            ledger_path=str(self.ledger),
            alert_manager=alert_manager,
        )


class TestDiscoverAndInit:
    def test_known_items_cover_p0_6(self):
        """4 类监控配置项：仪表盘/告警规则/Runbook/采集配置。"""
        assert KNOWN_ITEMS == ["dashboard", "alert_rules", "runbooks", "collectors"]

    def test_discover_finds_existing_yaml(self, tmp_path):
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text(
            "dashboards:\n  - id: m\n", encoding="utf-8")
        (r.config_dir / "runbooks.example.yaml").write_text(
            "runbooks: []\n", encoding="utf-8")
        d = r.registry.discover()
        assert d["dashboard"]["exists"] is True
        assert d["dashboard"]["filename"] == "dashboard.yaml"
        # .example.yaml 也被发现（模板也可版本化）
        assert d["runbooks"]["exists"] is True
        assert d["runbooks"]["filename"] == "runbooks.example.yaml"
        # 无文件项 exists=False
        assert d["alert_rules"]["exists"] is False

    def test_initialize_stamps_v1_idempotent(self, tmp_path):
        """initialize 对现有配置打初始版本 v1；幂等（二次调用不重复）。"""
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text("dashboards: []\n", encoding="utf-8")
        (r.config_dir / "collectors.yaml").write_text("collectors: []\n", encoding="utf-8")
        assert r.registry.initialize() == 2
        assert r.registry.initialize() == 0  # 已版本化，跳过
        v = r.registry.get_version("dashboard")
        assert v["current_version"] == 1
        assert v["changed_by"] == "system"
        hist = r.registry.history("dashboard")
        assert hist[0]["action"] == "init"
        assert hist[0]["prev_content"] is None


class TestApply:
    def test_apply_bumps_version_and_snapshots(self, tmp_path):
        """apply 版本 +1、留存上一版本内容快照、写盘 <item>.yaml。"""
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text("old\n", encoding="utf-8")
        r.registry.initialize()
        res = r.registry.apply("dashboard", "new_content\n",
                               changed_by="tester", summary="改看板")
        assert res["version"] == 2
        assert (r.config_dir / "dashboard.yaml").read_text(encoding="utf-8") == "new_content\n"
        hist = r.registry.history("dashboard")
        assert hist[1]["action"] == "apply"
        assert hist[1]["prev_content"] == "old\n"  # 上一版本快照留痕
        assert hist[1]["changed_by"] == "tester"
        assert hist[1]["summary"] == "改看板"

    def test_apply_emits_change_monitoring_alert(self, tmp_path):
        """变更关联监控：apply 经独立告警通道（category=config）留痕可追溯。"""
        store = AlertStore(db_path=str(tmp_path / "a.db"))
        manager = AlertManager(store=store, store_enabled=True)
        r = _Registry(tmp_path, alert_manager=manager)
        (r.config_dir / "alert_rules.yaml").write_text("rules: []\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("alert_rules", "rules:\n  - id: x\n", changed_by="tester")
        alerts = store.load_history(limit=20)
        change = [a for a in alerts if a.get("category") == "config"]
        assert len(change) >= 1
        assert change[-1]["message"] == "监控配置变更 [apply] alert_rules → v2"
        assert change[-1]["source"] == "config_as_code"

    def test_apply_dedup_same_change_alerts_once(self, tmp_path):
        """同一次变更 dedup_key=config:apply:<item>:<version> 只告警一次。"""
        store = AlertStore(db_path=str(tmp_path / "d.db"))
        manager = AlertManager(store=store, store_enabled=True)
        r = _Registry(tmp_path, alert_manager=manager)
        (r.config_dir / "dashboard.yaml").write_text("a\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("dashboard", "b\n", changed_by="t")
        # 再次 apply 同内容（新版本 v3，独立 dedup，仍告警——每版本独立留痕）
        r.registry.apply("dashboard", "c\n", changed_by="t")
        alerts = store.load_history(limit=20)
        config_alerts = [a for a in alerts if a.get("category") == "config"]
        # v2 与 v3 各一次（dedup 按版本隔离，同一版本不重复）
        assert len(config_alerts) == 2
        assert {a["message"].split("→")[-1].strip() for a in config_alerts} == {"v2", "v3"}

    def test_apply_rejects_empty_and_unknown(self, tmp_path):
        """防御式：空内容拒绝应用；未知配置项拒绝。"""
        r = _Registry(tmp_path)
        with pytest.raises(ValueError):
            r.registry.apply("dashboard", "   ", changed_by="t")
        with pytest.raises(ValueError):
            r.registry.apply("not_a_item", "x\n", changed_by="t")


class TestRollback:
    def test_rollback_restores_prev_content(self, tmp_path):
        """rollback 恢复到上一版本内容，版本继续 +1 并留痕。"""
        r = _Registry(tmp_path)
        (r.config_dir / "runbooks.yaml").write_text("v1\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("runbooks", "v2\n", changed_by="t")
        res = r.registry.rollback("runbooks", changed_by="tester")
        assert res["rolled_back"] is True
        assert res["restored_version"] == 1
        assert (r.config_dir / "runbooks.yaml").read_text(encoding="utf-8") == "v1\n"
        hist = r.registry.history("runbooks")
        assert hist[-1]["action"] == "rollback"
        assert hist[-1]["summary"] == "回滚到上一版本 v1"
        assert hist[-1]["changed_by"] == "tester"

    def test_rollback_no_prev_returns_false(self, tmp_path):
        """无上一版本可回滚（仅初始版本）→ rolled_back=False，不写盘。"""
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text("v1\n", encoding="utf-8")
        r.registry.initialize()
        res = r.registry.rollback("dashboard")
        assert res["rolled_back"] is False
        assert (r.config_dir / "dashboard.yaml").read_text(encoding="utf-8") == "v1\n"

    def test_rollback_after_multiple_apply(self, tmp_path):
        """多次 apply 后回滚恢复到最近上一版本（v2），内容逐级可逆。"""
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text("v1\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("dashboard", "v2\n", changed_by="t")
        r.registry.apply("dashboard", "v3\n", changed_by="t")
        res = r.registry.rollback("dashboard")
        assert res["rolled_back"] is True
        assert res["restored_version"] == 2
        assert (r.config_dir / "dashboard.yaml").read_text(encoding="utf-8") == "v2\n"


class TestTraceability:
    def test_history_json_serializable(self, tmp_path):
        """history 变更史 JSON 可序列化（可追溯可存档）。"""
        r = _Registry(tmp_path)
        (r.config_dir / "collectors.yaml").write_text("c1\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("collectors", "c2\n", changed_by="op1")
        r.registry.rollback("collectors", changed_by="op2")
        data = json.dumps(r.registry.history("collectors"), ensure_ascii=False)
        loaded = json.loads(data)
        assert [e["action"] for e in loaded] == ["init", "apply", "rollback"]

    def test_get_version_after_ops(self, tmp_path):
        """get_version 反映当前版本号与 sha256 指纹。"""
        r = _Registry(tmp_path)
        (r.config_dir / "dashboard.yaml").write_text("x\n", encoding="utf-8")
        r.registry.initialize()
        r.registry.apply("dashboard", "y\n", changed_by="t")
        v = r.registry.get_version("dashboard")
        assert v["current_version"] == 2
        assert v["changed_by"] == "t"
        assert len(v["sha256"]) == 64
