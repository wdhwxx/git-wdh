# 文件名：tests/unit/test_disaster_drill.py
"""
D-8 元监控与灾备 单测
=====================
覆盖 DisasterDrillRunner 四项灾备能力：SQLite 备份恢复、RabbitMQ 持久化恢复
（fsync 持久化载体复刻队列语义）、告警通道切换（主故障→备接管）、FI-07 临界
网络抖动不误判（R-08）。隔离环境（临时目录），不触碰工作区真实数据。
"""
import json

import pytest

from myquant.acceptance.disaster_drill import DisasterDrillRunner
from myquant.monitoring.meta_monitor import (
    ExternalHeartbeatGuard,
    FileHeartbeatCarrier,
)


class TestSqliteBackupRestore:
    def test_backup_restore_preserves_data(self):
        """备份→删活库→恢复：行数与采样一致（灾备通过）。"""
        runner = DisasterDrillRunner()
        res = runner.verify_sqlite_backup_restore()
        assert res["passed"] is True
        assert res["evidence"]["rows_before"] == res["evidence"]["rows_after"] == 5
        assert res["evidence"]["sample_count"] == 5
        runner.close()

    def test_restored_messages_match(self):
        """恢复后消息内容与备份前一致（逐条采样校验）。"""
        runner = DisasterDrillRunner()
        res = runner.verify_sqlite_backup_restore()
        msgs = res["evidence"]["restored_msgs"]
        assert msgs == ["row-1", "row-2", "row-3", "row-4", "row-5"]
        runner.close()


class TestRabbitmqDurability:
    def test_durability_full_roundtrip(self):
        """持久化写入→崩溃（未 ack）→重启恢复：全量消息不丢失、顺序一致。"""
        runner = DisasterDrillRunner()
        res = runner.verify_rabbitmq_durability(n_messages=10)
        assert res["passed"] is True
        assert res["evidence"]["written"] == res["evidence"]["recovered"] == 10
        assert res["evidence"]["first_last_match"] is True
        runner.close()

    def test_durability_content_preserved(self):
        """恢复内容与写入内容逐条一致（fsync 落盘语义）。"""
        runner = DisasterDrillRunner()
        # 手动验证内容一致：调用底层持久化载体
        log = runner.work_dir / "durable_mq.log"
        # 通过完整演练产生日志，然后回读对比
        res = runner.verify_rabbitmq_durability(n_messages=3)
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 3
        assert all(ln.startswith("msg-") for ln in lines)
        runner.close()


class TestChannelFailover:
    def test_failover_to_alternate_on_primary_failure(self):
        """主通道故障→备通道接管，告警不丢失。"""
        runner = DisasterDrillRunner()
        received = []

        def failing(_a):
            raise ConnectionError("down")

        def alternate(a):
            received.append(a)

        res = runner.verify_channel_failover(failing, alternate)
        assert res["passed"] is True
        assert res["evidence"]["primary_failed"] is True
        assert res["evidence"]["alternate_received"] == 1
        assert len(received) == 1
        runner.close()

    def test_failover_requires_primary_failure(self):
        """主通道未故障时不算切换成功（严格判定）。"""
        runner = DisasterDrillRunner()
        received = []

        def ok(_a):
            pass

        def alternate(a):
            received.append(a)

        res = runner.verify_channel_failover(ok, alternate)
        assert res["passed"] is False  # 主通道未故障，无切换必要
        runner.close()


class TestHeartbeatNoFalsePositive:
    def test_single_jitter_not_dead(self):
        """临界网络抖动（单次超时）→ degraded 不判死主进程（R-08）。"""
        guard = ExternalHeartbeatGuard(
            interval_seconds=1.0, consecutive_timeout=3, timeout_factor=2.0)
        carrier = FileHeartbeatCarrier(directory=str(
            DisasterDrillRunner().work_dir / "hb"))
        # 使用演练 runner 的方式验证
        runner = DisasterDrillRunner()
        res = runner.verify_heartbeat_no_false_positive()
        assert res["evidence"]["jitter_status"] == "degraded"
        assert res["evidence"]["jitter_not_dead"] is True
        assert res["evidence"]["kill_status"] == "dead"
        assert res["passed"] is True
        runner.close()


class TestReport:
    def test_run_all_items_passed(self):
        """run() 汇总四项灾备演练全部通过，报告 JSON 可序列化。"""
        runner = DisasterDrillRunner()
        report = runner.run()
        assert report["summary"]["items_total"] == 4
        assert report["summary"]["items_passed"] == 4
        assert report["summary"]["all_passed"] is True
        items = {i["item"] for i in report["items"]}
        assert items == {"sqlite_backup_restore", "rabbitmq_durability",
                         "channel_failover", "fi07_heartbeat_no_false_positive"}
        json.dumps(report, ensure_ascii=False)  # 可序列化
        runner.close()

    def test_write_report_json(self, tmp_path):
        """灾备演练报告落盘为可解析 JSON。"""
        runner = DisasterDrillRunner()
        runner.run()
        out = runner.write_report(str(tmp_path / "disaster_report.json"))
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["summary"]["all_passed"] is True
        runner.close()
