# 文件名：myquant/data_access/cleaner/bar_cleaner.py
"""
bar_cleaner.py - K线Bar数据清洗 (阶段9面向对象重构)
======================================================
阶段9高级增强迭代：在v1普通函数基础上，新增 BarCleaner 类，继承 CleanerBase。

兼容性保证:
  - 旧函数 clean_bar_records() 完全保留，签名/返回值不变
  - BarCleaner.clean() 返回 CleanResult，支持三元组解包
  - 两者内部复用同一套校验逻辑，结果一致

清洗链路:
  1. 单条基础校验 (OHLC逻辑/非负/symbol/时间)
  2. 时间序列校验 (单调性/重复时间戳)
  3. 异常值检测 (价格跳变/成交量异常)
  4. 去重处理 (按配置策略)
  5. 排序输出

输入格式: List[Dict]，每条包含 date/open/high/low/close/volume/amount 等
         (与 MarketDataResult.data 格式一致)

输出格式:
  - 旧函数: (cleaned: List[Dict], bad: List[Dict], stats: Dict)
  - 新类:   CleanResult (支持 cleaned, bad, stats = result 解包)

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（流式清洗/事件钩子/规则热更新）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 序列校验只使用前一条记录，不访问后续记录

防未来函数说明:
  - 单条校验只使用当前记录字段
  - 价格跳变检测只比较当前open与前一条close
  - 成交量异常检测只比较当前volume与前一条volume
  - 时间单调性检查只比较当前timestamp与前一条timestamp
  - 实盘流式清洗时，每条记录只与已到达的历史记录比较
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from myquant.core.logger import get_logger
from myquant.utils.time_utils import parse_datetime, to_timestamp
from myquant.data_access.cleaner.base_cleaner import (
    CleanerBase,
    CleanRule,
    CleanResult,
    CleanerRegistry,
)
from myquant.data_access.cleaner.error_codes import (
    ErrorCode,
    format_error,
    infer_dirty_type_from_errors,
    errors_to_strings,
    parse_error_code,
)

logger = get_logger("data_clean.bar")


# ── 默认配置 ──

DEFAULT_BAR_CONFIG: Dict[str, Any] = {
    "check_ohlc_logic": True,
    "check_non_negative": True,
    "check_symbol": True,
    "check_timestamp": True,
    "check_time_monotonic": True,
    "check_duplicate_timestamp": True,
    "duplicate_strategy": "keep_last",   # keep_first | keep_last | drop_all
    "detect_price_jump": True,
    "max_price_jump_pct": 0.22,
    "detect_volume_anomaly": True,
    "max_volume_ratio": 100.0,
    "allow_zero_volume": True,
    "price_jump_action": "mark",      # mark=仅标记_warnings | drop=整行剔除进bad
    "volume_anomaly_action": "mark",  # mark=仅标记_warnings | drop=整行剔除进bad
    "reset_base_gap_days": 30,        # 相邻记录间隔超过N天，自动重置跳变检测基准（应对长期停牌/重组）
    "fill_missing": False,
    "fill_strategy": "ffill",
    "sort_by_timestamp": True,
}


def _merge_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """合并用户配置与默认配置"""
    merged = dict(DEFAULT_BAR_CONFIG)
    if config:
        merged.update(config)
    return merged


def _get_timestamp(record: Dict[str, Any]) -> float:
    """
    从记录中提取时间戳
    优先使用 timestamp 字段，其次解析 date 字段
    """
    ts = record.get("timestamp")
    if ts is not None:
        try:
            return float(ts)
        except (TypeError, ValueError) as e:
            logger.debug("timestamp 字段非数值容错，尝试 date 解析: %s", e)
    date_str = record.get("date") or record.get("trade_date") or record.get("datetime")
    if date_str:
        try:
            return to_timestamp(date_str)
        except (ValueError, TypeError):
            pass
    return 0.0


def _validate_single_bar(
    record: Dict[str, Any],
    config: Dict[str, Any],
) -> List[str]:
    """
    单条Bar基础校验，返回错误列表

    #E3修复：错误信息使用结构化错误码前缀 "[ERROR_CODE] message"，
    便于事件桥精确解析 dirty_type，不再依赖脆弱的文本模糊匹配。

    校验项:
      - OHLC 逻辑: high >= max(open,close), low <= min(open,close), high >= low
      - 非负: 价格/成交量 >= 0
      - symbol 非空
      - 时间戳有效
    """
    # #E2修复：config 防御性检查，防止 None 导致 AttributeError
    config = config or {}
    errors: List[str] = []

    # symbol 校验
    if config.get("check_symbol", True):
        symbol = record.get("symbol", "")
        if not symbol or not str(symbol).strip():
            errors.append(format_error(ErrorCode.MISSING_SYMBOL, "symbol 为空"))

    # 时间戳校验
    if config.get("check_timestamp", True):
        ts = _get_timestamp(record)
        if ts <= 0:
            errors.append(format_error(
                ErrorCode.INVALID_TIMESTAMP,
                f"时间戳无效: timestamp={record.get('timestamp')}, date={record.get('date')}"
            ))

    # 非负校验
    if config.get("check_non_negative", True):
        for field in ("open", "high", "low", "close"):
            val = record.get(field)
            if val is not None:
                try:
                    if float(val) < 0:
                        errors.append(format_error(ErrorCode.NEGATIVE_PRICE, f"{field} 为负: {val}"))
                except (TypeError, ValueError):
                    errors.append(format_error(ErrorCode.PRICE_NOT_NUMBER, f"{field} 无法转为数值: {val}"))
        vol = record.get("volume")
        if vol is not None:
            try:
                if float(vol) < 0:
                    errors.append(format_error(ErrorCode.NEGATIVE_VOLUME, f"volume 为负: {vol}"))
            except (TypeError, ValueError):
                errors.append(format_error(ErrorCode.VOLUME_NOT_NUMBER, f"volume 无法转为数值: {vol}"))

    # OHLC 逻辑校验
    if config.get("check_ohlc_logic", True):
        try:
            o = float(record.get("open", 0))
            h = float(record.get("high", 0))
            l = float(record.get("low", 0))
            c = float(record.get("close", 0))
            # #C3修复：OHLC全0处理
            # 全0记录通常是停牌占位，默认允许（向后兼容）；
            # 可通过 allow_all_zero=False 拒绝（交易信号场景推荐）
            all_zero = all(p == 0 for p in (o, h, l, c))
            if all_zero:
                if not config.get("allow_all_zero", True):
                    errors.append(format_error(
                        ErrorCode.ALL_ZERO_FORBIDDEN,
                        "OHLC全为0且配置不允许（allow_all_zero=False）"
                    ))
                # 全0记录跳过OHLC逻辑校验（无意义）
            else:
                # 非全0记录：执行OHLC逻辑校验
                if h < l:
                    errors.append(format_error(ErrorCode.OHLC_HIGH_LOW, f"high({h}) < low({l})"))
                if h < max(o, c):
                    errors.append(format_error(
                        ErrorCode.OHLC_HIGH_OPEN_CLOSE,
                        f"high({h}) < max(open={o}, close={c})"
                    ))
                if l > min(o, c):
                    errors.append(format_error(
                        ErrorCode.OHLC_LOW_OPEN_CLOSE,
                        f"low({l}) > min(open={o}, close={c})"
                    ))
        except (TypeError, ValueError):
            errors.append(format_error(ErrorCode.OHLC_PARSE_ERROR, "OHLC 字段包含无法转换的数值"))

    # 成交量为0警告（不视为错误，除非配置禁止）
    if not config.get("allow_zero_volume", True):
        vol = record.get("volume")
        if vol is not None and float(vol) == 0:
            errors.append(format_error(ErrorCode.ZERO_VOLUME_FORBIDDEN, "volume 为0且配置不允许"))

    return errors


def _detect_sequence_anomalies(
    records: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    """
    序列级异常检测：价格跳变、成交量异常

    处理策略（可配置，默认 mark 向后兼容）:
      - action=mark（默认）: 异常记录仅标记 _warnings 字段，保留在有效数据中
      - action=drop       : 异常记录整行剔除，移入 bad（带 [PRICE_JUMP]/[VOLUME_ANOMALY] 错误码）

    重置基准机制（reset_base_gap_days）:
      - 相邻两条记录时间间隔超过 N 天（默认30天）时，自动重置跳变检测基准
      - 应对长期停牌/重组导致的价格跳变（如000028案例：停牌数年复牌后价格跳变）
      - 重置时仅记录警告，不判定为异常，不剔除数据

    注意: 此函数在去重和排序之后调用，records 已按时间升序排列；
          被剔除的记录不作为后续比较基准（prev 不更新），防未来函数。

    Returns
    -------
    (kept_records, bad_records, anomaly_counts)
      kept_records : 保留记录（标记或剔除后）
      bad_records  : action=drop 时被剔除的异常记录（带 _errors）
      anomaly_counts: {price_jump: N, volume_anomaly: N}
    """
    counts = {"price_jump": 0, "volume_anomaly": 0}
    if not records:
        return records, [], counts

    price_action = str(config.get("price_jump_action", "mark")).lower()
    volume_action = str(config.get("volume_anomaly_action", "mark")).lower()

    kept: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    prev = None
    reset_gap_days = float(config.get("reset_base_gap_days", 30))
    reset_gap_seconds = reset_gap_days * 86400  # 30天 = 2592000秒

    for rec in records:
        warnings = rec.setdefault("_warnings", [])
        drop_errors: List[str] = []

        if prev is not None:
            # ★ 重置基准检测：相邻记录时间间隔超过阈值时，跳过跳变检测
            # 应对长期停牌/重组导致的价格跳变（如000028案例）
            prev_ts = _get_timestamp(prev)
            curr_ts = _get_timestamp(rec)
            gap_seconds = curr_ts - prev_ts
            need_reset_base = (
                reset_gap_days > 0
                and gap_seconds > reset_gap_seconds
                and prev_ts > 0
                and curr_ts > 0
            )

            if not need_reset_base:
                # 价格跳变检测
                if config.get("detect_price_jump", True):
                    prev_close = prev.get("close")
                    curr_open = rec.get("open")
                    if prev_close and curr_open and float(prev_close) > 0:
                        try:
                            change_pct = abs(float(curr_open) - float(prev_close)) / float(prev_close)
                            if change_pct > config.get("max_price_jump_pct", 0.22):
                                msg = f"价格跳变 {change_pct:.2%} (open={curr_open}, prev_close={prev_close})"
                                counts["price_jump"] += 1
                                if price_action == "drop":
                                    drop_errors.append(format_error(ErrorCode.PRICE_JUMP, msg))
                                else:
                                    warnings.append(msg)
                                logger.warning(msg)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass

                # 成交量异常检测
                if config.get("detect_volume_anomaly", True):
                    prev_vol = prev.get("volume")
                    curr_vol = rec.get("volume")
                    if prev_vol and curr_vol and float(prev_vol) > 0:
                        try:
                            ratio = float(curr_vol) / float(prev_vol)
                            if ratio > config.get("max_volume_ratio", 100.0):
                                msg = f"成交量异常放大 {ratio:.1f}x (curr={curr_vol}, prev={prev_vol})"
                                counts["volume_anomaly"] += 1
                                if volume_action == "drop":
                                    drop_errors.append(format_error(ErrorCode.VOLUME_ANOMALY, msg))
                                else:
                                    warnings.append(msg)
                                logger.warning(msg)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
            else:
                # 时间间隔过长，重置基准，记录警告但不判定为异常
                gap_days = gap_seconds / 86400
                reset_msg = (
                    f"时间间隔 {gap_days:.1f} 天超过阈值 {reset_gap_days:.0f} 天，"
                    f"重置跳变检测基准"
                )
                warnings.append(reset_msg)
                logger.info(reset_msg)

        if drop_errors:
            bad_rec = dict(rec)
            bad_rec["_errors"] = drop_errors
            bad_rec.pop("_warnings", None)  # 剔除记录以错误码为准
            bad.append(bad_rec)
            # 剔除记录不作为后续比较基准（prev 不更新）
        else:
            kept.append(rec)
            prev = rec

    return kept, bad, counts


def _deduplicate_by_timestamp(
    records: List[Dict[str, Any]],
    strategy: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    按时间戳去重

    Parameters
    ----------
    strategy : str
      keep_first: 保留第一条
      keep_last:  保留最后一条
      drop_all:   全部丢弃重复的

    Returns
    -------
    (deduped, removed_count)
    """
    if not records:
        return records, 0

    # 按 timestamp 分组
    groups: Dict[float, List[Dict[str, Any]]] = {}
    for rec in records:
        ts = _get_timestamp(rec)
        groups.setdefault(ts, []).append(rec)

    result: List[Dict[str, Any]] = []
    removed = 0
    for ts, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            if strategy == "keep_first":
                kept = group[0]
                # #C7修复：合并被覆盖记录的 _warnings 到保留记录
                _merge_warnings(kept, group[1:])
                result.append(kept)
                removed += len(group) - 1
            elif strategy == "keep_last":
                kept = group[-1]
                # #C7修复：合并被覆盖记录的 _warnings 到保留记录
                _merge_warnings(kept, group[:-1])
                result.append(kept)
                removed += len(group) - 1
            elif strategy == "drop_all":
                removed += len(group)
            else:
                # 默认 keep_last
                kept = group[-1]
                _merge_warnings(kept, group[:-1])
                result.append(kept)
                removed += len(group) - 1

    return result, removed


def _merge_warnings(kept: Dict[str, Any], discarded: List[Dict[str, Any]]) -> None:
    """
    #C7修复：将被丢弃记录的 _warnings 合并到保留记录中（原地修改）

    去重阶段记录已通过单条校验（无 errors），但可能有 _warnings
    （来自序列校验）。合并 warnings 避免异常信息丢失。

    Parameters
    ----------
    kept : Dict
        保留的记录（原地修改 _warnings 字段）
    discarded : List[Dict]
        被丢弃的记录列表
    """
    if not discarded:
        return
    merged = list(kept.get("_warnings", []))
    for rec in discarded:
        for w in rec.get("_warnings", []):
            if w not in merged:
                merged.append(w)
    if merged:
        kept["_warnings"] = merged


def clean_bar_records(
    records: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    清洗K线Bar数据 (核心入口函数)

    Parameters
    ----------
    records : List[Dict]
        原始Bar数据列表，每条包含 date/open/high/low/close/volume/amount 等
    config : Dict | None
        清洗配置（bar 部分），None 则使用默认配置

    Returns
    -------
    Tuple[List[Dict], List[Dict], Dict]
      cleaned: 清洗后有效数据列表（已去重、排序）
      bad:     脏数据列表，每条附加 _errors 字段
      stats:   清洗统计 {
          total, valid, bad, removed_duplicates,
          price_jumps, volume_anomalies,
          time_violations, duration_sec
      }

    Usage
    -----
        cleaned, bad, stats = clean_bar_records(raw_data, config={"max_price_jump_pct": 0.2})
    """
    import time
    start_time = time.time()
    cfg = _merge_config(config)

    total = len(records)
    if total == 0:
        logger.info("输入数据为空，跳过清洗")
        return [], [], {
            "total": 0, "valid": 0, "bad": 0,
            "removed_duplicates": 0, "price_jumps": 0,
            "volume_anomalies": 0, "time_violations": 0,
            "duration_sec": 0.0,
        }

    logger.info(f"开始清洗Bar数据: 共 {total} 条")

    # ═══════════════════════════════════════════
    # 步骤1: 单条基础校验
    # ═══════════════════════════════════════════
    valid: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []

    for rec in records:
        errors = _validate_single_bar(rec, cfg)
        if errors:
            bad_rec = dict(rec)
            bad_rec["_errors"] = errors
            bad.append(bad_rec)
            for err in errors:
                logger.debug(f"脏数据: {err} | record={rec}")
        else:
            valid.append(rec)

    logger.info(f"单条校验完成: 有效 {len(valid)}, 脏数据 {len(bad)}")

    # ═══════════════════════════════════════════
    # 步骤2: 时间单调性检查 (在去重前做标记)
    # ═══════════════════════════════════════════
    time_violations = 0
    if cfg.get("check_time_monotonic", True) and valid:
        # 先按时间戳排序以便检查单调性
        valid.sort(key=_get_timestamp)
        prev_ts = None
        for rec in valid:
            ts = _get_timestamp(rec)
            if prev_ts is not None and ts < prev_ts:
                warnings = rec.setdefault("_warnings", [])
                msg = f"时间回退: ts={ts} < prev_ts={prev_ts}"
                warnings.append(msg)
                time_violations += 1
                logger.warning(msg)
            prev_ts = ts

    # ═══════════════════════════════════════════
    # 步骤3: 重复时间戳去重
    # ═══════════════════════════════════════════
    removed_duplicates = 0
    if cfg.get("check_duplicate_timestamp", True):
        valid, removed_duplicates = _deduplicate_by_timestamp(
            valid, cfg.get("duplicate_strategy", "keep_last")
        )
        if removed_duplicates > 0:
            logger.info(f"去重完成: 移除 {removed_duplicates} 条重复记录")

    # ═══════════════════════════════════════════
    # 步骤4: 序列异常检测 (价格跳变/成交量异常)
    # ═══════════════════════════════════════════
    valid, anomaly_bad, anomaly_counts = _detect_sequence_anomalies(valid, cfg)
    if anomaly_bad:
        bad.extend(anomaly_bad)

    # ═══════════════════════════════════════════
    # 步骤5: 最终排序
    # ═══════════════════════════════════════════
    if cfg.get("sort_by_timestamp", True):
        valid.sort(key=_get_timestamp)

    duration = time.time() - start_time
    stats = {
        "total": total,
        "valid": len(valid),
        "bad": len(bad),
        "removed_duplicates": removed_duplicates,
        "price_jumps": anomaly_counts["price_jump"],
        "volume_anomalies": anomaly_counts["volume_anomaly"],
        "time_violations": time_violations,
        "duration_sec": round(duration, 4),
    }

    logger.info(
        f"Bar清洗完成: 有效={stats['valid']}, 脏数据={stats['bad']}, "
        f"去重={stats['removed_duplicates']}, 耗时={stats['duration_sec']}s"
    )

    return valid, bad, stats


# ═══════════════════════════════════════════════
#  BarCleaner 面向对象清洗器 (阶段9新增)
# ═══════════════════════════════════════════════

class BarCleaner(CleanerBase):
    """
    K线Bar数据清洗器（继承 CleanerBase）

    复用v1普通函数的校验逻辑，提供面向对象接口：
      - 规则管理（add_rule/remove_rule/enable_rule/disable_rule）
      - 事件钩子（on_clean_start/on_clean_complete/on_bad_record/...）
      - 配置热更新（update_config/reset_config）
      - 插件式注册（CleanerRegistry）

    使用方式:
        # 方式1：面向对象
        cleaner = BarCleaner(config={"max_price_jump_pct": 0.2})
        result = cleaner.clean(raw_data)
        cleaned, bad, stats = result  # 三元组解包

        # 方式2：旧函数（完全兼容）
        cleaned, bad, stats = clean_bar_records(raw_data, config={...})

    为实盘无缝对接预留:
      - 流式清洗：逐条调用 _validate_single()，无需全量数据
      - 事件钩子：on_bad_record 可触发实时告警
      - 规则热更新：实盘运行中动态调整清洗阈值
      - 自定义规则：CleanRule.check_fn 注入业务特定校验
    """

    @property
    def data_type(self) -> str:
        """数据类型标识"""
        return "bar"

    def _default_config(self) -> Dict[str, Any]:
        """返回默认配置（复用 DEFAULT_BAR_CONFIG）"""
        return dict(DEFAULT_BAR_CONFIG)

    def _validate_single(
        self,
        record: Dict[str, Any],
        config: Dict[str, Any],
    ) -> List[str]:
        """
        单条Bar基础校验（复用 _validate_single_bar）

        校验项:
          - OHLC 逻辑: high >= max(open,close), low <= min(open,close), high >= low
          - 非负: 价格/成交量 >= 0
          - symbol 非空
          - 时间戳有效
        """
        return _validate_single_bar(record, config)

    def _validate_sequence(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """
        序列级校验：时间单调性检查

        注意：只比较当前记录与前一条记录，严禁访问后续记录（防未来函数）。
        价格跳变/成交量异常放到 _detect_anomalies 中执行。
        """
        counts = {"time_violation": 0}
        if not records:
            return records, counts

        if config.get("check_time_monotonic", True):
            # 先按时间戳排序以便检查单调性
            records.sort(key=_get_timestamp)
            prev_ts = None
            for rec in records:
                ts = _get_timestamp(rec)
                if prev_ts is not None and ts < prev_ts:
                    warnings = rec.setdefault("_warnings", [])
                    msg = f"时间回退: ts={ts} < prev_ts={prev_ts}"
                    warnings.append(msg)
                    counts["time_violation"] += 1
                    logger.warning(msg)
                prev_ts = ts

        return records, counts

    def _deduplicate(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        按时间戳去重（复用 _deduplicate_by_timestamp）

        策略: keep_first | keep_last | drop_all
        """
        strategy = config.get("duplicate_strategy", "keep_last")
        return _deduplicate_by_timestamp(records, strategy)

    def _detect_anomalies(
        self,
        records: List[Dict[str, Any]],
        config: Dict[str, Any],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
        """
        异常检测：价格跳变、成交量异常（复用 _detect_sequence_anomalies）

        注意：只比较当前记录与前一条记录，严禁访问后续记录（防未来函数）。
        异常记录处理策略由配置 price_jump_action/volume_anomaly_action 决定：
          mark（默认）标记 _warnings 保留；drop 整行剔除进 bad。
        """
        return _detect_sequence_anomalies(records, config)

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
        流式单条清洗（为实盘实时行情设计）

        只校验单条记录，可选与前一条记录做序列异常检测。
        不做去重和排序（实盘场景由调用方管理）。

        Parameters
        ----------
        record : Dict
            当前Bar记录
        prev_record : Dict | None
            前一条Bar记录（用于价格跳变/成交量异常检测）

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
            # ★ 重置基准检测：相邻记录时间间隔超过阈值时，跳过跳变检测
            reset_gap_days = float(self._config.get("reset_base_gap_days", 30))
            reset_gap_seconds = reset_gap_days * 86400
            prev_ts = _get_timestamp(prev_record)
            curr_ts = _get_timestamp(record)
            gap_seconds = curr_ts - prev_ts
            need_reset_base = (
                reset_gap_days > 0
                and gap_seconds > reset_gap_seconds
                and prev_ts > 0
                and curr_ts > 0
            )

            if not need_reset_base:
                # 价格跳变检测
                if self._config.get("detect_price_jump", True):
                    prev_close = prev_record.get("close")
                    curr_open = record.get("open")
                    if prev_close and curr_open and float(prev_close) > 0:
                        try:
                            change_pct = abs(float(curr_open) - float(prev_close)) / float(prev_close)
                            if change_pct > self._config.get("max_price_jump_pct", 0.22):
                                msg = f"价格跳变 {change_pct:.2%} (open={curr_open}, prev_close={prev_close})"
                                warnings.append(msg)
                                logger.warning(msg)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass

                # 成交量异常检测
                if self._config.get("detect_volume_anomaly", True):
                    prev_vol = prev_record.get("volume")
                    curr_vol = record.get("volume")
                    if prev_vol and curr_vol and float(prev_vol) > 0:
                        try:
                            ratio = float(curr_vol) / float(prev_vol)
                            if ratio > self._config.get("max_volume_ratio", 100.0):
                                msg = f"成交量异常放大 {ratio:.1f}x (curr={curr_vol}, prev={prev_vol})"
                                warnings.append(msg)
                                logger.warning(msg)
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
            else:
                # 时间间隔过长，重置基准
                gap_days = gap_seconds / 86400
                reset_msg = (
                    f"时间间隔 {gap_days:.1f} 天超过阈值 {reset_gap_days:.0f} 天，"
                    f"重置跳变检测基准"
                )
                warnings.append(reset_msg)
                logger.info(reset_msg)

        is_valid = len(errors) == 0
        return is_valid, errors, warnings


# ═══════════════════════════════════════════════
#  注册到全局清洗器注册器
# ═══════════════════════════════════════════════

def _register_bar_cleaner() -> None:
    """注册 BarCleaner 到全局清洗器注册器（模块加载时自动执行）"""
    try:
        registry = CleanerRegistry()
        if not registry.has("bar"):
            registry.register("bar", BarCleaner)
            logger.debug("BarCleaner 已注册到 CleanerRegistry (type=bar)")
    except Exception as e:
        logger.warning(f"BarCleaner 注册失败: {e}")


# 模块加载时自动注册
_register_bar_cleaner()
