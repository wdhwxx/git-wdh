# -*- coding: utf-8 -*-
"""配置死键扫描器（P2-07）：一次防住整类"配置段写了但代码从不消费"的死键问题。

背景
----
第七节偏差里反复出现"配置段已加 / 消费点死键"：yaml 里写了某个配置键，
但 myquant 源码里没有任何读取点（.get / ["键"] / 构造器字段 / 字符串字面量），
结果是"配置白写、行为不变"。本模块把这件事做成可重复扫描的门禁：

    解析 backtest_config.yaml 的叶子键
        → 在 myquant 源码全仓查找该键名是否出现（作为字符串字面量）
        → 报告"配置有但代码零消费"的死键清单。

设计取舍（防误报）
------------------
- 只检测叶子键（末级带值的键名），不检测段路径。
- 白名单段：纯数据 / 透传段（codes 股票列表、策略 params、price_columns 列名映射），
  它们天然不被 .get 消费，而是整段透传给 dataclass / 策略，扫描会误报。
- 读取点判定宽松：只要键名字符串在 myquant/ 任一 .py 出现（字符串字面量），
  即视为有消费点——覆盖 `.get("键")`、`config["键"]`、`get_键()`、注释提及等。
  真正的死键是"源码里连这个字符串都搜不到"。
- WARNING 档：本扫描输出报告，不阻断回测（误报成本高）；把已知死键列入基线白名单，
  新增死键才报警。

★★★ 防未来函数 ★★★：本模块只读配置与源码，不参与回测数据流，无未来函数风险。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── 常量 ─────────────────────────────────────────────
# 默认配置文件（相对项目根）与源码根
DEFAULT_CONFIG_YAML: str = "config/backtest_config.yaml"
DEFAULT_SOURCE_ROOT: str = "myquant"

# 白名单段：整段跳过死键检测（纯数据 / 透传，非业务消费键）
# - codes：股票代码列表（数据，非配置行为）
# - params：策略参数字典（透传给策略构造器，键名即策略内部参数）
# - price_columns：中文列名映射（数据列定义，透传给引擎 data_col）
# - overrides / baselines：回测编排器专用段，由编排器按段透传消费
WHITELIST_SEGMENTS: frozenset[str] = frozenset({
    "codes",
    "params",
    "price_columns",
    "overrides",
    "baselines",
})

# 已知死键基线白名单（登记在此的死键不报警；新增死键会被报告）。
# 每条注明登记原因，避免"先扫描后遗忘"。
KNOWN_DEAD_KEYS: frozenset[str] = frozenset()


@dataclass
class DeadKeyReport:
    """配置死键扫描报告。

    Attributes:
        config_path: 被扫描的配置文件路径。
        total_leaf_keys: 纳入检测的叶子键总数（扣除白名单段）。
        dead_keys: 死键清单（配置里有、源码零消费的键名）。
        consumed_keys: 有读取点的键数。
        whitelisted_segments: 被跳过的白名单段名列表。
    """

    config_path: str
    total_leaf_keys: int = 0
    consumed_keys: int = 0
    dead_keys: list[str] = field(default_factory=list)
    whitelisted_segments: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否无新增死键（扣除 KNOWN_DEAD_KEYS 基线后）。"""
        return not self.new_dead_keys

    @property
    def new_dead_keys(self) -> list[str]:
        """超出已知基线的新增死键（真正需要处理的）。"""
        return [k for k in self.dead_keys if k not in KNOWN_DEAD_KEYS]

    def summary(self) -> str:
        """单行可读摘要。"""
        return (
            f"配置死键扫描: {self.config_path} | 叶子键 {self.total_leaf_keys} | "
            f"有消费 {self.consumed_keys} | 死键 {len(self.dead_keys)} "
            f"(新增 {len(self.new_dead_keys)})"
        )


def _collect_leaf_keys(
    node: Any,
    whitelisted: list[str],
    current_segment: str | None = None,
) -> set[str]:
    """递归收集 yaml 叶子键名。

    遇到白名单段名（current_segment）整段跳过；只收叶子键名（不带值或末级映射的键）。

    Args:
        node: yaml 解析后的 dict / list / 标量节点。
        whitelisted: 出参——记录实际命中的白名单段名。
        current_segment: 当前所处的段名（用于判断是否命中白名单段）。

    Returns:
        叶子键名集合（去重）。
    """
    keys: set[str] = set()
    if isinstance(node, dict):
        # 当前段是白名单段 → 整段不收（含其全部子键）
        if current_segment is not None and current_segment in WHITELIST_SEGMENTS:
            if current_segment not in whitelisted:
                whitelisted.append(current_segment)
            return keys
        for key, value in node.items():
            if isinstance(value, dict):
                # 嵌套段：把本段名传给下一层判断
                keys |= _collect_leaf_keys(value, whitelisted, current_segment=key)
            elif isinstance(value, list):
                # list 容器本身不是叶子键（如 codes 列表），但它的父段名已在
                # current_segment 判定；若本段是白名单段上面已 return
                keys |= _collect_leaf_keys(value, whitelisted, current_segment=current_segment)
            else:
                # 叶子键（标量值）
                keys.add(str(key))
    elif isinstance(node, list):
        # 列表元素：逐元素递归（如 overrides 里的 dict 项）
        for item in node:
            keys |= _collect_leaf_keys(item, whitelisted, current_segment=current_segment)
    return keys


def _find_read_point(key: str, source_root: Path) -> bool:
    """在 myquant 源码全仓查找键名字符串是否出现。

    判定宽松：键名作为任何字符串字面量 / 标识符出现在任一 .py 即视为有消费点。
    用正则按"词边界"匹配，避免子串误命中（如键名 "end" 命中 "legend"）。

    Args:
        key: 叶子键名。
        source_root: 源码根目录（如 myquant/）。

    Returns:
        True=源码有读取点；False=死键。
    """
    if not key or not key.strip():
        return True  # 空键名不算死键
    # 词边界正则：标识符字符边界，避免 legend 命中 end
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])")
    for py_file in source_root.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 跳过无法读取的文件（损坏/权限），不中断整仓扫描
            continue
        if pattern.search(text):
            return True
    return False


def scan_dead_keys(
    config_path: str = DEFAULT_CONFIG_YAML,
    source_root: str = DEFAULT_SOURCE_ROOT,
    project_root: str = ".",
) -> DeadKeyReport:
    """扫描配置死键主入口。

    Args:
        config_path: 配置文件路径（相对 project_root 或绝对）。
        source_root: 源码根（相对 project_root 或绝对）。
        project_root: 项目根目录。

    Returns:
        DeadKeyReport 报告。

    Raises:
        FileNotFoundError: 配置文件不存在时抛出。
        yaml.YAMLError: 配置解析失败时抛出。
    """
    root = Path(project_root)
    cfg_file = (root / config_path) if not Path(config_path).is_absolute() else Path(config_path)
    src_dir = (root / source_root) if not Path(source_root).is_absolute() else Path(source_root)

    if not cfg_file.exists():
        raise FileNotFoundError(f"配置文件不存在: {cfg_file}")

    with open(cfg_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    whitelisted: list[str] = []
    leaf_keys = _collect_leaf_keys(config or {}, whitelisted)

    report = DeadKeyReport(config_path=str(cfg_file))
    report.whitelisted_segments = sorted(set(whitelisted))
    report.total_leaf_keys = len(leaf_keys)

    for key in sorted(leaf_keys):
        if _find_read_point(key, src_dir):
            report.consumed_keys += 1
        else:
            report.dead_keys.append(key)

    return report
