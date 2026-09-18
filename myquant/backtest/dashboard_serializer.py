# 文件名：myquant/backtest/dashboard_serializer.py
"""
dashboard_serializer.py - 回测看板数据序列化器
================================================

将回测引擎真实 ``BacktestResult`` 序列化为回测看板
（``docs/design/回测看板.html``）可消费的 ``__DASHBOARD_DATA__`` JSON，
替换 §DATA 区块的 MOCK 数据生成器，产出单文件真实数据看板。

对接依据
--------
``docs/design/回测看板_数据对接_最终定稿.md``（v2.0, 2026-09-13）：

- 只注入 ``EQUITY_CURVE + TRADES + 配置/元数据``，指标由看板前端 §CALC 现算，**不注入 metrics**；
- 补齐 7 个字段：``date / entry_date / entry_idx / _idx / exit_reason_class / stamp_tax / transfer_fee``；
- 成本拆分调用 ``FeeCalculator``（费用唯一真相来源，``fee_common.py``），**不自实现费率公式**；
- 零引擎改动；路径 A（回测内存直连） / 路径 B（批量 ``include_detail`` 导出）。

防未来函数说明
--------------
本模块是回测**结果整理层**，不生成信号、不参与撮合：
所有计算（日期映射、买卖配对、成本拆分）均基于回测结束后已落定的成交与净值数据，
不引用任何未来行情，符合 ``docs/py文件代码规则.md`` 第 11 条红线。
"""

from __future__ import annotations

import bisect
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    CommissionModel,
    EquityPoint,
    SlippageModel,
    TradeRecord,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 常量（模块顶部，禁止魔法数字散落业务代码）
# ─────────────────────────────────────────────

# 注入数据契约版本（看板前端可据此做兼容判断）
SCHEMA_VERSION: str = "1.0"

# 看板 HTML 中代码行注入点（DATA=占位符）的占位符：渲染时替换为 json.dumps 结果。
# 注意：仅匹配 "DATA=__DASHBOARD_DATA_JSON__" 整串，注释行中的占位符字样不受影响。
_DASHBOARD_PLACEHOLDER: str = "__DASHBOARD_DATA_JSON__"

# 净值曲线降采样上限（对齐 pdf_report.downsample_equity_curve 默认行为，
# 控单文件体积，验收标准：10 年日线 + 500 笔交易 < 5MB）
_MAX_EQUITY_POINTS: int = 2000

# epoch 秒下限：引擎 _row_to_timestamp 返回 pd.Timestamp.timestamp()（epoch 秒），
# 2022 年前后约 1.64e9；低于 1e8（约 1973 年）视为"疑似整数索引"，拒绝静默错位
_MIN_EPOCH_SEC: float = 1e8

# 成本拆分与引擎混合 commission 的容差（元）：超过则记录 warning
_COST_SPLIT_TOLERANCE: float = 0.01

# 出场原因归类：与看板 §DERIVE deriveExitReasons 的 JS 正则（dashboard_mock.html:827-847）对齐。
# 顺序即优先级，命中即停；未命中归 other（前端同样兜底）。
_EXIT_REASON_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("take_profit", "涨幅=|take_profit", "止盈 +20%"),
    ("target_exit", "target=", "目标价退出"),
    ("rsrs_exit", "RSRS=|rsrs", "RSRS 择时退出"),
    ("stop_loss", "stop_loss|止损", "止损（核心策略未实现）"),
    ("max_hold", "holding|max_hold", "持仓到期"),
    ("signal_flip", "flip|反向", "信号反转"),
)

# K线区块注入的 OHLCV 字段顺序（与看板 §Z1K 前端消费字段一一对应）
_OHLCV_FIELDS: Tuple[str, str, str, str, str] = (
    "open", "high", "low", "close", "volume",
)


# ─────────────────────────────────────────────
# 纯函数：字段补齐基础件
# ─────────────────────────────────────────────

def _epoch_to_date(timestamp: float) -> str:
    """将 epoch 秒时间戳转换为 ``YYYY-MM-DD`` 日期字符串（本地时区）。

    Args:
        timestamp: epoch 秒时间戳（引擎 bar.timestamp / TradeRecord.timestamp 语义）。

    Returns:
        本地时区的 ``YYYY-MM-DD`` 日期。

    Raises:
        ValueError: 时间戳疑似整数索引（< 1e8）或超出 datetime 可表示范围时抛出。
    """
    value = float(timestamp)
    if value < _MIN_EPOCH_SEC:
        raise ValueError(
            f"时间戳 {value!r} 疑似整数索引而非 epoch 秒"
            f"（引擎 _row_to_timestamp 返回 epoch 秒，请核对数据来源）"
        )
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError) as exc:
        raise ValueError(f"无法将时间戳转换为日期: {timestamp!r}") from exc


def _exit_reason_class(exit_reason: str) -> str:
    """按前缀关键词将出场原因归类（与看板 §DERIVE JS 正则同源）。

    Args:
        exit_reason: 引擎成交记录的出场原因文本（可为空）。

    Returns:
        归类键（take_profit / rsrs_exit / stop_loss / max_hold / signal_flip / other）。
        空文本直接归 other。
    """
    if not exit_reason:
        return "other"
    for key, pattern, _label in _EXIT_REASON_RULES:
        for token in pattern.split("|"):
            if token and token.lower() in exit_reason.lower():
                return key
    return "other"


def _build_fee_config(config: BacktestConfig) -> Dict[str, Any]:
    """从 BacktestConfig 提取费率/滑点配置（对齐看板 CFG.fee 字段）。

    Args:
        config: 回测配置（含 CommissionModel / SlippageModel）。

    Returns:
        看板 CFG.fee 同构字典。
    """
    commission: CommissionModel = config.commission or CommissionModel()
    slippage: SlippageModel = config.slippage or SlippageModel()
    slippage_bps: float = 0.0
    if slippage.type == "fixed_bps":
        slippage_bps = float(slippage.fixed_bps)
    return {
        "commission_rate": float(commission.buy_rate),
        "min_commission": float(commission.min_commission),
        "stamp_tax_rate": float(commission.stamp_tax),
        "transfer_fee_rate": float(commission.transfer_fee),
        "slippage_bps": slippage_bps,
        "slippage_mode": slippage.type,
    }


def _build_config_payload(
    config: BacktestConfig,
    *,
    engine_mode: str,
    guard_passed: bool,
) -> Dict[str, Any]:
    """构建看板 CFG 同构配置字典。

    Args:
        config: 回测配置。
        engine_mode: 引擎模式（event_driven / vectorized / mixed / auto）。
        guard_passed: 防未来函数校验是否通过。

    Returns:
        看板 CFG 消费字段的同构字典（含 fee 子对象与策略参数平铺）。
    """
    payload: Dict[str, Any] = {
        "strategy_id": config.strategy_name,
        "strategy_name": config.strategy_name,
        "start": config.start_date,
        "end": config.end_date,
        "initial_capital": float(config.initial_capital),
        "benchmark": config.benchmark,
        "engine_mode": engine_mode,
        "guard_passed": guard_passed,
        # seed 仅 MOCK 兜底使用；真实数据注入后不触发 buildMock，保留字段兼容模板
        "seed": 0,
        "fee": _build_fee_config(config),
    }
    # 策略参数平铺（MOCK CFG 含 period/buy_threshold 等，真实数据保留结构完整）
    for key, value in (config.params or {}).items():
        if key not in payload:
            payload[key] = value
    return payload


def _derive_pool(trades: Sequence[TradeRecord]) -> List[str]:
    """从成交记录派生股票池（去重保序）。

    看板 §CALC:1057-1065 正收益占比的分母必须为真实股票池，
    不得使用 MOCK 的 49 只假股票。

    Args:
        trades: 成交记录列表。

    Returns:
        去重后的标的代码列表（保持首次出现顺序）。
    """
    pool: List[str] = []
    seen = set()
    for trade in trades:
        symbol = (trade.symbol or "").strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            pool.append(symbol)
    return pool


def _pair_entry_dates(trades: Sequence[TradeRecord]) -> Dict[str, str]:
    """按标的对 BUY→SELL 配对，返回 ``{sell_trade_id: buy_date}`` 映射。

    配对规则：同一标的按时间戳顺序 FIFO——BUY 入队，SELL 弹出最早未配对
    BUY 并记录其成交日期作为该笔的入场日期。全部基于回测结束后已落定
    数据，无未来函数风险。

    Args:
        trades: 按时间戳升序排列的成交记录（引擎输出天然有序）。

    Returns:
        卖出记录 trade_id → 配对买入日期（YYYY-MM-DD）的映射。
    """
    # 按时间戳排序防御：引擎输出通常有序，显式排序保证配对正确
    ordered = sorted(trades, key=lambda t: (t.timestamp, t.trade_id))
    queues: Dict[str, List[str]] = {}  # symbol -> [买入日期]
    pair_map: Dict[str, str] = {}
    for trade in ordered:
        side = (trade.side or "").strip().upper()
        if side in ("BUY", "B"):
            queues.setdefault(trade.symbol, []).append(_epoch_to_date(trade.timestamp))
        else:
            queue = queues.get(trade.symbol)
            if queue:
                pair_map[trade.trade_id] = queue.pop(0)
    return pair_map


def _nearest_date_index(date: str, dates: Sequence[str]) -> int:
    """在升序日期序列中定位 ``date`` 的索引；缺失时取 ≤ date 的最近日期。

    Args:
        date: ``YYYY-MM-DD`` 日期。
        dates: 升序 ``YYYY-MM-DD`` 日期序列（ISO 字典序即时间序）。

    Returns:
        命中索引；未命中则返回 ≤ date 的最近日期索引（最小为 0）。
    """
    pos = bisect.bisect_right(dates, date)
    return max(0, pos - 1)


# ─────────────────────────────────────────────
# 核心：交易记录补齐（7 字段）
# ─────────────────────────────────────────────

def _enrich_trade(
    trade: TradeRecord,
    *,
    idx_by_date: Dict[str, int],
    dates: Sequence[str],
    pair_map: Dict[str, str],
    commission_model: CommissionModel,
    slippage_model: SlippageModel,
) -> Dict[str, Any]:
    """将单笔成交记录补齐为看板 TRADES 消费结构（7 字段）。

    补齐字段：
      date             : epoch 秒 → YYYY-MM-DD
      entry_date       : 配对买入日期（仅 SELL；BUY 为空字符串）
      entry_idx        : entry_date 在注入净值序列中的索引
      _idx             : date 在注入净值序列中的索引（替代 MOCK 的 timestamp 当索引）
      exit_reason_class: 出场原因归类
      stamp_tax        : 成本反向拆分（FeeCalculator 权威口径，仅 SELL 非 0）
      transfer_fee     : 成本反向拆分（双向）

    Args:
        trade: 引擎成交记录。
        idx_by_date: 注入（降采样后）净值序列的日期→索引映射。
        dates: 注入净值序列的日期列表（升序）。
        pair_map: ``{sell_trade_id: buy_date}`` 配对映射。
        commission_model: 手续费模型（成本拆分用）。
        slippage_model: 滑点模型（成本拆分签名需要，费率拆分用不到滑点）。

    Returns:
        看板 TRADES 元素字典（TradeRecord.to_dict() + 7 个补齐字段）。
    """
    from myquant.backtest.fee_common import FeeCalculator

    payload: Dict[str, Any] = trade.to_dict()
    date = _epoch_to_date(trade.timestamp)

    # 索引补齐：_idx 必须对齐注入（降采样后）的净值序列
    payload["date"] = date
    payload["_idx"] = idx_by_date.get(date, _nearest_date_index(date, dates))

    # 买卖点标记：仅 SELL 有配对买入
    entry_date = pair_map.get(trade.trade_id, "")
    payload["entry_date"] = entry_date
    payload["entry_idx"] = (
        idx_by_date.get(entry_date, _nearest_date_index(entry_date, dates))
        if entry_date
        else -1
    )

    # 出场原因归类（与看板 §DERIVE JS 正则同源；前端亦会兜底现算）
    payload["exit_reason_class"] = _exit_reason_class(trade.exit_reason or "")

    # 成本反向拆分：调用 FeeCalculator（费用唯一真相来源）
    calc = FeeCalculator(
        commission_model=commission_model, slippage_model=slippage_model
    )
    notional = float(trade.quantity) * float(trade.price)
    commission_part, stamp_tax, transfer_fee = calc.calc_commission_breakdown(
        side=trade.side, notional=notional
    )
    split_total = commission_part + stamp_tax + transfer_fee
    if abs(split_total - float(trade.commission)) > _COST_SPLIT_TOLERANCE:
        logger.warning(
            "交易 %s 成本拆分与引擎混合值不一致: 拆分合计=%s 引擎commission=%s"
            "（费率模型差异或最低佣金归属，按拆分值注入并保留原值字段）",
            trade.trade_id,
            round(split_total, 4),
            trade.commission,
        )
    payload["commission"] = round(commission_part, 2)
    payload["stamp_tax"] = round(stamp_tax, 2)
    payload["transfer_fee"] = round(transfer_fee, 2)
    payload["extra"] = {
        **(payload.get("extra") or {}),
        "engine_commission_total": round(float(trade.commission), 2),
    }
    return payload


# ─────────────────────────────────────────────
# 核心：OHLCV 行情序列（K线四窗格区块数据源）
# ─────────────────────────────────────────────

def _finite_float(value: Any) -> float:
    """将任意值转换为有限浮点数；NaN/Inf/非法值统一归 0.0。

    Args:
        value: 原始值（可能为 None / NaN / 字符串 / 数字）。

    Returns:
        有限浮点数（无法转换时为 0.0，保证 JSON 序列化合法）。
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def build_ohlcv(
    symbol: str,
    start_date: str,
    end_date: str,
    *,
    processed_dir: Optional[Path | str] = None,
) -> List[Dict[str, Any]]:
    """从 processed 清洗数据读取回测窗口的 OHLCV 序列（看板 K线区块数据源）。

    数据通道：``HistoricalDataProvider.get_bars``（backtest/data_loader.py）——
    该接口内部对 ``end_date`` 之后的数据严格过滤（防未来函数红线，
    data_loader.py:350-364），本函数仅做窗口裁剪与字段规整，不参与
    任何信号/撮合计算，属回测结果整理层。

    Args:
        symbol: 标的代码（如 ``600343.SH``，对应 processed/<symbol>.parquet）。
        start_date: 回测起始日期 ``YYYY-MM-DD``。
        end_date: 回测截止日期 ``YYYY-MM-DD``。
        processed_dir: processed 数据目录；None 时用 data_loader 默认路径
            （项目根 datasets/processed）。

    Returns:
        按日期升序的 OHLCV 列表，每项 ``{date, open, high, low, close, volume}``
        （volume 单位为股）；标的无数据/读取失败时返回空列表
        （看板前端据此隐藏 K线区块，不阻断看板渲染）。

    Raises:
        不主动抛出：读取失败仅记录 warning 并返回空列表。
    """
    try:
        from myquant.backtest.data_loader import HistoricalDataProvider
    except ImportError as exc:  # 依赖缺失：K线区块降级为空，不影响核心看板
        logger.warning("HistoricalDataProvider 导入失败，K线区块数据不可用: %s", exc)
        return []
    provider = HistoricalDataProvider(processed_dir=processed_dir)
    try:
        # 本行使用的数据截至 end_date 时刻（get_bars 内部严格过滤未来数据）
        bars = provider.get_bars(
            symbol, start_date=start_date, end_date=end_date, period="daily"
        )
    except Exception as exc:  # 读取失败降级为空，不阻断看板渲染
        logger.warning("读取 %s 行情失败（%s），K线区块数据不可用", symbol, exc)
        return []
    if bars is None or bars.empty:
        logger.warning("%s 在 %s~%s 无 processed 行情数据，K线区块隐藏", symbol, start_date, end_date)
        return []

    rows: List[Dict[str, Any]] = []
    for timestamp, row in bars.iterrows():
        item: Dict[str, Any] = {
            "date": timestamp.strftime("%Y-%m-%d"),
        }
        for field in _OHLCV_FIELDS:
            item[field] = _finite_float(row.get(field))
        rows.append(item)
    return rows


# ─────────────────────────────────────────────
# 核心：构建看板数据载荷
# ─────────────────────────────────────────────

def build_dashboard_dict(
    equity_points: Sequence[EquityPoint],
    trades: Sequence[TradeRecord],
    config: BacktestConfig,
    *,
    engine_mode: str = "auto",
    guard_passed: bool = True,
    run_id: str = "",
    status: str = "completed",
    generated_at: Optional[str] = None,
    ohlcv: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构建看板 ``__DASHBOARD_DATA__`` 载荷（核心纯函数，路径 A/B 共用）。

    Args:
        equity_points: 引擎净值曲线数据点（原始逐日，内部降采样）。
        trades: 引擎成交记录。
        config: 回测配置（路径 B 可传最小配置）。
        engine_mode: 引擎模式，写入 CFG 与 meta。
        guard_passed: 防未来函数校验结果，写入 CFG 与 meta。
        run_id: 回测运行 ID（路径 B 可空）。
        status: 回测状态（completed / failed）。
        generated_at: 生成时间 ISO 字符串（None 时取当前时间）。
        ohlcv: 回测窗口 OHLCV 序列（``build_ohlcv`` 产物）；None 或空时
            payload 写入空列表（看板前端据此隐藏 K线区块）。

    Returns:
        看板可消费的完整载荷字典
        （键：meta / config / equity_curve / trades / pool / ohlcv）。

    Raises:
        ValueError: 净值曲线为空时抛出（看板无法渲染空曲线）。
    """
    if not equity_points:
        raise ValueError("equity_curve 为空，无法渲染看板（请检查回测结果）。")

    # 降采样：_idx 必须对齐注入后的序列，索引映射基于降采样结果构建
    from myquant.backtest.pdf_report import downsample_equity_curve

    sampled: List[EquityPoint] = downsample_equity_curve(
        list(equity_points), max_points=_MAX_EQUITY_POINTS
    )
    equity_curve: List[Dict[str, Any]] = [ep.to_dict() for ep in sampled]
    dates: List[str] = [ep.date for ep in sampled if ep.date]
    idx_by_date: Dict[str, int] = {date: i for i, date in enumerate(dates)}

    pair_map: Dict[str, str] = _pair_entry_dates(trades)
    commission_model: CommissionModel = config.commission or CommissionModel()
    slippage_model: SlippageModel = config.slippage or SlippageModel()
    enriched_trades: List[Dict[str, Any]] = [
        _enrich_trade(
            trade,
            idx_by_date=idx_by_date,
            dates=dates,
            pair_map=pair_map,
            commission_model=commission_model,
            slippage_model=slippage_model,
        )
        for trade in trades
    ]

    now = generated_at or datetime.now().isoformat(timespec="seconds")
    meta: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": status,
        "engine_mode": engine_mode,
        "guard_passed": guard_passed,
        "generated_at": now,
        "data_source": "backtest_result",
    }
    return {
        "meta": meta,
        "config": _build_config_payload(
            config, engine_mode=engine_mode, guard_passed=guard_passed
        ),
        "equity_curve": equity_curve,
        "trades": enriched_trades,
        "pool": _derive_pool(trades),
        "ohlcv": list(ohlcv) if ohlcv else [],
    }


def build_dashboard_json(
    result: BacktestResult,
    *,
    engine_mode: Optional[str] = None,
    guard_passed: Optional[bool] = None,
    ohlcv: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """路径 A：从内存中的 ``BacktestResult`` 构建看板载荷。

    Args:
        result: 回测引擎返回的完整结果（含 equity_curve / trades / config）。
        engine_mode: 引擎模式覆盖值；None 时优先取 result.extra["engine_mode"]。
        guard_passed: 防未来函数校验覆盖值；None 时按 result.is_success 推断。
        ohlcv: OHLCV 序列覆盖值；None 时按 result.config 的
            symbols[0] / start_date / end_date 自动调用 ``build_ohlcv`` 读取。

    Returns:
        看板可消费的完整载荷字典（含 ohlcv 键）。
    """
    resolved_mode: str = engine_mode or str(
        (result.extra or {}).get("engine_mode", "auto")
    )
    resolved_guard: bool = (
        guard_passed if guard_passed is not None else result.is_success
    )
    resolved_ohlcv: List[Dict[str, Any]]
    if ohlcv is not None:
        resolved_ohlcv = list(ohlcv)
    else:
        # 自动读取：symbol 取 config.symbols 首标的；无标的不注入（前端隐藏 K线区块）
        symbols: Sequence[str] = result.config.symbols or []
        if symbols:
            resolved_ohlcv = build_ohlcv(
                symbols[0],
                result.config.start_date,
                result.config.end_date,
            )
        else:
            resolved_ohlcv = []
    return build_dashboard_dict(
        equity_points=result.equity_curve,
        trades=result.trades,
        config=result.config,
        engine_mode=resolved_mode,
        guard_passed=resolved_guard,
        run_id=result.run_id,
        status=result.status,
        ohlcv=resolved_ohlcv,
    )


def load_batch_detail(
    results_path: Path | str,
    symbol: str,
    *,
    strategy_name: str = "batch",
    start_date: str = "",
    end_date: str = "",
    initial_capital: float = 1_000_000.0,
    benchmark: str = "000300.SH",
) -> Dict[str, Any]:
    """路径 B：从批量回测 ``results.json``（include_detail=True）读取目标标的载荷。

    数据格式：``base["detail"] = {"trades": [TradeRecord.to_dict()...],
    "equity": [EquityPoint.to_dict()...]}``（batch_backtest.py:185-189）。

    Args:
        results_path: 批量回测结果 JSON 路径。
        symbol: 目标标的代码。
        strategy_name: 策略名（批量结果未持久化该字段，由调用方补）。
        start_date: 回测起始日期（可选覆盖）。
        end_date: 回测结束日期（可选覆盖）。
        initial_capital: 初始资金（可选覆盖）。
        benchmark: 基准代码（可选覆盖）。

    Returns:
        看板可消费的完整载荷字典。

    Raises:
        FileNotFoundError: results.json 不存在时抛出。
        KeyError: 目标标的或 detail 数据缺失时抛出（提示需 include_detail=True 重跑）。
        ValueError: 数据无法解析为 EquityPoint / TradeRecord 时抛出。
    """
    path = Path(results_path)
    if not path.exists():
        raise FileNotFoundError(f"批量回测结果文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    results = payload.get("results", payload) if isinstance(payload, dict) else {}
    entry = results.get(symbol)
    if entry is None:
        raise KeyError(
            f"results.json 中未找到标的 {symbol!r}（可选标的: {list(results)[:10]}）"
        )
    detail = entry.get("detail")
    if not detail or "equity" not in detail or "trades" not in detail:
        raise KeyError(
            f"标的 {symbol!r} 缺少 detail.trades / detail.equity，"
            f"请开启 include_detail=True 重跑批量回测后再导出。"
        )

    try:
        equity_points: List[EquityPoint] = [
            EquityPoint(**point_dict) for point_dict in detail["equity"]
        ]
        trade_records: List[TradeRecord] = [
            TradeRecord(**trade_dict) for trade_dict in detail["trades"]
        ]
    except TypeError as exc:
        raise ValueError(f"批量 detail 数据无法解析为模型对象: {exc}") from exc

    config = BacktestConfig(
        strategy_name=strategy_name,
        symbols=[symbol],
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        benchmark=benchmark,
    )
    return build_dashboard_dict(
        equity_points=equity_points,
        trades=trade_records,
        config=config,
        run_id=f"batch_{symbol}",
        ohlcv=build_ohlcv(symbol, start_date, end_date),
    )


# ─────────────────────────────────────────────
# 渲染：注入 HTML 模板
# ─────────────────────────────────────────────

def _locate_template(template_path: Optional[Path | str]) -> Path:
    """定位看板模板文件。

    Args:
        template_path: 显式模板路径；None 时探测项目根 docs/design/回测看板.html。

    Returns:
        模板绝对路径。

    Raises:
        FileNotFoundError: 模板不存在时抛出。
    """
    if template_path is not None:
        path = Path(template_path)
        if path.exists():
            return path.resolve()
        raise FileNotFoundError(f"看板模板不存在: {path}")

    # 向上探测项目根（pyproject.toml），拼接默认模板路径
    current = Path(__file__).resolve().parent
    for _ in range(6):
        if (current / "pyproject.toml").exists():
            candidate = current / "docs" / "design" / "回测看板.html"
            if candidate.exists():
                return candidate.resolve()
            break
        current = current.parent
    raise FileNotFoundError(
        "未找到看板模板（默认 docs/design/回测看板.html），请通过 template_path 显式指定。"
    )


def render_dashboard(
    data: Dict[str, Any],
    out_path: Path | str,
    *,
    template_path: Optional[Path | str] = None,
) -> Path:
    """将载荷注入看板模板，写出单文件真实数据看板。

    注入方式：把模板代码行注入点 ``DATA=__DASHBOARD_DATA_JSON__`` 中的占位符
    替换为 ``json.dumps(data)`` 结果（``</`` 转义为 ``<\\/`` 防 ``</script>`` 截断）。
    仅替换代码行，注释行中的占位符字样保持不变（避免注释被 JSON 污染）。

    Args:
        data: ``build_dashboard_json`` / ``build_dashboard_dict`` 的产物。
        out_path: 输出 HTML 路径（写入新文件，原模板不动，保留 MOCK 演示通道）。
        template_path: 模板路径（None 自动探测）。

    Returns:
        输出文件绝对路径。

    Raises:
        FileNotFoundError: 模板不存在时抛出。
        ValueError: 模板未含占位符（尚未完成 §5.3 模板化改造）时抛出。
    """
    template = _locate_template(template_path)
    html = template.read_text(encoding="utf-8")
    if _DASHBOARD_PLACEHOLDER not in html:
        raise ValueError(
            f"模板 {template} 未包含占位符 {_DASHBOARD_PLACEHOLDER!r}——"
            f"看板模板化改造（最终定稿 §5.3）尚未完成，请先执行任务 2 后再渲染。"
        )

    payload_json = (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("</", "<\\/")
    )
    # 精确替换代码行注入点（DATA=占位符），避免注释行中的占位符字样被全局
    # replace 一并替换成 JSON（历史 bug：模板 633 行注释被 179KB JSON 污染）。
    rendered = html.replace(
        "DATA=" + _DASHBOARD_PLACEHOLDER,
        "DATA=" + payload_json,
    )

    output = Path(out_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    logger.info("看板已渲染: %s（%d 字节）", output.resolve(), output.stat().st_size)
    return output.resolve()


# ─────────────────────────────────────────────
# 自测 Demo（py 文件代码规则第 5 条：可执行文件必须有入口 + demo）
# ─────────────────────────────────────────────

def _build_demo_result() -> BacktestResult:
    """构造最小示例 BacktestResult（演示用，非真实回测数据）。

    Returns:
        含 3 日净值曲线与 2 笔成交（BUY/SELL）的示例结果对象。
    """
    config = BacktestConfig(
        strategy_name="demo_ma",
        symbols=["000001.SZ"],
        start_date="2024-01-02",
        end_date="2024-01-05",
        initial_capital=1_000_000.0,
        benchmark="000300.SH",
    )
    # 示例净值：3 个交易日（epoch 秒取 2024-01-02 起）
    base_ts = 1704153600.0  # 2024-01-02 00:00:00 (UTC+8 为 08:00)
    equity_points = [
        EquityPoint(timestamp=base_ts + i * 86400, date=d, equity=e, cash=c, position_value=pv, drawdown=dd, benchmark_equity=b)
        for i, (d, e, c, pv, dd, b) in enumerate(
            [
                ("2024-01-02", 1_000_000.0, 800_000.0, 200_000.0, 0.0, 1_000_000.0),
                ("2024-01-03", 1_010_000.0, 600_000.0, 410_000.0, 0.0, 1_005_000.0),
                ("2024-01-04", 1_020_000.0, 400_000.0, 620_000.0, 0.0, 1_010_000.0),
            ]
        )
    ]
    trades = [
        TradeRecord(
            trade_id="T1",
            symbol="000001.SZ",
            side="BUY",
            quantity=1000,
            price=10.0,
            timestamp=base_ts,
            commission=5.1,  # 佣金+过户费混合 = max(10000×0.0002,5)+10000×0.00001
            slippage=2.0,
            pnl=0.0,
            pnl_pct=0.0,
            holding_days=0,
            strategy_id="demo_ma",
            signal_source="demo",
            entry_reason="demo",
            exit_reason="",
        ),
        TradeRecord(
            trade_id="T2",
            symbol="000001.SZ",
            side="SELL",
            quantity=1000,
            price=11.0,
            timestamp=base_ts + 2 * 86400,
            commission=16.11,  # 佣金+印花税+过户费混合 = 5.0+11.0+0.11
            slippage=2.2,
            pnl=980.0,
            pnl_pct=0.098,
            holding_days=2,
            strategy_id="demo_ma",
            signal_source="demo",
            entry_reason="demo",
            exit_reason="RSRS=-0.85 < -0.7",
        ),
    ]
    result = BacktestResult(config=config, equity_curve=equity_points, trades=trades)
    result.run_id = "demo_run"
    result.mark_complete()
    result.extra["engine_mode"] = "event_driven"
    return result


def _run_self_test() -> int:
    """运行序列化器自测 Demo。

    Returns:
        通过项数（失败项以日志 + 非零返回提示）。
    """
    import sys

    print("=" * 60)
    print("dashboard_serializer 自测开始（示例数据，非真实回测）")
    print("=" * 60)
    result = _build_demo_result()
    data = build_dashboard_json(result)

    checks: List[Tuple[str, bool, str]] = []
    checks.append(
        ("meta 含 schema_version",
         data["meta"]["schema_version"] == SCHEMA_VERSION,
         data["meta"].get("schema_version"))
    )
    checks.append(
        ("equity_curve 注入 3 点",
         len(data["equity_curve"]) == 3,
         f"len={len(data['equity_curve'])}")
    )
    checks.append(
        ("TRADES 补齐 2 笔",
         len(data["trades"]) == 2,
         f"len={len(data['trades'])}")
    )
    t_buy = data["trades"][0]
    t_sell = data["trades"][1]
    checks.append(
        ("BUY 无印花税",
         t_buy["stamp_tax"] == 0.0,
         f"stamp_tax={t_buy['stamp_tax']}")
    )
    checks.append(
        ("SELL 有印花税",
         t_sell["stamp_tax"] > 0.0,
         f"stamp_tax={t_sell['stamp_tax']}")
    )
    checks.append(
        ("SELL 配对入场日期",
         t_sell["entry_date"] == "2024-01-02",
         f"entry_date={t_sell['entry_date']}")
    )
    checks.append(
        ("_idx 对齐注入序列",
         t_sell["_idx"] == 2 and t_buy["_idx"] == 0,
         f"buy._idx={t_buy['_idx']}, sell._idx={t_sell['_idx']}")
    )
    checks.append(
        ("exit_reason_class 归类",
         t_sell["exit_reason_class"] == "rsrs_exit",
         f"class={t_sell['exit_reason_class']}")
    )
    checks.append(
        ("pool 从 trades 派生",
         data["pool"] == ["000001.SZ"],
         str(data["pool"]))
    )
    checks.append(
        ("payload 含 ohlcv 键（K线区块契约）",
         "ohlcv" in data and isinstance(data["ohlcv"], list),
         f"type={type(data['ohlcv']).__name__}")
    )

    passed = 0
    failures = 0
    for name, ok, detail in checks:
        if ok:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failures += 1
            print(f"  [FAIL] {name}: {detail}")
    print("=" * 60)
    if failures:
        print(f"自测结果: {passed} 项通过, {failures} 项失败")
        sys.exit(1)
    print(f"自测结果: 全部 {passed} 项通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
