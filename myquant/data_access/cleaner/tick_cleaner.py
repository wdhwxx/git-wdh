# 文件名：myquant/data_access/cleaner/tick_cleaner.py
"""
tick_cleaner.py - Tick逐笔数据清洗 (阶段9面向对象重构)
==========================================================
阶段9高级增强迭代：在v1普通函数基础上，新增 TickCleaner 类，继承 CleanerBase。

兼容性保证:
  - 旧函数 clean_tick_records() 完全保留，签名/返回值不变
  - TickCleaner.clean() 返回 CleanResult，支持三元组解包
  - 两者内部复用同一套校验逻辑，结果一致

清洗链路:
  1. 单条基础校验 (价格/成交量非负/bid<ask/时间戳/symbol)
  2. 重复检测 (同时间同价格同量三元组去重)
  3. 价格跳变检测
  4. 排序输出

输入格式: List[Dict]，每条包含 symbol/timestamp/price/volume/bid_price/ask_price 等
输出格式:
  - 旧函数: (cleaned: List[Dict], bad: List[Dict], stats: Dict)
  - 新类:   CleanResult (支持 cleaned, bad, stats = result 解包)

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（流式清洗/事件钩子/规则热更新）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 价格跳变只比较当前price与前一条price，不访问后续记录

防未来函数说明:
  - 单条校验只使用当前记录字段
  - 价格跳变检测只比较当前price与前一条price
  - 去重基于 (timestamp, price, volume) 三元组，不引入未来数据
  - 实盘流式清洗时，每条记录只与已到达的前一条记录比较
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger
from myquant.data_access.cleaner.base_cleaner import (
    CleanerBase,
    CleanRule,
    CleanResult,
    CleanerRegistry,
)

logger = get_logger("data_clean.tick")


# ── 默认配置 ──

DEFAULT_TICK_CONFIG: Dict[str, Any] = {
    "check_price_non_negative": True,
    "check_volume_non_negative": True,
    "check_bid_ask": True,
    "check_timestamp": True,
    "check_duplicate": True,
    "duplicate_strategy": "keep_last",   # keep_first | keep_last | drop_all
    "detect_price_jump": True,
    "max_price_jump_pct": 0.22,
    "sort_by_timestamp": True,
}


def _merge_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并用户配置与默认配置"""
    merged = dict(DEFAULT_TICK_CONFIG)
    if config:
        merged.update(config)
    return merged


def _get_timestamp(record: Dict[str, Any]) -> float:
    """从记录中提取时间戳"""
    ts = record.get("timestamp")
    if ts is not None:
        try:
            return float(ts)
        except (TypeError, ValueError):
            pass
    return 0.0


def _validate_single_tick(
    record: Dict[str, Any],
    config: Dict[str, Any],
) -> List[str]:
    """
    单条Tick基础校验，返回错误列表
    """
    # #E2修复：config 防御性检查，防止 None 导致 AttributeError
    config = config or {}
    errors: List[str] = []

    # symbol 校验
    symbol = record.get("symbol", "")
    if not symbol or not str(symbol).strip():
        errors.append("symbol 为空")

    # 时间戳校验
    if config.get("check_timestamp", True):
        ts = _get_timestamp(record)
        if ts <= 0:
            errors.append(f"时间戳无效: {record.get('timestamp')}")

    # 价格非负
    if config.get("check_price_non_negative", True):
        price = record.get("price")
        if price is not None:
            try:
                if float(price) < 0:
                    errors.append(f"price 为负: {price}")
            except (TypeError, ValueError):
                errors.append(f"price 无法转为数值: {price}")

    # 成交量非负
    if config.get("check_volume_non_negative", True):
        vol = record.get("volume")
        if vol is not None:
            try:
                if float(vol) < 0:
                    errors.append(f"volume 为负: {vol}")
            except (TypeError, ValueError):
                errors.append(f"volume 无法转为数值: {vol}")

    # bid/ask 校验
    if config.get("check_bid_ask", True):
        bid = record.get("bid_price")
        ask = record.get("ask_price")
        if bid is not None and ask is not None:
            try:
                bid_f = float(bid)
                ask_f = float(ask)
                if bid_f > 0 and ask_f > 0 and bid_f >= ask_f:
                    errors.append(f"bid({bid_f}) >= ask({ask_f}) 异常")
            except (TypeError, ValueError):
                errors.append(f"bid/ask 无法转为数值: bid={bid}, ask={ask}")

    return errors


def _deduplicate_ticks(
    records: List[Dict[str, Any]],
    strategy: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Tick去重：按 (timestamp, price, volume) 三元组去重
    """
    if not records:
        return records, 0

    seen: Dict[Tuple[float, float, float], Dict[str, Any]] = {}
    removed = 0

    for rec in records:
        key = (
            _get_timestamp(rec),
            float(rec.get("price", 0)),
            float(rec.get("volume", 0)),
        )
        if key in seen:
            removed += 1
            if strategy == "keep_last":
                seen[key] = rec
            # keep_first: 保留已有的
            # drop_all: 会在后面处理
        else:
            seen[key] = rec

    if strategy == "drop_all":
        # 需要找出所有重复的key并全部移除
        from collections import Counter
        key_counts: Dict[Tuple, int] = {}
        for rec in records:
            key = (
                _get_timestamp(rec),
                float(rec.get("price", 0)),
                float(rec.get("volume", 0)),
            )
            key_counts[key] = key_counts.get(key, 0) + 1
        result = [rec for key, rec in seen.items() if key_counts.get(key, 0) == 1]
        # #C1修复：drop_all 策略下，被移除的记录数 = 所有重复key的记录总数
        # 原代码 * 0 导致 removed 永远为0，统计失真
        removed = sum(c for k, c in key_counts.items() if c > 1)
        return result, removed

    return list(seen.values()), removed


def _detect_price_jumps(
    records: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> int:
    """
    检测价格跳变，标记 _warnings，返回跳变次数

    #C4修复：增加 max_time_gap_sec 时间间隔检查。
    若两条 Tick 间隔超过阈值（如隔夜/休市），价格跳变是正常的，
    跳过跳变检测并重置参考价，避免误报。

    【防未来函数】只比较当前记录与前一条记录，不访问未来数据。
    """
    if not records or not config.get("detect_price_jump", True):
        return 0

    count = 0
    prev_price = None
    prev_ts = None
    threshold = config.get("max_price_jump_pct", 0.22)
    # #C4修复：最大时间间隔阈值（秒），超过则跳过跳变检测
    # 默认3600秒（1小时），可根据交易时段调整（如A股4小时=14400）
    max_time_gap_sec = config.get("max_time_gap_sec", 3600)

    for rec in records:
        price = rec.get("price")
        cur_ts = _get_timestamp(rec)

        # #C4修复：时间间隔检查
        time_gap = None
        if prev_ts is not None and cur_ts is not None:
            time_gap = cur_ts - prev_ts
            # 间隔超过阈值：跳过跳变检测，重置参考价（隔夜/休市场景）
            if time_gap > max_time_gap_sec:
                prev_price = None
                prev_ts = cur_ts if price is not None else prev_ts
                if price is not None:
                    prev_price = float(price)
                continue

        if price is not None and prev_price is not None and prev_price > 0:
            try:
                change_pct = abs(float(price) - float(prev_price)) / float(prev_price)
                if change_pct > threshold:
                    warnings = rec.setdefault("_warnings", [])
                    gap_info = f", 间隔={time_gap:.0f}s" if time_gap is not None else ""
                    msg = f"Tick价格跳变 {change_pct:.2%} (price={price}, prev={prev_price}{gap_info})"
                    warnings.append(msg)
                    count += 1
                    logger.warning(msg)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if price is not None:
            prev_price = float(price)
            prev_ts = cur_ts

    return count


def clean_tick_records(
    records: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    清洗Tick逐笔数据 (核心入口函数)

    Parameters
    ----------
    records : List[Dict]
        原始Tick数据列表
    config : Dict | None
        清洗配置（tick 部分），None 则使用默认配置

    Returns
    -------
    Tuple[List[Dict], List[Dict], Dict]
      cleaned: 清洗后有效数据
      bad:     脏数据（附加 _errors 字段）
      stats:   清洗统计
    """
    import time
    start_time = time.time()
    cfg = _merge_config(config)

    total = len(records)
    if total == 0:
        logger.info("输入Tick数据为空，跳过清洗")
        return [], [], {
            "total": 0, "valid": 0, "bad": 0,
            "removed_duplicates": 0, "price_jumps": 0,
            "duration_sec": 0.0,
        }

    logger.info(f"开始清洗Tick数据: 共 {total} 条")

    # ═══════════════════════════════════════════
    # 步骤1: 单条基础校验
    # ═══════════════════════════════════════════
    valid: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []

    for rec in records:
        errors = _validate_single_tick(rec, cfg)
        if errors:
            bad_rec = dict(rec)
            bad_rec["_errors"] = errors
            bad.append(bad_rec)
            for err in errors:
                logger.debug(f"脏Tick: {err} | record={rec}")
        else:
            valid.append(rec)

    logger.info(f"单条校验完成: 有效 {len(valid)}, 脏数据 {len(bad)}")

    # ═══════════════════════════════════════════
    # 步骤2: 去重
    # ═══════════════════════════════════════════
    removed_duplicates = 0
    if cfg.get("check_duplicate", True):
        valid, removed_duplicates = _deduplicate_ticks(
            valid, cfg.get("duplicate_strategy", "keep_last")
        )
        if removed_duplicates > 0:
            logger.info(f"去重完成: 移除 {removed_duplicates} 条重复Tick")

    # ═══════════════════════════════════════════
    # 步骤3: 价格跳变检测 (需先排序)
    # ═══════════════════════════════════════════
    if cfg.get("sort_by_timestamp", True):
        valid.sort(key=_get_timestamp)

    price_jumps = _detect_price_jumps(valid, cfg)

    # ═══════════════════════════════════════════
    # 步骤4: 最终排序 (确保)
    # ═══════════════════════════════════════════
    if cfg.get("sort_by_timestamp", True):
        valid.sort(key=_get_timestamp)

    duration = time.time() - start_time
    stats = {
        "total": total,
        "valid": len(valid),
        "bad": len(bad),
        "removed_duplicates": removed_duplicates,
        "price_jumps": price_jumps,
        "duration_sec": round(duration, 4),
    }

    logger.info(
        f"Tick清洗完成: 有效={stats['valid']}, 脏数据={stats['bad']}, "
        f"去重={stats['removed_duplicates']}, 耗时={stats['duration_sec']}s"
    )

    return valid, bad, stats


# ═══════════════════════════════════════════════
#  TickCleaner 面向对象清洗器 (阶段9新增)
# ═══════════════════════════════════════════════

class TickCleaner(CleanerBase):
    """
    Tick逐笔数据清洗器（继承 CleanerBase）

    复用v1普通函数的校验逻辑，提供面向对象接口：
      - 规则管理（add_rule/remove_rule/enable_rule/disable_rule）
      - 事件钩子（on_clean_start/on_clean_complete/on_bad_record/...）
      - 配置热更新（update_config/reset_config）
      - 插件式注册（CleanerRegistry）

    使用方式:
        # 方式1：面向对象
        cleaner = TickCleaner(config={"max_price_jump_pct": 0.2})
        result = cleaner.clean(raw_ticks)
        cleaned, bad, stats = result  # 三元组解包

        # 方式2：旧函数（完全兼容）
        cleaned, bad, stats = clean_tick_records(raw_ticks, config={...})

    为实盘无缝对接预留:
      - 流式清洗：逐条调用 clean_single()，实盘Tick到达时实时校验
      - 事件钩子：on_bad_record 可触发实时告警
      - 规则热更新：实盘运行中动态调整清洗阈值
      - 自定义规则：CleanRule.check_fn 注入业务特定校验（如涨跌停/停牌检测）
    """

    @property
    def data_type(self) -> str:
        """数据类型标识"""
        return "tick"

    def _default_config(self) -> Dict[str, Any]:
        """返回默认配置（复用 DEFAULT_TICK_CONFIG）"""
        return dict(DEFAULT_TICK_CONFIG)

    def _validate_single(
        self,
        record: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[str]:
        """
        单条Tick基础校验（复用 _validate_single_tick）

        校验项:
          - symbol 非空
          - 时间戳有效
          - 价格非负
          - 成交量非负
          - bid < ask（买卖盘价格逻辑）
        """
        return _validate_single_tick(record, config)

    def _deduplicate(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Tick去重：按 (timestamp, price, volume) 三元组去重

        策略: keep_first | keep_last | drop_all
        """
        strategy = config.get("duplicate_strategy", "keep_last")
        return _deduplicate_ticks(records, strategy)

    def _detect_anomalies(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
        """
        异常检测：Tick价格跳变（复用 _detect_price_jumps）

        注意：只比较当前price与前一条price，严禁访问后续记录（防未来函数）。
        异常记录标记 _warnings 字段（不剔除，仅标记）。返回三元组保持接口统一，
        tick 暂不提供 drop 剔除能力（bad 恒为空）。
        """
        count = _detect_price_jumps(records, config)
        return records, [], {"price_jump": count}

    def _sort_output(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """按时间戳升序排序"""
        if config.get("sort_by_timestamp", True):
            records.sort(key=_get_timestamp)
        return records

    def clean_single(
        self,
        record: Dict[str, Any],
        prev_record: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, List[str], List[str]]:
        """
        流式单条清洗（为实盘实时Tick设计）

        只校验单条记录，可选与前一条记录做价格跳变检测。
        不做去重和排序（实盘场景由调用方管理）。

        Parameters
        ----------
        record : Dict
            当前Tick记录
        prev_record : Dict | None
            前一条Tick记录（用于价格跳变检测）

        Returns
        -------
        (is_valid, errors, warnings)
          is_valid : 是否通过校验
          errors : 错误列表（非空则为脏数据）
          warnings : 警告列表（异常标记，不视为脏数据）
        """
        errors = self._validate_single(record, self._config)
        warnings: List[str] = []

        if not errors and prev_record is not None:
            # 价格跳变检测
            if self._config.get("detect_price_jump", True):
                prev_price = prev_record.get("price")
                curr_price = record.get("price")
                if prev_price is not None and curr_price is not None and float(prev_price) > 0:
                    try:
                        change_pct = abs(float(curr_price) - float(prev_price)) / float(prev_price)
                        threshold = self._config.get("max_price_jump_pct", 0.22)
                        if change_pct > threshold:
                            msg = f"Tick价格跳变 {change_pct:.2%} (price={curr_price}, prev={prev_price})"
                            warnings.append(msg)
                            logger.warning(msg)
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass

        is_valid = len(errors) == 0
        return is_valid, errors, warnings


# ═══════════════════════════════════════════════
#  注册到全局清洗器注册器
# ═══════════════════════════════════════════════

def _register_tick_cleaner() -> None:
    """注册 TickCleaner 到全局清洗器注册器（模块加载时自动执行）"""
    try:
        registry = CleanerRegistry()
        if not registry.has("tick"):
            registry.register("tick", TickCleaner)
            logger.debug("TickCleaner 已注册到 CleanerRegistry (type=tick)")
    except Exception as e:
        logger.warning(f"TickCleaner 注册失败: {e}")


# 模块加载时自动注册
_register_tick_cleaner()
