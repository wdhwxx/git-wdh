# 文件名：myquant/live/run_mode.py
"""运行模式抽象层：仿真(SIM/paper) ↔ 实盘(LIVE) 统一切割接口。

本模块是"便捷切割接口"的权威实现，把仿真模拟盘与真实实盘之间的切换
收敛为单一真相源。设计目标：
  - 一个模式即全局真相（CLI > 配置 > 默认 paper），不再散落在
    trade_mode / broker.type / broker.mode / 各处 flag；
  - 实盘(LIVE)必须显式确认 + 券商账户就绪 + 配置一致，杜绝误接入；
  - 配置一致性校验在装配前拦截（防御式：即使绕过 CLI 直接调工厂也拦得住）。

与实盘链路的关系（蓝图 myquant/live/ 段）：
  run_实盘入口.py 解析 mode → apply_mode_to_config() 写回配置 →
  myquant.live.engine.create_live_engine 装配前 check_mode_consistency() 拦截。
  未来实盘切割：`--mode sim`（券商同源仿真）→ `--mode live --confirm-live`（真钱）。
"""

from __future__ import annotations

import enum
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("live.run_mode")

# 实盘(LIVE)要求券商必须是这些类型之一（paper 本地撮合不能用于真钱实盘）
_LIVE_BROKER_TYPES = frozenset({"qmt", "ptrade"})
# paper 本地模拟券商类型别名（三者等价，均走本地模拟撮合）
_PAPER_BROKER_TYPES = frozenset({"paper", "sim", "simulation"})
# CLI 合法模式取值（monitor 由 --mode monitor 或 --monitor-only 表达）
_VALID_CLI_MODES = frozenset({"monitor", "paper", "sim", "live"})


class RunMode(str, enum.Enum):
    """仿真/实盘统一运行模式。"""

    MONITOR = "monitor"  # 仅监控：订阅行情、生成信号，不下单
    PAPER = "paper"  # 纯本地模拟盘：本地模拟撮合，零外部依赖
    SIM = "sim"  # 券商同源仿真：QMT/Ptrade SIM 模拟撮合（需对应券商客户端）
    LIVE = "live"  # 真钱实盘：QMT/Ptrade LIVE 真实下单（需显式确认）


# 统一模式的中文说明
_MODE_DESC: Dict[RunMode, str] = {
    RunMode.MONITOR: "监控模式（订阅行情/生成信号，不提交订单）",
    RunMode.PAPER: "本地模拟盘（本地模拟撮合，零外部依赖，仿真默认）",
    RunMode.SIM: "券商同源仿真（QMT/Ptrade SIM 模拟撮合，需安装对应券商客户端）",
    RunMode.LIVE: "真钱实盘（QMT/Ptrade LIVE 真实下单，需显式 --confirm-live 确认）",
}


def describe(mode: RunMode) -> str:
    """返回运行模式的中文说明。

    Args:
        mode: 运行模式。

    Returns:
        该模式对应的中文描述；未知模式返回占位描述。
    """
    return _MODE_DESC.get(mode, f"未知模式: {mode}")


def resolve_run_mode(
    config: Dict[str, Any],
    cli_mode: Optional[str] = None,
    paper_flag: bool = False,
    monitor_only: bool = False,
) -> RunMode:
    """从 CLI 与配置解析最终运行模式（单一真相源）。

    优先级：显式 cli_mode > paper_flag > monitor_only > 配置推导 > 默认 paper。

    Args:
        config: 实盘配置字典（ConfigLoader.load 结果）。
        cli_mode: 命令行 --mode 参数（monitor/paper/sim/live），None=未指定。
        paper_flag: 命令行 --paper-trading 开关（等价 --mode paper）。
        monitor_only: 命令行 --monitor-only 开关。

    Returns:
        解析出的统一运行模式。

    Raises:
        ValueError: cli_mode 不是合法模式值。
    """
    if cli_mode:
        norm = str(cli_mode).strip().lower()
        if norm not in _VALID_CLI_MODES:
            raise ValueError(
                f"非法运行模式: {cli_mode!r}（可选: {sorted(_VALID_CLI_MODES)}）"
            )
        if norm == "monitor":
            return RunMode.MONITOR
        return RunMode(norm)
    if paper_flag:
        return RunMode.PAPER
    if monitor_only:
        return RunMode.MONITOR
    return _derive_from_config(config)


def _derive_from_config(config: Dict[str, Any]) -> RunMode:
    """从配置推导运行模式（trade_mode + broker.type + broker.mode）。

    防未来函数说明：只依据当前配置字段推导，不预测任何未来运行状态。
    """
    live_cfg = dict(config.get("live", {}) or {})
    broker_cfg = dict(config.get("broker", {}) or {})
    trade_mode = str(live_cfg.get("trade_mode", "paper")).lower()
    broker_type = str(broker_cfg.get("type", "paper")).lower()
    broker_mode = str(broker_cfg.get("mode", "SIM")).upper()

    if trade_mode == "live":
        return RunMode.LIVE
    if broker_type in _PAPER_BROKER_TYPES:
        return RunMode.PAPER
    if broker_mode == "LIVE":
        # broker 配置为 LIVE 但 trade_mode 非 live —— 配置不一致，保守按 SIM 处理，
        # 不一致由 check_mode_consistency 在装配前显式拦截
        logger.warning(
            "配置不一致：broker.mode=LIVE 但 trade_mode=%s，resolve 保守按 SIM 处理",
            trade_mode,
        )
        return RunMode.SIM
    return RunMode.SIM


def apply_mode_to_config(config: Dict[str, Any], mode: RunMode) -> Dict[str, Any]:
    """把统一运行模式写回配置（trade_mode / broker.type / broker.mode）。

    原地修改 config 并返回。由入口在装配前调用，保证 create_live_engine
    消费到与 CLI 一致的模式。

    Args:
        config: 实盘配置字典（原地修改）。
        mode: 解析后的统一运行模式。

    Returns:
        修改后的配置字典（同 config）。
    """
    live_cfg = config.setdefault("live", {})
    broker_cfg = config.setdefault("broker", {})

    if mode is RunMode.MONITOR:
        live_cfg["trade_mode"] = "paper"
        live_cfg["monitor_only"] = True
        return config

    live_cfg.pop("monitor_only", None)
    if mode is RunMode.PAPER:
        # 纯本地模拟盘：broker.mode 无意义，清除以避免与配置残留 LIVE 冲突
        live_cfg["trade_mode"] = "paper"
        broker_cfg["type"] = "paper"
        broker_cfg.pop("mode", None)
        return config
    if mode is RunMode.SIM:
        # 券商同源仿真：保持 broker.type（qmt/ptrade）不变，只锁定 broker.mode=SIM
        live_cfg["trade_mode"] = "paper"
        broker_cfg["mode"] = "SIM"
        return config
    # LIVE（真钱实盘）
    # KP-M04（T-30）：实盘模式切换属高危写操作——准入拒绝则显式抛错（默认仅审计留痕）
    from myquant.api.middleware.auth import get_operation_guard
    ok, why = get_operation_guard().check(
        "mode_switch_live", operator="run_mode",
        action="apply_mode_to_config -> LIVE")
    if not ok:
        raise ValueError(f"实盘模式切换被准入拒绝: {why}")
    live_cfg["trade_mode"] = "live"
    broker_cfg["mode"] = "LIVE"
    return config


def check_mode_consistency(config: Dict[str, Any]) -> List[str]:
    """校验配置内部一致性，返回问题列表（空列表=无问题）。

    防误接入：即使 CLI 未走到（工厂被第三方直接调用），也在装配前拦截
    trade_mode / broker.type / broker.mode 之间的冲突组合。

    Args:
        config: 实盘配置字典。

    Returns:
        不一致问题列表；为空表示配置自洽。
    """
    issues: List[str] = []
    live_cfg = dict(config.get("live", {}) or {})
    broker_cfg = dict(config.get("broker", {}) or {})
    trade_mode = str(live_cfg.get("trade_mode", "paper")).lower()
    broker_type = str(broker_cfg.get("type", "paper")).lower()
    broker_mode = str(broker_cfg.get("mode", "SIM")).upper()

    if trade_mode == "live":
        if broker_type in _PAPER_BROKER_TYPES:
            issues.append(
                f"trade_mode=live 但 broker.type={broker_type}（实盘需 qmt/ptrade）"
            )
        if broker_mode != "LIVE":
            issues.append(f"trade_mode=live 但 broker.mode={broker_mode}（实盘需 LIVE）")
    if broker_mode == "LIVE" and broker_type in _PAPER_BROKER_TYPES:
        issues.append(f"broker.type={broker_type} 不接受 broker.mode=LIVE")
    return issues


def validate_live(
    mode: RunMode,
    config: Dict[str, Any],
    confirm_live: bool = False,
) -> List[str]:
    """实盘(LIVE)安全门：返回阻止进入实盘的错误列表（空=允许）。

    仅当 mode 为 LIVE 时校验；非 LIVE 直接返回空。

    Args:
        mode: 解析后的运行模式。
        config: 实盘配置字典。
        confirm_live: 是否已显式携带 --confirm-live 确认开关。

    Returns:
        阻止进入实盘的问题列表；为空表示允许。
    """
    if mode is not RunMode.LIVE:
        return []
    errors: List[str] = []
    broker_cfg = dict(config.get("broker", {}) or {})
    broker_type = str(broker_cfg.get("type", "paper")).lower()
    account = str(
        broker_cfg.get("account", "") or broker_cfg.get("account_id", "") or ""
    ).strip()
    if not confirm_live:
        errors.append("实盘(LIVE)模式必须显式携带 --confirm-live 确认开关")
    if broker_type not in _LIVE_BROKER_TYPES:
        errors.append(f"实盘(LIVE)要求券商为 qmt/ptrade，当前 broker.type={broker_type}")
    if not account:
        errors.append("实盘(LIVE)要求配置券商账户（broker.account）")
    # KP-P6-01：程序化交易报备合规门禁（未备案拒绝实盘启动）
    try:
        from myquant.live.acceptance import ComplianceGate
    except Exception as exc:  # pragma: no cover - 防御性导入失败
        logger.error("导入 ComplianceGate 失败: %s", exc)
        errors.append("合规门禁不可用，禁止实盘启动")
    else:
        comp = ComplianceGate(config).check()
        errors.extend(comp.errors)
    return errors


def _demo() -> None:
    """命令行自检 demo（不含任何业务副作用）。"""
    cfg: Dict[str, Any] = {"live": {"trade_mode": "paper"}, "broker": {"type": "paper"}}
    for mode in RunMode:
        print(f"{mode.value:8s} -> {describe(mode)}")
    print("resolve(空配置)      =", resolve_run_mode({}).value)
    print("resolve(cli=sim)     =", resolve_run_mode({}, cli_mode="sim").value)
    print(
        "apply(paper)         =",
        apply_mode_to_config({"live": {}, "broker": {}}, RunMode.PAPER),
    )
    print("validate_live(无确认) =", validate_live(RunMode.LIVE, cfg))
    print("consistency(LIVE/trade paper) =", check_mode_consistency(cfg))


if __name__ == "__main__":
    _demo()
