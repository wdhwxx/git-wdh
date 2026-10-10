# 文件名：tests/leakage_gate/test_static_scan.py
"""
test_static_scan.py - 静态 AST 扫描器反向测试（阶段 1-B，M1B-06）
=================================================================
每条阻断级规则注入命中源 → 必报红（阻断档）；R01b 放行登记；R03/R07 警告档；
列引用清单与 consumption 判定一致；内容哈希缓存命中不重扫；
横截面 as-of 校验（M1B-04）与因子截断重算自检（M1B-05）反向验证。

反向测试原则（v3.0 §10 / M1B-06 验收）：注入带泄漏的源码/面板，
断言扫描器/校验器确实命中并阻断；真实代码库基线（7 策略 + myquant/scripts）
0 阻断在 test_gate_governance/test_ledger_truth_source 中另有覆盖。
"""

from __future__ import annotations

import pytest

import myquant.leakage_gate.static_scan as ss
import myquant.leakage_gate.consumption as consumption_mod
import myquant.leakage_gate.cross_sectional as cs

# 台账列集合（测试用精简副本，与 config/data_time_semantics.yaml 口径一致）
_LEDGER_KEYS = {
    "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额",
    "前收盘价", "每股收益", "available_time",
}
_BLOCKED = {"每股收益"}


def _hits(source: str, keys=_LEDGER_KEYS, blocked=_BLOCKED, filename: str = "x.py"):
    """便捷：对源码执行全规则扫描，返回 ScanResult。"""
    return ss.analyze_source(source, keys, blocked, filename=filename)


def _rule_ids(result) -> set:
    return {h.rule_id for h in result.hits}


# ═══════════════════════════════════════════════
#  阻断级规则反向测试（注入命中 → 必报红）
# ═══════════════════════════════════════════════

def test_r01a_shift_negative_in_feature_blocks():
    """R01a：特征侧 .shift(-1) → 阻断（RL-01）。"""
    src = (
        "def compute_features(df):\n"
        "    df['fut_close'] = df['收盘价'].shift(-1)\n"
        "    return df\n"
    )
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R01a"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r01b_shift_negative_in_label_context_registered_not_blocked():
    """R01b：标签/前瞻收益侧 .shift(-1) → 放行登记，不阻断（§10.3）。"""
    src = (
        "def generate_labels(df):\n"
        "    df['fwd_ret'] = df['收盘价'].shift(-1) / df['收盘价'] - 1\n"
        "    return df\n"
    )
    r = _hits(src)
    assert "R01a" not in _rule_ids(r)
    assert len(r.r01b_registered) == 1
    assert r.r01b_registered[0].rule_id == "R01b"


def test_r02_rolling_center_blocks():
    """R02：rolling(window, center=True) → 阻断（RL-06）。"""
    src = (
        "def compute_features(df):\n"
        "    df['mid'] = df['收盘价'].rolling(5, center=True).mean()\n"
        "    return df\n"
    )
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R02"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r04_train_test_split_blocks():
    """R04：train_test_split → 阻断（RL-04）。"""
    src = "from sklearn.model_selection import train_test_split\nX_tr, X_te = train_test_split(X, test_size=0.2)\n"
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R04"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r04_kfold_shuffle_blocks():
    """R04：KFold(shuffle=True) → 阻断（RL-04）。"""
    src = "kf = KFold(n_splits=5, shuffle=True)\n"
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R04"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r05_standardscaler_full_blocks():
    """R05：StandardScaler().fit_transform(全量) → 阻断（RL-05）。"""
    src = (
        "from sklearn.preprocessing import StandardScaler\n"
        "z = StandardScaler().fit_transform(df)\n"
    )
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R05"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r09_report_date_pit_match_blocks():
    """R09：财务 PIT join 用 report_date 做时点匹配 → 阻断（RL-02）。"""
    src = (
        "sql = \"SELECT eps FROM financial_data WHERE symbol=? ORDER BY report_date\"\n"
        "if fd <= trade_date:\n"
        "    pass\n"
    )
    r = _hits(src, filename="data_export.py")
    hits = [h for h in r.hits if h.rule_id == "R09"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r09_correct_module_whitelist():
    """R09：正确 PIT 实现（白名单模块）含 report_date 不误报。"""
    src = (
        "sql = \"SELECT report_date, ann_date, eps FROM financial_data ORDER BY report_date\"\n"
        "if fd <= trade_date:\n"
        "    pass\n"
    )
    r = _hits(src, filename="financial_service.py")
    assert "R09" not in _rule_ids(r)


def test_r10_unregistered_insert_column_blocks():
    """R10：数据导出 .insert() 写入未登记派生列 → 阻断（RL-11）。"""
    src = (
        "df.insert(int(df.columns.get_loc('收盘价')) + 1, '未登记派生列', vals)\n"
    )
    r = _hits(src, filename="data_export.py")
    hits = [h for h in r.hits if h.rule_id == "R10"]
    assert len(hits) == 1
    assert hits[0].severity == "阻断"


def test_r10_registered_insert_column_ok():
    """R10：.insert() 写入已登记列（前收盘价）→ 不命中。"""
    src = "df.insert(int(df.columns.get_loc('收盘价')) + 1, '前收盘价', _pre_close)\n"
    r = _hits(src, filename="data_export.py")
    assert "R10" not in _rule_ids(r)


# ═══════════════════════════════════════════════
#  警告档规则（G-04：R03 先警告；R07 横截面告警）
# ═══════════════════════════════════════════════

def test_r03_full_sample_fit_warning_not_blocking():
    """R03：跨时间全样本拟合 → 警告档（G-04，不阻断出口）。"""
    src = (
        "def train_feature(df_panel):\n"
        "    full = load_all()\n"
        "    mu = full.mean()\n"
        "    sd = full.std()\n"
        "    return df_panel\n"
    )
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R03"]
    assert len(hits) >= 1
    assert all(h.severity == "警告" for h in hits)
    # 警告档不进入阻断
    assert all(h.severity != "阻断" for h in hits)


def test_r03_cross_section_whitelist_no_warning():
    """R03：截面/滚动对照实现（白名单函数）不误报。"""
    src = (
        "def _zscore_cross_sectional(raw):\n"
        "    z = (raw - raw.mean()) / raw.std()\n"
        "    return z\n"
    )
    r = _hits(src)
    assert "R03" not in _rule_ids(r)


def test_r07_cross_section_misuse_warning():
    """R07：横截面排名/分位输入未按决策日切齐 → 警告。"""
    src = (
        "def build_candidates():\n"
        "    panel = load_full_panel()\n"
        "    ranked = panel.rank(axis=1)\n"
        "    return ranked\n"
    )
    r = _hits(src)
    hits = [h for h in r.hits if h.rule_id == "R07"]
    assert len(hits) == 1
    assert hits[0].severity == "警告"


# ═══════════════════════════════════════════════
#  列引用清单：与 consumption 判定一致（单实现）
# ═══════════════════════════════════════════════

def test_referenced_columns_and_consumption_consistency():
    """static_scan 列引用与 consumption.extract_columns_from_source 判定一致。"""
    src = (
        "def algo(df):\n"
        "    a = df['收盘价'].shift(1)\n"
        "    b = df.开盘价\n"
        "    c = df[['成交量','成交额']]\n"
        "    return a + b + c['每股收益']\n"
    )
    ss_cols = ss.extract_referenced_columns(src, _LEDGER_KEYS)
    cs_cols = consumption_mod.extract_columns_from_source(src, _LEDGER_KEYS)
    assert ss_cols == cs_cols
    assert ss_cols == {"收盘价", "开盘价", "成交量", "成交额", "每股收益"}
    # 扫描结果内的 referenced_columns 同样一致
    r = _hits(src)
    assert r.referenced_columns == ss_cols


# ═══════════════════════════════════════════════
#  内容哈希缓存 + 只扫变更文件
# ═══════════════════════════════════════════════

def test_content_hash_cache_reuses_result(tmp_path):
    """内容未变 → 缓存命中不重扫（同一对象）；内容变更 → 重扫。"""
    f = tmp_path / "a.py"
    f.write_text("def compute_features(df):\n    df['fut']=df['收盘价'].shift(-1)\n    return df\n", encoding="utf-8")
    ss.clear_cache()
    r1 = ss.scan_file(f, _LEDGER_KEYS, _BLOCKED, use_cache=True)
    r2 = ss.scan_file(f, _LEDGER_KEYS, _BLOCKED, use_cache=True)
    assert r1 is r2  # 缓存命中，同一对象
    # 内容变更 → 重扫且命中 R01a
    f.write_text("def compute_features(df):\n    return df\n", encoding="utf-8")
    r3 = ss.scan_file(f, _LEDGER_KEYS, _BLOCKED, use_cache=True)
    assert r3 is not r1
    assert "R01a" not in _rule_ids(r3)


def test_scan_paths_only_py(tmp_path):
    """scan_paths 只扫传入的 .py 变更文件，忽略非 py。"""
    (tmp_path / "b.py").write_text("def compute_features(df):\n    df['x']=df['收盘价'].shift(-1)\n    return df\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("shift(-1)", encoding="utf-8")
    ss.clear_cache()
    results = ss.scan_paths([tmp_path / "b.py", tmp_path / "note.txt"], _LEDGER_KEYS, _BLOCKED)
    assert len(results) == 1
    assert "R01a" in _rule_ids(results[0])


# ═══════════════════════════════════════════════
#  门禁检查入口（check_static_scan / check_cross_sectional）
# ═══════════════════════════════════════════════

def test_check_static_scan_blocks_injected_leak(monkeypatch):
    """check_static_scan：注入特征侧 shift(-1) → 返回阻断违规。"""
    src = "def compute_features(df):\n    df['fut']=df['收盘价'].shift(-1)\n    return df\n"
    monkeypatch.setattr(ss, "_strategy_source", lambda sid: src)
    ledger = {"columns": {c: {"日基": "T", "severity": "提示"} for c in _LEDGER_KEYS}}
    ledger["columns"]["每股收益"]["severity"] = "阻断"
    monkeypatch.setattr(ss, "_load_ledger", lambda: ledger)
    vs = ss.check_static_scan("fake_strategy", "2026-10-10")
    assert len(vs) == 1
    assert vs[0].rule_id == "R01a"
    assert vs[0].severity == "阻断"
    assert vs[0].gate == "static_scan"


def test_check_static_scan_clean_source_no_blocking(monkeypatch):
    """check_static_scan：干净源码 → 无阻断违规。"""
    src = "def compute_features(df):\n    df['ma']=df['收盘价'].rolling(5).mean()\n    return df\n"
    monkeypatch.setattr(ss, "_strategy_source", lambda sid: src)
    ledger = {"columns": {c: {"日基": "T", "severity": "提示"} for c in _LEDGER_KEYS}}
    monkeypatch.setattr(ss, "_load_ledger", lambda: ledger)
    vs = ss.check_static_scan("fake_strategy", "2026-10-10")
    assert vs == []


def test_check_cross_sectional_warns_on_misuse(monkeypatch):
    """check_cross_sectional：注入横截面全样本 rank → R07 警告。"""
    src = "def build():\n    panel = load_full_panel()\n    return panel.rank(axis=1)\n"
    monkeypatch.setattr(cs, "_strategy_source", lambda sid: src)
    vs = cs.check_cross_sectional("fake_strategy", "2026-10-10")
    assert len(vs) == 1
    assert vs[0].rule_id == "R07"
    assert vs[0].severity == "警告"


def test_check_cross_sectional_clean_no_warning(monkeypatch):
    """check_cross_sectional：干净源码 → 无警告。"""
    src = "def build():\n    today = df[df['日期'] <= decision_time]\n    return today.rank(axis=1)\n"
    monkeypatch.setattr(cs, "_strategy_source", lambda sid: src)
    vs = cs.check_cross_sectional("fake_strategy", "2026-10-10")
    assert vs == []


# ═══════════════════════════════════════════════
#  横截面 as-of 校验（M1B-04）
# ═══════════════════════════════════════════════

def test_validate_asof_flags_future_data():
    """validate_asof：面板含决策日之后数据 → 报未来数据。"""
    import pandas as pd
    data = pd.DataFrame({"日期": ["2026-10-10", "2026-10-12"]})
    bad = cs.validate_asof(data, "2026-10-10")
    assert len(bad) >= 1
    assert "之后" in bad[0] or "晚于" in bad[0]


def test_validate_asof_clean():
    """validate_asof：面板全部 <= 决策日 → 无未来数据。"""
    import pandas as pd
    data = pd.DataFrame({"日期": ["2026-10-08", "2026-10-09", "2026-10-10"]})
    assert cs.validate_asof(data, "2026-10-10") == []


def test_validate_cross_sectional_panel_flags_future():
    """validate_cross_sectional_panel：截面面板含未来截面日 → 报违规（RL-07）。"""
    import pandas as pd
    panel = pd.DataFrame({
        "symbol": ["a", "b", "a"],
        "date": ["2026-10-09", "2026-10-09", "2026-10-12"],
        "z": [1.0, 2.0, 3.0],
    })
    bad = cs.validate_cross_sectional_panel(panel, "2026-10-10", date_col="date")
    assert len(bad) >= 1


def test_validate_cross_sectional_panel_clean():
    """validate_cross_sectional_panel：当日截面只用当日数据 → 无违规。"""
    import pandas as pd
    panel = pd.DataFrame({
        "symbol": ["a", "b", "c"],
        "date": ["2026-10-10", "2026-10-10", "2026-10-10"],
        "z": [1.0, 2.0, 3.0],
    })
    assert cs.validate_cross_sectional_panel(panel, "2026-10-10", date_col="date") == []


# ═══════════════════════════════════════════════
#  因子截断重算自检（M1B-05）
# ═══════════════════════════════════════════════

@pytest.fixture
def _ohlcv():
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=100, freq="D")
    rng = np.random.default_rng(7)
    close = rng.normal(10, 1, 100).cumsum()
    return pd.DataFrame(
        {"开盘价": close, "最高价": close + 0.3, "最低价": close - 0.3, "收盘价": close},
        index=idx,
    )


def test_factor_selfcheck_trailing_passes(_ohlcv):
    """截断重算自检：正常 trailing 指标（sma）通过。"""
    from myquant.indicators.factor_engine import FactorEngine

    eng = FactorEngine(use_cache=False)
    res = eng.calculate(
        _ohlcv, "600519.SH",
        [{"name": "sma", "params": {"period": 5}, "prefix": "sma_", "enabled": True}],
    )
    assert len(res) == 100


def test_factor_selfcheck_lookahead_blocks(_ohlcv):
    """截断重算自检：泄漏指标（shift(-1)）→ FactorLookaheadError 阻断。"""
    from myquant.indicators import register_indicator, IndicatorBase
    from myquant.indicators.factor_engine import FactorEngine, FactorLookaheadError

    @register_indicator
    class _LookaheadSMA(IndicatorBase):
        name = "test_lookahead_sma"

        def __init__(self, period=5):
            self.period = period

        def calculate(self, df):
            fut = df["收盘价"].shift(-1)  # 泄漏：把下一根收盘价前移当输入
            return fut.rolling(self.period).mean().to_frame("收盘价")

    try:
        eng = FactorEngine(use_cache=False)
        with pytest.raises(FactorLookaheadError):
            eng.calculate(
                _ohlcv, "600519.SH",
                [{"name": "test_lookahead_sma", "params": {"period": 5},
                  "prefix": "lk_", "enabled": True}],
                raise_on_error=True,
            )
    finally:
        # 清理全局指标注册，避免污染其他测试
        from myquant.indicators import _indicator_registry
        _indicator_registry.pop("test_lookahead_sma", None)


def test_factor_selfcheck_short_data_skips(_ohlcv):
    """截断重算自检：数据过短（<30）跳过，不误伤短序列合法因子。"""
    from myquant.indicators.factor_engine import FactorEngine

    short = _ohlcv.iloc[:20]
    eng = FactorEngine(use_cache=False)
    res = eng.calculate(
        short, "600519.SH",
        [{"name": "sma", "params": {"period": 5}, "prefix": "sma_", "enabled": True}],
    )
    assert len(res) == 20
