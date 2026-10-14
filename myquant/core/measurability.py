# 文件名：myquant/monitoring/measurability.py
"""
measurability.py - P0-8 可测量性与故障注入统计模块
========================================================================
依据《量化系统监控模块施工总清单_V1.2》P0-8 展开（三项定义定稿）：

  ① 误报/准确率 ground-truth 判定规则（TP / FP / FN）
  ② MTTD / MTTA / MTTR 计时起止点（一律以系统时间戳计算，禁止人工估算）
  ③ 标准故障注入用例集 FI-01~07（与 D-6 混沌工程共用同一用例库）

本模块是 §9 KPI-2（MTTx）与 KPI-3（准确率/误报率）的唯一权威测量依据：
  - MTTD = 故障注入时间戳（事件时间，非告警时间）→ 告警落库 created_at
  - MTTA = 告警 created_at → 系统确认事件 acked_at（不以"人看到"为起点）
  - MTTR = 故障注入时间戳 → 告警→恢复成对记录 resolved_at
  - 准确率 = TP/(TP+FP)，误报率 = FP/(TP+FP)，检出率 = TP/(TP+FN)

防未来铁律：本模块只消费**已发生**的时间戳与告警记录
（fault_ts / created_at / acked_at / resolved_at / 人工标记），
不引用任何未来数据；统计口径与计时起止点完全按 P0-8 定义，禁止人工估算。

依赖: dataclasses, enum, logging, typing（标准库）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("myquant.core.measurability")


# ═══════════════════════════════════════════════
#  常量（P0-8 口径，禁止硬编码魔法数字）
# ═══════════════════════════════════════════════

CLASSIFICATION_TP: str = "TP"          # 真告警
CLASSIFICATION_FP: str = "FP"          # 误报
CLASSIFICATION_FN: str = "FN"          # 漏报（由 FI 注入主动测量）
CLASSIFICATION_UNKNOWN: str = "UNCLASSIFIED"  # 无法判定（诚实标注，不强行归类）

# 人工处置标记（Runbook ack 时打标，P0-4 模板新增 classification 字段）
_TP_DISPOSITIONS: frozenset[str] = frozenset({"需处置"})
_FP_DISPOSITIONS: frozenset[str] = frozenset({"误报", "噪声", "已自愈无需动作"})

# 默认引擎心跳组态（A-5 建议值，5×10s=50s < 60s 保 MTTD<1min）
DEFAULT_HEARTBEAT_PROBE_SECONDS: float = 10.0
DEFAULT_HEARTBEAT_CONSECUTIVE_MISSES: int = 5


# ═══════════════════════════════════════════════
#  ① 分类判定：TP / FP / FN
# ═══════════════════════════════════════════════

class ClassificationStats:
    """TP/FP/FN ground-truth 分类与准确率/误报率/检出率统计。

    判定规则（P0-8 ①）：
      - 真告警 TP：告警产生后在 Runbook 中被确认（acked）为"需处置"，
        或 resolved 时标记根因为真实系统/市场事件。
      - 误报 FP：被人工标记为"误报/噪声/已自愈无需动作"，
        或在无处置动作下自动 resolved 且元监控确认无对应真实事件。
      - 漏报 FN：由 FI-01~07 故障注入主动测量——注入的故障未被任何告警捕获。

    统计口径（P0-8 ①）：数据源为告警表（BASE-1）的 status + classification 字段，
    由 §7 周复盘自动统计；人工仅抽检校正（≥20%）。
    """

    @staticmethod
    def classify_alert(alert: Dict[str, Any]) -> str:
        """按 ground-truth 规则判定单条告警为 TP / FP / UNCLASSIFIED。

        Args:
            alert: 告警记录字典，支持字段（未来与 BASE-1 alerts 表对齐）：
                acked_disposition: str | None  Runbook 确认时的处置标记
                    （"需处置"/"误报"/"噪声"/"已自愈无需动作"）
                root_cause_real: bool | None   resolved 时根因为真实事件
                auto_resolved: bool            未处置自动 resolved
                meta_confirmed_no_event: bool | None
                    元监控确认无对应真实事件

        Returns:
            str: "TP" / "FP" / "UNCLASSIFIED"。

        Raises:
            TypeError: alert 不是字典时抛出。
        """
        if not isinstance(alert, dict):
            raise TypeError(f"告警记录必须是 dict，收到 {type(alert).__name__}")

        disposition = alert.get("acked_disposition")
        root_cause_real = alert.get("root_cause_real")
        auto_resolved = bool(alert.get("auto_resolved", False))
        meta_no_event = alert.get("meta_confirmed_no_event")

        # TP：确认"需处置"或根因为真实事件
        if disposition in _TP_DISPOSITIONS or root_cause_real is True:
            return CLASSIFICATION_TP
        # FP：人工标记误报/噪声/已自愈，或未处置自动 resolved 且元监控无真实事件
        if disposition in _FP_DISPOSITIONS:
            return CLASSIFICATION_FP
        if auto_resolved and meta_no_event is True:
            return CLASSIFICATION_FP
        # 无法判定：显式标注，不强行归类（人工周复盘抽检校正）
        return CLASSIFICATION_UNKNOWN

    @staticmethod
    def count_fn(fi_records: Sequence[Dict[str, Any]]) -> int:
        """统计故障注入漏报数（FN）。

        Args:
            fi_records: 故障注入记录列表，每条含：
                case_id: str   用例 ID（FI-01~07）
                captured: bool  注入的故障是否被任何告警捕获

        Returns:
            int: 未被捕获的注入次数（FN）。

        Raises:
            TypeError: 元素不是字典时抛出。
        """
        fn: int = 0
        for record in fi_records:
            if not isinstance(record, dict):
                raise TypeError(f"故障注入记录必须是 dict，收到 {type(record).__name__}")
            if not bool(record.get("captured", False)):
                fn += 1
        return fn

    @staticmethod
    def compute(
        alert_records: Sequence[Dict[str, Any]],
        fi_records: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """计算 TP/FP/FN 与准确率/误报率/检出率。

        Args:
            alert_records: 告警记录列表（classification 判定输入）。
            fi_records: 故障注入记录列表（FN 主动测量输入），可为 None。

        Returns:
            dict: {
                "tp", "fp", "fn", "unclassified",
                "accuracy", "fp_rate", "detection_rate",
                "formula": {"accuracy": "TP/(TP+FP)", ...}
            }
            分母为 0 的比率返回 None（诚实标注，不造数）。
        """
        tp: int = 0
        fp: int = 0
        unclassified: int = 0
        for alert in alert_records:
            label = ClassificationStats.classify_alert(alert)
            if label == CLASSIFICATION_TP:
                tp += 1
            elif label == CLASSIFICATION_FP:
                fp += 1
            else:
                unclassified += 1

        fn: int = (
            ClassificationStats.count_fn(fi_records)
            if fi_records is not None
            else 0
        )

        tp_plus_fp: int = tp + fp
        tp_plus_fn: int = tp + fn

        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "unclassified": unclassified,
            "accuracy": round(tp / tp_plus_fp, 6) if tp_plus_fp > 0 else None,
            "fp_rate": round(fp / tp_plus_fp, 6) if tp_plus_fp > 0 else None,
            "detection_rate": round(tp / tp_plus_fn, 6) if tp_plus_fn > 0 else None,
            "formula": {
                "accuracy": "TP/(TP+FP)",
                "fp_rate": "FP/(TP+FP)",
                "detection_rate": "TP/(TP+FN)",
            },
        }


# ═══════════════════════════════════════════════
#  ② MTTx 计时（系统时间戳口径，禁止人工估算）
# ═══════════════════════════════════════════════

class MttxCalculator:
    """MTTD/MTTA/MTTR 计时起止点计算（P0-8 ②，一律系统时间戳）。

    计时口径：
      - MTTD（检测）: 起点=故障注入脚本写入的"故障发生时间戳"（事件时间），
        终点=对应告警在 BASE-1 落库的 created_at。
      - MTTA（确认）: 起点=告警 created_at，终点=告警 acked_at
        （以系统确认事件为准，不以"人看到"为起点）。
      - MTTR（恢复）: 起点=故障注入时间戳，终点=BASE-1"告警→恢复"成对记录
        的 resolved_at（健康恢复自动发 INFO 并置 resolved）。
    """

    @staticmethod
    def elapsed_seconds(start_ts: float, end_ts: float) -> float:
        """计算两个系统时间戳的间隔秒数（防御式校验）。

        Args:
            start_ts: 起点时间戳（秒，系统时钟，≥0）。
            end_ts: 终点时间戳（秒，≥ start_ts）。

        Returns:
            float: end_ts - start_ts（秒）。

        Raises:
            ValueError: 时间戳为负或终点早于起点时抛出（计时无效，禁止静默）。
        """
        if start_ts < 0 or end_ts < 0:
            raise ValueError(f"时间戳必须非负: start={start_ts}, end={end_ts}")
        if end_ts < start_ts:
            raise ValueError(
                f"终点早于起点（计时无效）: start={start_ts}, end={end_ts}"
            )
        return end_ts - start_ts

    @staticmethod
    def average(seconds: Sequence[float]) -> Optional[float]:
        """求 MTTx 平均秒数（空序列返回 None，诚实标注不可平均）。

        Args:
            seconds: 单次计时秒数序列。

        Returns:
            float | None: 平均秒数；序列为空返回 None。
        """
        if not seconds:
            return None
        return round(sum(seconds) / len(seconds), 6)

    @staticmethod
    def mttd(fault_ts: float, created_at: float) -> float:
        """MTTD 单次计时：故障注入时间戳 → 告警落库 created_at。

        Args:
            fault_ts: 故障注入脚本写入的故障发生时间戳（事件时间）。
            created_at: 对应告警在 BASE-1 落库的时间。

        Returns:
            float: 检测耗时（秒）。

        Raises:
            ValueError: 非法时间戳序列时抛出。
        """
        return MttxCalculator.elapsed_seconds(fault_ts, created_at)

    @staticmethod
    def mtta(created_at: float, acked_at: float) -> float:
        """MTTA 单次计时：告警 created_at → 系统确认 acked_at。

        Args:
            created_at: 告警落库时间。
            acked_at: 系统确认事件时间（非"人看到"）。

        Returns:
            float: 确认耗时（秒）。

        Raises:
            ValueError: 非法时间戳序列时抛出。
        """
        return MttxCalculator.elapsed_seconds(created_at, acked_at)

    @staticmethod
    def mttr(fault_ts: float, resolved_at: float) -> float:
        """MTTR 单次计时：故障注入时间戳 → 告警→恢复成对记录 resolved_at。

        Args:
            fault_ts: 故障发生时间戳。
            resolved_at: 告警 resolved 时间（健康恢复自动置 resolved）。

        Returns:
            float: 恢复耗时（秒）。

        Raises:
            ValueError: 非法时间戳序列时抛出。
        """
        return MttxCalculator.elapsed_seconds(fault_ts, resolved_at)

    @staticmethod
    def summarize(
        deltas: Sequence[Dict[str, Any]],
        kind: str = "mttd",
    ) -> Dict[str, Any]:
        """汇总 MTTx 样本：次数/平均/最大（用于周复盘与 FI 演练逐用例计时）。

        Args:
            deltas: 计时样本列表，每条为：
                {"seconds": float, "case_id": str | None}
            kind: 计时类型（mttd/mtta/mttr），仅用于输出标注。

        Returns:
            dict: {"kind", "samples", "avg_seconds", "max_seconds"}；
                空样本时 avg/max 为 None。
        """
        seconds: List[float] = []
        for sample in deltas:
            if not isinstance(sample, dict) or "seconds" not in sample:
                raise TypeError(f"计时样本必须是含 seconds 的 dict: {sample!r}")
            seconds.append(float(sample["seconds"]))
        return {
            "kind": kind,
            "samples": len(seconds),
            "avg_seconds": MttxCalculator.average(seconds),
            "max_seconds": round(max(seconds), 6) if seconds else None,
        }


# ═══════════════════════════════════════════════
#  ③ 标准故障注入用例集 FI-01~07（与 D-6 共用）
# ═══════════════════════════════════════════════

@dataclass(frozen=True)
class FICase:
    """标准故障注入用例（P0-8 ③ / D-6 混沌工程共用同一用例库）。

    Attributes:
        case_id: 用例 ID（FI-01 ~ FI-07）。
        fault_desc: 注入故障描述。
        expected_capture: 期望捕获项（对应监控器/检测器）。
        trigger_mode: 触发模式（EVENT / POLL / 元监控 / 外部通道）。
        expected_level: 期望告警级别。
        kpi: 对应验收 KPI（如 "MTTD<1min" / "MTTA<5min" / "KPI-8"）。
        timer_start: 计时起点（P0-8 ② 定义）。
        timer_end: 计时终点（P0-8 ② 定义）。
        inject_action: 注入动作描述（供演练脚本调用，D-6 最小可行版起步）。
    """

    case_id: str
    fault_desc: str
    expected_capture: str
    trigger_mode: str
    expected_level: str
    kpi: str
    timer_start: str
    timer_end: str
    inject_action: str

    def to_dict(self) -> Dict[str, str]:
        """序列化为字典（供 --list-fi 打印/JSON 输出）。"""
        return {
            "case_id": self.case_id,
            "fault_desc": self.fault_desc,
            "expected_capture": self.expected_capture,
            "trigger_mode": self.trigger_mode,
            "expected_level": self.expected_level,
            "kpi": self.kpi,
            "timer_start": self.timer_start,
            "timer_end": self.timer_end,
            "inject_action": self.inject_action,
        }


# FI-01~07 用例库（字段全部取自施工清单 P0-8 展开表，未自行定义）
FI_CASES: List[FICase] = [
    FICase(
        case_id="FI-01",
        fault_desc="行情源断流（交易时段停喂 bar）",
        expected_capture="A-4 StalenessDetector",
        trigger_mode="EVENT",
        expected_level="P0",
        kpi="MTTD<1min",
        timer_start="断流故障注入时间戳（事件时间）",
        timer_end="A-4 告警在 BASE-1 落库 created_at",
        inject_action="交易时段内停止向 data_feed 喂 bar（模拟行情源停摆）",
    ),
    FICase(
        case_id="FI-02",
        fault_desc="引擎僵死（get_status 无响应）",
        expected_capture="A-5 引擎心跳",
        trigger_mode="POLL（窗口<60s）",
        expected_level="P0",
        kpi="MTTD<1min",
        timer_start="引擎僵死故障注入时间戳",
        timer_end="心跳告警落库 created_at",
        inject_action="冻结引擎 get_status 响应（连续 N 次无响应触发僵死判定）",
    ),
    FICase(
        case_id="FI-03",
        fault_desc="券商连接断开",
        expected_capture="A-6 _check_broker",
        trigger_mode="EVENT",
        expected_level="P0",
        kpi="MTTD<1min",
        timer_start="券商断连故障注入时间戳",
        timer_end="连接状态变更告警落库 created_at",
        inject_action="断开券商连接（交易中断场景）并触发连接状态变更事件",
    ),
    FICase(
        case_id="FI-04",
        fault_desc="伪造拒单",
        expected_capture="A-8 ORDER_REJECTED",
        trigger_mode="EVENT",
        expected_level="ERROR/P1",
        kpi="MTTA<5min",
        timer_start="拒单事件注入时间戳",
        timer_end="告警落库 created_at（MTTA 另以 acked_at 计时确认）",
        inject_action="伪造一笔 ORDER_REJECTED 事件（含 symbol/原因）",
    ),
    FICase(
        case_id="FI-05",
        fault_desc="人为制造持仓差异",
        expected_capture="A-7 reconcile 差异",
        trigger_mode="EVENT",
        expected_level="CRITICAL",
        kpi="MTTR<30min",
        timer_start="持仓差异注入时间戳",
        timer_end="告警→恢复成对记录 resolved_at",
        inject_action="在券商侧人为修改持仓制造账实差异（实时容差=0 触发 CRITICAL）",
    ),
    FICase(
        case_id="FI-06",
        fault_desc="告警主通道故障",
        expected_capture="A-9 冗余/落盘兜底",
        trigger_mode="元监控",
        expected_level="元监控",
        kpi="KPI-8",
        timer_start="主通道故障注入时间戳",
        timer_end="冗余通道或落盘兜底完成时间",
        inject_action="停用告警主通道（验证 P0 至少 2 通道可达/未投递落盘）",
    ),
    FICase(
        case_id="FI-07",
        fault_desc="主进程 kill",
        expected_capture="BASE-8 外部心跳超时",
        trigger_mode="外部通道",
        expected_level="外部通道",
        kpi="KPI-8",
        timer_start="主进程 kill 时间戳",
        timer_end="外部独立载体收到存活告警时间（连续多次超时判定）",
        inject_action="kill 主进程（外部心跳连续多次超时升级，规避临界网络误报）",
    ),
]


def list_fi_cases() -> List[Dict[str, str]]:
    """返回 FI-01~07 用例库的字典列表（薄入口 --list-fi 与演练清单共用）。"""
    return [case.to_dict() for case in FI_CASES]


# ═══════════════════════════════════════════════
#  自测 Demo（__main__ 入口，可运行）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """运行 P0-8 统计模块自测：分类判定 + MTTx 计时 + FI 用例库。"""
    passed: int = 0
    failures: List[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name}: {detail}")

    # ── ① 分类判定 ──
    tp_alert: Dict[str, Any] = {
        "acked_disposition": "需处置",
        "auto_resolved": False,
    }
    fp_alert: Dict[str, Any] = {
        "acked_disposition": "误报",
        "auto_resolved": False,
    }
    fp_auto: Dict[str, Any] = {
        "acked_disposition": None,
        "auto_resolved": True,
        "meta_confirmed_no_event": True,
    }
    unk_alert: Dict[str, Any] = {
        "acked_disposition": None,
        "auto_resolved": False,
    }
    check("TP 判定", ClassificationStats.classify_alert(tp_alert) == "TP")
    check("FP 判定(人工标记)", ClassificationStats.classify_alert(fp_alert) == "FP")
    check("FP 判定(自动+元监控)", ClassificationStats.classify_alert(fp_auto) == "FP")
    check("UNCLASSIFIED 判定", ClassificationStats.classify_alert(unk_alert) == "UNCLASSIFIED")
    try:
        ClassificationStats.classify_alert("not-a-dict")  # type: ignore[arg-type]
        check("非 dict 输入报错", False, "未抛 TypeError")
    except TypeError:
        check("非 dict 输入报错", True)

    # ── 统计 ──
    stats = ClassificationStats.compute(
        [tp_alert, fp_alert, fp_auto, unk_alert],
        fi_records=[{"case_id": "FI-01", "captured": True}, {"case_id": "FI-02", "captured": False}],
    )
    check("TP 计数", stats["tp"] == 1, f"{stats}")
    check("FP 计数", stats["fp"] == 2, f"{stats}")
    check("FN 计数(FI 漏捕)", stats["fn"] == 1, f"{stats}")
    check("准确率 1/3", stats["accuracy"] == round(1 / 3, 6), f"{stats['accuracy']}")
    check("检出率 1/2", stats["detection_rate"] == 0.5, f"{stats['detection_rate']}")

    # ── ② MTTx 计时 ──
    mttd = MttxCalculator.mttd(fault_ts=1000.0, created_at=1055.0)
    check("MTTD 55s", mttd == 55.0, f"{mttd}")
    mtta = MttxCalculator.mtta(created_at=1055.0, acked_at=1200.0)
    check("MTTA 145s", mtta == 145.0, f"{mtta}")
    mttr = MttxCalculator.mttr(fault_ts=1000.0, resolved_at=2000.0)
    check("MTTR 1000s", mttr == 1000.0, f"{mttr}")
    try:
        MttxCalculator.mttd(fault_ts=2000.0, created_at=1000.0)
        check("终点早于起点报错", False, "未抛 ValueError")
    except ValueError:
        check("终点早于起点报错", True)
    avg = MttxCalculator.average([50.0, 60.0, 70.0])
    check("MTTx 平均 60s", avg == 60.0, f"{avg}")
    check("空序列平均 None", MttxCalculator.average([]) is None)

    # ── ③ FI 用例库 ──
    check("FI 用例数=7", len(FI_CASES) == 7, f"{len(FI_CASES)}")
    check("FI-01 触发模式 EVENT", FI_CASES[0].trigger_mode == "EVENT")
    check("FI-05 期望 CRITICAL", FI_CASES[4].expected_level == "CRITICAL")

    print(f"自测结果: {passed} 项通过, {len(failures)} 项失败")
    for fail in failures:
        print(f"  - {fail}")
    return len(failures)


if __name__ == "__main__":
    # 自测 Demo：分类判定 + MTTx 计时 + FI 用例库
    raise SystemExit(_run_self_test())
