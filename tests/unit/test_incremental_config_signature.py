# -*- coding: utf-8 -*-
"""T-35 KP-P5-09 增量回测缓存签名：_config_signature 10字段 + format_version 升版全量重算。"""
import tempfile
from pathlib import Path

from myquant.backtest.consistency_check import make_demo_data
from myquant.backtest.incremental_backtest import IncrementalBacktester
from myquant.core.constants import RESULTS_FORMAT_VERSION

SYM = "000001.SZ"
BASE_PARAMS = {"fast_period": 5, "slow_period": 20}


def _mk(tmp_path: Path, **kw):
    data = make_demo_data((SYM,), n_bars=60, seed=42)
    return IncrementalBacktester(
        "ma_cross", dict(BASE_PARAMS), data,
        str(tmp_path / "fp.json"), str(tmp_path / "res.json"), **kw,
    )


def _prime_cache(bt: IncrementalBacktester, config_sig=None, fmt=None):
    """在缓存中预置一个签名匹配/不匹配的条目，并同步指纹仓库。"""
    fps = bt.compute_fingerprints()
    bt._store.sync(fps)
    bt._results[SYM] = {
        "fingerprint_hash": fps[SYM].hash_value,
        "config_signature": config_sig if config_sig is not None else bt._config_signature(),
        "format_version": fmt if fmt is not None else RESULTS_FORMAT_VERSION,
        "status": "ok",
        "metrics": {"total_return": 0.1},
        "trade_count": 2,
        "equity_length": 60,
        "computed_at": "2026-10-04T00:00:00+00:00",
    }


def test_config_signature_sensitive_to_params():
    a, b = _mk(Path(tempfile.mkdtemp())), _mk(Path(tempfile.mkdtemp()))
    b.params = {"fast_period": 10, "slow_period": 20}
    assert a._config_signature() != b._config_signature()


def test_config_signature_stable_same_config():
    a, b = _mk(Path(tempfile.mkdtemp())), _mk(Path(tempfile.mkdtemp()))
    assert a._config_signature() == b._config_signature()


def test_config_signature_sensitive_to_commission():
    a = _mk(Path(tempfile.mkdtemp()))
    b = _mk(Path(tempfile.mkdtemp()), commission={"rate": 0.001})
    assert a._config_signature() != b._config_signature()


def test_config_signature_sensitive_to_initial_capital():
    a = _mk(Path(tempfile.mkdtemp()))
    b = _mk(Path(tempfile.mkdtemp()), initial_capital=500000.0)
    assert a._config_signature() != b._config_signature()


def test_cache_match_reuses(tmp_path):
    """缓存签名/指纹/格式均匹配 → 复用，不重算。"""
    bt = _mk(tmp_path)
    _prime_cache(bt)
    plan = bt.plan()
    assert plan.force_full is False
    assert SYM not in plan.changed


def test_config_change_forces_recompute(tmp_path):
    """配置签名不一致（如费用/参数变更）→ 强制重算。"""
    bt = _mk(tmp_path)
    _prime_cache(bt, config_sig="STALE_SIGNATURE")
    plan = bt.plan()
    assert SYM in plan.changed


def test_format_version_mismatch_forces_recompute(tmp_path):
    """RESULTS_FORMAT_VERSION 升版 → 缓存不匹配，全量重算一次。"""
    bt = _mk(tmp_path)
    _prime_cache(bt, fmt=0)
    plan = bt.plan()
    assert SYM in plan.changed


def test_run_writes_config_signature(tmp_path):
    """run 后缓存条目带 config_signature + format_version。"""
    bt = _mk(tmp_path)
    bt.run(force_full=True)
    cached = bt._results[SYM]
    assert cached.get("config_signature") == bt._config_signature()
    assert cached.get("format_version") == RESULTS_FORMAT_VERSION
