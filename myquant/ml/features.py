# 文件名：myquant/ml/features.py
"""
features.py - 特征工程（因子计算 + 标签生成）
==============================================
机器学习特征工程核心（任务18，ML 完整 MLOps 链路第一环）。

数据来源（铁律）:
  - 输入只能读取 processed 清洗后数据（Bar 列表或 DataFrame），禁止读取 raw 原始数据
  - 依赖 processed 而非 raw，与总图 myquant/ml/ 注释一致

核心能力:
  - FeatureConfig 特征配置（训练/推理共用，JSON 可序列化，杜绝训练-推理不一致）
  - FeatureEngine 特征引擎：
      compute_features()    批量特征计算（严格因果，trailing 滚动窗口）
      generate_labels()     标签生成（未来 N 期收益，仅训练集使用）
      build_dataset()       特征+标签对齐为 FeatureDataset（丢弃无标签尾部）
      build_panel_dataset() 面板构建（F-08）：dict[symbol→数据] 逐标的合并
                            为 PanelDataset（group_ids 记录归属，供日历化切分）
      compute_incremental() 实盘增量特征（逐 bar，与训练特征逐列一致）
      lookahead_guard()     防未来断言（截断重算一致性验证）
  - FeatureDataset / PanelDataset 特征数据集（X/y/timestamps，JSON 可序列化供训练/推理）

防未来函数（铁律4，本任务核心）:
  - 【特征】全部 trailing 窗口：pandas rolling/pct_change/shift(n) 只使用
    <=t 的 bar 数据，t 时刻特征绝不引用未来 bar
  - 【标签】forward_return = close.shift(-horizon)/close - 1 引用未来，
    但仅存在于训练目标列；build_dataset 丢弃无标签尾部行（不参与训练）
  - 【验证】lookahead_guard：对每行截断重算特征，断言与全量计算一致
    （若特征泄露未来，截断重算结果必不同 → 断言失败）
  - 本模块不提供任何预测方法；inference（任务21）只产出特征，不产出未来收益

为实盘无缝对接（任务22-23 实盘增强前置）:
  - compute_incremental：实盘收到新 bar 增量算特征，与训练特征逐列一致
  - FeatureConfig JSON 序列化：训练配置与实盘推理共用同一配置
  - 特征列名固定：训练/推理完全一致，杜绝训练-推理偏移

依赖: pandas, numpy, myquant.indicators, myquant.models.market_data
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from myquant.core.logger import get_logger

from myquant.models.backtest import CommissionModel, CommissionType, SlippageModel

logger = get_logger("ml.features")

try:
    from myquant.indicators import get_indicator
    INDICATORS_AVAILABLE = True
except ImportError:  # pragma: no cover
    get_indicator = None  # type: ignore[assignment]
    INDICATORS_AVAILABLE = False
    logger.warning("[ml] myquant.indicators 不可用，技术指标特征将跳过")

from myquant.indicators.tradability import TradabilityConfig, filter_tradable

try:
    from myquant.models.market_data import Bar
    BAR_AVAILABLE = True
except ImportError:  # pragma: no cover
    Bar = None  # type: ignore[assignment]
    BAR_AVAILABLE = False


# ═══════════════════════════════════════════════
#  默认特征配置
# ═══════════════════════════════════════════════

# 中文列契约（F-01 整改，2026-10-03）
# 与 myquant/indicators（_validate_data 要求 开盘价/最高价/最低价/收盘价）、
# scripts/数据导出_DB到processed.py 的 CN_COLUMN_MAP 同口径；
# ML 特征层统一使用中文列契约，禁止混用英文列（open/high/low/close）。
BAR_CN_COLUMN_MAP: Dict[str, str] = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "volume": "成交量",
    "amount": "成交额",
}
# 指标层 _validate_data 要求的最小列集（防御式校验用）
CN_REQUIRED_COLUMNS: List[str] = ["开盘价", "最高价", "最低价", "收盘价"]
# bars_to_df 输出列顺序（timestamp 为时间列，与 processed 契约一致）
BAR_CN_COLUMNS: List[str] = (
    ["timestamp"] + list(BAR_CN_COLUMN_MAP.values()) + ["trades"]
)

# 技术指标默认配置（复用 myquant.indicators，prefix 为列名前缀）
# 输出列命名规则：{prefix}_{指标输出列}，例如 sma_20 → sma_20_sma
DEFAULT_INDICATOR_CONFIGS: List[Dict[str, Any]] = [
    {"name": "sma", "params": {"period": 20}, "prefix": "sma_20"},
    {"name": "sma", "params": {"period": 60}, "prefix": "sma_60"},
    {"name": "ema", "params": {"period": 12}, "prefix": "ema_12"},
    {"name": "ema", "params": {"period": 26}, "prefix": "ema_26"},
    {"name": "rsi", "params": {"period": 14}, "prefix": "rsi_14"},
    {"name": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}, "prefix": "macd"},
    {"name": "kdj", "params": {"n": 9, "m1": 3, "m2": 3}, "prefix": "kdj"},
    {"name": "atr", "params": {"period": 14}, "prefix": "atr_14"},
    {"name": "bollinger_bands", "params": {"period": 20, "std_dev": 2.0}, "prefix": "bb"},
    {"name": "volatility", "params": {"period": 20}, "prefix": "vol_20"},
]

# 派生特征开关（基于已算指标列二次加工，全部 trailing）
DEFAULT_DERIVED_FEATURES: List[str] = ["sma_gap", "atr_norm", "bb_width"]


# ═══════════════════════════════════════════════
#  训练标签成本模型（F-09 训练目标纳入交易成本）
# ═══════════════════════════════════════════════

@dataclass
class TradeCost:
    """
    训练标签成本化模型（F-09 训练目标纳入交易成本）

    组合复用回测撮合层成本模型（CommissionModel/SlippageModel，同一类同一参数），
    保证训练标签口径与回测撮合层成本参数一致（验收：标签口径 = config sim_broker 段）。

    roundtrip_rate() 返回一笔「买入→卖出」往返的交易成本率（按成交金额比例）：
      佣金（买卖双向） + 印花税（仅卖出） + 过户费（买卖双向） + 滑点（买卖双向）
      = 0.0002*2 + 0.001 + 0.00001*2 + 2/10000*2 = 0.00182（A股写实默认口径）

    最低佣金（min_commission，5 元）是撮合层的金额维度约束；标签层是收益率维度、
    无单笔金额信息，按费率近似（与 config_builder 同源参数，口径对拍测试锁定）。

    【防未来函数】成本率是配置常量、不来自行情数据，非时序计算；
    本行使用的数据截至 t 时刻（成本化仅做常数减法）。
    """

    commission: CommissionModel
    slippage: SlippageModel

    def __post_init__(self) -> None:
        """防御式校验：佣金/印花税/过户费/滑点必须非负"""
        if self.commission.buy_rate < 0 or self.commission.sell_rate < 0:
            raise ValueError("佣金费率不能为负")
        if self.commission.stamp_tax < 0 or self.commission.transfer_fee < 0:
            raise ValueError("印花税/过户费费率不能为负")
        if self.slippage.fixed_bps < 0:
            raise ValueError("滑点基点不能为负")

    def roundtrip_rate(self) -> float:
        """双边往返成本率（按成交金额比例，一买一卖）"""
        slip = self.slippage.fixed_bps / 10000.0
        return (
            self.commission.buy_rate
            + self.commission.sell_rate
            + self.commission.stamp_tax
            + 2.0 * self.commission.transfer_fee
            + 2.0 * slip
        )

    @classmethod
    def from_sim_broker_cfg(cls, cfg: Dict[str, Any]) -> "TradeCost":
        """从 config 的 sim_broker 段构造（与回测撮合层同口径，见 config_builder）"""
        slippage_mode = cfg.get("slippage_mode", "ratio")
        slippage_value = cfg.get("slippage_value", 0.0002)
        # ratio 模式：slippage_value 是比例（如 0.0002=万2），转为基点（与 config_builder 一致）
        fixed_bps = (
            slippage_value * 10000.0 if slippage_mode == "ratio" else slippage_value
        )
        return cls(
            commission=CommissionModel(
                type=CommissionType.RATIO,
                buy_rate=cfg.get("commission_rate", 0.0002),
                sell_rate=cfg.get("commission_rate", 0.0002),
                min_commission=cfg.get("min_commission", 5.0),
                stamp_tax=cfg.get("stamp_tax_rate", 0.001),
                transfer_fee=cfg.get("transfer_fee_rate", 0.00001),
            ),
            slippage=SlippageModel(type="fixed_bps", fixed_bps=fixed_bps),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的 dict（枚举转字符串）"""
        commission = asdict(self.commission)
        commission["type"] = commission["type"].value
        return {"commission": commission, "slippage": asdict(self.slippage)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeCost":
        """从 dict 反序列化（缺失字段用模型默认值，兼容旧数据）"""
        cdata = data.get("commission") or {}
        if "type" in cdata and isinstance(cdata["type"], str):
            cdata = {**cdata, "type": CommissionType(cdata["type"])}
        cknown = {f.name for f in CommissionModel.__dataclass_fields__.values()}
        sknown = {f.name for f in SlippageModel.__dataclass_fields__.values()}
        return cls(
            commission=CommissionModel(
                **{k: v for k, v in cdata.items() if k in cknown}),
            slippage=SlippageModel(
                **{k: v for k, v in (data.get("slippage") or {}).items()
                   if k in sknown}),
        )


# ═══════════════════════════════════════════════
#  FeatureConfig 特征配置
# ═══════════════════════════════════════════════

@dataclass
class FeatureConfig:
    """
    特征配置（训练/推理共用，JSON 可序列化）

    通过 to_dict()/from_dict() 持久化，训练与实盘推理使用同一份配置，
    保证特征逐列一致（杜绝训练-推理偏移）。

    Attributes
    ----------
    horizon : int
        标签未来收益期数（forward_return 的 lookahead 步数）
    label_type : str
        标签类型：return（连续收益）/ direction（涨跌二分类）
    price_col : str
        价格列名（默认 close）
    return_windows : List[int]
        多期收益窗口（return_N = close.pct_change(N)）
    rolling_windows : List[int]
        滚动均值/标准差/最大/最小窗口
    momentum_windows : List[int]
        动量窗口（momentum_N = close/close.shift(N) - 1）
    indicator_configs : List[Dict]
        技术指标配置（复用 myquant.indicators）
    derived_features : List[str]
        派生特征开关（sma_gap/atr_norm/bb_width）
    min_periods : int
        特征最小有效行数（窗口不足置 NaN）
    cost : Optional[TradeCost]
        训练标签成本化模型（F-09；None=不成本化零变化）。
        开启后 generate_labels 的 forward_return 扣除双边往返成本率（与回测
        撮合层 sim_broker 段同口径），direction 用扣费后方向。
    tradability : Optional[Dict[str, Any]]
        F-11 可交易性过滤配置（None=不过滤零变化）。dict 为
        TradabilityConfig 字段（min_avg_amount/lookback_days/limit_pct/
        min_periods/filter_suspend/filter_liquidity/filter_limit）；
        启用后 compute_features/generate_labels 对不可交易行置 NaN
        （行级 mask 保对齐，build_dataset dropna 自动剔除）。
    """
    horizon: int = 5
    label_type: str = "return"
    price_col: str = "收盘价"
    return_windows: List[int] = field(default_factory=lambda: [1, 5, 10, 20])
    rolling_windows: List[int] = field(default_factory=lambda: [5, 20, 60])
    momentum_windows: List[int] = field(default_factory=lambda: [5, 20])
    indicator_configs: List[Dict[str, Any]] = field(
        default_factory=lambda: [dict(c) for c in DEFAULT_INDICATOR_CONFIGS]
    )
    derived_features: List[str] = field(
        default_factory=lambda: list(DEFAULT_DERIVED_FEATURES)
    )
    min_periods: int = 30
    cost: Optional[TradeCost] = None
    tradability: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """序列化为可 JSON 持久化的 dict（cost 走 TradeCost.to_dict 枚举字符串化）"""
        d = asdict(self)
        if self.cost is not None:
            d["cost"] = self.cost.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FeatureConfig":
        """从 dict 反序列化（缺失字段用默认值；cost 嵌套 dict 转 TradeCost）"""
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        if isinstance(kwargs.get("cost"), dict):
            kwargs["cost"] = TradeCost.from_dict(kwargs["cost"])
        return cls(**kwargs)

    def to_json(self) -> str:
        """序列化为 JSON 字符串（训练配置持久化）"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "FeatureConfig":
        """从 JSON 字符串反序列化"""
        return cls.from_dict(json.loads(text))

    @property
    def max_lookback(self) -> int:
        """
        最大回看窗口（增量推理需缓存的历史 bar 数）

        取所有滚动窗口与指标周期的最大值 + 缓冲。
        """
        windows = list(self.return_windows) + list(self.rolling_windows) \
            + list(self.momentum_windows)
        for cfg in self.indicator_configs:
            for v in cfg.get("params", {}).values():
                if isinstance(v, int) and v > 1:
                    windows.append(v)
        return max(windows) if windows else self.min_periods


# ═══════════════════════════════════════════════
#  FeatureDataset 特征数据集
# ═══════════════════════════════════════════════

@dataclass
class FeatureDataset:
    """
    特征数据集（build_dataset 产物）

    Attributes
    ----------
    X : pd.DataFrame
        特征矩阵（行 = 样本，列 = feature_columns；无标签尾部已丢弃）
    y : pd.Series
        标签（未来 horizon 期收益或涨跌方向）
    timestamps : List[float]
        样本对应时间戳（与 X 行一一对应）
    feature_columns : List[str]
        特征列名
    label_column : str
        标签列名
    """
    X: pd.DataFrame
    y: pd.Series
    timestamps: List[float]
    feature_columns: List[str]
    label_column: str

    @property
    def n_samples(self) -> int:
        return len(self.X)

    def to_dict(self) -> Dict[str, Any]:
        """序列化（JSON 安全：NaN → None）"""
        return {
            "X": self.X.where(pd.notna(self.X), None).to_dict(orient="list"),
            "y": self.y.where(pd.notna(self.y), None).tolist(),
            "timestamps": [float(t) if t is not None else None for t in self.timestamps],
            "feature_columns": list(self.feature_columns),
            "label_column": self.label_column,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def __repr__(self) -> str:
        return (
            f"FeatureDataset(n_samples={self.n_samples}, features={len(self.feature_columns)}, "
            f"label={self.label_column})"
        )


@dataclass
class PanelDataset:
    """
    面板特征数据集（F-08 多标的 build_panel_dataset 产物）

    Attributes
    ----------
    X : pd.DataFrame
        特征矩阵（面板合并：行 = 全部标的样本，列 = feature_columns）
    y : pd.Series
        标签（未来 horizon 期收益或涨跌方向）
    timestamps : List[float]
        样本对应时间戳（与 X 行一一对应，按各标的合并顺序排列）
    group_ids : List[str]
        每个样本的标的归属 id（与 X 行一一对应；同时间戳跨标样本并列保留）
    feature_columns : List[str]
        特征列名（各标的必须一致，否则构建报错）
    label_column : str
        标签列名
    """
    X: pd.DataFrame
    y: pd.Series
    timestamps: List[float]
    group_ids: List[str]
    feature_columns: List[str]
    label_column: str

    @property
    def n_samples(self) -> int:
        """面板总样本数（X 行数）"""
        return len(self.X)

    @property
    def symbols(self) -> List[str]:
        """面板标的列表（排序去重，可追溯面板构成）"""
        return sorted(set(self.group_ids))

    def __repr__(self) -> str:
        return (
            f"PanelDataset(n_samples={self.n_samples}, symbols={len(self.symbols)}, "
            f"features={len(self.feature_columns)}, label={self.label_column})"
        )


# ═══════════════════════════════════════════════
#  辅助：Bar 列表 → DataFrame
# ═══════════════════════════════════════════════

def bars_to_df(bars: List[Any]) -> pd.DataFrame:
    """
    将 Bar 列表转换为标准 DataFrame（中文列契约）

    输入必须是 processed 清洗后数据（Bar 对象列表），禁止 raw 原始数据。
    输出列：timestamp/开盘价/最高价/最低价/收盘价/成交量/成交额/trades
    （F-01 整改：列名与指标层、processed 导出层统一为中文契约，
    杜绝指标计算因列名不匹配被静默跳过）。

    Parameters
    ----------
    bars : List[Bar]
        Bar 对象列表（myquant.models.market_data.Bar）

    Returns
    -------
    pd.DataFrame : 标准化行情 DataFrame（按 timestamp 升序，中文列契约）
    """
    if not bars:
        return pd.DataFrame(columns=BAR_CN_COLUMNS)
    rows = []
    for b in bars:
        rows.append({
            "timestamp": float(getattr(b, "timestamp", 0.0)),
            "开盘价": float(getattr(b, "open", 0.0)),
            "最高价": float(getattr(b, "high", 0.0)),
            "最低价": float(getattr(b, "low", 0.0)),
            "收盘价": float(getattr(b, "close", 0.0)),
            "成交量": float(getattr(b, "volume", 0.0)),
            "成交额": float(getattr(b, "amount", 0.0)),
            "trades": int(getattr(b, "trades", 0) or 0),
        })
    df = pd.DataFrame(rows, columns=BAR_CN_COLUMNS)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════
#  FeatureEngine 特征引擎
# ═══════════════════════════════════════════════

class FeatureEngine:
    """
    特征工程引擎（任务18）

    特征全部 trailing 滚动窗口（严格因果，不使用未来数据）；
    标签仅用于训练（build_dataset 丢弃无标签尾部）。

    为实盘对接：compute_incremental 支持逐 bar 增量特征，
    与训练 compute_features 使用同一 FeatureConfig，特征逐列一致。
    """

    def __init__(self, config: Optional[FeatureConfig] = None):
        self.config = config if config is not None else FeatureConfig()
        logger.info(
            f"[ml] 特征引擎初始化：{len(self.config.indicator_configs)}指标, "
            f"horizon={self.config.horizon}, max_lookback={self.config.max_lookback}"
        )

    # ─────────────────────────────────────────────
    #  输入准备
    # ─────────────────────────────────────────────

    def _prepare_df(self, data: Any) -> pd.DataFrame:
        """标准化输入：Bar 列表或 DataFrame → 按时间升序的 DataFrame（中文列契约）"""
        if isinstance(data, pd.DataFrame):
            df = data.copy()
        elif BAR_AVAILABLE and isinstance(data, (list, tuple)) and data and isinstance(data[0], Bar):
            df = bars_to_df(list(data))  # type: ignore[arg-type]
        elif isinstance(data, (list, tuple)):
            df = pd.DataFrame(data) if data else pd.DataFrame()
        else:
            raise TypeError(f"不支持的数据类型: {type(data)}")
        if df.empty:
            return df
        # 契约校验（F-01：统一中文列契约，杜绝英文列导致指标静默跳过/特征漂移）
        if self.config.price_col not in df.columns:
            raise ValueError(
                f"输入数据缺少价格列: {self.config.price_col!r}。"
                f"ML 特征层统一使用中文列契约（收盘价等，与 processed 导出层/"
                f"指标层一致），当前列: {list(df.columns)}。"
                f"请勿混用英文列（open/high/low/close）。"
            )
        missing_cn = [c for c in CN_REQUIRED_COLUMNS if c not in df.columns]
        if missing_cn:
            raise ValueError(
                f"输入数据缺少中文契约列: {missing_cn}（当前列: {list(df.columns)}）。"
                f"ML 特征层与指标层统一要求中文列契约，请使用 processed 数据"
                f"或 bars_to_df 输出。"
            )
        # 按时间升序（防未来：确保特征按时间因果顺序计算）
        if "timestamp" in df.columns:
            df = df.sort_values("timestamp")
        df = df.reset_index(drop=True)
        return df

    def _tradability_mask(self, df: pd.DataFrame) -> pd.Series:
        """F-11 行级可交易性 mask（True=可交易，index 与 df 对齐）。

        config.tradability 为 None 时返回全 True（现有行为零变化）；
        启用时调用共享过滤函数 filter_tradable（与多因子策略同口径）。

        【防未来函数】filter_tradable 内部均额 rolling 只消费 <=t 成交额，
        一字板判定用 t 时点收盘后已得 OHLC/涨跌幅，无未来引用。

        Args:
            df: 中文列契约 DataFrame（_prepare_df 产物，时间升序）。

        Returns:
            pd.Series：bool 行级 mask（全部 True=不过滤）。
        """
        if self.config.tradability is None:
            return pd.Series(True, index=df.index, dtype=bool)
        cfg = TradabilityConfig.from_dict(self.config.tradability)
        mask, _ = filter_tradable(df, cfg)
        return mask.astype(bool)

    # ─────────────────────────────────────────────
    #  特征计算（严格因果）
    # ─────────────────────────────────────────────

    def compute_features(self, data: Any) -> pd.DataFrame:
        """
        批量计算特征（严格因果）

        【防未来函数】所有特征使用 pandas rolling/pct_change/shift(n) 的
        trailing 窗口，t 时刻特征只依赖 <=t 的 bar，绝不引用未来。

        Returns
        -------
        pd.DataFrame : 特征列（含派生特征），index 与输入对齐
        """
        df = self._prepare_df(data)
        if df.empty:
            return pd.DataFrame()
        close = df[self.config.price_col].astype(float)
        feat = pd.DataFrame(index=df.index)

        # 1. 价格衍生（trailing）
        for n in self.config.return_windows:
            feat[f"return_{n}"] = close.pct_change(periods=n)
        feat["log_return_1"] = np.log(close / close.shift(1))
        for w in self.config.rolling_windows:
            feat[f"rolling_mean_{w}"] = close.rolling(w).mean()
            feat[f"rolling_std_{w}"] = close.rolling(w).std()
            feat[f"rolling_max_{w}"] = close.rolling(w).max()
            feat[f"rolling_min_{w}"] = close.rolling(w).min()
        for n in self.config.momentum_windows:
            feat[f"momentum_{n}"] = close / close.shift(n) - 1

        # 2. 技术指标（复用 myquant.indicators）
        # F-01 整改：指标计算失败显式抛错（记录日志后向上抛出），
        # 禁止静默跳过——静默跳过曾导致训练实际特征列与静态列名漂移。
        if INDICATORS_AVAILABLE:
            for cfg in self.config.indicator_configs:
                name = cfg["name"]
                params = cfg.get("params", {})
                prefix = cfg.get("prefix", name)
                try:
                    indicator = get_indicator(name, **params)
                    res = indicator.calculate(df)
                except Exception as e:
                    logger.error(
                        "[ml] 指标 %s 计算失败（F-01 起禁止静默跳过）: %s", name, e)
                    raise
                out_cols = [c for c in res.columns if c not in df.columns]
                for oc in out_cols:
                    feat[f"{prefix}_{oc}"] = res[oc]

        # 3. 派生特征（基于已算指标列，trailing）
        self._compute_derived(feat, close, df)

        # 4. 窗口不足置 NaN（min_periods 之前）
        if self.config.min_periods > 0 and len(feat) > 0:
            feat.iloc[: self.config.min_periods] = np.nan

        # 5. F-11 可交易性过滤：不可交易行特征置 NaN（行级 mask 保对齐，
        #    build_dataset dropna 自动剔除；防未来见 _tradability_mask docstring）
        if self.config.tradability is not None and not feat.empty:
            feat.loc[~self._tradability_mask(df), :] = np.nan

        return feat

    def _compute_derived(
        self, feat: pd.DataFrame, close: pd.Series, df: pd.DataFrame
    ) -> None:
        """派生特征（基于已算指标列，全部 trailing）"""
        for name in self.config.derived_features:
            try:
                if name == "sma_gap" and "sma_20_sma" in feat.columns and "sma_60_sma" in feat.columns:
                    feat["sma_gap"] = feat["sma_20_sma"] / feat["sma_60_sma"] - 1
                elif name == "atr_norm" and "atr_14_atr" in feat.columns:
                    feat["atr_norm"] = feat["atr_14_atr"] / close
                elif name == "bb_width" and "bb_upper" in feat.columns and "bb_lower" in feat.columns and "bb_mid" in feat.columns:
                    feat["bb_width"] = (feat["bb_upper"] - feat["bb_lower"]) / feat["bb_mid"]
            except Exception as e:  # pragma: no cover
                logger.warning(f"[ml] 派生特征 {name} 计算失败: {e}")

    @property
    def feature_columns(self) -> List[str]:
        """
        特征列名（按默认配置预演计算，供 build_dataset/序列化使用）

        【说明】该属性需基于实际数据计算得出最准确，这里提供默认配置下
        的静态列名清单，用于快速获取列名；动态场景请以 compute_features 输出为准。
        """
        cols: List[str] = []
        for n in self.config.return_windows:
            cols.append(f"return_{n}")
        cols.append("log_return_1")
        for w in self.config.rolling_windows:
            cols += [f"rolling_mean_{w}", f"rolling_std_{w}",
                     f"rolling_max_{w}", f"rolling_min_{w}"]
        for n in self.config.momentum_windows:
            cols.append(f"momentum_{n}")
        if INDICATORS_AVAILABLE:
            for cfg in self.config.indicator_configs:
                name = cfg["name"]
                prefix = cfg.get("prefix", name)
                # F-01：指标实例化失败显式抛出（与 compute_features 一致，
                # 禁止静默降级导致静态列名与实际列漂移）
                indicator = get_indicator(name, **cfg.get("params", {}))
                for oc in getattr(indicator, "output_columns", []) or []:
                    cols.append(f"{prefix}_{oc}")
        for name in self.config.derived_features:
            if name in ("sma_gap", "atr_norm", "bb_width"):
                cols.append(name)
        return cols

    # ─────────────────────────────────────────────
    #  标签生成（仅训练集使用）
    # ─────────────────────────────────────────────

    def generate_labels(
        self,
        data: Any,
        horizon: Optional[int] = None,
        label_type: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        生成标签（未来 horizon 期收益 / 涨跌方向）

        【防未来函数】标签引用未来数据，但仅用于训练目标：
          - return 型：forward_return = close.shift(-horizon)/close - 1
            配置 cost（F-09）时扣双边往返成本率：毛收益 − roundtrip_rate
          - direction 型：(forward_return > 0) 二分类（cost 开启时用扣费后方向）
        成本率是配置常量、非时序数据（本行使用的数据截至 t 时刻）；成本化只做
        常数减法，不引入任何未来行情引用。
        标签列在 build_dataset 中与特征对齐，无标签尾部行被丢弃（不参与训练）。
        推理阶段绝不使用标签列。
        """
        df = self._prepare_df(data)
        if df.empty:
            return pd.DataFrame()
        horizon = horizon if horizon is not None else self.config.horizon
        label_type = label_type if label_type is not None else self.config.label_type
        close = df[self.config.price_col].astype(float)
        forward_return = close.shift(-horizon) / close - 1.0
        cost = self.config.cost
        if cost is not None:
            # F-09 训练目标纳入交易成本：扣双边往返成本率（一买一卖，与撮合层同口径）
            forward_return = forward_return - cost.roundtrip_rate()
        labels = pd.DataFrame(index=df.index)
        labels["forward_return"] = forward_return
        if label_type == "direction":
            labels["direction"] = (forward_return > 0).astype(float)
            labels.loc[forward_return.isna(), "direction"] = np.nan
        labels["horizon"] = horizon
        # F-11 可交易性过滤：不可交易行标签置 NaN（保行对齐，build_dataset
        # 中与特征一并 dropna；防未来见 _tradability_mask docstring）
        if self.config.tradability is not None and not labels.empty:
            labels.loc[~self._tradability_mask(df), ["forward_return", "direction"]] = np.nan
        return labels

    @property
    def label_column(self) -> str:
        """标签列名"""
        return "direction" if self.config.label_type == "direction" else "forward_return"

    # ─────────────────────────────────────────────
    #  训练数据集构建
    # ─────────────────────────────────────────────

    def build_dataset(self, data: Any) -> FeatureDataset:
        """
        构建训练数据集（特征 + 标签对齐）

        【防未来函数】丢弃：
          1. 窗口不足的行（特征 NaN，min_periods 之前）
          2. 无标签的尾部行（最后 horizon 行，标签 NaN）
        返回的 X/y 只包含特征与标签均有效的样本。

        Returns
        -------
        FeatureDataset : X/y/timestamps（JSON 可序列化）
        """
        df = self._prepare_df(data)
        if df.empty:
            return FeatureDataset(
                X=pd.DataFrame(), y=pd.Series(dtype=float),
                timestamps=[], feature_columns=[], label_column=self.label_column,
            )
        feat = self.compute_features(df)
        labels = self.generate_labels(df)
        fcols = [c for c in feat.columns if c != self.config.price_col]
        combined = pd.concat([feat, labels], axis=1)
        keep = [c for c in fcols] + [self.label_column]
        sub = combined[keep]
        valid = sub.dropna(subset=keep)
        timestamps = (
            df.loc[valid.index, "timestamp"].tolist()
            if "timestamp" in df.columns else list(valid.index)
        )
        X = valid[fcols]
        y = valid[self.label_column]
        return FeatureDataset(
            X=X, y=y, timestamps=timestamps,
            feature_columns=fcols, label_column=self.label_column,
        )

    def build_panel_dataset(self, data: Dict[str, Any]) -> PanelDataset:
        """
        构建面板特征数据集（F-08 多标的训练入口配套）。

        面板 = dict[symbol → 单标的 processed 数据（DataFrame / Bar 列表）]：
        逐标的调用 build_dataset（特征+标签对齐，各标的口径与单标的完全一致），
        合并为面板 X/y/timestamps，group_ids 记录每个样本的标的归属。

        同时间戳跨标样本并列保留（不做时间对齐裁剪）——时序切分阶段以
        "整组"（同一时间戳全部标的样本）为单位确定性归属，保证同时间戳
        样本永不跨训练/验证边界（防未来）。

        【防未来函数】只消费各标的训练时已得数据：逐标的丢弃窗口不足与
        无标签尾部行，与单标的 build_dataset 同口径，无未来引用。

        Parameters
        ----------
        data : dict[str, DataFrame | Bar 列表]
            面板数据：symbol → 单标的 processed 数据（与单标的 build_dataset
            同输入格式）

        Returns
        -------
        PanelDataset : 面板特征数据集（X/y/timestamps/group_ids 行级对齐）

        Raises
        ------
        ValueError : data 非 dict / 空 / 无任何有效样本 / 各标的特征列不一致
        """
        if not isinstance(data, dict) or not data:
            raise ValueError(
                f"面板数据必须是非空 dict[symbol→数据]，实际 {type(data).__name__}")
        frames_x: List[pd.DataFrame] = []
        frames_y: List[pd.Series] = []
        all_ts: List[float] = []
        all_gids: List[str] = []
        fcols: Optional[List[str]] = None
        for sym, sub in data.items():
            sym_id = str(sym)
            ds = self.build_dataset(sub)
            if ds.n_samples == 0:
                logger.warning("[ml] 面板标的 %s 样本不足被全部丢弃，跳过", sym_id)
                continue
            if fcols is None:
                fcols = list(ds.feature_columns)
            elif list(ds.feature_columns) != fcols:
                raise ValueError(
                    f"面板特征列不一致: 标 {sym_id} {list(ds.feature_columns)} != {fcols}")
            frames_x.append(ds.X)
            frames_y.append(ds.y)
            all_ts.extend(float(t) for t in ds.timestamps)
            all_gids.extend([sym_id] * ds.n_samples)
        if not frames_x:
            raise ValueError("面板数据无任何有效样本（全部标的样本不足被丢弃）")
        panel_x = pd.concat(frames_x, axis=0, ignore_index=True)
        panel_y = pd.concat(frames_y, axis=0, ignore_index=True)
        return PanelDataset(
            X=panel_x, y=panel_y, timestamps=all_ts, group_ids=all_gids,
            feature_columns=list(fcols or []), label_column=self.label_column,
        )

    # ─────────────────────────────────────────────
    #  实盘增量特征（为任务21-23 实盘对接）
    # ─────────────────────────────────────────────

    def compute_incremental(self, data: Any, lookback: Optional[int] = None) -> Dict[str, Any]:
        """
        实盘增量特征（只计算最新 bar 的特征）

        【与训练精确一致】默认使用全部传入数据（从序列起点计算到最新 bar），
        与训练阶段 compute_features(df) 的最后一行**逐列精确一致**——
        这是实盘推理与训练特征对齐的硬保证（杜绝训练-推理偏移）。

        Parameters
        ----------
        data : Bar 列表 / DataFrame
            历史数据（实盘场景：引擎启动时加载 processed 历史，
            之后每根新 bar 追加后调用本方法）
        lookback : int | None
            可选：显式截断最近 N 根重算。
            ⚠️ 仅对纯 trailing 统计（pct_change/rolling/momentum/MA）精确；
            递推指标（EMA/RSI/MACD/KDJ）截断后递推起点变化会产生 warmup
            偏移，与训练全量结果可能不一致。默认 None（全量，精确一致）。

        Returns
        -------
        dict : 最新 bar 的特征（NaN → None，JSON 安全）
        """
        df = self._prepare_df(data)
        if df.empty:
            return {}
        if lookback is not None:
            # 显式截断（仅 trailing 统计精确，递推指标有 warmup 偏移）
            df = df.iloc[-lookback:].reset_index(drop=True)
        feats = self.compute_features(df)
        if feats.empty:
            return {}
        row = feats.iloc[-1]
        return {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}

    def create_incremental_session(self) -> "IncrementalFeatureSession":
        """创建有状态增量特征会话（F-14）。

        实盘逐 bar 场景：首次 update(全量历史) warmup，之后每来新 bar
        update(历史+新bar) 只做 O(1)/O(窗口) 增量，单 bar 耗时不随历史增长。

        Returns:
            IncrementalFeatureSession 实例。
        """
        return IncrementalFeatureSession(self)

    # ─────────────────────────────────────────────
    #  防未来断言
    # ─────────────────────────────────────────────

    @staticmethod
    def lookahead_guard(engine: "FeatureEngine", data: Any, max_rows: int = 10) -> bool:
        """
        防未来断言：截断重算一致性验证

        对前 i+1 行用同配置重算特征，断言第 i 行特征值与全量计算一致。
        若特征泄露未来数据，截断重算结果必与全量不同 → 返回 False。

        Parameters
        ----------
        engine : FeatureEngine
            特征引擎
        data : Bar 列表 / DataFrame
            输入数据
        max_rows : int
            验证的最大行数（每行都做一次截断重算）

        Returns
        -------
        bool : True=通过防未来断言（特征严格因果）
        """
        df = engine._prepare_df(data)
        if len(df) < 3:
            return True
        full = engine.compute_features(df)
        if full.empty:
            return True
        cols = [c for c in full.columns]
        # P3-19：从预热结束后取样，避免早期全NaN的行被 continue 空过（空过会return True误判通过）
        start = min(len(df), max(2, len(df) // 2))
        checked = 0
        for i in range(start, min(len(df), start + max_rows)):
            prefix = df.iloc[: i + 1]
            if len(prefix) < 3:
                continue
            prefix_feat = engine.compute_features(prefix)
            if prefix_feat.empty:
                continue
            for col in cols:
                v_full = full.iloc[i][col]
                v_pref = prefix_feat.iloc[-1][col]
                if pd.isna(v_full) and pd.isna(v_pref):
                    continue
                if pd.isna(v_full) != pd.isna(v_pref):
                    return False
                if not pd.isna(v_full) and abs(float(v_full) - float(v_pref)) > 1e-9:
                    return False
                checked += 1
        # P3-19：若全程无任何有效（非全NaN）比较样本，不能放行
        return checked > 0


# ═══════════════════════════════════════════════════════════
#  增量特征会话（F-14）：有状态 warmup + push(new_bar)，O(1)/O(窗口)
# ═══════════════════════════════════════════════════════════

# 基类 update 方法引用（用于检测子类是否真正实现了流式 update）
_BASE_INDICATOR_UPDATE = None
if INDICATORS_AVAILABLE:
    try:
        from myquant.indicators import IndicatorBase as _IB
        _BASE_INDICATOR_UPDATE = _IB.update
    except ImportError:  # pragma: no cover
        _BASE_INDICATOR_UPDATE = None


class IncrementalFeatureSession:
    """有状态增量特征会话（F-14）。

    解决问题：原 compute_incremental 每次对全量历史 compute_features 重算，
    耗时随历史长度 O(N) 线性增长。本会话：
      - 首次 update(df)：回放全量历史，逐行喂入各指标流式 update() 建立内部状态，
        价格衍生特征落入 deque 环形缓冲；返回最后一行特征。
      - 之后 update(df_grown)：只处理新增行（df 比上次多的尾部），每个指标 O(1)/O(窗口)，
        单 bar 耗时不随历史长度增长。

    防未来函数（铁律 11）：
      - 流式 update(bar) 传入的 bar 视为已收盘 K 线，结果在该 bar 收盘后可用；
      - deque 只保留最近 max_window+2 根收盘价，计算尾部特征仅用 <=t 数据；
      - 不生成任何标签（无 forward_return 未来引用）。

    降级兜底（零变化纪律）：
      - 任一已配置指标未实现流式 update()（沿用基类 NotImplementedError），
        会话自动置 streaming_ok=False，后续每次 update 回退全量 compute_features 重算——
        宁可慢，不可错。

    Usage:
        engine = FeatureEngine(cfg)
        sess = IncrementalFeatureSession(engine)
        feats0 = sess.update(df_history)           # warmup（含全量历史）
        feats1 = sess.update(df_history + new_bar) # 仅新 bar，O(1)
    """

    def __init__(self, engine: "FeatureEngine"):
        """
        Args:
            engine: 已配置好的 FeatureEngine（复用其 config 与 _prepare_df）。
        """
        self.engine = engine
        self.config = engine.config

        # 收盘价环形缓冲（价格衍生特征尾部 O(窗口) 计算用）
        need = 1
        for lst in (self.config.return_windows,
                    self.config.rolling_windows,
                    self.config.momentum_windows):
            if lst:
                need = max(need, max(lst))
        self._closes: deque[float] = deque(maxlen=need + 2)

        # 流式指标实例：[indicator, prefix, out_cols, last_return_dict]
        self._indicators: List[Any] = []
        self._streaming_ok = True
        if INDICATORS_AVAILABLE:
            for cfg in self.config.indicator_configs:
                name = cfg["name"]
                params = cfg.get("params", {})
                prefix = cfg.get("prefix", name)
                ind = get_indicator(name, **params)
                # 检测是否真正实现了 update()（仍为基类方法 → 未实现）
                if _BASE_INDICATOR_UPDATE is not None and type(ind).update is _BASE_INDICATOR_UPDATE:
                    self._streaming_ok = False
                self._indicators.append(
                    [ind, prefix, list(getattr(ind, "output_columns", []) or []), {}]
                )

        # 已喂入行数（用于 diff 新增尾部）
        self._last_len: int = 0
        self._ready: bool = False

    # ─────────────────────────────────────────
    #  价格衍生特征（deque 尾部，O(窗口)）
    # ─────────────────────────────────────────
    def _price_features_tail(self) -> Dict[str, float]:
        """从收盘价 deque 计算最后一行的价格衍生特征（全部 trailing）。

        【防未来】只用 deque 内 <=t 的收盘价。
        """
        c = list(self._closes)
        n = len(c)
        out: Dict[str, float] = {}
        if n == 0:
            return out
        last = c[-1]
        for w in self.config.return_windows:
            out[f"return_{w}"] = last / c[-1 - w] - 1.0 if n > w else float("nan")
        out["log_return_1"] = (
            float(np.log(last / c[-2])) if n >= 2 else float("nan")
        )
        for w in self.config.rolling_windows:
            if n >= w:
                win = c[-w:]
                out[f"rolling_mean_{w}"] = float(np.mean(win))
                out[f"rolling_std_{w}"] = float(np.std(win, ddof=1))
                out[f"rolling_max_{w}"] = float(np.max(win))
                out[f"rolling_min_{w}"] = float(np.min(win))
            else:
                for k in ("mean", "std", "max", "min"):
                    out[f"rolling_{k}_{w}"] = float("nan")
        for w in self.config.momentum_windows:
            out[f"momentum_{w}"] = last / c[-1 - w] - 1.0 if n > w else float("nan")
        return out

    def _compose_row(self, last_close: float) -> Dict[str, Any]:
        """组合价格衍生 + 指标流式末值 + 派生特征，NaN→None（JSON 安全）。"""
        out = self._price_features_tail()
        # 指标流式末值（key 加 prefix，与 compute_features 列名一致）
        for _ind, prefix, out_cols, last_ret in self._indicators:
            for oc in out_cols:
                v = last_ret.get(oc, float("nan"))
                out[f"{prefix}_{oc}"] = v
        # 派生特征（与 _compute_derived 同口径）
        for name in self.config.derived_features:
            try:
                if (name == "sma_gap" and "sma_20_sma" in out
                        and "sma_60_sma" in out
                        and not pd.isna(out.get("sma_20_sma"))
                        and not pd.isna(out.get("sma_60_sma"))):
                    out["sma_gap"] = out["sma_20_sma"] / out["sma_60_sma"] - 1.0
                elif (name == "atr_norm" and "atr_14_atr" in out
                      and not pd.isna(out.get("atr_14_atr"))
                      and last_close != 0):
                    out["atr_norm"] = out["atr_14_atr"] / last_close
                elif (name == "bb_width" and "bb_upper" in out
                      and "bb_lower" in out and "bb_mid" in out
                      and not pd.isna(out.get("bb_mid"))
                      and out.get("bb_mid") not in (0, 0.0)):
                    out["bb_width"] = (
                        out["bb_upper"] - out["bb_lower"]
                    ) / out["bb_mid"]
            except Exception as e:  # pragma: no cover
                logger.warning("[ml] 派生特征 %s 增量计算失败: %s", name, e)
        # NaN → None
        return {k: (None if pd.isna(v) else float(v)) for k, v in out.items()}

    # ─────────────────────────────────────────
    #  主入口：update（自动区分 warmup / push）
    # ─────────────────────────────────────────
    def update(self, data: Any) -> Dict[str, Any]:
        """喂入数据（全量历史或历史+新 bar），返回最新 bar 特征 dict。

        首次调用自动 warmup；后续调用自动 diff 新增尾部行做 O(1) push。

        Args:
            data: Bar 列表 / DataFrame（中文列契约，时间升序）。

        Returns:
            dict：最新 bar 特征（NaN→None，JSON 安全）。
        """
        df = self.engine._prepare_df(data)
        if df.empty:
            return {}

        # 降级：任一指标无流式 update → 每次全量重算（零变化兜底）
        if not self._streaming_ok:
            feats = self.engine.compute_features(df)
            self._last_len = len(df)
            if feats.empty:
                return {}
            row = feats.iloc[-1]
            return {k: (None if pd.isna(v) else float(v)) for k, v in row.items()}

        new_start = self._last_len if self._ready else 0
        # 数据变短/重置：强制 warmup
        if len(df) < self._last_len:
            new_start = 0
            self._closes.clear()
            for tup in self._indicators:
                tup[0].reset()
                tup[3] = {}

        for i in range(new_start, len(df)):
            row = df.iloc[i]
            self._closes.append(float(row[self.config.price_col]))
            bar = row.to_dict()
            degraded = False
            for ind, _prefix, _out_cols, last_ret in self._indicators:
                try:
                    last_ret.update(ind.update(bar))
                except NotImplementedError:
                    # 运行期才暴露的未实现 → 降级全量（保险）
                    self._streaming_ok = False
                    degraded = True
                    break
            if degraded:
                feats = self.engine.compute_features(df)
                self._last_len = len(df)
                if feats.empty:
                    return {}
                r = feats.iloc[-1]
                return {k: (None if pd.isna(v) else float(v)) for k, v in r.items()}

        self._last_len = len(df)
        self._ready = True
        last_close = float(df.iloc[-1][self.config.price_col])
        return self._compose_row(last_close)

    @property
    def streaming_ok(self) -> bool:
        """当前是否走流式增量（False=已降级全量重算）。"""
        return self._streaming_ok

    @property
    def rows_fed(self) -> int:
        """已喂入会话的行数。"""
        return self._last_len


__all__ = [
    "FeatureConfig",
    "FeatureEngine",
    "FeatureDataset",
    "bars_to_df",
    "DEFAULT_INDICATOR_CONFIGS",
    "DEFAULT_DERIVED_FEATURES",
    "IncrementalFeatureSession",
]
