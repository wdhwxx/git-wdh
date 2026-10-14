# 文件名：myquant/indicators/factor_engine.py
"""
factor_engine.py - 向量化因子引擎
====================================
为多因子批量计算提供统一引擎，是回测性能优化 V5 的核心调度组件。

核心能力：
  1. 单标的多因子批量计算：一次调用计算多个因子，结果合并为单一 DataFrame
  2. 多标的批量计算：遍历多个标的，支持缓存复用
  3. 三级缓存联动：自动接入 FactorCache（L1内存 / L2磁盘 / L3回源计算）
  4. 数据指纹联动：未提供指纹时自动计算，确保数据变更时缓存失效
  5. 向量化计算：所有因子计算复用现有向量化指标（pandas rolling/ewm/numpy）
  6. 性能统计：各因子缓存命中层级、计算耗时、总命中率

设计原则：
  - 引擎本身不做任何时间序列计算，仅负责调度、缓存、合并
  - 因子计算委托给已注册的 IndicatorBase 子类（向量化实现）
  - 无未来函数风险：引擎不接触行情数据计算逻辑
  - 防御式编程：因子配置校验、数据校验、异常隔离（单个因子失败不影响其他因子）

依赖: logging, time, typing
第三方: pandas, numpy
本地: myquant.indicators (get_indicator), myquant.indicators.factor_cache,
      myquant.utils.data_fingerprint
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from myquant.indicators import get_indicator
from myquant.indicators.factor_cache import (
    CACHE_HIT_L1,
    CACHE_HIT_L2,
    CACHE_HIT_L3,
    FactorCache,
)
from myquant.utils.data_fingerprint import DataFingerprint, compute_fingerprint

logger = logging.getLogger(__name__)


class FactorLookaheadError(RuntimeError):
    """截断重算自检失败：新因子在边界行的全量结果与截断重算结果不一致 → 疑似未来函数。

    仅对新注册因子强制（M1B-05）；存量因子不受影响。阻断进入回测/实盘链路。
    """


# ─────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────

# 因子配置必需字段
REQUIRED_FACTOR_FIELDS: Tuple[str, ...] = ("name",)

# 因子配置可选字段及其默认值
OPTIONAL_FACTOR_FIELDS: Dict[str, Any] = {
    "params": {},       # 因子参数
    "prefix": "",       # 输出列名前缀
    "enabled": True,    # 是否启用该因子
}

# 引擎统计默认值
DEFAULT_STATS: Dict[str, Any] = {
    "total_calls": 0,
    "total_factors": 0,
    "l1_hits": 0,
    "l2_hits": 0,
    "l3_misses": 0,
    "factor_errors": 0,
    "total_calc_time": 0.0,
}


# ─────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────

def _validate_factor_config(config: Dict[str, Any], index: int) -> Dict[str, Any]:
    """校验并规范化单个因子配置。

    Args:
        config: 原始因子配置字典。
        index: 配置在列表中的索引（用于错误信息）。

    Returns:
        规范化后的配置字典（包含所有必需和可选字段）。

    Raises:
        TypeError: config 不是字典。
        ValueError: 缺少必需字段或字段类型错误。
    """
    if not isinstance(config, dict):
        raise TypeError(
            f"因子配置[{index}]必须是字典，got: {type(config).__name__}"
        )

    # 校验必需字段
    for field in REQUIRED_FACTOR_FIELDS:
        if field not in config:
            raise ValueError(
                f"因子配置[{index}]缺少必需字段: '{field}'，"
                f"当前配置: {config}"
            )

    # 校验 name 字段
    name = config["name"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError(
            f"因子配置[{index}]的 'name' 必须是非空字符串，got: {name!r}"
        )

    # 校验 params 字段
    params = config.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(
            f"因子配置[{index}]的 'params' 必须是字典，got: {type(params).__name__}"
        )

    # 校验 prefix 字段
    prefix = config.get("prefix", "")
    if not isinstance(prefix, str):
        raise ValueError(
            f"因子配置[{index}]的 'prefix' 必须是字符串，got: {type(prefix).__name__}"
        )

    # 校验 enabled 字段
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"因子配置[{index}]的 'enabled' 必须是布尔值，got: {type(enabled).__name__}"
        )

    return {
        "name": name.strip(),
        "params": dict(params),
        "prefix": prefix,
        "enabled": enabled,
    }


def _validate_factor_configs(
    configs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """批量校验因子配置列表。

    Args:
        configs: 因子配置列表。

    Returns:
        规范化后的配置列表。

    Raises:
        TypeError: configs 不是列表。
        ValueError: 列表为空或包含无效配置。
    """
    if not isinstance(configs, list):
        raise TypeError(
            f"factor_configs 必须是列表，got: {type(configs).__name__}"
        )
    if len(configs) == 0:
        raise ValueError("factor_configs 不能为空列表，至少需要一个因子配置")

    validated = []
    for i, config in enumerate(configs):
        validated.append(_validate_factor_config(config, i))
    return validated


def _as_float(value: Any) -> Optional[float]:
    """把边界行单元值转 float（NaN/非数值/无法转换 → None）。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return f


def _apply_prefix(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """为 DataFrame 的列名添加前缀。

    Args:
        df: 原始 DataFrame。
        prefix: 列名前缀。

    Returns:
        添加前缀后的新 DataFrame（原 DataFrame 不被修改）。
    """
    if not prefix:
        return df.copy()
    new_cols = {col: f"{prefix}{col}" for col in df.columns}
    return df.rename(columns=new_cols)


# ─────────────────────────────────────────────
# FactorEngine 向量化因子引擎
# ─────────────────────────────────────────────

class FactorEngine:
    """向量化因子引擎，批量计算多个因子，联动三级缓存与数据指纹。

    Usage:
        engine = FactorEngine(cache_dir="cache/factor", enable_l1=True)

        # 单标的多因子计算
        result = engine.calculate(
            data=df,
            symbol="600519.SH",
            factor_configs=[
                {"name": "sma", "params": {"period": 20}, "prefix": "sma_20_"},
                {"name": "rsrs", "params": {"period": 18, "norm_window": 60}},
                {"name": "atr", "params": {"period": 14}},
            ],
        )

        # 多标的批量计算
        results = engine.calculate_batch(
            data_dict={"600519.SH": df1, "000001.SZ": df2},
            factor_configs=factor_configs,
        )
    """

    def __init__(
        self,
        cache: Optional[FactorCache] = None,
        cache_dir: str = "cache/factor",
        enable_l1: bool = True,
        l1_max_size: int = 1024,
        l1_ttl: int = 3600,
        l2_enabled: bool = True,
        l2_ttl: int = 604800,
        use_cache: bool = True,
        auto_fingerprint: bool = True,
        truncated_selfcheck: bool = True,
    ) -> None:
        """
        Args:
            cache: 外部传入的 FactorCache 实例；None 时自动创建。
            cache_dir: L2 磁盘缓存目录（cache 为 None 时生效）。
            enable_l1: 是否启用 L1 内存缓存（cache 为 None 时生效）。
            l1_max_size: L1 最大缓存条目数（cache 为 None 时生效）。
            l1_ttl: L1 缓存过期时间秒（cache 为 None 时生效）。
            l2_enabled: 是否启用 L2 磁盘缓存（cache 为 None 时生效）。
            l2_ttl: L2 缓存过期时间秒（cache 为 None 时生效）。
            use_cache: 全局缓存开关；False 时完全绕过缓存，每次都重新计算。
            auto_fingerprint: 未提供数据指纹时是否自动计算；False 时使用占位符（不推荐）。
                （v3.0 §10.5 R08 白名单：factor_engine 正确调用，:303-305 auto_fingerprint 兜底）
            truncated_selfcheck: 是否对新注册因子执行截断重算自检（M1B-05，v3.0 §6.5-3）；
                仅首次计算该因子名时执行，存量不受影响。确认泄漏（边界行全量≠截断重算）抛
                FactorLookaheadError 阻断进入回测/实盘链路。
        """
        self._use_cache = use_cache
        self._auto_fingerprint = auto_fingerprint
        self._truncated_selfcheck = truncated_selfcheck
        # 已通过/已执行截断重算自检的因子名集合（仅新注册因子强制，存量不受影响）
        self._self_checked_factors: set[str] = set()

        # 初始化缓存
        if cache is not None:
            self._cache = cache
        elif use_cache:
            self._cache = FactorCache(
                cache_dir=cache_dir,
                enable_l1=enable_l1,
                l1_max_size=l1_max_size,
                l1_ttl=l1_ttl,
                l2_enabled=l2_enabled,
                l2_ttl=l2_ttl,
            )
        else:
            self._cache = None

        # 统计计数器
        self._stats: Dict[str, Any] = dict(DEFAULT_STATS)
        # 各因子详细统计: {factor_name: {"l1": n, "l2": n, "l3": n, "errors": n, "time": s}}
        self._factor_stats: Dict[str, Dict[str, Any]] = {}

        logger.info(
            "FactorEngine 初始化: use_cache=%s, auto_fingerprint=%s, cache=%s",
            use_cache, auto_fingerprint,
            self._cache is not None,
        )

    # ── 属性 ──

    @property
    def cache(self) -> Optional[FactorCache]:
        """底层 FactorCache 实例（可能为 None，当 use_cache=False 时）。"""
        return self._cache

    @property
    def use_cache(self) -> bool:
        """当前是否启用缓存。"""
        return self._use_cache and self._cache is not None

    # ── 截断重算自检（M1B-05，仅新注册因子强制） ──

    def _truncated_recompute_selfcheck(
        self,
        data: pd.DataFrame,
        factor_name: str,
        params: Dict[str, Any],
        full_result: pd.DataFrame,
    ) -> Dict[str, Any]:
        """对新因子执行截断重算自检（防未来函数，v3.0 §6.5-3）。

        原理：把数据截断到最后 1 根之前的序列重算因子，比较**截断边界行**上
        全量结果与截断重算结果是否一致。若指标在边界行用了未来数据（如 next bar、
        全样本标准化），全量值≠截断重算值 → 确认泄漏。

        Args:
            data: 全量行情数据。
            factor_name: 因子/指标名称。
            params: 因子参数。
            full_result: 已计算的全量因子结果（复用，避免重复全量计算）。

        Returns:
            Dict[str, Any]: {"checked": bool, "passed": bool, "reason": str}。
              checked=False 表示因数据不足/异常跳过自检（不阻断）；
              passed=False 表示确认泄漏（调用方应抛 FactorLookaheadError 阻断）。
        """
        if full_result is None or len(full_result) == 0:
            return {"checked": False, "passed": True, "reason": "全量结果为空，跳过自检"}
        n = len(data)
        if n < 30:
            # 数据过短：warmup 不足，跳过自检（不误伤短序列合法因子）
            return {"checked": False, "passed": True, "reason": "数据过短，跳过截断重算自检"}
        trunc = data.iloc[: n - 1]
        if len(trunc) < 10:
            return {"checked": False, "passed": True, "reason": "截断后数据过短，跳过自检"}
        boundary = trunc.index[-1]
        try:
            indicator = get_indicator(factor_name, **params)
            trunc_result = indicator.calculate(trunc)
        except Exception as e:  # noqa: BLE001 - 自检本身异常不阻断（仅记录；未确证泄漏不放行异常）
            logger.warning(
                "截断重算自检跳过（计算异常，不误伤）: factor=%s err=%s", factor_name, e,
            )
            return {"checked": False, "passed": True, "reason": f"截断重算异常，跳过自检: {e}"}
        # 比较边界行（合法 trailing 指标：边界行全量与截断重算值恒等）
        try:
            for col in full_result.columns:
                if col not in trunc_result.columns:
                    return {
                        "checked": False, "passed": True,
                        "reason": f"截断重算缺列 {col}，跳过自检",
                    }
                fv = full_result.loc[boundary, col]
                tv = trunc_result.loc[boundary, col]
                f_num = _as_float(fv)
                t_num = _as_float(tv)
                # 存在性不一致（全量有值/截断为 NaN，或反之）= 该指标在边界行依赖了
                # 未来/全样本数据（如 shift(-1)、全样本标准化）→ 确认泄漏
                if (f_num is None) != (t_num is None):
                    return {
                        "checked": True, "passed": False,
                        "reason": (
                            f"截断重算自检失败：因子 {factor_name!r} 在边界行 {boundary} 列 {col} "
                            f"全量={fv!r} 截断重算={tv!r}（存在性不一致）——指标引用了未来/全样本数据，"
                            f"阻断进入回测/实盘链路（M1B-05）"
                        ),
                    }
                if f_num is None and t_num is None:
                    continue  # 边界行两者皆 NaN（warmup 内）：跳过该列比较
                if abs(f_num - t_num) > max(1e-8, 1e-8 * abs(t_num)):
                    return {
                        "checked": True, "passed": False,
                        "reason": (
                            f"截断重算自检失败：因子 {factor_name!r} 在边界行 {boundary} 列 {col} "
                            f"全量值 {f_num} ≠ 截断重算值 {t_num}（疑似未来函数：该指标在决策时点"
                            f"引用了未来/全样本数据，阻断进入回测/实盘链路，M1B-05）"
                        ),
                    }
        except Exception as e:  # noqa: BLE001
            logger.warning("截断重算自检边界比较异常，跳过: factor=%s err=%s", factor_name, e)
            return {"checked": False, "passed": True, "reason": f"边界比较异常，跳过自检: {e}"}
        return {"checked": True, "passed": True, "reason": "截断重算自检通过"}

    # ── 核心：单因子计算（带缓存） ──

    def _calculate_single_factor(
        self,
        data: pd.DataFrame,
        symbol: str,
        factor_name: str,
        params: Dict[str, Any],
        data_fingerprint: Optional[DataFingerprint],
    ) -> Tuple[pd.DataFrame, str]:
        """计算单个因子，自动接入三级缓存。

        Args:
            data: 行情数据 DataFrame。
            symbol: 标的代码。
            factor_name: 因子/指标名称。
            params: 因子参数字典。
            data_fingerprint: 数据指纹；None 时按 auto_fingerprint 配置处理。

        Returns:
            (result_df, hit_level) 元组。hit_level 为 "l1"/"l2"/"l3"。

        Raises:
            ValueError: 因子未注册或计算失败。
        """
        # 处理数据指纹
        fp = data_fingerprint
        if fp is None and self._auto_fingerprint:
            fp = compute_fingerprint(data, symbol=symbol, data_source="factor_engine")

        # 定义计算函数（L3 回源）
        def _compute(df: pd.DataFrame, p: Dict[str, Any]) -> pd.DataFrame:
            indicator = get_indicator(factor_name, **p)
            return indicator.calculate(df)

        # 走缓存或直接计算
        if self.use_cache:
            result, hit_level = self._cache.get_or_compute(
                factor_name=factor_name,
                symbol=symbol,
                params=params,
                data=data,
                compute_func=_compute,
                data_fingerprint=fp,
            )
        else:
            result = _compute(data, params)
            hit_level = CACHE_HIT_L3

        # 截断重算自检（M1B-05）：仅新注册因子首次计算强制；存量/缓存命中复用不受影响
        if (
            self._truncated_selfcheck
            and factor_name not in self._self_checked_factors
        ):
            self._self_checked_factors.add(factor_name)
            check = self._truncated_recompute_selfcheck(data, factor_name, params, result)
            if check.get("checked") and not check.get("passed"):
                logger.error("截断重算自检失败（阻断）: %s", check["reason"])
                raise FactorLookaheadError(check["reason"])
            if check.get("checked"):
                logger.debug("截断重算自检通过: factor=%s", factor_name)

        return result, hit_level

    # ── 公开接口：单标的多因子批量计算 ──

    def calculate(
        self,
        data: pd.DataFrame,
        symbol: str,
        factor_configs: List[Dict[str, Any]],
        data_fingerprint: Optional[DataFingerprint] = None,
        raise_on_error: bool = False,
    ) -> pd.DataFrame:
        """单标的多因子批量计算，结果合并为单一 DataFrame。

        逐个计算因子，每个因子独立接入缓存。单个因子计算失败时：
        - raise_on_error=False（默认）：记录错误，跳过该因子，继续计算其他因子
        - raise_on_error=True：立即抛出异常

        Args:
            data: 行情数据 DataFrame，必须包含 open/high/low/close 列。
            symbol: 标的代码（如 "600519.SH"）。
            factor_configs: 因子配置列表，每个元素为:
                {
                    "name": "sma",                    # 必需：指标名称
                    "params": {"period": 20},         # 可选：指标参数
                    "prefix": "sma_20_",              # 可选：输出列名前缀
                    "enabled": True,                   # 可选：是否启用
                }
            data_fingerprint: 数据指纹；None 时按 auto_fingerprint 配置自动计算。
            raise_on_error: 单个因子失败时是否抛出异常；False 时跳过失败因子。

        Returns:
            合并后的因子结果 DataFrame，索引与输入 data 对齐，
            列为各因子的输出列名（含 prefix）。

        Raises:
            TypeError: data 不是 DataFrame 或 factor_configs 不是列表。
            ValueError: 数据为空、symbol 为空、factor_configs 为空或无效。
        """
        # ── 输入校验 ──
        if not isinstance(data, pd.DataFrame):
            raise TypeError(
                f"data 必须是 pandas DataFrame，got: {type(data).__name__}"
            )
        if data.empty:
            raise ValueError("输入数据 DataFrame 不能为空")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"symbol 必须是非空字符串，got: {symbol!r}")

        validated_configs = _validate_factor_configs(factor_configs)
        symbol = symbol.strip()

        # 自动计算数据指纹（一次计算，所有因子复用）
        fp = data_fingerprint
        if fp is None and self._auto_fingerprint:
            fp = compute_fingerprint(data, symbol=symbol, data_source="factor_engine")
            logger.debug("自动计算数据指纹: symbol=%s, hash=%s", symbol, fp.hash_value[:16])

        # ── 逐个计算因子 ──
        self._stats["total_calls"] += 1
        call_start = time.time()
        factor_results: List[pd.DataFrame] = []

        for config in validated_configs:
            if not config["enabled"]:
                logger.debug("因子已禁用，跳过: %s", config["name"])
                continue

            factor_name = config["name"]
            params = config["params"]
            prefix = config["prefix"]

            # 初始化因子统计
            if factor_name not in self._factor_stats:
                self._factor_stats[factor_name] = {
                    "l1": 0, "l2": 0, "l3": 0, "errors": 0, "time": 0.0,
                }

            factor_start = time.time()
            try:
                result, hit_level = self._calculate_single_factor(
                    data=data,
                    symbol=symbol,
                    factor_name=factor_name,
                    params=params,
                    data_fingerprint=fp,
                )

                # 更新统计
                self._factor_stats[factor_name][hit_level] += 1
                if hit_level == CACHE_HIT_L1:
                    self._stats["l1_hits"] += 1
                elif hit_level == CACHE_HIT_L2:
                    self._stats["l2_hits"] += 1
                else:
                    self._stats["l3_misses"] += 1

                # 应用列名前缀
                result = _apply_prefix(result, prefix)
                factor_results.append(result)

                factor_time = time.time() - factor_start
                self._factor_stats[factor_name]["time"] += factor_time
                self._stats["total_factors"] += 1

                logger.debug(
                    "因子计算完成: %s (symbol=%s, hit=%s, time=%.4fs)",
                    factor_name, symbol, hit_level, factor_time,
                )

            except Exception as e:
                self._factor_stats[factor_name]["errors"] += 1
                self._stats["factor_errors"] += 1
                logger.error(
                    "因子计算失败: %s (symbol=%s), 错误: %s",
                    factor_name, symbol, e,
                )
                if raise_on_error:
                    raise

        # ── 合并结果 ──
        call_time = time.time() - call_start
        self._stats["total_calc_time"] += call_time

        if not factor_results:
            logger.warning("所有因子均计算失败或被禁用，返回空 DataFrame: symbol=%s", symbol)
            return pd.DataFrame(index=data.index)

        combined = pd.concat(factor_results, axis=1)

        logger.info(
            "因子批量计算完成: symbol=%s, 因子数=%d/%d, 耗时=%.4fs",
            symbol, len(factor_results), len(validated_configs), call_time,
        )
        return combined

    # ── 公开接口：多标的批量计算 ──

    def calculate_batch(
        self,
        data_dict: Dict[str, pd.DataFrame],
        factor_configs: List[Dict[str, Any]],
        fingerprints: Optional[Dict[str, DataFingerprint]] = None,
        raise_on_error: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """多标的批量因子计算。

        遍历每个标的，调用 calculate() 计算因子。数据指纹可按标的提供，
        未提供时自动计算。

        Args:
            data_dict: {symbol: DataFrame} 字典，每个标的的行情数据。
            factor_configs: 因子配置列表（格式同 calculate()）。
            fingerprints: {symbol: DataFingerprint} 字典，可选；
                          未提供的标的自动计算指纹。
            raise_on_error: 单个因子失败时是否抛出异常。

        Returns:
            {symbol: result_df} 字典，每个标的的因子计算结果。

        Raises:
            TypeError: data_dict 不是字典。
            ValueError: data_dict 为空。
        """
        if not isinstance(data_dict, dict):
            raise TypeError(
                f"data_dict 必须是字典，got: {type(data_dict).__name__}"
            )
        if len(data_dict) == 0:
            raise ValueError("data_dict 不能为空字典")

        fingerprints = fingerprints or {}
        results: Dict[str, pd.DataFrame] = {}
        batch_start = time.time()

        logger.info(
            "多标的批量计算开始: 标的数=%d, 因子数=%d",
            len(data_dict), len(factor_configs),
        )

        for symbol, data in data_dict.items():
            fp = fingerprints.get(symbol)
            try:
                result = self.calculate(
                    data=data,
                    symbol=symbol,
                    factor_configs=factor_configs,
                    data_fingerprint=fp,
                    raise_on_error=raise_on_error,
                )
                results[symbol] = result
            except Exception as e:
                logger.error("标的计算失败: %s, 错误: %s", symbol, e)
                if raise_on_error:
                    raise
                results[symbol] = pd.DataFrame(index=data.index)

        batch_time = time.time() - batch_start
        logger.info(
            "多标的批量计算完成: 标的数=%d, 耗时=%.4fs",
            len(results), batch_time,
        )
        return results

    # ── 公开接口：带统计的计算 ──

    def calculate_with_stats(
        self,
        data: pd.DataFrame,
        symbol: str,
        factor_configs: List[Dict[str, Any]],
        data_fingerprint: Optional[DataFingerprint] = None,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """计算因子并返回本次调用的详细统计。

        Args:
            data: 行情数据 DataFrame。
            symbol: 标的代码。
            factor_configs: 因子配置列表。
            data_fingerprint: 数据指纹。

        Returns:
            (result_df, stats) 元组。stats 包含本次调用的缓存命中、耗时等信息。
        """
        call_start = time.time()
        l1_before = self._stats["l1_hits"]
        l2_before = self._stats["l2_hits"]
        l3_before = self._stats["l3_misses"]
        err_before = self._stats["factor_errors"]

        result = self.calculate(
            data=data,
            symbol=symbol,
            factor_configs=factor_configs,
            data_fingerprint=data_fingerprint,
        )

        call_time = time.time() - call_start
        call_stats = {
            "symbol": symbol,
            "factor_count": len(factor_configs),
            "l1_hits": self._stats["l1_hits"] - l1_before,
            "l2_hits": self._stats["l2_hits"] - l2_before,
            "l3_misses": self._stats["l3_misses"] - l3_before,
            "errors": self._stats["factor_errors"] - err_before,
            "calc_time": round(call_time, 4),
            "hit_rate": 0.0,
        }
        total = call_stats["l1_hits"] + call_stats["l2_hits"] + call_stats["l3_misses"]
        if total > 0:
            call_stats["hit_rate"] = round(
                (call_stats["l1_hits"] + call_stats["l2_hits"]) / total, 4
            )
        return result, call_stats

    # ── 公开接口：缓存管理 ──

    def invalidate(
        self,
        symbol: Optional[str] = None,
        factor_name: Optional[str] = None,
    ) -> int:
        """失效匹配条件的缓存（L1+L2）。

        Args:
            symbol: 标的代码过滤；None 表示不限制。
            factor_name: 因子名称过滤；None 表示不限制。

        Returns:
            失效的缓存条目总数。
        """
        if self._cache is None:
            logger.warning("缓存未启用，invalidate 无效")
            return 0
        return self._cache.invalidate(factor_name=factor_name, symbol=symbol)

    def clear_cache(self) -> None:
        """清空所有缓存（L1+L2）。"""
        if self._cache is not None:
            self._cache.clear()
            logger.info("FactorEngine 缓存已清空")

    def cleanup_expired(self) -> int:
        """清理 L2 中已过期的缓存文件。

        Returns:
            清理的过期文件数量。
        """
        if self._cache is not None:
            return self._cache.cleanup_expired()
        return 0

    # ── 公开接口：统计 ──

    def stats(self) -> Dict[str, Any]:
        """获取引擎累计统计信息。

        Returns:
            包含总调用数、各层级命中数、命中率、因子错误数、总耗时的字典。
        """
        total_requests = (
            self._stats["l1_hits"]
            + self._stats["l2_hits"]
            + self._stats["l3_misses"]
        )
        hit_rate = (
            (self._stats["l1_hits"] + self._stats["l2_hits"]) / total_requests
            if total_requests > 0
            else 0.0
        )
        return {
            "total_calls": self._stats["total_calls"],
            "total_factors": self._stats["total_factors"],
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "l3_misses": self._stats["l3_misses"],
            "factor_errors": self._stats["factor_errors"],
            "total_requests": total_requests,
            "hit_rate": round(hit_rate, 4),
            "total_calc_time": round(self._stats["total_calc_time"], 4),
            "avg_calc_time": (
                round(self._stats["total_calc_time"] / self._stats["total_calls"], 4)
                if self._stats["total_calls"] > 0
                else 0.0
            ),
            "use_cache": self.use_cache,
            "cache_stats": self._cache.stats() if self._cache is not None else None,
        }

    def factor_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取各因子的详细统计信息。

        Returns:
            {factor_name: {l1, l2, l3, errors, time}} 字典。
        """
        return {
            name: dict(stats)
            for name, stats in self._factor_stats.items()
        }

    def reset_stats(self) -> None:
        """重置引擎统计计数器（不影响缓存数据）。"""
        self._stats = dict(DEFAULT_STATS)
        self._factor_stats.clear()
        logger.info("FactorEngine 统计已重置")

    def __repr__(self) -> str:
        s = self.stats()
        return (
            f"FactorEngine(cache={self.use_cache}, "
            f"calls={s['total_calls']}, factors={s['total_factors']}, "
            f"hit_rate={s['hit_rate']:.2%})"
        )


# ─────────────────────────────────────────────
# 全局单例管理
# ─────────────────────────────────────────────

_default_factor_engine: Optional[FactorEngine] = None
_engine_lock = __import__("threading").Lock()


def get_factor_engine(**kwargs: Any) -> FactorEngine:
    """获取全局因子引擎单例。

    首次调用时创建实例，后续调用返回同一实例。
    如需不同配置的独立实例，请直接使用 FactorEngine(...) 构造。

    Args:
        **kwargs: 透传给 FactorEngine 构造函数的参数（仅首次调用生效）。

    Returns:
        全局 FactorEngine 单例。
    """
    global _default_factor_engine
    with _engine_lock:
        if _default_factor_engine is None:
            _default_factor_engine = FactorEngine(**kwargs)
        return _default_factor_engine


def reset_factor_engine() -> None:
    """重置全局因子引擎单例（下次调用 get_factor_engine 时重新创建）。"""
    global _default_factor_engine
    with _engine_lock:
        if _default_factor_engine is not None:
            _default_factor_engine.clear_cache()
        _default_factor_engine = None
    logger.info("全局因子引擎单例已重置")


# ─────────────────────────────────────────────
# 自测 Demo
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    import tempfile
    from pathlib import Path

    print("=" * 60)
    print("  factor_engine.py — 向量化因子引擎自测 Demo")
    print("=" * 60)

    # 构造测试数据
    print("\n[1] 构造测试数据")
    dates = pd.date_range("2024-01-02", periods=120, freq="B")
    np.random.seed(42)
    test_data = pd.DataFrame(
        {
            "open": np.cumsum(np.random.randn(120)) + 100,
            "high": np.cumsum(np.random.randn(120)) + 102,
            "low": np.cumsum(np.random.randn(120)) + 98,
            "close": np.cumsum(np.random.randn(120)) + 100,
            "volume": np.random.randint(10000, 50000, 120),
        },
        index=dates,
    )
    test_data.index.name = "date"
    print(f"    数据形状: {test_data.shape}")

    # 构造数据指纹
    fp = compute_fingerprint(test_data, symbol="TEST.SH", data_source="demo")
    print(f"    数据指纹: {fp.hash_value[:16]}...")

    # 因子配置
    factor_configs = [
        {"name": "sma", "params": {"period": 20}, "prefix": "sma_20_"},
        {"name": "ema", "params": {"period": 12}, "prefix": "ema_12_"},
        {"name": "rsi", "params": {"period": 14}, "prefix": "rsi_14_"},
        {"name": "atr", "params": {"period": 14}, "prefix": "atr_14_"},
    ]

    # 使用临时缓存目录
    tmp_cache_dir = Path(tempfile.mkdtemp(prefix="factor_engine_test_"))
    print(f"    临时缓存目录: {tmp_cache_dir}")

    try:
        # ── 测试 2: 基本批量计算（L3 未命中） ──
        print("\n[2] 基本批量计算（首次，L3 未命中）")
        engine = FactorEngine(cache_dir=str(tmp_cache_dir), enable_l1=True, l2_ttl=0)
        result2, stats2 = engine.calculate_with_stats(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=factor_configs,
            data_fingerprint=fp,
        )
        print(f"    结果形状: {result2.shape}")
        print(f"    结果列: {list(result2.columns)}")
        print(f"    本次统计: L1={stats2['l1_hits']}, L2={stats2['l2_hits']}, L3={stats2['l3_misses']}")
        assert result2.shape[0] == 120, f"结果行数应为120，got: {result2.shape[0]}"
        assert stats2["l3_misses"] == 4, f"首次应全部L3未命中，got: {stats2['l3_misses']}"
        print("    ✓ 批量计算正常，4个因子全部L3未命中")

        # ── 测试 3: 第二次计算（L1 命中） ──
        print("\n[3] 第二次计算（L1 内存缓存命中）")
        result3, stats3 = engine.calculate_with_stats(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=factor_configs,
            data_fingerprint=fp,
        )
        print(f"    本次统计: L1={stats3['l1_hits']}, L2={stats3['l2_hits']}, L3={stats3['l3_misses']}")
        assert stats3["l1_hits"] == 4, f"第二次应全部L1命中，got: {stats3['l1_hits']}"
        pd.testing.assert_frame_equal(result3, result2, check_freq=False)
        print("    ✓ L1 缓存命中，结果一致")

        # ── 测试 4: 禁用 L1 的新引擎（L2 命中） ──
        print("\n[4] 禁用L1的新引擎（L2 磁盘缓存命中）")
        engine_no_l1 = FactorEngine(cache_dir=str(tmp_cache_dir), enable_l1=False, l2_ttl=0)
        result4, stats4 = engine_no_l1.calculate_with_stats(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=factor_configs,
            data_fingerprint=fp,
        )
        print(f"    本次统计: L1={stats4['l1_hits']}, L2={stats4['l2_hits']}, L3={stats4['l3_misses']}")
        assert stats4["l2_hits"] == 4, f"应全部L2命中，got: {stats4['l2_hits']}"
        pd.testing.assert_frame_equal(result4, result2, check_freq=False)
        print("    ✓ L2 磁盘缓存命中，结果一致")

        # ── 测试 5: 数据指纹变更（L3 未命中） ──
        print("\n[5] 数据指纹变更（缓存自动失效，L3 未命中）")
        modified_data = test_data.copy()
        modified_data.iloc[-1, modified_data.columns.get_loc("close")] = 999.0
        fp_modified = compute_fingerprint(modified_data, symbol="TEST.SH", data_source="demo")
        result5, stats5 = engine.calculate_with_stats(
            data=modified_data,
            symbol="TEST.SH",
            factor_configs=factor_configs,
            data_fingerprint=fp_modified,
        )
        print(f"    本次统计: L1={stats5['l1_hits']}, L2={stats5['l2_hits']}, L3={stats5['l3_misses']}")
        assert stats5["l3_misses"] == 4, f"指纹变更应全部L3未命中，got: {stats5['l3_misses']}"
        print("    ✓ 数据指纹变更，缓存自动失效")

        # ── 测试 6: 自动计算指纹 ──
        print("\n[6] 自动计算数据指纹（不传入指纹）")
        engine_auto = FactorEngine(cache_dir=str(tmp_cache_dir / "auto"), enable_l1=True, l2_ttl=0)
        result6 = engine_auto.calculate(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=factor_configs[:2],  # 只算2个因子
        )
        print(f"    结果形状: {result6.shape}")
        assert result6.shape[1] == 2, f"应2个因子输出列，got: {result6.shape[1]}"
        print("    ✓ 自动计算指纹正常")

        # ── 测试 7: 禁用因子 ──
        print("\n[7] 禁用特定因子")
        configs_with_disabled = factor_configs.copy()
        configs_with_disabled[2] = {"name": "rsi", "params": {"period": 14}, "enabled": False}
        result7 = engine.calculate(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=configs_with_disabled,
            data_fingerprint=fp,
        )
        print(f"    结果列: {list(result7.columns)}")
        assert "rsi_14_rsi" not in result7.columns, "禁用的因子不应出现在结果中"
        assert result7.shape[1] == 3, f"应3个因子（1个被禁用），got: {result7.shape[1]}"
        print("    ✓ 禁用因子正常跳过")

        # ── 测试 8: 多标的批量计算 ──
        print("\n[8] 多标的批量计算")
        data2 = test_data.copy()
        data2["close"] = data2["close"] * 1.5
        fp2 = compute_fingerprint(data2, symbol="TEST2.SH", data_source="demo")
        batch_results = engine.calculate_batch(
            data_dict={"TEST.SH": test_data, "TEST2.SH": data2},
            factor_configs=factor_configs[:2],
            fingerprints={"TEST.SH": fp, "TEST2.SH": fp2},
        )
        print(f"    标的数: {len(batch_results)}")
        assert len(batch_results) == 2
        assert "TEST.SH" in batch_results and "TEST2.SH" in batch_results
        print("    ✓ 多标的批量计算正常")

        # ── 测试 9: 引擎统计 ──
        print("\n[9] 引擎统计信息")
        engine_stats = engine.stats()
        print(f"    总调用数: {engine_stats['total_calls']}")
        print(f"    总因子数: {engine_stats['total_factors']}")
        print(f"    L1命中: {engine_stats['l1_hits']}")
        print(f"    L2命中: {engine_stats['l2_hits']}")
        print(f"    L3未命中: {engine_stats['l3_misses']}")
        print(f"    命中率: {engine_stats['hit_rate']:.2%}")
        assert engine_stats["total_calls"] > 0
        assert engine_stats["hit_rate"] > 0
        print("    ✓ 统计信息正常")

        # ── 测试 10: 失效缓存 ──
        print("\n[10] 缓存失效")
        removed = engine.invalidate(symbol="TEST.SH", factor_name="sma")
        print(f"    失效条目数: {removed}")
        assert removed > 0
        # 失效后重新计算应 L3 未命中
        result10, stats10 = engine.calculate_with_stats(
            data=test_data,
            symbol="TEST.SH",
            factor_configs=factor_configs[:1],  # 只算sma
            data_fingerprint=fp,
        )
        assert stats10["l3_misses"] == 1, f"失效后应L3未命中，got: {stats10['l3_misses']}"
        print("    ✓ 缓存失效正常")

        # ── 测试 11: 全局单例 ──
        print("\n[11] 全局单例测试")
        reset_factor_engine()
        singleton1 = get_factor_engine(cache_dir=str(tmp_cache_dir / "global"))
        singleton2 = get_factor_engine()
        assert singleton1 is singleton2, "get_factor_engine 应返回同一实例"
        print("    ✓ 全局单例正常")
        reset_factor_engine()

        # ── 测试 12: 输入校验 ──
        print("\n[12] 输入校验")
        try:
            engine.calculate(data=test_data, symbol="", factor_configs=factor_configs)
            assert False, "空 symbol 应抛出 ValueError"
        except ValueError:
            print("    ✓ 空 symbol 抛出 ValueError")

        try:
            engine.calculate(data=test_data, symbol="TEST", factor_configs=[])
            assert False, "空因子配置应抛出 ValueError"
        except ValueError:
            print("    ✓ 空因子配置抛出 ValueError")

        try:
            engine.calculate(data=pd.DataFrame(), symbol="TEST", factor_configs=factor_configs)
            assert False, "空数据应抛出 ValueError"
        except ValueError:
            print("    ✓ 空数据抛出 ValueError")

    finally:
        # 清理临时目录
        shutil.rmtree(tmp_cache_dir, ignore_errors=True)
        print(f"\n[13] 临时缓存目录已清理: {tmp_cache_dir}")

    print("\n" + "=" * 60)
    print("  ✓ factor_engine.py 全部自测通过")
    print("=" * 60)
