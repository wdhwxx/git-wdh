# 文件名：myquant/monitoring/attribution.py
"""
attribution.py - 收益归因分析框架（C-5，回写#234）
==========================================================================
本模块承载《量化系统监控模块施工总清单_V1.2》C-5：
  收益归因四维框架 + 实盘/回测归因同口径对比 + 归因结果进日报摘要 +
  归因差异可视化数据。

归因四维（每组输出：贡献值 contribution + 贡献占比 ratio + 样本数 count +
风险 risk / 风险占比 risk_ratio）：
  - strategy  ：按策略名（TradeRecord.strategy_id）归因；
  - factor     ：按因子/信号源（TradeRecord.signal_source）归因；
  - symbol     ：按标的（TradeRecord.symbol）归因；
  - period     ：按时间桶（day=自然日 / week=ISO 周）归因。

归因口径（简单可解释的加总差值/占比归因，自洽无近似）：
  - 一条归因记录 = 一笔已实现盈亏事件 {timestamp, strategy, factor, symbol, pnl}；
  - 组贡献值 = Σ 组内 pnl；总贡献 = Σ 全部记录 pnl；
  - 贡献占比 = 组贡献 / 总贡献。总贡献=0 时占比置 0.0（分母无定义，日报口径中披露）；
  - 自洽：Σ 组贡献值 == 总贡献（加总口径，daily_summary 对账核验，不通过置 self_consistent=False）。

实盘/回测对比（同时间窗同参数，逐维输出差异）：
  - 同口径分别对 backtest / live 记录做四维归因；
  - 按 group 对齐输出：backtest_value / live_value / abs_diff(=live-bt) /
    rel_diff(=(live-bt)/bt，bt=0 → None) / missing（缺失侧标注）。

日报归因摘要（daily_summary()）：
  - JSON 可序列化 dict：含四维归因表（backtest/live）+ 实盘回测差异表（comparison）
    + 口径说明（caliber_note）+ 对账标记（self_consistent），供日报直接引用。

归因差异可视化数据（visualization_data()）：
  - 每维 backtest_series / live_series（label/value/ratio，柱状图用）+
    diff_table（label/backtest/live/abs_diff/rel_diff，对比表用）+
    period_series（日/周 pnl 时序，折线图用），全部 dict/list 可 JSON 渲染。

严格防未来（红线）：
  - add_record() 拒绝 timestamp > now（容差 _FUTURE_EPS=1s）的记录；
  - period 桶键只用记录自身 timestamp（本行使用的数据截至该记录时刻）；
  - 不读未来行情/未实现盈亏，只消费已发生的收益/信号记录。
"""

from __future__ import annotations

import json
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from myquant.backtest.metrics import (
    ATTRIBUTION_DEFAULT_GROUP,
    compare_attribution,
    group_contribution,
    group_risk_contribution,
    period_bucket_of,
)
from myquant.core.logger import get_logger

logger = get_logger("backtest.attribution")

# ─────────────────────────────────────────────
# 常量提取（非业务阈值；本模块无 SLO 阈值，仅口径常量）
# ─────────────────────────────────────────────
#: 归因四维
DIMENSION_STRATEGY: str = "strategy"
DIMENSION_FACTOR: str = "factor"
DIMENSION_SYMBOL: str = "symbol"
DIMENSION_PERIOD: str = "period"
ALL_DIMENSIONS: Tuple[str, ...] = (
    DIMENSION_STRATEGY,
    DIMENSION_FACTOR,
    DIMENSION_SYMBOL,
    DIMENSION_PERIOD,
)

#: 数据来源（回测 / 实盘）
SOURCE_BACKTEST: str = "backtest"
SOURCE_LIVE: str = "live"
ALL_SOURCES: Tuple[str, ...] = (SOURCE_BACKTEST, SOURCE_LIVE)

#: 周期粒度（period 维）
PERIOD_DAY: str = "day"
PERIOD_WEEK: str = "week"
ALL_PERIODS: Tuple[str, ...] = (PERIOD_DAY, PERIOD_WEEK)

#: 记录缺字段时的归组名
DEFAULT_GROUP: str = ATTRIBUTION_DEFAULT_GROUP

#: 未来点判定容差（秒）：timestamp 超过 now 的绝对上限
_FUTURE_EPS: float = 1.0

#: 归因对账容差（Σ组贡献 vs 总贡献，绝对值）
_RECONCILE_TOL: float = 1e-6

#: 四维 → 记录字段名映射（period 维用 period_bucket 桶键）
_DIM_FIELD: Dict[str, str] = {
    DIMENSION_STRATEGY: "strategy",
    DIMENSION_FACTOR: "factor",
    DIMENSION_SYMBOL: "symbol",
    DIMENSION_PERIOD: "period_bucket",
}

#: 日报口径说明（进 daily_summary["caliber_note"]）
CALIBER_NOTE: str = (
    "收益归因=加总pnl口径：总收益=Σ各组贡献（无近似）；"
    "贡献占比=组贡献/总收益，总收益=0时占比置0（分母无定义）；"
    "回测/实盘同时间窗同参数逐维按group对齐，差异=实盘-回测"
    "（abs_diff=live-bt，rel_diff=(live-bt)/bt，bt=0时rel_diff=null）；"
    "风险=组内逐周期pnl标准差（代理，不年化）。"
)


class AttributionAnalyzer:
    """
    收益归因分析器：四维归因 + 回测/实盘同口径对比 + 日报摘要 + 可视化数据。

    用法:
        from myquant.backtest.attribution import AttributionAnalyzer

        an = AttributionAnalyzer(period="day")
        an.add_record("backtest", ts1, pnl=120.0, strategy="trend_a",
                      factor="ma_cross", symbol="600519")
        an.add_record("live",     ts2, pnl=90.0,  strategy="trend_a",
                      factor="ma_cross", symbol="600519")
        summary = an.daily_summary()      # 日报引用（JSON 可序列化）
        viz     = an.visualization_data() # 差异可视化渲染数据
    """

    def __init__(
        self,
        period: str = PERIOD_DAY,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Parameters
        ----------
        period : str
            period 维桶粒度："day"（自然日）或 "week"（ISO 周）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        """
        if period not in ALL_PERIODS:
            raise ValueError(f"period 必须是 {ALL_PERIODS} 之一，收到 {period!r}")
        self._period: str = period
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._records: Dict[str, List[Dict[str, Any]]] = {
            SOURCE_BACKTEST: [],
            SOURCE_LIVE: [],
        }
        self._last_summary: Dict[str, Any] = {}
        logger.debug("AttributionAnalyzer 初始化: period=%s", period)

    # ── 记录摄入（严格防未来）──────────────────────────────────────────
    def add_record(
        self,
        source: str,
        timestamp: float,
        pnl: float,
        strategy: str = DEFAULT_GROUP,
        factor: str = DEFAULT_GROUP,
        symbol: str = DEFAULT_GROUP,
    ) -> None:
        """
        摄入一条"已发生"的归因记录（一笔已实现盈亏事件）。

        防未来：timestamp 超过当前时刻 now（容差 _FUTURE_EPS）即拒绝抛 ValueError；
        period 桶键只用该记录自身 timestamp（本行使用的数据截至记录时刻）。

        Parameters
        ----------
        source : str
            数据来源："backtest" 或 "live"。
        timestamp : float
            记录发生时刻（epoch 秒，必须 <= now）。
        pnl : float
            本笔已实现盈亏贡献（可正可负，有限数）。
        strategy / factor / symbol : str
            三维分组标签；缺省归 DEFAULT_GROUP。

        Raises
        ------
        ValueError : source 非法 / timestamp 或 pnl 不可解析为有限数 /
            timestamp 为未来时刻时抛出。
        """
        if source not in ALL_SOURCES:
            raise ValueError(f"source 必须是 {ALL_SOURCES} 之一，收到 {source!r}")
        try:
            ts = float(timestamp)
            v = float(pnl)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"timestamp/pnl 必须可转 float，收到 ts={timestamp!r} pnl={pnl!r}"
            ) from e
        if not math.isfinite(ts) or not math.isfinite(v):
            raise ValueError(f"timestamp/pnl 必须为有限数，收到 ts={ts!r} pnl={v!r}")
        now = self._clock()
        if ts > now + _FUTURE_EPS:
            raise ValueError(
                f"拒绝未来归因记录: timestamp={ts:.3f} 晚于当前 {now:.3f}（防未来红线）"
            )

        # period 桶键：仅用记录自身时间戳，不引入未来数据
        bucket = period_bucket_of(ts, self._period)
        self._records[source].append({
            "timestamp": ts,
            "strategy": str(strategy or DEFAULT_GROUP),
            "factor": str(factor or DEFAULT_GROUP),
            "symbol": str(symbol or DEFAULT_GROUP),
            "pnl": v,
            "period_bucket": bucket,
        })

    def add_records(self, source: str, records: List[Dict[str, Any]]) -> int:
        """
        批量摄入归因记录（非法行跳过不阻断，返回成功摄入条数）。

        Parameters
        ----------
        source : str
            "backtest" / "live"。
        records : list[dict]
            每条至少含 timestamp / pnl；可选 strategy / factor / symbol。

        Returns
        -------
        int : 成功摄入条数。
        """
        if not isinstance(records, list):
            raise ValueError(f"records 必须为 list，收到 {type(records).__name__}")
        ingested = 0
        for rec in records:
            if not isinstance(rec, dict):
                logger.warning("归因批量摄入跳过非 dict 记录: %r", rec)
                continue
            try:
                self.add_record(
                    source=source,
                    timestamp=rec["timestamp"],
                    pnl=rec["pnl"],
                    strategy=rec.get("strategy", DEFAULT_GROUP),
                    factor=rec.get("factor", DEFAULT_GROUP),
                    symbol=rec.get("symbol", DEFAULT_GROUP),
                )
                ingested += 1
            except (ValueError, KeyError, TypeError) as e:
                logger.warning("归因批量摄入跳过非法行（不阻断）: %s", e)
        logger.info("归因批量摄入: source=%s 成功 %d/%d", source, ingested, len(records))
        return ingested

    # ── 四维归因计算（纯函数编排，自洽对账）───────────────────────────
    def attribution(self, source: str) -> Dict[str, Any]:
        """
        计算某一来源（backtest/live）的四维归因（收益贡献 + 风险贡献）。

        Parameters
        ----------
        source : str
            "backtest" / "live"。

        Returns
        -------
        dict : {"source", "period", "total_pnl", "record_count", "dimensions"}；
            dimensions[dim] = [{"group","contribution","ratio","count",
                                "risk","risk_ratio"}, ...]。

        Raises
        ------
        ValueError : source 非法时抛出。
        """
        if source not in ALL_SOURCES:
            raise ValueError(f"source 必须是 {ALL_SOURCES} 之一，收到 {source!r}")
        recs = self._records[source]
        total = float(sum(r["pnl"] for r in recs))

        dim_rows: Dict[str, List[Dict[str, Any]]] = {}
        for dim in ALL_DIMENSIONS:
            fkey = _DIM_FIELD[dim]
            contrib_rows = group_contribution(recs, fkey)
            risk_rows = group_risk_contribution(recs, fkey)
            rmap = {r["group"]: r for r in risk_rows}
            for row in contrib_rows:
                rr = rmap.get(row["group"], {})
                row["risk"] = float(rr.get("risk", 0.0))
                row["risk_ratio"] = float(rr.get("risk_ratio", 0.0))
            dim_rows[dim] = contrib_rows

        return {
            "source": source,
            "period": self._period,
            "total_pnl": total,
            "record_count": len(recs),
            "dimensions": dim_rows,
        }

    # ── 实盘/回测同口径对比 ───────────────────────────────────────────
    def compare(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        逐维对比实盘与回测归因差异（同时间窗同参数，按 group 对齐）。

        Returns
        -------
        dict : {dim: [{"group","backtest_value","live_value","abs_diff",
                       "rel_diff","missing"}, ...]}，dim ∈ 四维。
        """
        bt = self.attribution(SOURCE_BACKTEST)
        live = self.attribution(SOURCE_LIVE)
        diff: Dict[str, List[Dict[str, Any]]] = {}
        for dim in ALL_DIMENSIONS:
            diff[dim] = compare_attribution(
                bt["dimensions"][dim], live["dimensions"][dim]
            )
        return diff

    # ── 日报归因摘要（C-5 验收：日报含归因摘要）──────────────────────
    def daily_summary(self) -> Dict[str, Any]:
        """
        产出结构化日报归因摘要 dict（JSON 可序列化，供日报引用）。

        含：四维归因表（backtest/live）+ 实盘回测差异表（comparison）+
        口径说明 caliber_note + 对账标记 self_consistent。

        Returns
        -------
        dict : 日报归因摘要（全 JSON 可序列化；None 仅用于 rel_diff 无定义项）。
        """
        bt = self.attribution(SOURCE_BACKTEST)
        live = self.attribution(SOURCE_LIVE)
        diff = self.compare()

        summary: Dict[str, Any] = {
            "generated_at": float(self._clock()),
            "period": self._period,
            "method": "additive_pnl",
            "self_consistent": True,
            "caliber_note": CALIBER_NOTE,
            "backtest": {
                "total_pnl": bt["total_pnl"],
                "record_count": bt["record_count"],
                "dimensions": bt["dimensions"],
            },
            "live": {
                "total_pnl": live["total_pnl"],
                "record_count": live["record_count"],
                "dimensions": live["dimensions"],
            },
            "comparison": diff,
        }

        # 归因自洽对账：每维 Σ组贡献 必须 == 总贡献（容差内）
        for src, attr in ((SOURCE_BACKTEST, bt), (SOURCE_LIVE, live)):
            for dim in ALL_DIMENSIONS:
                s = sum(r["contribution"] for r in attr["dimensions"][dim])
                if abs(s - attr["total_pnl"]) > _RECONCILE_TOL:
                    logger.error(
                        "归因对账失败: source=%s dim=%s Σ组=%r != 总=%r",
                        src, dim, s, attr["total_pnl"],
                    )
                    summary["self_consistent"] = False

        self._last_summary = summary
        return summary

    # ── 归因差异可视化数据（C-5 验收：差异可视化）────────────────────
    def visualization_data(self) -> Dict[str, Any]:
        """
        产出归因差异可视化数据（dict/list 结构，可 JSON 序列化渲染）。

        结构：
          - dimensions[dim].backtest_series / live_series：
              [{"label": 组键, "value": 贡献值, "ratio": 占比}]（柱状图）；
          - dimensions[dim].diff_table：
              [{"label", "backtest", "live", "abs_diff", "rel_diff"}]（对比表）；
          - period_series.backtest / live：
              [{"period": 桶键, "pnl": 该周期盈亏}]（折线图，按周期升序）。

        Returns
        -------
        dict : 可视化渲染数据。
        """
        summary = self.daily_summary()
        viz: Dict[str, Any] = {
            "period": self._period,
            "dimensions": {},
        }
        for dim in ALL_DIMENSIONS:
            bt_rows = summary["backtest"]["dimensions"][dim]
            live_rows = summary["live"]["dimensions"][dim]
            cmp_rows = summary["comparison"][dim]
            viz["dimensions"][dim] = {
                "backtest_series": [
                    {"label": r["group"], "value": r["contribution"], "ratio": r["ratio"]}
                    for r in bt_rows
                ],
                "live_series": [
                    {"label": r["group"], "value": r["contribution"], "ratio": r["ratio"]}
                    for r in live_rows
                ],
                "diff_table": [
                    {
                        "label": r["group"],
                        "backtest": r["backtest_value"],
                        "live": r["live_value"],
                        "abs_diff": r["abs_diff"],
                        "rel_diff": r["rel_diff"],
                    }
                    for r in cmp_rows
                ],
            }
        viz["period_series"] = {
            "backtest": self._period_series(SOURCE_BACKTEST),
            "live": self._period_series(SOURCE_LIVE),
        }
        return viz

    def _period_series(self, source: str) -> List[Dict[str, Any]]:
        """
        聚合某来源的逐周期盈亏时序（按周期桶升序，折线图用）。

        Parameters
        ----------
        source : str
            "backtest" / "live"。

        Returns
        -------
        list[dict] : [{"period": 桶键, "pnl": 该周期Σ盈亏}, ...]，按周期升序。
        """
        bucket_pnl: Dict[str, float] = {}
        for r in self._records[source]:
            b = r["period_bucket"]
            bucket_pnl[b] = bucket_pnl.get(b, 0.0) + r["pnl"]
        return [{"period": b, "pnl": bucket_pnl[b]} for b in sorted(bucket_pnl.keys())]

    # ── 状态可观测 ────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """返回最近一次 daily_summary() 结果（未调用则为空 dict）。"""
        return dict(self._last_summary)

    @property
    def period(self) -> str:
        """当前 period 维桶粒度。"""
        return self._period


# ═══════════════════════════════════════════════
#  __main__ 自测 demo
# ═══════════════════════════════════════════════
def _run_self_test() -> int:
    """模块自测 demo：固定时钟喂合成记录，验证四维归因/自洽/对比/日报/可视化/防未来闭环。"""
    passed = 0
    failures: List[str] = []

    def check(name: str, cond: bool, info: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"{name} {info}")

    # 固定时钟：now = 第 5 天收盘后
    base = 1_700_000_000.0
    day = 86_400.0
    clock_now = [base + 5 * day + 3600.0]

    def clock() -> float:
        return clock_now[0]

    an = AttributionAnalyzer(period=PERIOD_DAY, clock=clock)

    # 回测记录：策略 trend_a 两日盈亏；factor=ma_cross/rsi；symbol=600519/000001
    bt_recs = [
        {"timestamp": base + 0 * day, "pnl": 100.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "600519"},
        {"timestamp": base + 1 * day, "pnl": 50.0, "strategy": "trend_a",
         "factor": "rsi", "symbol": "600519"},
        {"timestamp": base + 2 * day, "pnl": -30.0, "strategy": "meanrev_b",
         "factor": "rsi", "symbol": "000001"},
        {"timestamp": base + 3 * day, "pnl": 80.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "000001"},
    ]
    # 实盘记录：同口径（总盈亏 200 vs 回测 200），但趋势策略实盘少赚
    live_recs = [
        {"timestamp": base + 0 * day, "pnl": 80.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "600519"},
        {"timestamp": base + 1 * day, "pnl": 40.0, "strategy": "trend_a",
         "factor": "rsi", "symbol": "600519"},
        {"timestamp": base + 2 * day, "pnl": -10.0, "strategy": "meanrev_b",
         "factor": "rsi", "symbol": "000001"},
        {"timestamp": base + 3 * day, "pnl": 90.0, "strategy": "trend_a",
         "factor": "ma_cross", "symbol": "000001"},
    ]
    n_bt = an.add_records(SOURCE_BACKTEST, bt_recs)
    n_live = an.add_records(SOURCE_LIVE, live_recs)

    # 1) 摄入条数
    check("1) 回测/实盘各摄入 4 条", n_bt == 4 and n_live == 4, f"{n_bt},{n_live}")

    # 2) 四维归因计算正确性（回测）
    bt = an.attribution(SOURCE_BACKTEST)
    check("2) 回测总盈亏=200", abs(bt["total_pnl"] - 200.0) < 1e-9, f"{bt['total_pnl']}")
    strat = {r["group"]: r for r in bt["dimensions"][DIMENSION_STRATEGY]}
    # trend_a = 100+50+80 = 230；meanrev_b = -30
    check("2) trend_a 贡献=230", abs(strat["trend_a"]["contribution"] - 230.0) < 1e-9, f"{strat}")
    check("2) trend_a 占比=230/200=1.15", abs(strat["trend_a"]["ratio"] - 1.15) < 1e-9, f"{strat['trend_a']}")
    check("2) meanrev_b 贡献=-30", abs(strat["meanrev_b"]["contribution"] - (-30.0)) < 1e-9, "")
    check("2) 四维齐全", set(bt["dimensions"].keys()) == set(ALL_DIMENSIONS), "")

    # 3) 归因自洽：每维 Σ组贡献 == 总贡献
    for dim in ALL_DIMENSIONS:
        s = sum(r["contribution"] for r in bt["dimensions"][dim])
        check(f"3) 自洽对账 {dim}: Σ组==总", abs(s - 200.0) < _RECONCILE_TOL, f"Σ={s}")

    # 4) period 维：4 个自然日桶
    check("4) period 维 4 个日桶", len(bt["dimensions"][DIMENSION_PERIOD]) == 4,
          f"{bt['dimensions'][DIMENSION_PERIOD]}")

    # 5) 实盘/回测对比差异
    cmp = an.compare()
    strat_cmp = {r["group"]: r for r in cmp[DIMENSION_STRATEGY]}
    # trend_a: bt=230 live=80+40+90=210 → abs_diff=-20
    check("5) trend_a abs_diff=-20", abs(strat_cmp["trend_a"]["abs_diff"] - (-20.0)) < 1e-9,
          f"{strat_cmp['trend_a']}")
    # rel_diff = -20/230
    check("5) trend_a rel_diff≈-0.087",
          strat_cmp["trend_a"]["rel_diff"] is not None
          and abs(strat_cmp["trend_a"]["rel_diff"] - (-20.0 / 230.0)) < 1e-6,
          f"{strat_cmp['trend_a']['rel_diff']}")

    # 6) 日报摘要结构 + JSON 可序列化
    summary = an.daily_summary()
    check("6) 摘要含四维归因表+差异表",
          "backtest" in summary and "live" in summary and "comparison" in summary
          and "caliber_note" in summary and summary["self_consistent"] is True,
          f"{list(summary.keys())}")
    try:
        s_json = json.dumps(summary, ensure_ascii=False)
        check("6) 日报摘要 JSON 可序列化", True, "")
    except (TypeError, ValueError) as e:
        check("6) 日报摘要 JSON 可序列化", False, f"{e}")

    # 7) 可视化数据 + JSON 可序列化
    viz = an.visualization_data()
    check("7) 可视化含四维+period_series",
          set(viz["dimensions"].keys()) == set(ALL_DIMENSIONS)
          and "period_series" in viz, "")
    try:
        v_json = json.dumps(viz, ensure_ascii=False)
        check("7) 可视化数据 JSON 可序列化", True, f"len={len(v_json)}")
    except (TypeError, ValueError) as e:
        check("7) 可视化数据 JSON 可序列化", False, f"{e}")
    # diff_table 含 backtest/live/abs_diff/rel_diff
    row0 = viz["dimensions"][DIMENSION_STRATEGY]["diff_table"][0]
    check("7) diff_table 字段齐全",
          {"label", "backtest", "live", "abs_diff", "rel_diff"} <= set(row0), f"{row0}")

    # 8) 防未来：拒绝未来记录
    rejected = False
    try:
        an.add_record(SOURCE_LIVE, clock_now[0] + 100.0, 1.0)
    except ValueError:
        rejected = True
    check("8) 拒绝未来归因记录", rejected, "")

    # 9) 防御校验：非法 source / 非法 period / 非有限 pnl / 非 list 批量
    bad = False
    try:
        an.add_record("paper", base, 1.0)
    except ValueError:
        bad = True
    check("9) 非法 source 拒绝", bad, "")
    bad2 = False
    try:
        AttributionAnalyzer(period="month")
    except ValueError:
        bad2 = True
    check("9) 非法 period 拒绝", bad2, "")
    bad3 = False
    try:
        an.add_record(SOURCE_LIVE, base, float("nan"))
    except ValueError:
        bad3 = True
    check("9) 非有限 pnl 拒绝", bad3, "")

    print("=" * 60)
    print(f"attribution.py 自测: {passed} 项通过")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        print(f"失败 {len(failures)} 项")
        return 1
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_self_test())
