# 文件名：myquant/data_access/cleaner/derived_metrics.py
"""
derived_metrics.py - 日线派生量计算（涨跌幅/振幅/量比）
==================================================================

在全量数据清洗完成后，对单只标的的按日排序行情记录批量计算三个派生量：

  1. pct_chg       涨跌幅（%）：(close_t - close_{t-1}) / close_{t-1} * 100
  2. amplitude     振幅（%）  ：(high_t - low_t) / close_{t-1} * 100
  3. volume_ratio 量比       ：volume_t / mean(volume_{t-5..t-1})（过去5日均量，不含当日）

设计原则（代码规则）：
  - 全量类型注解 + Google docstring
  - logging 禁 print
  - 严格防未来函数：第 t 行只依赖第 t 行及之前的历史行，绝不引入未来数据
  - 第一条无前收盘价，三个派生量均设为 None
  - 复权说明：当前库内为后复权(hfq)数据，用 hfq 收盘价近似计算；
    除权除息日的涨跌幅可能与不复权口径略有偏差（B类换手率/历史股本另案处理）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 量比计算窗口（过去 N 日平均成交量）
VOLUME_RATIO_WINDOW: int = 5


def compute_derived_metrics(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    对单只标的的按日排序行情记录批量计算涨跌幅/振幅/量比。

    输入记录必须已按 trade_date 升序排列（清洗 pipeline 的 sort 步骤保证）。
    函数会原地修改每条 record，写入 pct_chg / amplitude / volume_ratio 三个字段。

    Parameters
    ----------
    records : list[dict]
        按日期升序的清洗后行情记录列表，每条含 open/high/low/close/volume 等字段。

    Returns
    -------
    list[dict]
        写入派生量后的同一批记录（原地修改后返回，方便链式调用）。

    Notes
    -----
    - 第 0 行无前收盘价，三个派生量均设为 None。
    - 量比窗口内若有 0 成交量导致均值为 0，量比设为 None（避免除零）。
    - close_{t-1} 为 0 或 None 时，涨跌幅/振幅设为 None。
    """
    if not records:
        return records

    prev_close: Optional[float] = None
    # 维护过去 N 日成交量队列，用于量比计算
    vol_history: List[float] = []

    for rec in records:
        close = _to_float(rec.get("close"))
        high = _to_float(rec.get("high"))
        low = _to_float(rec.get("low"))
        volume = _to_float(rec.get("volume"))

        # ── 涨跌幅 ──
        if prev_close is not None and prev_close != 0 and close is not None:
            rec["pct_chg"] = round((close - prev_close) / prev_close * 100.0, 4)
        else:
            rec["pct_chg"] = None

        # ── 振幅 ──
        if (
            prev_close is not None
            and prev_close != 0
            and high is not None
            and low is not None
        ):
            rec["amplitude"] = round((high - low) / prev_close * 100.0, 4)
        else:
            rec["amplitude"] = None

        # ── 量比（过去 N 日均量，不含当日）──
        if vol_history and volume is not None:
            avg_vol = sum(vol_history) / len(vol_history)
            if avg_vol > 0:
                rec["volume_ratio"] = round(volume / avg_vol, 4)
            else:
                rec["volume_ratio"] = None
        else:
            rec["volume_ratio"] = None

        # ── 滚动更新历史（防未来函数：先算再入队，当日成交量不参与当日量比）──
        prev_close = close
        if volume is not None:
            vol_history.append(volume)
            if len(vol_history) > VOLUME_RATIO_WINDOW:
                vol_history.pop(0)

    return records


def _to_float(value: Any) -> Optional[float]:
    """
    安全地将任意值转为 float，失败返回 None。

    Parameters
    ----------
    value : Any
        待转换的值（可能是 int/float/str/None）。

    Returns
    -------
    float | None
        转换后的浮点值，失败或空值返回 None。
    """
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def compute_b_class_metrics(
    records: List[Dict[str, Any]],
    financial_records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    B类派生量计算：换手率、流通股本、市盈(动)。

    参数:
        records: 按日期升序的清洗后行情记录列表（含 close/volume）。
        financial_records: 该标的的财务记录列表，每条含 report_date/circ_mv/total_mv/net_profit。
            circ_mv（流通市值）、total_mv（总市值）通常仅最新一期有值。

    计算口径（近似说明）:
        - 流通股本 = 最新一期 circ_mv / 最新一期 close（近似，忽略历史股本变化）
        - 总股本 = 最新一期 total_mv / 最新一期 close（近似）
        - 换手率 = volume(股) / 流通股本(股) × 100
        - 市盈(动) = close × 总股本 / 年化净利润
            年化净利润按 report_date 月份推断报告期类型:
              03-31 一季报 -> net_profit × 4
              06-30 半年报 -> net_profit × 2
              09-30 三季报 -> net_profit × 4/3
              12-31 年报   -> net_profit × 1

    Notes:
        - 流通股本/总股本用最新值近似历史，长期（解禁/增发）会有偏差。
        - 严格防未来函数：每个交易日只用该日之前最近一期财务数据。
    """
    if not records or not financial_records:
        return records

    fin_sorted = sorted(financial_records, key=lambda r: r.get("report_date", ""))

    # 找最新一期有 circ_mv/total_mv 的记录
    latest_circ_mv: Optional[float] = None
    latest_total_mv: Optional[float] = None
    for fin in reversed(fin_sorted):
        if latest_circ_mv is None:
            latest_circ_mv = _to_float(fin.get("circ_mv"))
        if latest_total_mv is None:
            latest_total_mv = _to_float(fin.get("total_mv"))
        if latest_circ_mv and latest_total_mv:
            break

    # 最新收盘价
    latest_close: Optional[float] = None
    for rec in reversed(records):
        c = _to_float(rec.get("close"))
        if c:
            latest_close = c
            break

    # 反推流通股本/总股本（股）
    float_share: Optional[float] = None
    total_share: Optional[float] = None
    if latest_circ_mv and latest_close:
        float_share = latest_circ_mv / latest_close
    if latest_total_mv and latest_close:
        total_share = latest_total_mv / latest_close

    for rec in records:
        trade_date = rec.get("trade_date", "")
        close = _to_float(rec.get("close"))
        volume = _to_float(rec.get("volume"))

        # 换手率
        if float_share and volume and float_share > 0:
            rec["turnover"] = round(volume / float_share * 100.0, 4)
        else:
            rec["turnover"] = None

        # 流通股本（写回每条记录）
        rec["float_share"] = round(float_share, 2) if float_share else None

        # 市盈(动)：找该交易日之前最近一期财务数据
        fin_idx = -1
        for i, fin in enumerate(fin_sorted):
            if fin.get("report_date", "") <= trade_date:
                fin_idx = i
            else:
                break

        pe_dynamic: Optional[float] = None
        if fin_idx >= 0 and close and total_share:
            fin = fin_sorted[fin_idx]
            net_profit = _to_float(fin.get("net_profit"))
            report_date = fin.get("report_date", "")
            if net_profit and net_profit > 0:
                annual_factor = _annualize_factor(report_date)
                annual_profit = net_profit * annual_factor
                if annual_profit > 0:
                    pe_dynamic = round(close * total_share / annual_profit, 4)
        rec["pe_dynamic"] = pe_dynamic

    return records


def _annualize_factor(report_date: str) -> float:
    """
    根据报告期日期推断年化因子。

    参数:
        report_date: 报告期日期 YYYY-MM-DD。

    返回:
        float: 年化因子（Q1->4, Q2->2, Q3->4/3, Q4->1, 未知->1）。
    """
    month_day = report_date[5:] if len(report_date) >= 10 else ""
    if month_day == "03-31":
        return 4.0
    elif month_day == "06-30":
        return 2.0
    elif month_day == "09-30":
        return 4.0 / 3.0
    elif month_day == "12-31":
        return 1.0
    else:
        return 1.0