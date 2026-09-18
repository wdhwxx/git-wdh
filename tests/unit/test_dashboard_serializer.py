# 文件名：tests/unit/test_dashboard_serializer.py
"""
test_dashboard_serializer.py - 回测看板数据序列化器单元测试
============================================================
覆盖 myquant/backtest/dashboard_serializer.py（最终定稿 §6）：

  1. build_dashboard_json 全链路（meta/config/equity_curve/trades/pool）
  2. 成本反向拆分：BUY 无印花税、SELL 有印花税、过户费双向、拆分合计≈引擎混合值
  3. 买卖配对：entry_date / entry_idx / _idx 对齐注入（降采样后）净值序列
  4. exit_reason_class 归类（与看板 §DERIVE JS 正则同源）
  5. pool 从 trades 派生（去重保序）
  6. 路径 B：load_batch_detail（include_detail 导出格式）
  7. 防御式校验：空净值曲线 / 非 epoch 秒时间戳 / 缺 detail

运行方式:
  pytest tests/unit/test_dashboard_serializer.py -v
"""

from __future__ import annotations

import json

import pytest

from myquant.backtest.dashboard_serializer import (
    _derive_pool,
    _epoch_to_date,
    _exit_reason_class,
    _pair_entry_dates,
    build_dashboard_dict,
    build_dashboard_json,
    load_batch_detail,
    render_dashboard,
)
from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    TradeRecord,
)

# 2024-01-02 00:00:00 本地时区的 epoch 秒基准（示例数据用）
_BASE_TS: float = 1704153600.0
_DAY: float = 86400.0


# ═══════════════════════════════════════════════
#  Fixture：示例净值曲线 + 成交记录
# ═══════════════════════════════════════════════

def _make_equity_points() -> list[EquityPoint]:
    """构造 3 个交易日的示例净值曲线（2024-01-02 ~ 2024-01-04）。"""
    specs = [
        ("2024-01-02", 1_000_000.0, 800_000.0, 200_000.0, 0.0, 1_000_000.0),
        ("2024-01-03", 1_010_000.0, 600_000.0, 410_000.0, 0.0, 1_005_000.0),
        ("2024-01-04", 1_020_000.0, 400_000.0, 620_000.0, 0.0, 1_010_000.0),
    ]
    return [
        EquityPoint(
            timestamp=_BASE_TS + i * _DAY,
            date=date,
            equity=equity,
            cash=cash,
            position_value=pv,
            drawdown=dd,
            benchmark_equity=bm,
        )
        for i, (date, equity, cash, pv, dd, bm) in enumerate(specs)
    ]


def _make_trades() -> list[TradeRecord]:
    """构造 1 笔 BUY + 1 笔 SELL 的示例成交（含混合 commission 口径）。"""
    return [
        TradeRecord(
            trade_id="T1",
            symbol="000001.SZ",
            side="BUY",
            quantity=1000,
            price=10.0,
            timestamp=_BASE_TS,
            commission=5.1,  # 佣金 + 过户费混合 = max(10000×0.0002,5) + 10000×0.00001
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
            timestamp=_BASE_TS + 2 * _DAY,
            commission=16.11,  # 佣金 + 印花税 + 过户费混合 = 5.0 + 11.0 + 0.11
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


def _make_result() -> BacktestResult:
    """构造可直接喂给 build_dashboard_json 的示例结果。"""
    config = BacktestConfig(
        strategy_name="demo_ma",
        symbols=["000001.SZ"],
        start_date="2024-01-02",
        end_date="2024-01-04",
        initial_capital=1_000_000.0,
        benchmark="000300.SH",
    )
    result = BacktestResult(
        config=config,
        equity_curve=_make_equity_points(),
        trades=_make_trades(),
    )
    result.run_id = "unit_demo"
    result.mark_complete()
    result.extra["engine_mode"] = "event_driven"
    return result


# ═══════════════════════════════════════════════
#  纯函数
# ═══════════════════════════════════════════════

class TestEpochToDate:
    """_epoch_to_date 时间戳→日期转换。"""

    def test_normal_epoch(self) -> None:
        assert _epoch_to_date(_BASE_TS) == "2024-01-02"

    def test_index_like_timestamp_raises(self) -> None:
        with pytest.raises(ValueError, match="疑似整数索引"):
            _epoch_to_date(5.0)  # 索引值而非 epoch 秒


class TestExitReasonClass:
    """_exit_reason_class 出场原因归类（与看板 JS 正则同源）。"""

    def test_rsrs(self) -> None:
        assert _exit_reason_class("RSRS=-0.85 < -0.7") == "rsrs_exit"

    def test_take_profit(self) -> None:
        assert _exit_reason_class("涨幅=21.3% >= 20%") == "take_profit"

    def test_target_exit(self) -> None:
        assert _exit_reason_class("target=17300") == "target_exit"
        assert _exit_reason_class("TARGET=18100") == "target_exit"

    def test_stop_loss(self) -> None:
        assert _exit_reason_class("stop_loss 触发") == "stop_loss"

    def test_max_hold(self) -> None:
        assert _exit_reason_class("max_hold 持仓到期") == "max_hold"

    def test_signal_flip(self) -> None:
        assert _exit_reason_class("flip 反向信号") == "signal_flip"

    def test_other_and_empty(self) -> None:
        assert _exit_reason_class("自定义原因") == "other"
        assert _exit_reason_class("") == "other"


class TestDerivePool:
    """_derive_pool 股票池派生。"""

    def test_dedup_preserves_order(self) -> None:
        trades = _make_trades()
        trades.append(
            TradeRecord(
                trade_id="T3", symbol="000001.SZ", side="BUY",
                quantity=100, price=10.0, timestamp=_BASE_TS + _DAY,
            )
        )
        assert _derive_pool(trades) == ["000001.SZ"]

    def test_empty(self) -> None:
        assert _derive_pool([]) == []


# ═══════════════════════════════════════════════
#  build_dashboard_json 全链路
# ═══════════════════════════════════════════════

class TestBuildDashboardJson:
    """路径 A：从 BacktestResult 构建看板载荷。"""

    def test_structure(self) -> None:
        data = build_dashboard_json(_make_result())
        assert set(data.keys()) == {"meta", "config", "equity_curve", "trades", "pool", "ohlcv"}
        assert data["meta"]["schema_version"] == "1.0"
        assert data["meta"]["run_id"] == "unit_demo"
        assert data["meta"]["engine_mode"] == "event_driven"
        assert data["meta"]["guard_passed"] is True

    def test_ohlcv_key(self) -> None:
        """载荷契约：ohlcv 键始终存在（列表；无行情/读取失败时为空列表）。"""
        data = build_dashboard_json(_make_result())
        assert isinstance(data["ohlcv"], list)
        if data["ohlcv"]:
            first = data["ohlcv"][0]
            assert {"date", "open", "high", "low", "close", "volume"} <= set(first.keys())
            assert first["date"] == "2024-01-02"

    def test_config_payload(self) -> None:
        data = build_dashboard_json(_make_result())
        cfg = data["config"]
        assert cfg["strategy_name"] == "demo_ma"
        assert cfg["start"] == "2024-01-02"
        assert cfg["end"] == "2024-01-04"
        assert cfg["initial_capital"] == 1_000_000.0
        assert cfg["benchmark"] == "000300.SH"
        assert cfg["fee"]["commission_rate"] == 0.0002
        assert cfg["fee"]["min_commission"] == 5.0
        assert cfg["fee"]["stamp_tax_rate"] == 0.001
        assert cfg["fee"]["transfer_fee_rate"] == 0.00001
        assert cfg["fee"]["slippage_bps"] == 2.0

    def test_equity_curve(self) -> None:
        data = build_dashboard_json(_make_result())
        curve = data["equity_curve"]
        assert len(curve) == 3
        assert curve[0]["date"] == "2024-01-02"
        assert curve[0]["equity"] == 1_000_000.0

    def test_trades_enriched_7_fields(self) -> None:
        data = build_dashboard_json(_make_result())
        buy, sell = data["trades"]
        # 7 个补齐字段全部存在
        for field in (
            "date", "entry_date", "entry_idx", "_idx",
            "exit_reason_class", "stamp_tax", "transfer_fee",
        ):
            assert field in buy, f"BUY 缺字段 {field}"
            assert field in sell, f"SELL 缺字段 {field}"

    def test_pool_from_trades(self) -> None:
        data = build_dashboard_json(_make_result())
        assert data["pool"] == ["000001.SZ"]

    def test_no_metrics_injected(self) -> None:
        data = build_dashboard_json(_make_result())
        assert "metrics" not in data  # 指标由前端现算，禁止注入


class TestCostSplit:
    """成本反向拆分（FeeCalculator 权威口径）。"""

    def test_buy_no_stamp_tax(self) -> None:
        data = build_dashboard_json(_make_result())
        buy = data["trades"][0]
        assert buy["stamp_tax"] == 0.0
        assert buy["transfer_fee"] > 0.0  # 过户费双向

    def test_sell_has_stamp_tax_and_transfer(self) -> None:
        data = build_dashboard_json(_make_result())
        sell = data["trades"][1]
        assert sell["stamp_tax"] == round(1000 * 11.0 * 0.001, 2)  # 11 元
        assert sell["transfer_fee"] == round(1000 * 11.0 * 0.00001, 2)

    def test_split_total_matches_engine_commission(self) -> None:
        data = build_dashboard_json(_make_result())
        for trade in data["trades"]:
            split_total = trade["commission"] + trade["stamp_tax"] + trade["transfer_fee"]
            engine_total = trade["extra"]["engine_commission_total"]
            assert abs(split_total - engine_total) < 0.02, (
                f"拆分合计 {split_total} 与引擎混合值 {engine_total} 偏差过大"
            )


class TestIndexAlignment:
    """_idx / entry_idx 对齐注入（降采样后）序列。"""

    def test_idx_aligns_injected_curve(self) -> None:
        data = build_dashboard_json(_make_result())
        buy, sell = data["trades"]
        curve_dates = [p["date"] for p in data["equity_curve"]]
        # sell.date = 2024-01-04 → 索引 2
        assert sell["date"] == "2024-01-04"
        assert sell["_idx"] == curve_dates.index(sell["date"])
        # sell.entry_date = BUY 日期 2024-01-02 → 索引 0
        assert sell["entry_date"] == "2024-01-02"
        assert sell["entry_idx"] == 0

    def test_idx_after_downsampling(self) -> None:
        """超过降采样上限时，_idx 仍须对齐注入后的曲线。"""
        points = _make_equity_points()
        # 造 2100 个点（> 2000 上限）
        many = list(points)
        for i in range(3, 2100):
            many.append(
                EquityPoint(
                    timestamp=_BASE_TS + i * _DAY,
                    date=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                    equity=1_000_000.0 + i,
                    cash=500_000.0,
                    position_value=500_000.0 + i,
                    drawdown=0.0,
                    benchmark_equity=1_000_000.0,
                )
            )
        trades = _make_trades()
        data = build_dashboard_dict(
            equity_points=many,
            trades=trades,
            config=_make_result().config,
        )
        injected_dates = [p["date"] for p in data["equity_curve"]]
        assert len(data["equity_curve"]) <= 2000
        sell = data["trades"][1]
        assert sell["date"] in injected_dates
        assert sell["_idx"] == injected_dates.index(sell["date"])


class TestPairEntry:
    """BUY→SELL 配对（FIFO 按标的）。"""

    def test_pair_map(self) -> None:
        trades = _make_trades()
        pair_map = _pair_entry_dates(trades)
        assert pair_map["T2"] == "2024-01-02"  # T2 配对到 T1 的买入日期


# ═══════════════════════════════════════════════
#  路径 B：批量 include_detail 导出
# ═══════════════════════════════════════════════

class TestLoadBatchDetail:
    """从批量 results.json 读取目标标的。"""

    def test_load_detail(self, tmp_path) -> None:
        result = _make_result()
        results_json = {
            "format_version": "1.1",
            "results": {
                "000001.SZ": {
                    "symbol": "000001.SZ",
                    "status": "completed",
                    "metrics": {
                        "total_return": 0.02,
                        "sharpe_ratio": 1.2,
                        "max_drawdown": -0.05,
                    },
                    "trade_count": 2,
                    "equity_length": 3,
                    "mode": "event_driven",
                    "detail": {
                        "trades": [t.to_dict() for t in result.trades],
                        "equity": [p.to_dict() for p in result.equity_curve],
                    },
                }
            },
        }
        path = tmp_path / "results.json"
        path.write_text(json.dumps(results_json, ensure_ascii=False), encoding="utf-8")

        data = load_batch_detail(
            str(path),
            "000001.SZ",
            strategy_name="demo_ma",
            start_date="2024-01-02",
            end_date="2024-01-04",
        )
        assert len(data["equity_curve"]) == 3
        assert len(data["trades"]) == 2
        assert data["trades"][1]["entry_date"] == "2024-01-02"
        assert data["pool"] == ["000001.SZ"]

    def test_missing_symbol(self, tmp_path) -> None:
        path = tmp_path / "results.json"
        path.write_text(json.dumps({"results": {}}, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(KeyError, match="未找到标的"):
            load_batch_detail(str(path), "600000.SH")

    def test_missing_detail(self, tmp_path) -> None:
        path = tmp_path / "results.json"
        path.write_text(
            json.dumps(
                {"results": {"000001.SZ": {"status": "completed", "metrics": {}}}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with pytest.raises(KeyError, match="include_detail"):
            load_batch_detail(str(path), "000001.SZ")

    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            load_batch_detail(str(tmp_path / "nope.json"), "000001.SZ")


# ═══════════════════════════════════════════════
#  防御式校验
# ═══════════════════════════════════════════════

class TestDefensive:
    """异常输入处理。"""

    def test_empty_equity_raises(self) -> None:
        with pytest.raises(ValueError, match="equity_curve 为空"):
            build_dashboard_dict(
                equity_points=[],
                trades=_make_trades(),
                config=_make_result().config,
            )

    def test_empty_trades_ok(self) -> None:
        data = build_dashboard_dict(
            equity_points=_make_equity_points(),
            trades=[],
            config=_make_result().config,
        )
        assert data["trades"] == []
        assert data["pool"] == []


# ═══════════════════════════════════════════════
#  渲染入口
# ═══════════════════════════════════════════════

class TestRenderDashboard:
    """占位符注入渲染。"""

    def test_template_without_placeholder_raises(self, tmp_path) -> None:
        template = tmp_path / "board.html"
        template.write_text(
            "<html><script>var DATA = buildMock();</script></html>",
            encoding="utf-8",
        )
        data = build_dashboard_json(_make_result())
        with pytest.raises(ValueError, match="占位符"):
            render_dashboard(data, tmp_path / "out.html", template_path=str(template))

    def test_render_injects_and_writes(self, tmp_path) -> None:
        template = tmp_path / "board.html"
        placeholder = "__DASHBOARD_DATA_JSON__"
        template.write_text(
            "<html><script>try{ DATA=" + placeholder + "; __REAL_DATA__=true; }catch(_e){}</script></html>",
            encoding="utf-8",
        )
        data = build_dashboard_json(_make_result())
        out = render_dashboard(data, tmp_path / "out.html", template_path=str(template))
        assert out.exists()
        rendered = out.read_text(encoding="utf-8")
        assert "__DASHBOARD_DATA_JSON__" not in rendered  # 注入点占位符已替换
        assert '"strategy_name":"demo_ma"' in rendered
        assert '"schema_version":"1.0"' in rendered
        assert 'try{ DATA={"meta":' in rendered

    def test_render_does_not_pollute_comment_placeholder(self, tmp_path) -> None:
        """回归：只替换代码行注入点，注释行中的占位符字样不得被 JSON 污染
        （历史 bug：模板注释“渲染时将 __DASHBOARD_DATA_JSON__ 替换”被全局
        replace 替换成 179KB JSON，导致模板与输出不一致）。"""
        template = tmp_path / "board.html"
        placeholder = "__DASHBOARD_DATA_JSON__"
        template.write_text(
            "/* 渲染时将 " + placeholder + " 替换为 JSON 载荷 */\n"
            "<script>try{ DATA=" + placeholder + "; __REAL_DATA__=true; }catch(_e){}</script>",
            encoding="utf-8",
        )
        data = build_dashboard_json(_make_result())
        out = render_dashboard(data, tmp_path / "out.html", template_path=str(template))
        rendered = out.read_text(encoding="utf-8")
        # 注释行保留原占位符字样（未被 JSON 污染）
        assert "/* 渲染时将 " + placeholder + " 替换为 JSON 载荷 */" in rendered
        # 代码行注入点已被替换为 JSON 载荷
        assert 'try{ DATA={"meta":' in rendered
        assert "; __REAL_DATA__=true; }catch(_e){}" in rendered
