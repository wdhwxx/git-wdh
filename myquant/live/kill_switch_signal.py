"""跨进程急停信号 + ack 回路（V3 决策点 2，阶段 4 独立监护进程同一通道）。

推荐结论（决策点 2）：
    跨进程急停 = 文件信号 + 独立监护进程读，且与阶段 4 合并成一条通道，必须带 ack 回路。
    - 写标志：request_kill_switch(...) → 写 kill_switch.flag（含 requested_at + 请求方 + 原因）；
    - 消费后确认：acknowledge_kill_switch(...) → 写 kill_switch.flag.ack（含 acted_at + 动作结果）；
    - 读 ack：check_kill_switch_health(...) → 超时未 ack → 升级 CRITICAL，明确提示
      "急停未生效，请用一键平仓"（否则人会误以为已急停而放弃其他手段——比没有急停更危险）。

与 0.9 broker 提交边界硬闸同一真源：base_broker.kill_switch_active() 判定"标志文件存在=置位"
（零依赖，进程崩了文件还在）。本模块是该通道的信号 + ack 读写侧，消费方（阶段 4 监护进程）
按决策点 1 语义执行：置位 = 禁开仓（0.9 硬闸只拒 BUY）+ 撤全部挂单（EmergencyLiquidationService
.cancel_all）+ 全平人工二次确认（保持 auto_liquidate=False，不在代码里放开自动全平）。

本模块不依赖 engine / risk / guardian / 门禁——应急链（I-1）独立。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# 标准日志器（与 myquant 通用日志规范一致；禁 print 输出运行信息）
logger = logging.getLogger("myquant.live.kill_switch_signal")

# kill_switch 标志文件默认路径（与 base_broker._KILL_SWITCH_FLAG_DEFAULT 同一真源，
# 阶段 4 监护进程与 0.9 硬闸共用——任何一处改动需同步两处，保持"文件存在=置位"不变式）。
KILL_SWITCH_FLAG_DEFAULT: str = "run/kill_switch.flag"
# ack 文件 = 标志文件 + 该后缀
KILL_SWITCH_ACK_SUFFIX: str = ".ack"
# 急停超时默认值（s）。阈值真源：V3 决策点2 ack 回路"超时未 ack → 升级 CRITICAL"；
# 具体秒数引用 P0-8 MTTR（恢复由人在盘前/盘中值守，MTTR ≤ P0-8）。
KILL_SWITCH_ACK_TIMEOUT_SECONDS_DEFAULT: float = 60.0

# 时间戳 ISO 8601 格式（决策点2：写入标志记 requested_at）
_ISO_FORMAT: str = "%Y-%m-%dT%H:%M:%S%z"


@dataclass
class KillSwitchRequest:
    """急停请求（写入标志文件时落盘，供监护进程与审计读取）。

    Attributes:
        actor: 请求方（admin / guardian / 人工 CLI 等，非空）。
        reason: 急停原因。
        requested_at: 请求时刻（ISO 8601，带时区）。
        requested_unix: 请求时刻（Unix 秒，供超时判定不依赖字符串解析）。
    """

    actor: str
    reason: str
    requested_at: str
    requested_unix: float

    def to_dict(self) -> Dict[str, Any]:
        """转可序列化 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KillSwitchRequest":
        """从 JSON dict 恢复（字段缺失用安全默认，防御式，不抛未预期异常）。"""
        return cls(
            actor=str(data.get("actor", "")),
            reason=str(data.get("reason", "")),
            requested_at=str(data.get("requested_at", "")),
            requested_unix=float(data.get("requested_unix", 0.0)),
        )


@dataclass
class KillSwitchAck:
    """急停确认（监护进程消费急停信号、执行动作后写 ack 文件）。

    Attributes:
        actor: 执行方（监护进程）。
        result: 动作结果（如 "cancel_all:5,cancelled" / "cancel_all:failed:..."）。
        acted_at: 动作完成时刻（ISO 8601）。
        acted_unix: 动作完成时刻（Unix 秒）。
    """

    actor: str
    result: str
    acted_at: str
    acted_unix: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> Dict[str, Any]:
        """转可序列化 dict。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KillSwitchAck":
        """从 JSON dict 恢复（字段缺失用安全默认）。"""
        return cls(
            actor=str(data.get("actor", "")),
            result=str(data.get("result", "")),
            acted_at=str(data.get("acted_at", "")),
            acted_unix=float(data.get("acted_unix", time.time())),
        )


def _utc_now_iso() -> str:
    """当前 UTC 时刻（ISO 8601，带时区）。"""
    return datetime.now(timezone.utc).strftime(_ISO_FORMAT)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> bool:
    """原子写 JSON：先写临时文件再 os.replace，避免崩溃留下半截标志/ack。

    Args:
        path: 目标文件路径。
        payload: 要写入的 dict。

    Returns:
        bool: 写入成功返回 True。
    """
    try:
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as exc:
        logger.error("[kill_switch] 写 %s 失败: %s", path, exc)
        return False


def request_kill_switch(
    flag_path: Optional[str] = None,
    actor: str = "system",
    reason: str = "",
    now_unix: Optional[float] = None,
) -> bool:
    """请求急停：写入急停标志文件（含 requested_at + 请求方 + 原因）。

    与 0.9 硬闸同一真源：文件存在即置位（base_broker.kill_switch_active 判定）。
    写入前不要求 actor 非空则拒绝（决策点2：请求方必须留痕，可审计）。

    Args:
        flag_path: 标志文件路径；None 用默认 run/kill_switch.flag。
        actor: 请求方（非空，否则 ValueError）。
        reason: 急停原因。
        now_unix: 请求时刻（Unix 秒）；None 用当前时间（测试可注入）。

    Returns:
        bool: 写成功返回 True。

    Raises:
        ValueError: actor 为空。
    """
    if not (actor or "").strip():
        raise ValueError("request_kill_switch actor 不能为空")
    ts = now_unix if now_unix is not None else time.time()
    req = KillSwitchRequest(
        actor=actor,
        reason=reason or "未说明原因",
        requested_at=datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_ISO_FORMAT),
        requested_unix=float(ts),
    )
    ok = _atomic_write_json(Path(flag_path or KILL_SWITCH_FLAG_DEFAULT), req.to_dict())
    if ok:
        logger.critical(
            "[kill_switch] 急停请求已落盘（requested_at=%s, actor=%s, reason=%s）",
            req.requested_at, actor, reason or "未说明原因",
        )
    return ok


def kill_switch_requested(flag_path: Optional[str] = None) -> Optional[KillSwitchRequest]:
    """读取急停请求（标志文件存在且可解析时返回，否则 None）。

    Args:
        flag_path: 标志文件路径；None 用默认。

    Returns:
        Optional[KillSwitchRequest]: 已置位返回请求对象；未置位/解析失败返回 None。
    """
    path = Path(flag_path or KILL_SWITCH_FLAG_DEFAULT)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return KillSwitchRequest.from_dict(json.load(f))
    except (OSError, ValueError) as exc:
        logger.warning("[kill_switch] 读取标志 %s 失败（按未置位处理）: %s", path, exc)
        return None


def acknowledge_kill_switch(
    flag_path: Optional[str] = None,
    actor: str = "guardian",
    result: str = "",
    now_unix: Optional[float] = None,
) -> bool:
    """确认急停已生效：写 ack 文件（含 acted_at + 动作结果）。

    由急停信号消费方（阶段 4 监护进程）在完成"撤全部挂单 + 禁开仓"动作后调用。

    Args:
        flag_path: 标志文件路径；None 用默认。
        actor: 执行方。
        result: 动作结果（如 "cancel_all ok:5" / "cancel_all failed:..."）。
        now_unix: 确认时刻（Unix 秒）；None 用当前时间。

    Returns:
        bool: 写成功返回 True。
    """
    ts = now_unix if now_unix is not None else time.time()
    ack = KillSwitchAck(
        actor=actor,
        result=result or "ok",
        acted_at=datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_ISO_FORMAT),
        acted_unix=float(ts),
    )
    path = Path(flag_path or KILL_SWITCH_FLAG_DEFAULT)
    ok = _atomic_write_json(path.with_name(path.name + KILL_SWITCH_ACK_SUFFIX), ack.to_dict())
    if ok:
        logger.critical("[kill_switch] 急停已确认生效（acted_at=%s, result=%s）", ack.acted_at, ack.result)
    return ok


def kill_switch_ack(flag_path: Optional[str] = None) -> Optional[KillSwitchAck]:
    """读取急停确认（ack 文件存在且可解析时返回，否则 None）。

    Args:
        flag_path: 标志文件路径；None 用默认。

    Returns:
        Optional[KillSwitchAck]: 已确认返回 ack 对象；未确认/解析失败返回 None。
    """
    path = Path(flag_path or KILL_SWITCH_FLAG_DEFAULT)
    ack_path = path.with_name(path.name + KILL_SWITCH_ACK_SUFFIX)
    if not ack_path.exists():
        return None
    try:
        with open(ack_path, "r", encoding="utf-8") as f:
            return KillSwitchAck.from_dict(json.load(f))
    except (OSError, ValueError) as exc:
        logger.warning("[kill_switch] 读取 ack %s 失败（按未确认处理）: %s", ack_path, exc)
        return None


def check_kill_switch_health(
    flag_path: Optional[str] = None,
    ack_timeout_seconds: float = KILL_SWITCH_ACK_TIMEOUT_SECONDS_DEFAULT,
    now_unix: Optional[float] = None,
) -> Dict[str, Any]:
    """检查急停是否已确认生效（ack 回路健康判定）。

    决策点 2 必做项：人写了标志但监护进程未消费（未启动/崩了/被误杀）时，界面若显示
    "已急停"而交易进程照旧开仓，比没有急停更危险——因此必须读 ack 并做超时判定。

    Args:
        flag_path: 标志文件路径；None 用默认。
        ack_timeout_seconds: 超时阈值（requested 到 acted 允许的最大间隔）。
        now_unix: 当前时刻（Unix 秒）；None 用当前时间。

    Returns:
        Dict 判定结果，含键:
          - active: bool，急停是否置位（标志存在）。
          - acked: bool，是否已有确认。
          - healthy: bool，急停已确认且未超时（或未置位视为健康）。
          - status: str，OK / PENDING（未 ack 但未超时）/ OVERDUE（超时未 ack）。
          - message: str，供界面/告警展示；超时未 ack 时明确提示"急停未生效，请用一键平仓"。
    """
    ts = now_unix if now_unix is not None else time.time()
    req = kill_switch_requested(flag_path)
    if req is None:
        # 未置位：无急停，视为健康。
        return {
            "active": False,
            "acked": False,
            "healthy": True,
            "status": "OK",
            "message": "未置位急停（无急停信号）",
        }
    ack = kill_switch_ack(flag_path)
    elapsed = max(0.0, ts - req.requested_unix)
    if ack is None:
        if elapsed > ack_timeout_seconds:
            message = (
                f"急停未生效（requested_at={req.requested_at} 后 {elapsed:.0f}s 未收到 ack），"
                "请用一键平仓（I-1 应急链）"
            )
            status = "OVERDUE"
            logger.critical("[kill_switch] %s", message)
            return {"active": True, "acked": False, "healthy": False, "status": status, "message": message}
        message = f"急停已请求，等待确认（requested_at={req.requested_at}，{elapsed:.0f}s/{ack_timeout_seconds:.0f}s）"
        return {"active": True, "acked": False, "healthy": False, "status": "PENDING", "message": message}
    acted = max(0.0, ack.acted_unix - req.requested_unix)
    message = f"急停已确认生效（acted_at={ack.acted_at}，耗时{acted:.0f}s，result={ack.result}）"
    return {"active": True, "acked": True, "healthy": True, "status": "OK", "message": message}


def clear_kill_switch(flag_path: Optional[str] = None) -> bool:
    """复位急停：删除标志文件与 ack 文件（人工确认恢复交易后调用）。

    Args:
        flag_path: 标志文件路径；None 用默认。

    Returns:
        bool: 两文件均不存在（删除成功或本就不存在）返回 True。
    """
    path = Path(flag_path or KILL_SWITCH_FLAG_DEFAULT)
    ack_path = path.with_name(path.name + KILL_SWITCH_ACK_SUFFIX)
    ok = True
    for p in (path, ack_path):
        try:
            if p.exists():
                p.unlink()
        except OSError as exc:
            logger.error("[kill_switch] 删除 %s 失败: %s", p, exc)
            ok = False
    if ok:
        logger.warning("[kill_switch] 急停已复位（删除标志与 ack）")
    return ok


def _demo() -> None:
    """模块级可运行 demo（py文件规则：可执行文件含 if __name__ == '__main__' 入口）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        flag = str(Path(tmp) / "kill_switch.flag")
        assert request_kill_switch(flag, actor="admin", reason="演练急停")
        req = kill_switch_requested(flag)
        assert req is not None and req.actor == "admin"
        assert not check_kill_switch_health(flag, now_unix=req.requested_unix + 5.0)["acked"]
        assert acknowledge_kill_switch(flag, actor="guardian", result="cancel_all ok:5")
        assert kill_switch_ack(flag) is not None
        health = check_kill_switch_health(flag, now_unix=req.requested_unix + 6.0)
        assert health["acked"] and health["healthy"] and health["status"] == "OK"
        assert clear_kill_switch(flag)
        assert kill_switch_requested(flag) is None
        print("kill_switch_signal demo OK")


if __name__ == "__main__":
    _demo()
