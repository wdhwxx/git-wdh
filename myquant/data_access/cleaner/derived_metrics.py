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
  - 复权说明：当前库内为不复权原始层(raw)数据，用 raw 收盘价近似计算；
    除权除息日涨跌幅为真实跳变（未复权），待方案B复权因子表上线后再复核（B类另案处理）
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from myquant.data_access.time_semantics import report_deadline

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
            # P2-⑤(#83)：窗口内 0 量日（停牌/无成交）不计入均量，避免均量虚低、量比虚高
            valid_vol = [v for v in vol_history if v and v > 0]
            if valid_vol:
                avg_vol = sum(valid_vol) / len(valid_vol)
                if avg_vol > 0:
                    rec["volume_ratio"] = round(volume / avg_vol, 4)
                else:
                    rec["volume_ratio"] = None
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
    share_records: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    B类派生量计算：换手率、流通股本、市盈(动)。

    参数:
        records: 按日期升序的清洗后行情记录列表（含 close/volume）。
        financial_records: 该标的的财务记录列表，每条含 report_date/circ_mv/total_mv/net_profit。
            circ_mv（流通市值）、total_mv（总市值）通常仅最新一期有值。
        share_records: 该标的的股本变动事件列表（P1-② #83，可为空），每条含
            change_date/total_share/float_share，按 change_date 升序；
            每个交易日取 <= trade_date 最近变更的真实股本，无记录回退 circ_mv 反推近似。

    计算口径（近似说明）:
        - 流通股本 = 该日已披露（报告截止日<=trade_date）的 circ_mv / 该日自身收盘价
          （P0-03修复：原用"最新市值÷整段最后一行收盘价"反推后套用全部历史bar=未来函数；
          当日无已披露市值快照则置 None——宁可缺失，不可穿越）
        - 总股本 = 该日已披露 total_mv / 该日自身收盘价（同上口径）
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

    # P1-②(#83)：真实股本事件（按 change_date 升序），每交易日取 <= trade_date 最近变更
    share_sorted = sorted(
        (share_records or []), key=lambda r: str(r.get("change_date", ""))
    )

    for rec in records:
        trade_date = rec.get("trade_date", "")
        close = _to_float(rec.get("close"))
        volume = _to_float(rec.get("volume"))

        # 市盈(动)：找该交易日之前最近一期财务数据
        # P1-①(#83)：按披露截止日（发布日推算）而非报告期判定，
        #   消除 报告期~披露日 窗口（如 Q1: 4/1~4/29）的未来函数
        fin_idx = -1
        for i, fin in enumerate(fin_sorted):
            if _report_deadline(str(fin.get("report_date", ""))) <= trade_date:
                fin_idx = i
            else:
                break

        # P0-03修复：兜底反推股本不得用"整段最后一行收盘价"反推后套用全部历史bar（未来函数）。
        # 改为：仅用"该交易日已披露的市值快照（报告截止日<=trade_date）÷ 该日自身收盘价"；
        # 当日无已披露市值快照则置 None（宁可缺失，不可穿越）。
        fb_float: Optional[float] = None
        fb_total: Optional[float] = None
        if fin_idx >= 0 and close and close > 0:
            fin_avail = fin_sorted[fin_idx]
            circ_mv = _to_float(fin_avail.get("circ_mv"))
            total_mv = _to_float(fin_avail.get("total_mv"))
            if circ_mv and circ_mv > 0:
                fb_float = circ_mv / close
            if total_mv and total_mv > 0:
                fb_total = total_mv / close

        # P1-②(#83)：真实股本优先（<= trade_date 最近变更），否则当日市值/当日收盘反推近似
        eff_float: Optional[float] = None
        eff_total: Optional[float] = None
        for sh in share_sorted:
            if str(sh.get("change_date", "")) <= trade_date:
                f_sh = _to_float(sh.get("float_share"))
                t_sh = _to_float(sh.get("total_share"))
                if f_sh and f_sh > 0:
                    eff_float = f_sh
                if t_sh and t_sh > 0:
                    eff_total = t_sh
            else:
                break  # 升序事件，后续变更日 > 该交易日（防未来）
        cur_float = eff_float if eff_float else fb_float
        cur_total = eff_total if eff_total else fb_total

        # 换手率
        if cur_float and volume and cur_float > 0:
            rec["turnover"] = round(volume / cur_float * 100.0, 4)
        else:
            rec["turnover"] = None

        # 流通股本（写回每条记录，真实股本优先）
        rec["float_share"] = round(cur_float, 2) if cur_float else None

        # 市盈(动)：找该交易日之前最近一期财务数据
        # P1-①(#83)：按披露截止日（发布日推算）而非报告期判定，
        #   消除 报告期~披露日 窗口（如 Q1: 4/1~4/29）的未来函数
        fin_idx = -1
        for i, fin in enumerate(fin_sorted):
            if _report_deadline(str(fin.get("report_date", ""))) <= trade_date:
                fin_idx = i
            else:
                break

        pe_dynamic: Optional[float] = None
        if fin_idx >= 0 and close and cur_total:
            fin = fin_sorted[fin_idx]
            net_profit = _to_float(fin.get("net_profit"))
            report_date = fin.get("report_date", "")
            if net_profit and net_profit > 0:
                annual_factor = _annualize_factor(report_date)
                annual_profit = net_profit * annual_factor
                if annual_profit > 0:
                    pe_dynamic = round(close * cur_total / annual_profit, 4)
        rec["pe_dynamic"] = pe_dynamic

    return records


def _report_deadline(report_date: str) -> str:
    """兼容别名：收敛至统一时点日落点（M1A-02，v3.0 §6.4.2）。

    旧本地实现（P1-① #83）已收敛至 myquant/data_access/time_semantics.report_deadline，
    本别名仅保留以兼容既有调用点，新代码请直接使用 time_semantics.report_deadline。

    Args:
        report_date: 报告期日期 YYYY-MM-DD。

    Returns:
        str: 法定披露截止日 YYYY-MM-DD；异常格式返回原值。
    """
    return report_deadline(report_date)


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