# -*- coding: utf-8 -*-
"""配置死键扫描器单测（P2-07）。

用临时 yaml fixture + 临时源码目录验证：
- 有消费点的键 → 非死键
- 独特无消费键 → 死键
- 白名单段（codes/params）整段跳过
- ok / new_dead_keys 属性语义
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from myquant.leakage_gate.config_deadkey import (
    DeadKeyReport,
    _collect_leaf_keys,
    scan_dead_keys,
)


@pytest.fixture()
def fixture_project(tmp_path: Path) -> Path:
    """构造临时项目：含一份 backtest_config.yaml + 一个有消费点的 .py 源码。

    结构:
        tmp/
          config/cfg.yaml
          src/consumer.py   # 消费了 "consumed_key" 和 "enabled"
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # consumer.py：源码里出现 consumed_key（有消费点）；dead_unique_key 故意不出现
    (src_dir / "consumer.py").write_text(
        textwrap.dedent(
            '''\
            def read_cfg(cfg):
                enabled = cfg.get("enabled")
                consumed = cfg["consumed_key"]
                return enabled, consumed
            '''
        ),
        encoding="utf-8",
    )

    # cfg.yaml：含一个有消费点键、一个死键、一个白名单段
    cfg = {
        "enabled": True,
        "consumed_key": 42,
        "dead_unique_key_xyz": "never-read",
        "strategy": {
            "name": "demo",
            "params": {"alpha": 0.5, "beta": 0.3},  # params 白名单段，跳过
        },
        "stock_pool": {
            "codes": ["000001.SZ"],  # codes 白名单段，跳过
        },
    }
    (cfg_dir / "cfg.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8"
    )
    return tmp_path


def test_collect_leaf_keys_skips_whitelist_segments(tmp_path: Path) -> None:
    """白名单段 params/codes 整段不收集子键。"""
    cfg = {
        "enabled": True,
        "strategy": {"params": {"alpha": 1}, "name": "s"},
        "stock_pool": {"codes": ["x"]},
    }
    whitelisted: list[str] = []
    keys = _collect_leaf_keys(cfg, whitelisted)
    # enabled / name 是叶子键；params.alpha / codes 不该被收
    assert "enabled" in keys
    assert "name" in keys
    assert "alpha" not in keys
    assert "codes" not in keys
    assert "params" in whitelisted or "codes" in whitelisted


def test_scan_reports_dead_unique_key(fixture_project: Path) -> None:
    """独特无消费键 dead_unique_key_xyz 应被报为死键。"""
    report = scan_dead_keys(
        config_path="config/cfg.yaml",
        source_root="src",
        project_root=str(fixture_project),
    )
    assert "dead_unique_key_xyz" in report.dead_keys
    # consumed_key / enabled 在 consumer.py 出现 → 非死键
    assert "consumed_key" not in report.dead_keys
    assert "enabled" not in report.dead_keys


def test_scan_ok_when_no_new_dead_keys(fixture_project: Path) -> None:
    """报告的 ok 属性：死键为空（或都在已知基线）时为 True。"""
    report = scan_dead_keys(
        config_path="config/cfg.yaml",
        source_root="src",
        project_root=str(fixture_project),
    )
    # fixture 里 dead_unique_key_xyz 不在 KNOWN_DEAD_KEYS 基线 → 新增死键 → ok=False
    assert report.ok is False
    assert "dead_unique_key_xyz" in report.new_dead_keys


def test_scan_missing_config_raises(tmp_path: Path) -> None:
    """配置文件不存在 → FileNotFoundError（fail-closed）。"""
    with pytest.raises(FileNotFoundError):
        scan_dead_keys(
            config_path="config/not_exist.yaml",
            source_root="src",
            project_root=str(tmp_path),
        )


def test_report_summary_runs() -> None:
    """summary() 可读字符串生成不报错。"""
    rpt = DeadKeyReport(config_path="x.yaml", total_leaf_keys=10, consumed_keys=9, dead_keys=["k"])
    assert "配置死键扫描" in rpt.summary()
