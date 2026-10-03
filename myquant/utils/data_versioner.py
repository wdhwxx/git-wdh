# 文件名：myquant/utils/data_versioner.py
"""
data_versioner.py - 数据集口径版本登记模块
=============================================
作为 data_fingerprint 之上的「数据集级」版本治理层（F-07 拆出，F-15 落地）。

背景：报告风险表明确——公告日时点化（F-15）、退市股接入等会改变历史数据
分布。历史结论（IC 档案、回测绩效、因子值）必须标注生成时的数据口径版本，
禁止跨口径直接比较。本模块提供：

  1. ``DATASET_SCHEMA_REVISIONS``：每个数据集的 schema 修订号常量，
     发生破坏性结构变更（加列/改语义）时手动 bump。
  2. ``DatasetVersion`` 值对象：dataset + schema_revision + row_count
     + date_range + columns_hash + computed_at。
  3. ``get_dataset_version(conn, dataset)``：从 SQLite 现算一份版本快照。
  4. ``write_result_version_sidecar``：在结果文件旁写 ``.version.json``
     标注生成时的数据口径 + 代码 git 短哈希。
  5. ``assert_versions_compatible``：跨口径比较守卫，schema_revision
     不一致时抛 ``DataVersionMismatchError``，阻止 IC 档案/绩效跨期直比。

本模块不做任何时间序列计算，无未来函数风险。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 数据集 schema 修订号（破坏性结构变更时手动 +1）
# ─────────────────────────────────────────────
# 修订号语义：
#   rev=1 初始口径
#   rev=2 发生过会改变历史数据分布的结构变更（加列/改时点语义）
# 任何跨修订号的 IC 档案 / 回测绩效不可直接比较。
DATASET_SCHEMA_REVISIONS: Dict[str, int] = {
    "market_data": 2,        # 振幅/量比/流通股本等派生列加入（见蓝图 #33）
    "financial_data": 2,     # F-15 新增 ann_date 公告日列，时点语义变更
    "factor_data": 1,
}

#: 每个数据集的日期列（用于 MIN/MAX 口径快照）
DATASET_DATE_COLUMNS: Dict[str, str] = {
    "market_data": "trade_date",
    "financial_data": "report_date",
    "factor_data": "trade_date",
}

#: sidecar 文件后缀（与结果文件同目录同名）
SIDECAR_SUFFIX: str = ".version.json"


class DataVersionMismatchError(RuntimeError):
    """跨数据口径比较时抛出（schema_revision 不一致）。"""


# ─────────────────────────────────────────────
# DatasetVersion 值对象
# ─────────────────────────────────────────────
@dataclass
class DatasetVersion:
    """单个数据集的口径版本快照。

    Attributes:
        dataset: 数据集名（market_data / financial_data / factor_data）。
        schema_revision: schema 修订号（见 DATASET_SCHEMA_REVISIONS）。
        row_count: 当前行数。
        min_date: 日期列最小值（ISO 字符串，无数据时为空）。
        max_date: 日期列最大值。
        columns_hash: 列名集合的 sha256 前 12 位（结构指纹）。
        computed_at: 快照生成时间（UTC ISO）。
    """

    dataset: str
    schema_revision: int
    row_count: int
    min_date: str
    max_date: str
    columns_hash: str
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetVersion":
        """从字典反序列化。"""
        return cls(
            dataset=data["dataset"],
            schema_revision=int(data["schema_revision"]),
            row_count=int(data["row_count"]),
            min_date=str(data.get("min_date", "")),
            max_date=str(data.get("max_date", "")),
            columns_hash=str(data.get("columns_hash", "")),
            computed_at=str(
                data.get("computed_at", datetime.now(timezone.utc).isoformat())
            ),
        )


# ─────────────────────────────────────────────
# 版本快照计算
# ─────────────────────────────────────────────
def _columns_hash(conn: Any, table: str) -> str:
    """计算表列名集合的 sha256 短指纹（结构指纹，与行内容无关）。"""
    rows = conn.execute(f"PRAGMA table_info({table})")
    names = sorted(r["name"] for r in rows)
    return hashlib.sha256(",".join(names).encode("utf-8")).hexdigest()[:12]


def get_dataset_version(conn: Any, dataset: str) -> DatasetVersion:
    """
    计算单个数据集的当前口径版本快照。

    Args:
        conn: 数据库连接（DatabaseConnection，支持 execute/execute_one）。
        dataset: 数据集名，须在 DATASET_SCHEMA_REVISIONS 中登记。

    Returns:
        DatasetVersion 快照。

    Raises:
        KeyError: dataset 未在 DATASET_SCHEMA_REVISIONS 登记。
    """
    if dataset not in DATASET_SCHEMA_REVISIONS:
        raise KeyError(
            f"数据集 {dataset!r} 未在 DATASET_SCHEMA_REVISIONS 登记，"
            f"已知: {sorted(DATASET_SCHEMA_REVISIONS)}"
        )
    rev = DATASET_SCHEMA_REVISIONS[dataset]
    date_col = DATASET_DATE_COLUMNS.get(dataset, "trade_date")

    row = conn.execute_one(f"SELECT COUNT(*) AS c FROM {dataset}")
    row_count = int(row["c"]) if row else 0

    min_date = ""
    max_date = ""
    if row_count > 0:
        dr = conn.execute_one(
            f"SELECT MIN({date_col}) AS mn, MAX({date_col}) AS mx FROM {dataset}"
        )
        if dr:
            min_date = str(dr["mn"] or "")
            max_date = str(dr["mx"] or "")

    chash = _columns_hash(conn, dataset)
    return DatasetVersion(
        dataset=dataset,
        schema_revision=rev,
        row_count=row_count,
        min_date=min_date,
        max_date=max_date,
        columns_hash=chash,
    )


def get_all_dataset_versions(conn: Any) -> Dict[str, DatasetVersion]:
    """返回所有已登记数据集的当前版本快照。"""
    return {ds: get_dataset_version(conn, ds) for ds in DATASET_SCHEMA_REVISIONS}


# ─────────────────────────────────────────────
# 结果 sidecar 标注
# ─────────────────────────────────────────────
def _git_short_hash() -> str:
    """取当前代码 git 短哈希；非 git 环境返回 'unknown'。"""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_result_version_sidecar(
    result_path: Path,
    versions: Dict[str, DatasetVersion],
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """
    在结果文件旁写 ``.version.json`` sidecar，标注生成时的数据口径。

    用法：任何回测/IC 档案/因子报告产出后调用本函数，把当时的数据集
    口径版本 + 代码 git 短哈希钉在结果文件旁，后续复盘时即可判断
    该结论是否与当前库口径可比。

    Args:
        result_path: 结果文件路径（sidecar 写在同目录同名 + .version.json）。
        versions: ``{dataset: DatasetVersion}`` 快照。
        extra: 额外元数据（run_id / 模型版本 / 备注等）。

    Returns:
        sidecar 文件路径。
    """
    result_path = Path(result_path)
    sidecar = result_path.with_name(result_path.name + SIDECAR_SUFFIX)
    payload = {
        "result_file": result_path.name,
        "code_git_short": _git_short_hash(),
        "written_at": datetime.now(timezone.utc).isoformat(),
        "datasets": {ds: v.to_dict() for ds, v in versions.items()},
    }
    if extra:
        payload["extra"] = extra
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.debug("版本 sidecar 已写: %s", sidecar)
    return sidecar


def load_result_version_sidecar(result_path: Path) -> Optional[Dict[str, Any]]:
    """读取结果文件旁的 sidecar；不存在或损坏返回 None。"""
    sidecar = Path(result_path).with_name(Path(result_path).name + SIDECAR_SUFFIX)
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("sidecar 损坏，忽略: %s", sidecar)
        return None


# ─────────────────────────────────────────────
# 跨口径比较守卫
# ─────────────────────────────────────────────
def assert_versions_compatible(
    stored: Dict[str, DatasetVersion],
    current: Dict[str, DatasetVersion],
) -> None:
    """
    比较两份数据集快照，schema_revision 不一致时抛错。

    用于禁止跨口径直接比较历史结论（如把 ann_date 时点化前的 IC
    与时点化后的 IC 放在一起排名）。

    Args:
        stored: 历史结论生成时登记的版本快照。
        current: 当前库的版本快照。

    Raises:
        DataVersionMismatchError: 任一已登记数据集 schema_revision 不一致。
    """
    mismatches = []
    for ds, old_v in stored.items():
        new_v = current.get(ds)
        if new_v is None:
            continue
        if old_v.schema_revision != new_v.schema_revision:
            mismatches.append(
                f"{ds}: rev {old_v.schema_revision} → {new_v.schema_revision}"
            )
    if mismatches:
        raise DataVersionMismatchError(
            "数据口径版本已变更，历史结论与当前库不可直接比较: "
            + "; ".join(mismatches)
            + "。请按新口径重跑 IC 档案/回测，或显式分版本归档。"
        )
