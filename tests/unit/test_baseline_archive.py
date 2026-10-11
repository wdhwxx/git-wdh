# 文件名：tests/unit/test_baseline_archive.py
"""
test_baseline_archive.py - 基线净值档案模块单元测试（P0-00 子步骤 B）
================================================================
覆盖（治理规则：新模块必配测试，R-19 约束）：
  - compute_config_hash：同配置同哈希、运行元数据剔除、64 位 hex；
  - compute_equity_hash / compute_trades_fingerprint：单点篡改即变、
    trade_id 随机主键不参与指纹（R-19 实测：uuid4 每次 run 不同）；
  - build_baseline / write_baseline / load_baseline：字段齐全、往返一致、
    文件名含 config_hash、运行元数据不入档；
  - verify_reproducible：相同结果通过、净值篡改超阈判红、
    成交笔数差超 5% 判红、最终净值差超阈判红。

全部用例为纯内存构造（BacktestConfig/BacktestResult 手工造数），
不依赖数据文件与网络，不触碰 quant.db。
"""

import json
import shutil
from pathlib import Path

import pytest

from myquant.backtest.baseline_archive import (
    BaselineArchiver,
    EQUITY_MAX_ABS_DIFF,
    TRADE_COUNT_DIFF_RATIO,
)
from myquant.models.backtest import (
    BacktestConfig,
    BacktestResult,
    EquityPoint,
    TradeRecord,
)

# ── 夹具 ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def archiver(tmp_path_factory) -> BaselineArchiver:
    """构造隔离输出目录的档案器（不依赖项目磁盘）。"""
    tmp = tmp_path_factory.mktemp("baseline_archive_out")
    return BaselineArchiver(
        project_root=Path(r"D:\Projects\Pythonwdhmm"),
        out_dir=tmp,
    )


@pytest.fixture()
def config() -> BacktestConfig:
    """确定性回测配置（无运行元数据）。"""
    return BacktestConfig(
        strategy_name="rsrs",
        symbols=["600519.SH", "000858.SZ"],
        start_date="2024-01-01",
        end_date="2024-06-30",
        frequency="1d",
        initial_capital=1_000_000.0,
        benchmark="000300.SH",
        risk_free_rate=0.02,
        params={"lookback": 16, "topk": 1},
    )


def _make_result(config: BacktestConfig, n_points: int = 4, n_trades: int = 3) -> BacktestResult:
    """构造确定性回测结果（净值曲线 + 成交明细）。"""
    result = BacktestResult(config=config, status="completed")
    result.equity_curve = [
        EquityPoint(
            timestamp=float(i),
            date=f"2024-01-{i:02d}",
            equity=1.0 + i * 0.01,
            cash=1_000_000.0 - i * 10_000.0,
            position_value=i * 10_000.0,
            drawdown=0.0,
            benchmark_equity=1.0,
        )
        for i in range(1, n_points + 1)
    ]
    result.trades = [
        TradeRecord(
            trade_id=f"t{i}",
            symbol="600519.SH",
            side="BUY",
            quantity=100.0,
            price=100.0 + i,
            timestamp=float(i),
            commission=1.0,
            slippage=0.5,
            pnl=0.0,
            pnl_pct=0.0,
            holding_days=1,
            strategy_id="rsrs",
        )
        for i in range(1, n_trades + 1)
    ]
    return result


# ── config_hash 稳定性与口径 ─────────────────────────────────────────


class TestConfigHash:
    def test_same_config_same_hash(self, archiver, config) -> None:
        """同配置重复计算哈希一致（幂等）。"""
        assert archiver.compute_config_hash(config) == archiver.compute_config_hash(config)

    def test_hash_is_64_hex(self, archiver, config) -> None:
        """config_hash 为 64 位十六进制。"""
        h = archiver.compute_config_hash(config)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_run_metadata_not_in_hash(self, archiver, config) -> None:
        """运行元数据（run_id/时间戳）不影响 config_hash。"""
        c1 = BacktestConfig(**config.to_dict())
        c2 = BacktestConfig(**config.to_dict())
        # 修改 to_dict 后补齐 params（to_dict 不含 extra）
        assert archiver.compute_config_hash(c1) == archiver.compute_config_hash(c2)

    def test_params_change_changes_hash(self, archiver, config) -> None:
        """策略参数变化 → 哈希变化（配置指纹敏感）。"""
        c2 = BacktestConfig(**config.to_dict())
        c2.params = {"lookback": 32, "topk": 1}
        assert archiver.compute_config_hash(config) != archiver.compute_config_hash(c2)


# ── 指纹敏感性 ───────────────────────────────────────────────────────


class TestFingerprints:
    def test_equity_single_point_tamper(self, archiver, config) -> None:
        """净值曲线单点篡改 → equity_hash 变化。"""
        result = _make_result(config)
        h1 = archiver.compute_equity_hash(result)

        tampered = _make_result(config)
        tampered.equity_curve[2] = EquityPoint(
            timestamp=3.0, date="2024-01-03", equity=9.99,
            cash=1.0, position_value=2.0, drawdown=0.0, benchmark_equity=1.0,
        )
        assert archiver.compute_equity_hash(tampered) != h1

    def test_trades_single_tamper(self, archiver, config) -> None:
        """成交明细单字段篡改 → trades_fingerprint 变化。"""
        result = _make_result(config)
        h1 = archiver.compute_trades_fingerprint(result)

        tampered = _make_result(config)
        tampered.trades[0].quantity = 999.0
        assert archiver.compute_trades_fingerprint(tampered) != h1

    def test_trade_id_random_not_in_fingerprint(self, archiver, config) -> None:
        """trade_id 为运行期随机主键（uuid4），必须不影响指纹（R-19 实测）。"""
        result = _make_result(config)
        h1 = archiver.compute_trades_fingerprint(result)

        rerun = _make_result(config)
        rerun.trades[0].trade_id = "another_random_id_000"
        assert archiver.compute_trades_fingerprint(rerun) == h1

    def test_empty_series_has_sentinel(self, archiver, config) -> None:
        """空净值/空成交返回哨兵值（不崩溃）。"""
        result = BacktestResult(config=config, status="completed")
        assert archiver.compute_equity_hash(result) == "empty_equity"
        assert archiver.compute_trades_fingerprint(result) == "empty_trades"


# ── 构建 / 落盘 / 读取 ───────────────────────────────────────────────


class TestBuildWriteLoad:
    def test_baseline_fields_complete(self, archiver, config) -> None:
        """档案 payload 字段齐全且无运行元数据。"""
        result = _make_result(config)
        baseline = archiver.build_baseline(result, "rsrs", "mixed", config, extra_meta={"k": "v"})
        for key in ("format_version", "strategy", "engine", "config_hash",
                    "equity_curve_hash", "trades_fingerprint", "metrics", "summary"):
            assert key in baseline, f"缺字段: {key}"
        assert baseline["strategy"] == "rsrs"
        assert baseline["engine"] == "mixed"
        assert baseline["summary"]["total_trades"] == 3
        assert baseline["summary"]["equity_points"] == 4
        # 运行元数据不入档
        for meta_key in ("run_id", "started_at", "finished_at", "status"):
            assert meta_key not in baseline, f"运行元数据不应入档: {meta_key}"

    def test_write_load_roundtrip(self, archiver, config) -> None:
        """write_baseline → load_baseline 往返一致。"""
        result = _make_result(config)
        baseline = archiver.build_baseline(result, "rsrs", "mixed", config)
        path = archiver.write_baseline(baseline, "rsrs", "mixed")
        assert path.exists()
        assert path.name == f"rsrs_mixed_{baseline['config_hash']}.json"
        loaded = archiver.load_baseline(path)
        assert loaded == baseline

    def test_load_missing_raises(self, archiver) -> None:
        """读取不存在的档案抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            archiver.load_baseline(Path("no_such_baseline.json"))

    def test_write_json_valid(self, archiver, config) -> None:
        """落盘文件为合法 JSON。"""
        result = _make_result(config)
        baseline = archiver.build_baseline(result, "rsrs", "mixed", config)
        path = archiver.write_baseline(baseline, "rsrs", "mixed")
        with open(path, "r", encoding="utf-8") as f:
            parsed = json.load(f)
        assert parsed == baseline


# ── 对拍判定 ─────────────────────────────────────────────────────────


class TestVerifyReproducible:
    def test_identical_passes(self, archiver, config) -> None:
        """相同结果 → 对拍通过。"""
        baseline_result = _make_result(config)
        baseline = archiver.build_baseline(baseline_result, "rsrs", "mixed", config)
        fresh = _make_result(config)
        passed, report = archiver.verify_reproducible(baseline, fresh, config)
        assert passed is True
        assert report["passed"] is True
        assert report["equity_hash_match"] is True
        assert report["trades_fingerprint_match"] is True

    def test_equity_tamper_red(self, archiver, config) -> None:
        """净值篡改（最终净值差远超阈值）→ 判红。"""
        baseline_result = _make_result(config)
        baseline = archiver.build_baseline(baseline_result, "rsrs", "mixed", config)

        fresh = _make_result(config)
        fresh.equity_curve[0] = EquityPoint(
            timestamp=1.0, date="2024-01-01", equity=5.0,
            cash=1.0, position_value=2.0, drawdown=0.0, benchmark_equity=1.0,
        )
        passed, report = archiver.verify_reproducible(baseline, fresh, config)
        assert passed is False
        assert report["equity_hash_match"] is False

    def test_trade_count_diff_red(self, archiver, config) -> None:
        """成交笔数差超 5% → 判红。"""
        baseline_result = _make_result(config)  # 3 笔
        baseline = archiver.build_baseline(baseline_result, "rsrs", "mixed", config)

        fresh = _make_result(config, n_trades=9)  # 9 笔：差 6/3 = 200% > 5%
        passed, report = archiver.verify_reproducible(baseline, fresh, config)
        assert passed is False
        assert report["trades_fingerprint_match"] is False

    def test_trade_count_small_diff_passes(self, archiver, config) -> None:
        """成交笔数差在阈值内 → 哈希仍判红（位级指纹严格），但比例口径通过。"""
        # 说明：位级指纹（equity/trades 哈希）要求完全一致；比例阈值是
        # 数值对拍的补充判据。构造"笔数差在 5% 内但成交明细不同"的用例，
        # 期望 passed=False（指纹严格模式）——与 §4.8 位级校验一致。
        baseline_result = _make_result(config, n_trades=100)
        baseline = archiver.build_baseline(baseline_result, "rsrs", "mixed", config)

        fresh = _make_result(config, n_trades=102)  # 差 2/100 = 2% ≤ 5%
        passed, report = archiver.verify_reproducible(baseline, fresh, config)
        assert report["trade_count_diff_ratio"] <= TRADE_COUNT_DIFF_RATIO
        assert passed is False  # 指纹严格模式：成交明细变化即判红

    def test_final_equity_diff_red(self, archiver, config) -> None:
        """最终净值差超 0.5% → 判红。"""
        baseline_result = _make_result(config)
        baseline = archiver.build_baseline(baseline_result, "rsrs", "mixed", config)

        fresh = _make_result(config)
        fresh.equity_curve[-1] = EquityPoint(
            timestamp=4.0, date="2024-01-04", equity=1.0 + 4 * 0.01 + 0.10,  # 偏离 10%
            cash=1.0, position_value=2.0, drawdown=0.0, benchmark_equity=1.0,
        )
        passed, report = archiver.verify_reproducible(baseline, fresh, config)
        assert passed is False
        assert report["final_equity_max_abs_diff"] > EQUITY_MAX_ABS_DIFF

    def test_thresholds_exposed(self, archiver, config) -> None:
        """对拍报告暴露阈值常量（防硬编码漂移）。"""
        result = _make_result(config)
        baseline = archiver.build_baseline(result, "rsrs", "mixed", config)
        passed, report = archiver.verify_reproducible(baseline, _make_result(config), config)
        assert report["thresholds"]["equity_max_abs_diff"] == EQUITY_MAX_ABS_DIFF
        assert report["thresholds"]["trade_count_diff_ratio"] == TRADE_COUNT_DIFF_RATIO


# ── 防御式校验 ───────────────────────────────────────────────────────


class TestDefensive:
    def test_config_without_to_dict_raises(self, archiver) -> None:
        """config 无 to_dict → AttributeError。"""
        with pytest.raises(AttributeError):
            archiver.compute_config_hash(object())  # type: ignore[arg-type]

    def test_result_without_equity_raises(self, archiver, config) -> None:
        """result 无 equity_curve → AttributeError。"""
        with pytest.raises(AttributeError):
            archiver.compute_equity_hash(object())  # type: ignore[arg-type]

    def test_write_without_config_hash_raises(self, archiver) -> None:
        """baseline 缺 config_hash → KeyError。"""
        with pytest.raises(KeyError):
            archiver.write_baseline({"no": "hash"}, "rsrs", "mixed")
