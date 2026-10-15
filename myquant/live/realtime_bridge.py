# 文件名：myquant/live/realtime_bridge.py
"""
realtime_bridge.py - 实时清洗→事件总线联动桥（B项核心）
=========================================================
实盘实时流式清洗检出脏数据后，经【窗口聚合 + 连续阈值升级】发布 DataQualityEvent，
自动联动 AlertManager（分级告警）/ MetricsCollector（脏数据占比）/ RiskManager（严重脏数据自动暂停）。

核心能力:
  1. 窗口聚合 — 同 (symbol, dirty_type) 在 aggregate_window 秒内合并为一条
     DataQualityEvent（bad_count/total_count 累加），窗口翻转才发布
     —— 避免每 tick 刷事件导致事件风暴/告警风暴。
  2. 连续阈值升级 — 严重类型（OHLC非法/负价/负量/时间戳无效/缺symbol）
     连续发生 ≥ critical_after 次才上报 critical；单次仅 warning
     —— 过滤瞬时抖动，避免单 tick 误报触发风控暂停。
  3. 线程安全 — 轮询在后台线程，聚合/计数状态加锁；发布在锁外执行。
  4. clean streak — 连续干净 bar 计数（供观测；恢复仍由任务12机制负责）。
  5. 故障隔离 — 事件发布失败不影响行情分发（try/except 包裹）。

设计原则:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md，live/ 模块群，
     实时桥层归 realtime_bridge.py，回写#423）
  2. 防未来函数: 事件时间戳=窗口检出时刻（publish_data_quality 内部取当前时间），
     恒≤当前时间；不含任何未来数据。
  3. 无反向 import 环: 只依赖 stdlib + data_access.cleaner.error_codes + core.logger，
     被 data_feed.py 单向依赖。

使用方式（通常通过 MarketDataFeed.attach_event_bus 创建）:
    bridge = _RealtimeEventBridge(event_bus=bus)
    feed.register_dirty_callback(bridge.on_dirty)   # 脏数据入口
    bridge.on_clean(symbol)                          # 干净bar计数
    bridge.flush_all()                               # 停止时补发

依赖: typing, time, threading, myquant.core.logger, myquant.data_access.cleaner.error_codes
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

from myquant.core.logger import get_logger
from myquant.data_access.cleaner.error_codes import (
    infer_dirty_type_from_errors,
)

logger = get_logger("live.realtime_bridge")


class _RealtimeEventBridge:
    """
    实时流式清洗 → 事件总线联动桥（B项核心）

    职责:
      1. 窗口聚合：同 (symbol, dirty_type) 在 aggregate_window 秒内合并为一条
         DataQualityEvent（bad_count/total_count 累加），窗口翻转才发布
         —— 避免每 tick 刷事件导致事件风暴/告警风暴。
      2. 连续阈值升级：严重类型（OHLC非法/负价/负量/时间戳无效/缺symbol）
         连续发生 ≥ critical_after 次才上报 critical；单次仅 warning
         —— 过滤瞬时抖动，避免单 tick 误报触发风控暂停。
      3. 线程安全：轮询在后台线程，聚合/计数状态加锁；发布在锁外执行。
      4. clean streak：连续干净 bar 计数（供观测；恢复仍由任务12机制负责）。
      5. 故障隔离：事件发布失败不影响行情分发（try/except 包裹）。

    【防未来函数】事件时间戳=窗口检出时刻（publish_data_quality 内部取当前时间），
    恒≤当前时间；不含任何未来数据。

    使用方式（通常通过 MarketDataFeed.attach_event_bus 创建）:
        bridge = _RealtimeEventBridge(event_bus=bus)
        feed.register_dirty_callback(bridge.on_dirty)   # 脏数据入口
        bridge.on_clean(symbol)                          # 干净bar计数
        bridge.flush_all()                               # 停止时补发
    """

    # 严重错误类型（连续达到 critical_after 才升级为 critical）
    _CRITICAL_TYPES = frozenset({
        "ohlc_logic_error",
        "negative_price",
        "negative_volume",
        "invalid_timestamp",
        "missing_symbol",
    })
    # 直接 warning 类型（不升级，真实行情可能发生）
    _WARNING_TYPES = frozenset({
        "price_jump",
        "volume_anomaly",
        "zero_price",
        "zero_volume",
        "time_monotonic_error",
        "duplicate_timestamp",
        "duplicate_tick",
        "bid_ask_cross",
    })

    def __init__(
        self,
        event_bus: Any,
        source: str = "live",
        severity_map: Optional[Dict[str, str]] = None,
        aggregate_window: int = 60,
        critical_after: int = 3,
        enabled: bool = True,
    ):
        """
        初始化联动桥

        Parameters
        ----------
        event_bus : EventBus
            事件总线实例（需有 publish_data_quality 方法）
        source : str
            事件来源标识（默认 "live"）
        severity_map : Dict[str, str] | None
            自定义 dirty_type → severity 覆盖
        aggregate_window : int
            聚合窗口秒数（≥1）
        critical_after : int
            严重类型连续次数阈值（≥1）
        enabled : bool
            是否启用发布
        """
        self._bus = event_bus
        self._source = str(source)
        self._severity_map: Dict[str, str] = dict(severity_map or {})
        self._window = max(1, int(aggregate_window))
        self._critical_after = max(1, int(critical_after))
        self._enabled = bool(enabled)
        self._lock = threading.Lock()

        # 聚合槽: (symbol, dirty_type) -> {window_start, bad_count, total_count, errors, record}
        self._buckets: Dict[tuple, Dict[str, Any]] = {}
        # 连续脏数据计数: (symbol, dirty_type) -> int（跨窗口累计，干净bar清零）
        self._consecutive: Dict[tuple, int] = {}
        # 连续干净bar计数: symbol -> int（供观测）
        self._clean_streak: Dict[str, int] = {}
        # 已发布事件数
        self._published_count = 0

    # ═══════════════════════════════════════════
    #  入口
    # ═══════════════════════════════════════════

    def on_dirty(self, symbol: str, bar: Dict[str, Any], errors: List[str]) -> None:
        """
        脏数据入口（被 register_dirty_callback 调用）

        推断 dirty_type → 窗口聚合 → 窗口到期则 flush 发布。
        发布在锁外执行，避免持锁调用外部总线。
        """
        if not self._enabled:
            return
        dirty_type = self._infer_dirty_type(errors, bar)
        now = time.time()
        key = (symbol, dirty_type)
        to_flush: Optional[tuple] = None

        with self._lock:
            # 干净 streak 清零
            self._clean_streak[symbol] = 0
            # 连续计数（跨窗口累计）
            self._consecutive[key] = self._consecutive.get(key, 0) + 1

            bucket = self._buckets.get(key)
            if bucket is None or (now - bucket["window_start"]) >= self._window:
                # 旧窗口到期 → 取出待发布（锁外发布）
                if bucket is not None and bucket.get("bad_count", 0) > 0:
                    to_flush = (key, bucket)
                # 开新窗口
                bucket = {
                    "window_start": now,
                    "bad_count": 0,
                    "total_count": 0,
                    "errors": [],
                    "record": None,
                }
                self._buckets[key] = bucket

            bucket["bad_count"] += 1
            bucket["total_count"] += 1
            bucket["errors"] = list(errors)
            bucket["record"] = dict(bar) if bar else None

        # 锁外发布旧窗口
        if to_flush is not None:
            self._flush_bucket(to_flush[0], to_flush[1])

    def on_clean(self, symbol: str) -> None:
        """干净 bar 入口（连续脏数据计数清零 + clean streak 累加）"""
        if not self._enabled:
            return
        with self._lock:
            self._clean_streak[symbol] = self._clean_streak.get(symbol, 0) + 1
            # 该标的所有 dirty_type 连续计数清零
            for key in list(self._consecutive.keys()):
                if key[0] == symbol:
                    self._consecutive[key] = 0

    def flush_all(self) -> None:
        """强制 flush 所有窗口（停止/断开时补发未发布事件）"""
        if not self._enabled:
            return
        pending: list = []
        with self._lock:
            for key, bucket in list(self._buckets.items()):
                if bucket.get("bad_count", 0) > 0:
                    pending.append((key, bucket))
            self._buckets.clear()
        for key, bucket in pending:
            self._flush_bucket(key, bucket)

    # ═══════════════════════════════════════════
    #  发布
    # ═══════════════════════════════════════════

    def _flush_bucket(self, key: tuple, bucket: Dict[str, Any]) -> None:
        """
        发布一个聚合窗口为 DataQualityEvent

        严重度判定：
          - severity_map 自定义覆盖优先
          - 严重类型且连续计数 ≥ critical_after → critical
          - 其余 → warning
        """
        symbol, dirty_type = key
        with self._lock:
            consecutive = self._consecutive.get(key, 0)

        severity = self._severity_map.get(dirty_type)
        if severity is None:
            if dirty_type in self._CRITICAL_TYPES and consecutive >= self._critical_after:
                severity = "critical"
            else:
                severity = "warning"

        try:
            self._bus.publish_data_quality(
                dirty_type=dirty_type,
                symbol=symbol,
                data_type="bar",
                severity=severity,
                errors=list(bucket.get("errors", [])),
                record=dict(bucket["record"]) if bucket.get("record") else None,
                bad_count=int(bucket.get("bad_count", 0)),
                total_count=int(bucket.get("total_count", 0)),
                source=self._source,
                source_step="realtime_feed",
            )
            self._published_count += 1
            logger.debug(
                f"[realtime_event] 已发布: {symbol} {dirty_type} "
                f"severity={severity} bad={bucket.get('bad_count')} "
                f"consecutive={consecutive}"
            )
        except Exception as e:
            logger.error(f"[realtime_event] 发布DataQualityEvent失败: {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════
    #  dirty_type 推断
    # ═══════════════════════════════════════════

    @staticmethod
    @staticmethod
    def _infer_dirty_type(errors: List[str], bar: Optional[Dict[str, Any]]) -> str:
        """
        从清洗错误推断 dirty_type（对齐任务6 DirtyDataType 枚举）

        #E3修复：优先解析结构化错误码前缀 "[ERROR_CODE] message" 精确匹配；
        若无错误码前缀（旧格式/跳变检测等动态生成的错误），回退到文本模糊匹配。

        匹配优先级：OHLC > 跳变 > 负价/负量 > 零价/零量 > 缺symbol >
                   时间戳 > 单调 > 重复 > 量异常 > unknown
        """
        # #E3修复：使用统一的错误码推断函数（结构化优先 + 模糊匹配回退）
        return infer_dirty_type_from_errors(errors)

    # ═══════════════════════════════════════════
    #  观测接口
    # ═══════════════════════════════════════════

    @property
    def published_count(self) -> int:
        """已发布事件数"""
        return self._published_count

    @property
    def clean_streak(self) -> Dict[str, int]:
        """各标的连续干净 bar 计数（快照）"""
        with self._lock:
            return dict(self._clean_streak)

    @property
    def consecutive_dirty(self) -> Dict[str, int]:
        """各 (symbol, dirty_type) 连续脏数据计数（快照）"""
        with self._lock:
            return {f"{k[0]}:{k[1]}": v for k, v in self._consecutive.items() if v > 0}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """运行时开关发布（灰度/调试）"""
        self._enabled = bool(enabled)

    def __repr__(self) -> str:
        return (
            f"_RealtimeEventBridge(source={self._source}, "
            f"window={self._window}s, critical_after={self._critical_after}, "
            f"published={self._published_count}, enabled={self._enabled})"
        )
