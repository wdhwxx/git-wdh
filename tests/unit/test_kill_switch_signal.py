"""跨进程急停信号 + ack 回路单测（V3 决策点 2）。

覆盖：写标志（含 requested_at + 请求方）→ 读请求 → ack 确认 → 健康判定（未 ack 超时升级
CRITICAL 提示用一键平仓）→ 复位；以及与 0.9 硬闸同一真源（标志存在 = base_broker
kill_switch_active 置位）的一致性。
"""

import time
from pathlib import Path

import pytest

from myquant.live.brokers.paper_broker import PaperBroker
from myquant.live.kill_switch_signal import (
    KILL_SWITCH_ACK_SUFFIX,
    acknowledge_kill_switch,
    check_kill_switch_health,
    clear_kill_switch,
    kill_switch_ack,
    kill_switch_requested,
    request_kill_switch,
)


@pytest.fixture()
def flag_path(tmp_path: Path) -> str:
    """每个用例独立的标志文件路径。"""
    return str(tmp_path / "kill_switch.flag")


def test_request_writes_flag_with_requested_at_and_actor(flag_path: str) -> None:
    """决策点2：写入标志时记 requested_at + 请求方（可审计）。"""
    now = time.time()
    assert request_kill_switch(flag_path, actor="admin", reason="演练急停", now_unix=now)
    req = kill_switch_requested(flag_path)
    assert req is not None
    assert req.actor == "admin"
    assert req.reason == "演练急停"
    assert abs(req.requested_unix - now) < 1.0
    assert req.requested_at  # ISO 时间戳非空
    assert Path(flag_path).exists()


def test_request_empty_actor_raises(flag_path: str) -> None:
    """请求方必须留痕：空 actor 拒绝写入。"""
    with pytest.raises(ValueError):
        request_kill_switch(flag_path, actor="  ", reason="x")


def test_requested_none_when_not_set(tmp_path: Path) -> None:
    """未置位返回 None（不误判为急停）。"""
    assert kill_switch_requested(str(tmp_path / "nonexistent.flag")) is None


def test_ack_roundtrip(flag_path: str) -> None:
    """决策点2：消费方写 ack（acted_at + 动作结果）→ 可读取。"""
    now = time.time()
    request_kill_switch(flag_path, actor="admin", now_unix=now)
    assert acknowledge_kill_switch(flag_path, actor="guardian", result="cancel_all ok:5", now_unix=now + 2.0)
    ack = kill_switch_ack(flag_path)
    assert ack is not None
    assert ack.actor == "guardian"
    assert ack.result == "cancel_all ok:5"
    assert abs(ack.acted_unix - (now + 2.0)) < 1.0
    assert Path(flag_path + KILL_SWITCH_ACK_SUFFIX).exists()


def test_health_overdue_without_ack_prompts_liquidation(flag_path: str) -> None:
    """决策点2 必做：超时未 ack → 升级 CRITICAL，明确提示"急停未生效，请用一键平仓"。"""
    now = time.time()
    request_kill_switch(flag_path, actor="admin", now_unix=now)
    health = check_kill_switch_health(flag_path, ack_timeout_seconds=10.0, now_unix=now + 30.0)
    assert health["active"] is True
    assert health["acked"] is False
    assert health["healthy"] is False
    assert health["status"] == "OVERDUE"
    assert "一键平仓" in health["message"]


def test_health_pending_within_timeout(flag_path: str) -> None:
    """未 ack 但未超时 → PENDING（不误升级，给监护进程时间）。"""
    now = time.time()
    request_kill_switch(flag_path, actor="admin", now_unix=now)
    health = check_kill_switch_health(flag_path, ack_timeout_seconds=60.0, now_unix=now + 5.0)
    assert health["active"] is True
    assert health["status"] == "PENDING"
    assert health["healthy"] is False


def test_health_ok_when_acked_within_timeout(flag_path: str) -> None:
    """ack 在时限内 → 急停已确认生效（健康）。"""
    now = time.time()
    request_kill_switch(flag_path, actor="admin", now_unix=now)
    acknowledge_kill_switch(flag_path, actor="guardian", result="ok", now_unix=now + 3.0)
    health = check_kill_switch_health(flag_path, ack_timeout_seconds=60.0, now_unix=now + 4.0)
    assert health["acked"] is True
    assert health["healthy"] is True
    assert health["status"] == "OK"


def test_health_ok_when_not_set(tmp_path: Path) -> None:
    """未置位 → 健康（无急停信号，不误报）。"""
    health = check_kill_switch_health(str(tmp_path / "nope.flag"), now_unix=time.time())
    assert health["active"] is False
    assert health["healthy"] is True


def test_clear_removes_flag_and_ack(flag_path: str) -> None:
    """复位：删除标志与 ack。"""
    now = time.time()
    request_kill_switch(flag_path, actor="admin", now_unix=now)
    acknowledge_kill_switch(flag_path, actor="guardian", now_unix=now + 1.0)
    assert clear_kill_switch(flag_path)
    assert not Path(flag_path).exists()
    assert not Path(flag_path + KILL_SWITCH_ACK_SUFFIX).exists()
    assert kill_switch_requested(flag_path) is None


def test_same_true_source_as_broker_hard_gate(flag_path: str) -> None:
    """0.9 同真源：写急停标志后，broker 薄闸 kill_switch_active 判定置位（文件存在=置位）。"""
    broker = PaperBroker(initial_capital=1_000_000.0)
    broker.set_kill_switch_flag_path(flag_path)
    assert broker.kill_switch_active() is False
    request_kill_switch(flag_path, actor="admin", reason="同真源核验")
    assert broker.kill_switch_active() is True
    clear_kill_switch(flag_path)
    assert broker.kill_switch_active() is False
