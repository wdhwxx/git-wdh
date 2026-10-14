# 文件名：tests/unit/test_st_blacklist_clean.py
"""全量清洗「剔除 ST/*ST/S*ST/SST 风险警示股」单测（#70）。

验证新增能力：
  - load_st_blacklist：名单文件读取（存在/缺失/损坏/空 → 空 dict 不阻断）
  - match_st_name：*ST / ST / S*ST / SST 名称前缀命中判定
  - filter_st_blacklist_symbols：按名单剔除（与 exclude_rules 正交）
  - refresh_st_blacklist：联网生成名单（fetcher 可注入），写 yaml
  - FullCleanService.resolve_symbols：ST 名单串联于代码前缀规则之后
  - build_report：报告含 exclude_st / st_excluded_count / st_excluded_symbols
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from myquant.data_access.full_clean_service import (
    FullCleanService,
    filter_st_blacklist_symbols,
    load_st_blacklist,
    match_st_name,
    refresh_st_blacklist,
)

# ── load_st_blacklist ──


def _write_blacklist(tmp_path: Path, content: str) -> Path:
    """写入名单文件并返回路径。"""
    path = tmp_path / "config" / "st_blacklist.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_load_名单正常读取(tmp_path: Path) -> None:
    """正常名单文件 → {symbol: name}。"""
    _write_blacklist(tmp_path, "000584.SZ: '*ST长动'\n002336.SZ: 'ST示例'\n")
    assert load_st_blacklist(tmp_path) == {
        "000584.SZ": "*ST长动",
        "002336.SZ": "ST示例",
    }


def test_load_文件缺失返回空(tmp_path: Path) -> None:
    """名单文件不存在 → 空 dict（不阻断清洗）。"""
    assert load_st_blacklist(tmp_path) == {}


def test_load_解析失败返回空(tmp_path: Path) -> None:
    """YAML 解析失败 → 空 dict（不阻断清洗）。"""
    _write_blacklist(tmp_path, ": : : bad yaml\n")
    assert load_st_blacklist(tmp_path) == {}


def test_load_空名单返回空(tmp_path: Path) -> None:
    """名单文件为空映射 → 空 dict。"""
    _write_blacklist(tmp_path, "{}\n")
    assert load_st_blacklist(tmp_path) == {}


def test_load_非字典内容返回空(tmp_path: Path) -> None:
    """名单内容非映射（如列表）→ 空 dict。"""
    _write_blacklist(tmp_path, "- 000584.SZ\n- 002336.SZ\n")
    assert load_st_blacklist(tmp_path) == {}


# ── match_st_name ──


@pytest.mark.parametrize("name", [
    "*ST长动", "ST示例", "S*ST示例", "SST示例", " *ST带空格 ", "ST",
])
def test_match_风险警示前缀命中(name: str) -> None:
    """*ST / ST / S*ST / SST 前缀 → True。"""
    assert match_st_name(name) is True


@pytest.mark.parametrize("name", [
    "贵州茅台", "平安银行", "中国平安", "工商银行", "万科A", "", None,
])
def test_match_普通名称不命中(name: str) -> None:
    """普通名称/空值 → False（真实非风险警示简称）。"""
    assert match_st_name(name) is False


# ── filter_st_blacklist_symbols ──


def test_filter_按名单剔除() -> None:
    """名单内 symbol 被剔除，其余保留。"""
    symbols = ["000001.SZ", "000584.SZ", "600519.SH", "002336.SZ"]
    blacklist = {"000584.SZ": "*ST长动", "002336.SZ": "ST示例"}
    keep, excluded = filter_st_blacklist_symbols(symbols, blacklist)
    assert keep == ["000001.SZ", "600519.SH"]
    assert excluded == ["000584.SZ", "002336.SZ"]


def test_filter_空名单全部保留() -> None:
    """空名单 → 全部保留、无剔除。"""
    symbols = ["000001.SZ", "600519.SH"]
    keep, excluded = filter_st_blacklist_symbols(symbols, {})
    assert keep == symbols
    assert excluded == []


def test_filter_名单外标的保留() -> None:
    """名单不存在于 symbols → 全部保留。"""
    symbols = ["000001.SZ", "600519.SH"]
    keep, excluded = filter_st_blacklist_symbols(symbols, {"601398.SH": "ST示例"})
    assert keep == symbols
    assert excluded == []


# ── refresh_st_blacklist ──


class _FakeFetcher:
    """伪 fetcher：get_stock_list 返回含名称的全市场列表。"""

    def get_stock_list(self, market: str = "A股") -> List[Dict[str, Any]]:
        return [
            {"symbol": "000001.SZ", "name": "平安银行"},
            {"symbol": "000584.SZ", "name": "*ST长动"},
            {"symbol": "002336.SZ", "name": "ST示例"},
            {"symbol": "600519.SH", "name": "贵州茅台"},
            {"symbol": "600705.SH", "name": "S*ST示例"},
            {"symbol": "601398.SH", "name": "SST示例"},
        ]


def test_refresh_生成名单文件(tmp_path: Path) -> None:
    """联网刷新：按前缀筛出 ST 标的并写入 yaml。"""
    blacklist = refresh_st_blacklist(tmp_path, fetcher=_FakeFetcher())
    assert blacklist == {
        "000584.SZ": "*ST长动",
        "002336.SZ": "ST示例",
        "600705.SH": "S*ST示例",
        "601398.SH": "SST示例",
    }
    # 落盘校验
    disk = yaml.safe_load((tmp_path / "config" / "st_blacklist.yaml").read_text(encoding="utf-8"))
    assert disk == blacklist


class _EmptyFetcher:
    """伪 fetcher：返回空列表。"""

    def get_stock_list(self, market: str = "A股") -> List[Dict[str, Any]]:
        return []


def test_refresh_无ST返回空名单(tmp_path: Path) -> None:
    """全市场无 ST 名称 → 写入空名单（不报错）。"""
    blacklist = refresh_st_blacklist(tmp_path, fetcher=_EmptyFetcher())
    assert blacklist == {}


# ── FullCleanService.resolve_symbols 串联 ──


class _FakeDataQueries:
    """伪 DataQueries：get_all_symbols 返回固定全量。"""

    def __init__(self, symbols: List[str]) -> None:
        self._symbols = symbols

    def get_all_symbols(self, period: str = "daily") -> List[str]:
        return list(self._symbols)


def _make_service(
    exclude_rules: Optional[List[str]] = None,
    exclude_st: bool = False,
    symbols_cfg: Optional[str] = None,
) -> FullCleanService:
    config: Dict[str, Any] = {
        "exclude_rules": exclude_rules or [],
        "exclude_st": exclude_st,
        "workers": 1,
    }
    if symbols_cfg is not None:
        config["symbols"] = symbols_cfg
    return FullCleanService(config=config, dry_run=True)


def test_resolve_前缀与ST串联(monkeypatch, tmp_path: Path) -> None:
    """ST 名单过滤串联于代码前缀规则之后：前缀剔除指数，ST 剔除风险警示。"""
    all_symbols = [
        "000001.SZ",   # 普通深市主板
        "000300.SH",   # 指数（被 000.SH 前缀规则剔除）
        "000584.SZ",   # *ST（被 ST 名单剔除）
        "002336.SZ",   # ST（被 ST 名单剔除）
        "600519.SH",   # 普通沪市主板（保留）
    ]
    service = _make_service(exclude_rules=["000.SH"], exclude_st=True)
    monkeypatch.setattr(
        "myquant.data_access.full_clean_service.load_st_blacklist",
        lambda *a, **k: {"000584.SZ": "*ST长动", "002336.SZ": "ST示例"},
    )
    service.st_blacklist = load_st_blacklist(tmp_path) or {"000584.SZ": "*ST长动", "002336.SZ": "ST示例"}
    keep, excluded = service.resolve_symbols(_FakeDataQueries(all_symbols))
    assert keep == ["000001.SZ", "600519.SH"]
    assert set(excluded) == {"000300.SH", "000584.SZ", "002336.SZ"}
    assert service.st_excluded == ["000584.SZ", "002336.SZ"]


def test_resolve_未启用ST仅前缀过滤(monkeypatch) -> None:
    """exclude_st=False → 行为与之前一致（仅前缀规则），st_excluded 为空。"""
    all_symbols = ["000001.SZ", "000300.SH", "600519.SH", "000584.SZ"]
    service = _make_service(exclude_rules=["000.SH"], exclude_st=False)
    keep, excluded = service.resolve_symbols(_FakeDataQueries(all_symbols))
    assert keep == ["000001.SZ", "600519.SH", "000584.SZ"]
    assert excluded == ["000300.SH"]
    assert service.st_excluded == []


def test_resolve_ST空名单不剔除(monkeypatch) -> None:
    """exclude_st=True 但名单为空 → 全部保留（不阻断）。"""
    all_symbols = ["000001.SZ", "600519.SH"]
    service = _make_service(exclude_rules=[], exclude_st=True)
    monkeypatch.setattr(
        "myquant.data_access.full_clean_service.load_st_blacklist",
        lambda *a, **k: {},
    )
    service.st_blacklist = {}
    keep, excluded = service.resolve_symbols(_FakeDataQueries(all_symbols))
    assert keep == all_symbols
    assert excluded == []
    assert service.st_excluded == []


def test_resolve_自定义symbols与ST串联(monkeypatch, tmp_path: Path) -> None:
    """指定 symbols 时 ST 名单仍生效（先过滤范围再剔除 ST）。"""
    all_symbols = ["000001.SZ", "000584.SZ", "600519.SH"]
    service = _make_service(
        exclude_rules=[], exclude_st=True, symbols_cfg="000584.SZ,600519.SH",
    )
    monkeypatch.setattr(
        "myquant.data_access.full_clean_service.load_st_blacklist",
        lambda *a, **k: {"000584.SZ": "*ST长动"},
    )
    service.st_blacklist = {"000584.SZ": "*ST长动"}
    keep, excluded = service.resolve_symbols(_FakeDataQueries(all_symbols))
    assert keep == ["600519.SH"]
    assert excluded == ["000584.SZ"]


# ── build_report ST 字段 ──


def test_report_含ST字段() -> None:
    """报告含 exclude_st / st_excluded_count / st_excluded_symbols。"""
    service = _make_service(exclude_st=True)
    service.st_excluded = ["000584.SZ", "002336.SZ"]
    report = service.build_report(
        counts={"ok": 1, "written": 1, "unchanged": 0, "error": 0, "empty": 0},
        stats_agg={"bad_rows": 0, "removed_duplicates": 0, "price_jumps": 0,
                   "volume_anomalies": 0, "time_violations": 0, "total_rows": 0},
        t0=0.0,
        backup_path=None,
    )
    assert report["exclude_st"] is True
    assert report["st_excluded_count"] == 2
    assert report["st_excluded_symbols"] == ["000584.SZ", "002336.SZ"]


def test_report_未启用ST字段默认() -> None:
    """exclude_st=False → 报告字段如实反映。"""
    service = _make_service(exclude_st=False)
    report = service.build_report(
        counts={"ok": 0, "written": 0, "unchanged": 0, "error": 0, "empty": 0},
        stats_agg={"bad_rows": 0, "removed_duplicates": 0, "price_jumps": 0,
                   "volume_anomalies": 0, "time_violations": 0, "total_rows": 0},
        t0=0.0,
        backup_path=None,
    )
    assert report["exclude_st"] is False
    assert report["st_excluded_count"] == 0
    assert report["st_excluded_symbols"] == []
