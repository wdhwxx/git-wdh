"""统一时点日落点（v3.0 §6.4.2，M1A-02）。

背景
----
历史上「财务记录 → 可消费时点」的语义散落在四个调用点，且口径不一致：

- ``financial_service.py:537-538``：``ann_date or report_date``（真实披露日优先）；
- ``derived_metrics.py:254-275``：``report_deadline``（法定披露截止日推定）；
- ``scripts/数据导出_DB到processed.py:131-146``：按 ``report_date`` 匹配（③ 穿越缺陷）；
- ``manager.py:1287-1297``：按 ``report_date`` 匹配（④ 穿越缺陷，与 ③ 互相背书）。

本模块将「有效披露日」收敛为唯一实现（ann_date 优先 → report_deadline 推定 →
report_date 兜底），并导出③④共用的「取最近一期」匹配函数，四个调用点统一改调，
杜绝口径分叉（③④ 穿越缺陷的根因）。

防未来语义
----------
财务数据只有在「有效披露日 <= 交易日」时才可被消费；有效披露日缺失的记录
以法定披露截止日推定（保守正确，宁可晚见不可早见）。
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

# ─────────────────────────────────────────────
# 统一时点日落点
# ─────────────────────────────────────────────


def next_day(iso_date: str) -> str:
    """ISO 日期 +1 天（M1A-10：财务列 available_time = 有效披露日 + 1）。

    Args:
        iso_date: ISO 日期字符串 YYYY-MM-DD。

    Returns:
        str: 次日 YYYY-MM-DD；异常格式保留原值（防御式，不阻断下游）。
    """
    try:
        return (datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )
    except (ValueError, TypeError):
        return iso_date


def report_deadline(report_date: str) -> str:
    """财报报告期 → 法定披露截止日（发布日推算，P1-① #83 逻辑收敛于此）。

    规则：一季报 4-30 / 半年报 8-31 / 三季报 10-31 / 年报 次年 4-30；
    异常报告期格式保留原值（不阻断下游，交接方案原定）。

    Args:
        report_date: 报告期日期 YYYY-MM-DD。

    Returns:
        str: 法定披露截止日 YYYY-MM-DD；异常格式返回原值。
    """
    if not report_date or len(report_date) < 10:
        return report_date
    try:
        year = int(report_date[:4])
    except (ValueError, TypeError):
        return report_date
    month_day = report_date[5:10]
    if month_day == "03-31":
        return f"{year}-04-30"
    if month_day == "06-30":
        return f"{year}-08-31"
    if month_day == "09-30":
        return f"{year}-10-31"
    if month_day == "12-31":
        return f"{year + 1}-04-30"
    return report_date


def eff_disclosure_date(record: Dict[str, Any]) -> str:
    """财务记录 → 有效披露日（统一时点日落点，v3.0 §6.4.2）。

    规则：``ann_date`` 存在且非空时取真实披露日；缺失回退法定披露截止日推定
    （``report_deadline``）；再缺失回退 ``report_date`` 本身（兜底，不阻断）。

    Args:
        record: 财务记录（须含 ``ann_date`` / ``report_date`` 键）。

    Returns:
        str: 有效披露日 YYYY-MM-DD；两键均缺失返回空串。
    """
    ann = str(record.get("ann_date") or "").strip()
    if ann:
        return ann
    report = str(record.get("report_date") or "").strip()
    if not report:
        return ""
    return report_deadline(report)


def match_latest_financial(
    records: Sequence[Dict[str, Any]], trade_date: str
) -> Optional[Dict[str, Any]]:
    """取 ``trade_date`` 时点可见的最新一期财务记录（③④ 共用单一实现）。

    按有效披露日降序排序，取首个 ``<= trade_date`` 的记录；无可见记录返回 None。
    参照 ``financial_service.py:540-551`` 口径（降序 + 首个 <= 交易日）。

    Args:
        records: 财务记录列表（每项须含 ``ann_date`` / ``report_date`` 键）。
        trade_date: 交易日 YYYY-MM-DD（信号日/写入日，防未来语义的时点基准）。

    Returns:
        Optional[Dict[str, Any]]: 最新可见一期；无可见记录返回 None。
    """
    if not records:
        return None
    if not trade_date:
        return None
    sorted_records: List[Dict[str, Any]] = sorted(
        records, key=eff_disclosure_date, reverse=True
    )
    for rec in sorted_records:
        eff = eff_disclosure_date(rec)
        if eff and eff <= trade_date:
            return rec
    return None
