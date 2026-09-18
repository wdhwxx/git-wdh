# 文件名：myquant/data_access/cleaner/error_codes.py
"""
error_codes.py - 数据清洗结构化错误码定义
=============================================
#E3修复：将数据校验错误从纯文本升级为结构化错误码，
解决 _infer_dirty_type 基于错误文本模糊匹配的脆弱性问题。

设计原则:
  1. 错误信息格式: "[ERROR_CODE] human readable message"
     - 错误码前缀便于程序精确解析
     - 人类可读信息便于日志和调试
  2. 向后兼容: 无错误码前缀的旧格式错误信息仍可被模糊匹配解析
  3. 对齐事件桥: 错误码分类与 _RealtimeEventBridge 的 _CRITICAL_TYPES/_WARNING_TYPES 对齐
  4. 为实盘准备: 结构化错误码便于实盘监控、告警分级、问题追踪

错误码分类:
  - 严重错误 (CRITICAL): OHLC逻辑错误、负价、负量、无效时间戳、缺symbol
  - 警告错误 (WARNING): 价格跳变、成交量异常、零价、零量、时间单调错误、重复
"""

from __future__ import annotations

import re
from enum import Enum
from typing import List, Optional, Tuple


# ═══════════════════════════════════════════
#  错误码枚举
# ═══════════════════════════════════════════

class ErrorCode(str, Enum):
    """数据清洗错误码枚举"""

    # ── 严重错误 (CRITICAL) ──
    MISSING_SYMBOL = "MISSING_SYMBOL"           # symbol 为空
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"     # 时间戳无效
    NEGATIVE_PRICE = "NEGATIVE_PRICE"           # 价格为负
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"         # 成交量为负
    PRICE_NOT_NUMBER = "PRICE_NOT_NUMBER"       # 价格无法转为数值
    VOLUME_NOT_NUMBER = "VOLUME_NOT_NUMBER"     # 成交量无法转为数值
    OHLC_HIGH_LOW = "OHLC_HIGH_LOW"             # high < low
    OHLC_HIGH_OPEN_CLOSE = "OHLC_HIGH_OPEN_CLOSE"  # high < max(open, close)
    OHLC_LOW_OPEN_CLOSE = "OHLC_LOW_OPEN_CLOSE"    # low > min(open, close)
    OHLC_PARSE_ERROR = "OHLC_PARSE_ERROR"       # OHLC字段无法转换
    ZERO_VOLUME_FORBIDDEN = "ZERO_VOLUME_FORBIDDEN"  # volume为0且配置不允许
    ALL_ZERO_FORBIDDEN = "ALL_ZERO_FORBIDDEN"   # #C3修复：OHLC全为0且配置不允许

    # ── 警告错误 (WARNING) ──
    PRICE_JUMP = "PRICE_JUMP"                   # 价格跳变
    VOLUME_ANOMALY = "VOLUME_ANOMALY"           # 成交量异常
    ZERO_PRICE = "ZERO_PRICE"                   # 价格为0
    ZERO_VOLUME = "ZERO_VOLUME"                 # 成交量为0
    TIME_MONOTONIC = "TIME_MONOTONIC"           # 时间单调错误
    DUPLICATE_TIMESTAMP = "DUPLICATE_TIMESTAMP" # 重复时间戳
    DUPLICATE_TICK = "DUPLICATE_TICK"           # 重复tick
    BID_ASK_CROSS = "BID_ASK_CROSS"             # 买卖盘价格交叉

    # ── 未知错误 ──
    UNKNOWN = "UNKNOWN"                          # 未知错误


# ═══════════════════════════════════════════
#  错误码分类映射（对齐事件桥）
# ═══════════════════════════════════════════

# 严重错误码集合（连续达到阈值才升级为 critical）
CRITICAL_ERROR_CODES = frozenset({
    ErrorCode.OHLC_HIGH_LOW,
    ErrorCode.OHLC_HIGH_OPEN_CLOSE,
    ErrorCode.OHLC_LOW_OPEN_CLOSE,
    ErrorCode.OHLC_PARSE_ERROR,
    ErrorCode.NEGATIVE_PRICE,
    ErrorCode.NEGATIVE_VOLUME,
    ErrorCode.INVALID_TIMESTAMP,
    ErrorCode.MISSING_SYMBOL,
    ErrorCode.ALL_ZERO_FORBIDDEN,  # #C3修复：OHLC全0且配置不允许
})

# 警告错误码集合（不升级，真实行情可能发生）
WARNING_ERROR_CODES = frozenset({
    ErrorCode.PRICE_JUMP,
    ErrorCode.VOLUME_ANOMALY,
    ErrorCode.ZERO_PRICE,
    ErrorCode.ZERO_VOLUME,
    ErrorCode.TIME_MONOTONIC,
    ErrorCode.DUPLICATE_TIMESTAMP,
    ErrorCode.DUPLICATE_TICK,
    ErrorCode.BID_ASK_CROSS,
})


# ═══════════════════════════════════════════
#  错误码 → dirty_type 映射（对齐事件桥）
# ═══════════════════════════════════════════

_ERROR_CODE_TO_DIRTY_TYPE = {
    ErrorCode.OHLC_HIGH_LOW: "ohlc_logic_error",
    ErrorCode.OHLC_HIGH_OPEN_CLOSE: "ohlc_logic_error",
    ErrorCode.OHLC_LOW_OPEN_CLOSE: "ohlc_logic_error",
    ErrorCode.OHLC_PARSE_ERROR: "ohlc_logic_error",
    ErrorCode.NEGATIVE_PRICE: "negative_price",
    ErrorCode.NEGATIVE_VOLUME: "negative_volume",
    ErrorCode.PRICE_NOT_NUMBER: "negative_price",
    ErrorCode.VOLUME_NOT_NUMBER: "negative_volume",
    ErrorCode.INVALID_TIMESTAMP: "invalid_timestamp",
    ErrorCode.MISSING_SYMBOL: "missing_symbol",
    ErrorCode.PRICE_JUMP: "price_jump",
    ErrorCode.VOLUME_ANOMALY: "volume_anomaly",
    ErrorCode.ZERO_PRICE: "zero_price",
    ErrorCode.ZERO_VOLUME: "zero_volume",
    ErrorCode.ZERO_VOLUME_FORBIDDEN: "zero_volume",
    ErrorCode.TIME_MONOTONIC: "time_monotonic_error",
    ErrorCode.DUPLICATE_TIMESTAMP: "duplicate_timestamp",
    ErrorCode.DUPLICATE_TICK: "duplicate_tick",
    ErrorCode.BID_ASK_CROSS: "bid_ask_cross",
}


# ═══════════════════════════════════════════
#  错误信息格式化工具
# ═══════════════════════════════════════════

# 错误码前缀正则: [ERROR_CODE]
_ERROR_CODE_PREFIX = re.compile(r"^\[([A-Z_]+)\]\s*")


def format_error(code: ErrorCode, message: str) -> str:
    """
    格式化错误信息，添加错误码前缀。

    Parameters
    ----------
    code : ErrorCode
        错误码枚举
    message : str
        人类可读错误信息

    Returns
    -------
    str : 格式化后的错误信息 "[CODE] message"
    """
    return f"[{code.value}] {message}"


def parse_error_code(error_msg: str) -> Tuple[Optional[ErrorCode], str]:
    """
    从错误信息中解析错误码前缀。

    Parameters
    ----------
    error_msg : str
        错误信息，可能包含 "[CODE] message" 格式

    Returns
    -------
    Tuple[ErrorCode | None, str] :
        (错误码, 纯错误信息)。若无错误码前缀，返回 (None, 原信息)
    """
    if not error_msg:
        return None, error_msg or ""
    m = _ERROR_CODE_PREFIX.match(error_msg)
    if m:
        code_str = m.group(1)
        try:
            code = ErrorCode(code_str)
            pure_msg = error_msg[m.end():]
            return code, pure_msg
        except ValueError:
            return None, error_msg
    return None, error_msg


def infer_dirty_type_from_errors(errors: List[str]) -> str:
    """
    从错误列表推断 dirty_type（结构化错误码版本）。

    优先解析错误码前缀精确匹配；若无前缀则回退到文本模糊匹配（向后兼容）。

    匹配优先级: OHLC > 跳变 > 负价/负量 > 零价/零量 > 缺symbol >
               时间戳 > 单调 > 重复 > 量异常 > unknown

    Parameters
    ----------
    errors : List[str]
        错误信息列表

    Returns
    -------
    str : dirty_type 字符串
    """
    if not errors:
        return "unknown"

    # 第一优先级：解析错误码前缀精确匹配
    for error_msg in errors:
        code, _ = parse_error_code(error_msg)
        if code is not None:
            dirty_type = _ERROR_CODE_TO_DIRTY_TYPE.get(code)
            if dirty_type:
                return dirty_type

    # 第二优先级：回退到文本模糊匹配（向后兼容旧格式）
    text = " ".join(str(e).lower() for e in errors)

    if "ohlc" in text or (("high" in text or "最高" in text) and
                           ("low" in text or "最低" in text)):
        return "ohlc_logic_error"
    if "跳变" in text or "jump" in text:
        return "price_jump"
    if "负" in text or "negative" in text:
        if "量" in text or "volume" in text:
            return "negative_volume"
        return "negative_price"
    if "零" in text or "zero" in text:
        if "量" in text or "volume" in text:
            return "zero_volume"
        return "zero_price"
    if "symbol" in text or "代码" in text or "标的" in text:
        return "missing_symbol"
    if "重复" in text or "duplicate" in text:
        return "duplicate_timestamp"
    if "时间戳" in text or "timestamp" in text or "未来" in text:
        return "invalid_timestamp"
    if "单调" in text or "monotonic" in text:
        return "time_monotonic_error"
    if "量" in text or "volume" in text:
        return "volume_anomaly"
    return "unknown"


def errors_to_strings(errors: List[str]) -> List[str]:
    """
    错误列表转为纯字符串列表（去除错误码前缀，用于日志展示）。

    Parameters
    ----------
    errors : List[str]
        错误信息列表（可能包含错误码前缀）

    Returns
    -------
    List[str] : 纯错误信息列表（无错误码前缀）
    """
    result = []
    for error_msg in errors:
        _, pure_msg = parse_error_code(error_msg)
        result.append(pure_msg or error_msg)
    return result


def get_error_codes(errors: List[str]) -> List[ErrorCode]:
    """
    从错误列表中提取所有错误码。

    Parameters
    ----------
    errors : List[str]
        错误信息列表

    Returns
    -------
    List[ErrorCode] : 错误码列表
    """
    codes = []
    for error_msg in errors:
        code, _ = parse_error_code(error_msg)
        if code is not None:
            codes.append(code)
    return codes


# ═══════════════════════════════════════════
#  模块自检
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("=== error_codes 自检 ===")

    # 格式化错误
    msg = format_error(ErrorCode.MISSING_SYMBOL, "symbol 为空")
    assert msg == "[MISSING_SYMBOL] symbol 为空", f"格式错误: {msg}"
    print(f"  format_error: {msg} ✅")

    # 解析错误码
    code, pure = parse_error_code("[MISSING_SYMBOL] symbol 为空")
    assert code == ErrorCode.MISSING_SYMBOL
    assert pure == "symbol 为空"
    print(f"  parse_error_code: code={code}, msg={pure} ✅")

    # 无错误码前缀
    code2, pure2 = parse_error_code("symbol 为空")
    assert code2 is None
    assert pure2 == "symbol 为空"
    print(f"  parse_error_code (无前缀): code={code2}, msg={pure2} ✅")

    # 推断 dirty_type（结构化）
    dt1 = infer_dirty_type_from_errors(["[MISSING_SYMBOL] symbol 为空"])
    assert dt1 == "missing_symbol", f"推断错误: {dt1}"
    print(f"  infer_dirty_type (结构化): {dt1} ✅")

    # 推断 dirty_type（模糊匹配兼容）
    dt2 = infer_dirty_type_from_errors(["symbol 为空"])
    assert dt2 == "missing_symbol", f"推断错误: {dt2}"
    print(f"  infer_dirty_type (模糊兼容): {dt2} ✅")

    # OHLC 错误
    dt3 = infer_dirty_type_from_errors(["[OHLC_HIGH_LOW] high(10) < low(20)"])
    assert dt3 == "ohlc_logic_error", f"推断错误: {dt3}"
    print(f"  infer_dirty_type (OHLC): {dt3} ✅")

    # 去除错误码前缀
    strings = errors_to_strings(["[MISSING_SYMBOL] symbol 为空", "[NEGATIVE_PRICE] open 为负"])
    assert strings == ["symbol 为空", "open 为负"], f"转换错误: {strings}"
    print(f"  errors_to_strings: {strings} ✅")

    # 提取错误码
    codes = get_error_codes(["[MISSING_SYMBOL] symbol 为空", "[NEGATIVE_PRICE] open 为负"])
    assert codes == [ErrorCode.MISSING_SYMBOL, ErrorCode.NEGATIVE_PRICE]
    print(f"  get_error_codes: {codes} ✅")

    # 严重/警告分类
    assert ErrorCode.MISSING_SYMBOL in CRITICAL_ERROR_CODES
    assert ErrorCode.PRICE_JUMP in WARNING_ERROR_CODES
    print(f"  分类映射: MISSING_SYMBOL=CRITICAL, PRICE_JUMP=WARNING ✅")

    print("\n所有自检通过 ✅")
