# 文件名：tests/leakage_gate/test_sanity.py
"""
test_sanity.py - 模型层统计门禁反向测试（阶段 2，轨道 E，M2-01~06）
==================================================================
  - M2-01 shuffle-label：打乱标签基线分布有阈值；真实指标显著 > 阈值 → 告警。
  - M2-02 训练侧时间校验：特征时间 ≤ train_end_time 且 > train_start_time；越界 → 拦截。
  - M2-03 特征-标签时间对齐：特征 available_time ≤ 标签 decision_time；不通过 → 拦截。
  - M2-04 特征白名单扩充过 R11：把 severity=阻断 列（每股收益）加进 BAR_CN_COLUMN_MAP
         → 出口报红；当前真实白名单干净。
  - M2-05/06 种子 sidecar：种子记录在案、可复现；种子不一致 → 阻断
        （monte_carlo / param_optimizer 接线验证）。

反向测试原则：注入泄漏/越界/阻断列，断言校验器确实命中并阻断；当前代码库基线干净。
"""

from __future__ import annotations

import numpy as np
import pytest

import myquant.leakage_gate.sanity as sy

# 台账列集合（与 config/data_time_semantics.yaml 口径一致）
_LEDGER_KEYS = {
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "前收盘价", "每股收益", "available_time",
}
_BLOCKED = {"每股收益"}


def _ledger():
    """构造含 每股收益=阻断 的测试台账。"""
    return {
        "columns": {
            c: {"日基": "T", "severity": "提示"} for c in _LEDGER_KEYS
        },
    }


def _corr_scorer(X, y):
    """shuffle-label 用回调：返回特征列与标签的相关系数。"""
    yf = np.asarray(y, dtype=float)
    if yf.std() == 0 or X[:, 0].std() == 0:
        return 0.0
    return float(np.corrcoef(X[:, 0], yf)[0, 1])


def _iso(epoch: float) -> str:
    """epoch 秒 → ISO 字符串（模拟 training_pipeline._ts_to_str 口径）。"""
    import datetime as _dt
    return _dt.datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════
#  M2-01 shuffle-label 探测（基线 + 阈值）
# ═══════════════════════════════════════════════

def test_shuffle_baseline_establishes_distribution_and_threshold():
    """打乱标签基线：分布有 mean/std/p95/p99，且阈值 = mean + k*std。"""
    rng = np.random.default_rng(42)
    X = rng.normal(size=(120, 3))
    y = rng.normal(size=120)  # 特征与标签独立 → 打乱后近零相关（噪声基线）
    b = sy.run_shuffle_label_baseline(_corr_scorer, X, y, n_perm=30, seed=42)
    assert b.n_perm == 30
    assert abs(b.mean) < 0.3  # 噪声基线均值≈0
    assert b.p95 <= b.p99  # 95 分位 ≤ 99 分位
    assert b.threshold == pytest.approx(b.mean + sy._SHUFFLE_K_STD * b.std)


def test_check_shuffle_label_alerts_on_suspiciously_high():
    """真实指标显著高于打乱基线噪声上界 → 告警。"""
    rng = np.random.default_rng(7)
    X = rng.normal(size=(120, 3))
    y = rng.normal(size=120)
    b = sy.run_shuffle_label_baseline(_corr_scorer, X, y, n_perm=25, seed=1)
    alerts = sy.check_shuffle_label(b, real_metric=0.95)  # 远高于噪声上界
    assert len(alerts) >= 1
    assert "shuffle-label" in alerts[0]


def test_check_shuffle_label_clean_within_noise():
    """真实指标落在噪声范围内 → 无告警（放行）。"""
    rng = np.random.default_rng(3)
    X = rng.normal(size=(120, 3))
    y = rng.normal(size=120)
    b = sy.run_shuffle_label_baseline(_corr_scorer, X, y, n_perm=25, seed=9)
    # 真实指标取基线均值 → 在噪声带内，放行
    assert sy.check_shuffle_label(b, real_metric=b.mean) == []


def test_shuffle_baseline_rejects_small_perm():
    """n_perm < 5 → 拒绝（基线不稳定）。"""
    rng = np.random.default_rng(1)
    X = rng.normal(size=(50, 2))
    y = rng.normal(size=50)
    with pytest.raises(ValueError):
        sy.run_shuffle_label_baseline(_corr_scorer, X, y, n_perm=2)


# ═══════════════════════════════════════════════
#  M2-02 训练侧时间校验（train_end_time 边界）
# ═══════════════════════════════════════════════

def test_train_temporal_clean_within_bounds():
    """特征时间 ≤ train_end_time 且 > train_start_time → 无违规。"""
    ts = ["2026-10-08", "2026-10-09", "2026-10-10"]
    assert sy.validate_train_temporal(ts, "2026-10-01", "2026-10-10") == []


def test_train_temporal_future_after_end_flags():
    """特征最近时间晚于 train_end_time → 未来泄漏，拦截。"""
    ts = ["2026-10-10", "2026-10-11"]
    bad = sy.validate_train_temporal(ts, "2026-10-01", "2026-10-10")
    assert len(bad) == 1
    assert "晚于" in bad[0]


def test_train_temporal_degenerate_window_flags():
    """特征最近时间未严格晚于 train_start_time（窗口退化）→ 拦截。"""
    ts = ["2026-10-01", "2026-10-01"]
    bad = sy.validate_train_temporal(ts, "2026-10-01", "2026-10-10")
    assert len(bad) == 1
    assert "未严格晚于" in bad[0]


def test_train_temporal_epoch_seconds_scale_clean():
    """epoch 秒口径（与 _ts_to_str 一致）：真实训练样本时间通过。"""
    start = 1711065000.0  # 2024-03-22 前后
    end = 1723161000.0    # 2024-08-09 前后
    ts = [start + 100.0 * i for i in range(5)] + [end]
    assert sy.validate_train_temporal(ts, _iso(start), _iso(end)) == []


def test_train_temporal_end_not_after_start_flags():
    """train_end_time 未严格晚于 train_start_time → 拦截。"""
    ts = ["2026-10-05"]
    bad = sy.validate_train_temporal(ts, "2026-10-10", "2026-10-10")
    assert len(bad) == 1


# ═══════════════════════════════════════════════
#  M2-03 特征-标签时间对齐校验
# ═══════════════════════════════════════════════

def test_alignment_clean_when_feature_le_label():
    """特征 available_time ≤ 标签 decision_time → 无违规。"""
    ft = ["2026-10-09", "2026-10-09", "2026-10-10"]
    lt = ["2026-10-09", "2026-10-10", "2026-10-10"]
    assert sy.validate_feature_label_alignment(ft, lt) == []


def test_alignment_feature_after_label_flags():
    """特征可用晚于标签决策（用了未来数据）→ 拦截。"""
    ft = ["2026-10-10", "2026-10-11"]
    lt = ["2026-10-09", "2026-10-11"]
    bad = sy.validate_feature_label_alignment(ft, lt)
    assert len(bad) == 1
    assert "晚于" in bad[0]


def test_alignment_length_mismatch_flags():
    """特征/标签样本数不一致 → 无法对齐，拦截。"""
    bad = sy.validate_feature_label_alignment(["2026-10-10"], ["2026-10-10", "2026-10-11"])
    assert len(bad) == 1
    assert "不一致" in bad[0]


# ═══════════════════════════════════════════════
#  M2-04 特征白名单扩充过 R11
# ═══════════════════════════════════════════════

def test_feature_whitelist_on_columns_blocks_eps(monkeypatch):
    """把 severity=阻断 列（每股收益）加入特征列 → R11 报红。"""
    ledger = _ledger()
    ledger["columns"]["每股收益"]["severity"] = "阻断"
    monkeypatch.setattr(sy, "_load_ledger", lambda: ledger)
    vs = sy.check_feature_whitelist_on_columns(["开盘价", "收盘价", "每股收益"])
    assert len(vs) == 1
    assert vs[0].rule_id == "R11"
    assert vs[0].severity == "阻断"


def test_feature_whitelist_on_columns_clean():
    """当前特征列（不含阻断列）→ 无违规。"""
    vs = sy.check_feature_whitelist_on_columns(["开盘价", "收盘价", "成交量"])
    assert vs == []


def test_check_feature_whitelist_gate_blocks_injected_eps(monkeypatch, tmp_path):
    """注入含 每股收益 的 BAR_CN_COLUMN_MAP → 门禁出口报红。"""
    fake = tmp_path / "features.py"
    fake.write_text(
        "BAR_CN_COLUMN_MAP = {\n"
        "    'open': '开盘价', 'close': '收盘价', 'eps': '每股收益',\n"
        "}\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(sy, "_FEATURES_PATH", fake)
    ledger = _ledger()
    ledger["columns"]["每股收益"]["severity"] = "阻断"
    monkeypatch.setattr(sy, "_load_ledger", lambda: ledger)
    vs = sy.check_feature_whitelist("fake_strategy", "2026-10-10")
    assert len(vs) == 1
    assert vs[0].rule_id == "R11"
    assert vs[0].severity == "阻断"
    assert "每股收益" in vs[0].message


def test_check_feature_whitelist_gate_clean_on_current_codebase():
    """当前 features.py BAR_CN_COLUMN_MAP（无阻断列）→ 门禁通过。"""
    vs = sy.check_feature_whitelist("fake_strategy", "2026-10-10")
    assert vs == []


def test_read_feature_whitelist_extracts_values():
    """AST 提取 BAR_CN_COLUMN_MAP 中文列值。"""
    src = (
        "BAR_CN_COLUMN_MAP = {\n"
        "    'open': '开盘价', 'high': '最高价',\n"
        "}\n"
    )
    assert sy._read_feature_whitelist(src) == ["开盘价", "最高价"]


# ═══════════════════════════════════════════════
#  M2-05/06 种子 sidecar（可复现 + 种子不一致阻断）
# ═══════════════════════════════════════════════

def test_seed_sidecar_record_verify(tmp_path):
    """首次记录种子；同种子 verify 通过；不同种子 verify 失败。"""
    p = tmp_path / "seed_sidecar.json"
    sc = sy.SeedSidecar(str(p))
    sc.check_or_record("monte_carlo", 42)
    assert sc.recorded_seed("monte_carlo") == 42
    assert sc.verify("monte_carlo", 42) is True
    assert sc.verify("monte_carlo", 7) is False


def test_seed_sidecar_mismatch_blocks(tmp_path):
    """种子不一致 → 阻断（SeedSidecarError）。"""
    p = tmp_path / "seed_sidecar.json"
    sc = sy.SeedSidecar(str(p))
    sc.check_or_record("param_optimizer", 42)
    with pytest.raises(sy.SeedSidecarError):
        sc.check_or_record("param_optimizer", 7)


def test_seed_sidecar_none_skips(tmp_path):
    """seed=None（真随机）→ 不校验不记录，不报错。"""
    p = tmp_path / "seed_sidecar.json"
    sc = sy.SeedSidecar(str(p))
    sc.check_or_record("monte_carlo", None)
    assert not p.exists()


def test_monte_carlo_analyzer_seed_sidecar(tmp_path):
    """monte_carlo 接线：同种子通过并留痕；不同种子 → 阻断。"""
    from myquant.backtest.monte_carlo import MonteCarloAnalyzer
    from myquant.models.backtest import EquityPoint

    curve = [
        EquityPoint(timestamp=1.0, date="2026-10-08", equity=1.0),
        EquityPoint(timestamp=2.0, date="2026-10-09", equity=1.01),
        EquityPoint(timestamp=3.0, date="2026-10-10", equity=0.99),
    ]
    p = tmp_path / "mc_sidecar.json"
    a1 = MonteCarloAnalyzer(n_simulations=200, seed=42, seed_sidecar_path=str(p))
    a1.analyze(curve)
    assert sy.SeedSidecar(str(p)).recorded_seed("monte_carlo") == 42
    # 同种子再跑：一致，不阻断
    MonteCarloAnalyzer(n_simulations=200, seed=42, seed_sidecar_path=str(p)).analyze(curve)
    # 不同种子：阻断
    a2 = MonteCarloAnalyzer(n_simulations=200, seed=7, seed_sidecar_path=str(p))
    with pytest.raises(sy.SeedSidecarError):
        a2.analyze(curve)


def test_param_optimizer_make_demo_data_seed_sidecar(tmp_path):
    """param_optimizer 接线：make_demo_data 同种子留痕；不同种子 → 阻断。"""
    from myquant.backtest.param_optimizer import make_demo_data

    p = tmp_path / "po_sidecar.json"
    make_demo_data(n_bars=30, seed=42, seed_sidecar_path=str(p))
    assert sy.SeedSidecar(str(p)).recorded_seed("param_optimizer") == 42
    make_demo_data(n_bars=30, seed=42, seed_sidecar_path=str(p))  # 一致通过
    with pytest.raises(sy.SeedSidecarError):
        make_demo_data(n_bars=30, seed=9, seed_sidecar_path=str(p))
