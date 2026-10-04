# 文件名：myquant/live/state_store.py
"""
state_store.py - 状态持久化 / 崩溃恢复（任务23：实盘增强）
==========================================================
实盘进程状态持久化与崩溃恢复核心。

核心能力:
  1. 状态快照 — 将实盘状态（引擎/账户/持仓/订单/调度器）持久化为 JSON 快照
  2. 版本管理 — 每次保存版本自增 + index 双写（对齐任务14/19 版本管理模式）
  3. 原子写入 — 临时文件 + os.replace，防止写一半崩溃损坏旧快照
  4. 完整性校验 — sha256 checksum，读时校验，篡改/损坏抛出 SnapshotIntegrityError
  5. 崩溃恢复 — recover_latest() 校验并加载最新快照，启动时一键恢复
  6. 实盘采集/回写 — capture_live_state() 采集组件状态 / restore_live_state() 回写组件
  7. 保留策略 — 自动清理，只保留最近 N 份快照
  8. 定时快照 — attach_snapshot() 与任务22调度器联动，注册定时快照任务

设计原则（4条铁律）:
  1. 以项目结构总图为核心（docs/project_layout_gear2.md）
  2. 为实时行情及实盘无缝对接做好充足准备（定时快照/启动恢复/多store隔离）
  3. 输出完整全部代码，文件开头标注文件名
  4. 严格禁用未来函数 — 快照只保存已发生状态，恢复只回写当前已知数据

防未来函数说明:
  - 快照内容全部为"已发生"事实（引擎状态/持仓/资金/订单/调度任务统计），无任何预测
  - 恢复操作只回写当前已知数据，不生成任何新交易信号
  - 无预测方法、无硬编码未来日期；时间戳均取当前时间

依赖: typing, time, hashlib, json, os, threading, dataclasses, pathlib, myquant.core.logger
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("live.state_store")

# 默认配置
_DEFAULT_KEEP = 50                  # 默认保留快照份数
_INDEX_FILE = "index.json"          # 索引文件名
_SNAPSHOT_PREFIX = "snap_"          # 快照文件名前缀


# ═══════════════════════════════════════════════
#  异常
# ═══════════════════════════════════════════════

class StateStoreError(Exception):
    """状态存储通用异常"""


class SnapshotNotFoundError(StateStoreError):
    """快照不存在"""


class SnapshotIntegrityError(StateStoreError):
    """快照完整性校验失败（损坏/篡改）"""


class SnapshotVersionError(StateStoreError):
    """快照版本非法"""


# ═══════════════════════════════════════════════
#  StateSnapshot - 状态快照
# ═══════════════════════════════════════════════

@dataclass
class StateSnapshot:
    """
    状态快照。

    结构:
      version  版本号（自增）
      timestamp 保存时间戳
      tag      自定义标签（如 "crash_backup" / "daily"）
      checksum state 的 sha256（完整性校验）
      metadata 附加元信息（JSON 安全）
      state    实际状态 dict（引擎/账户/持仓/订单/调度器...）
    """
    version: int
    timestamp: float
    tag: str = ""
    checksum: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)

    # ─────────────────────────────────────────
    #  序列化
    # ─────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 JSON 安全 dict"""
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "tag": self.tag,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateSnapshot":
        """从 dict 反序列化"""
        return cls(
            version=int(data.get("version", 0)),
            timestamp=float(data.get("timestamp", 0.0)),
            tag=str(data.get("tag", "")),
            checksum=str(data.get("checksum", "")),
            metadata=dict(data.get("metadata", {})),
            state=data.get("state", {}),
        )

    # ─────────────────────────────────────────
    #  校验
    # ─────────────────────────────────────────

    @staticmethod
    def compute_checksum(state: Dict[str, Any]) -> str:
        """计算状态 dict 的 sha256（规范 JSON 序列化，键排序保证稳定）。"""
        canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        """
        校验快照完整性：重算 state checksum 并与存储值比对。

        Returns
        -------
        bool : True=完整
        """
        if not self.checksum:
            return False
        return self.compute_checksum(self.state) == self.checksum

    def __repr__(self) -> str:
        return (
            f"StateSnapshot(v{self.version}, ts={self.timestamp:.0f}, "
            f"tag={self.tag!r}, checksum_ok={self.verify()})"
        )


# ═══════════════════════════════════════════════
#  StateStore - 状态存储
# ═══════════════════════════════════════════════

class StateStore:
    """
    状态持久化存储（对齐任务14/19 版本管理模式）。

    存储布局:
      <root>/live_state/<store_id>/index.json                 # 版本索引（head + versions 元信息）
      <root>/live_state/<store_id>/versions/snap_<ts>_v<n>.json  # 快照文件

    使用方式:
        from myquant.live.state_store import StateStore

        store = StateStore(store_id="default")
        snap = store.save({"account": {...}, "positions": [...]}, tag="crash_backup")

        latest = store.load_latest()          # 崩溃恢复
        v3 = store.load_version(3)
        store.delete_old(keep=20)             # 只保留最近 20 份
    """

    def __init__(
        self,
        root: Optional[Path] = None,
        store_id: str = "default",
        keep: int = _DEFAULT_KEEP,
    ):
        """
        初始化状态存储。

        Parameters
        ----------
        root : Path | None
            存储根目录（默认 项目根/datasets/live_state）
        store_id : str
            存储实例 ID（多实例/多账户隔离，默认 "default"）
        keep : int
            默认保留快照份数（>0；save 后自动清理更旧快照）
        """
        if root is None:
            root = Path(__file__).resolve().parent.parent.parent / "datasets" / "live_state"
        self._root = Path(root)
        self._store_id = store_id
        self._keep = int(keep)

        self._store_dir = self._root / store_id
        self._versions_dir = self._store_dir / "versions"
        self._versions_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()

        logger.info(
            f"状态存储初始化: store={store_id}, "
            f"目录={self._store_dir}, keep={self._keep}"
        )

    # ─────────────────────────────────────────
    #  属性
    # ─────────────────────────────────────────

    @property
    def store_id(self) -> str:
        """存储实例 ID"""
        return self._store_id

    @property
    def store_dir(self) -> Path:
        """存储目录"""
        return self._store_dir

    @property
    def root(self) -> Path:
        """存储根目录"""
        return self._root

    # ─────────────────────────────────────────
    #  索引管理
    # ─────────────────────────────────────────

    def _index_path(self) -> Path:
        return self._store_dir / _INDEX_FILE

    def _read_index(self) -> Dict[str, Any]:
        """读取索引（不存在返回空索引）。"""
        idx = self._index_path()
        if not idx.exists():
            return {"head": 0, "versions": {}}
        try:
            return json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[state_store] 索引损坏，重置为空索引: {e}")
            return {"head": 0, "versions": {}}

    def _write_index(self, index: Dict[str, Any]) -> None:
        """原子写入索引。"""
        self._atomic_write_json(self._index_path(), index)

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        """
        原子写入 JSON：先写临时文件再 os.replace，防止写一半崩溃损坏。

        Parameters
        ----------
        path : Path
            目标路径
        data : Any
            待写入数据
        """
        tmp = path.with_name(path.name + ".tmp")
        # P2-16：原子写补 flush+fsync，避免掉电窗口内 tmp 内容未落盘导致快照不完整
        payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    # ─────────────────────────────────────────
    #  保存
    # ─────────────────────────────────────────

    def save(
        self,
        state: Dict[str, Any],
        tag: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StateSnapshot:
        """
        保存状态快照（版本自增 + 原子写入 + 索引双写）。

        Parameters
        ----------
        state : Dict
            状态 dict（JSON 安全）
        tag : str
            快照标签（如 "crash_backup" / "hourly"）
        metadata : dict | None
            附加元信息

        Returns
        -------
        StateSnapshot : 已保存的快照
        """
        with self._lock:
            index = self._read_index()
            version = int(index.get("head", 0)) + 1

            ts = time.time()
            snap = StateSnapshot(
                version=version,
                timestamp=ts,
                tag=tag,
                checksum=StateSnapshot.compute_checksum(state),
                metadata=dict(metadata or {}),
                state=state,
            )

            # 1. 写快照文件（原子）
            path = self._snapshot_path(version)
            self._atomic_write_json(path, snap.to_dict())

            # 2. 索引双写
            index["head"] = version
            index.setdefault("versions", {})[str(version)] = {
                "timestamp": ts,
                "tag": tag,
                "checksum": snap.checksum,
                "file": path.name,
            }
            self._write_index(index)

        # 3. 保留策略（锁外执行，避免长锁）
        self._apply_keep()
        logger.info(
            f"[state_store] 快照已保存: v{version} tag={tag!r} "
            f"checksum={snap.checksum[:12]}"
        )
        return snap

    def _snapshot_path(self, version: int) -> Path:
        """快照文件路径（按版本查找文件，避免依赖时间戳猜测）。"""
        # 优先从索引取文件名，其次按前缀扫描
        index = self._read_index()
        meta = index.get("versions", {}).get(str(version))
        if meta and meta.get("file"):
            return self._versions_dir / meta["file"]
        # 兜底：扫描目录中该版本的文件
        for p in self._versions_dir.glob(f"{_SNAPSHOT_PREFIX}*_v{version}.json"):
            return p
        return self._versions_dir / f"{_SNAPSHOT_PREFIX}{0.0:.0f}_v{version}.json"

    # ─────────────────────────────────────────
    #  加载
    # ─────────────────────────────────────────

    def load_version(self, version: int) -> StateSnapshot:
        """
        按版本加载快照（含完整性校验）。

        Parameters
        ----------
        version : int
            版本号

        Returns
        -------
        StateSnapshot : 快照

        Raises
        ------
        SnapshotNotFoundError : 快照不存在
        SnapshotIntegrityError : 完整性校验失败（损坏/篡改）
        """
        if version <= 0:
            raise SnapshotVersionError(f"版本号非法: {version}")

        path = self._snapshot_path(version)
        if not path.exists():
            raise SnapshotNotFoundError(f"快照 v{version} 不存在: {path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise SnapshotIntegrityError(f"快照 v{version} 无法解析: {e}") from e

        snap = StateSnapshot.from_dict(raw)
        if not snap.verify():
            raise SnapshotIntegrityError(
                f"快照 v{version} 完整性校验失败（checksum 不匹配）"
            )
        return snap

    def load_latest(self) -> StateSnapshot:
        """
        加载最新快照（崩溃恢复主入口）。

        Returns
        -------
        StateSnapshot : 最新快照

        Raises
        ------
        SnapshotNotFoundError : 无任何快照
        """
        version = self.get_latest_version()
        if version is None:
            raise SnapshotNotFoundError("无可用快照（store 为空）")
        snap = self.load_version(version)
        logger.info(f"[state_store] 已加载最新快照: v{snap.version} tag={snap.tag!r}")
        return snap

    def recover_latest(self) -> StateSnapshot:
        """
        崩溃恢复：校验并加载最新快照（load_latest 的语义别名）。

        供启动引导调用；无快照时抛出 SnapshotNotFoundError 由调用方决定策略。
        """
        return self.load_latest()

    def get_latest_version(self) -> Optional[int]:
        """最新版本号（无快照返回 None）。"""
        with self._lock:
            index = self._read_index()
            head = int(index.get("head", 0))
            return head if head > 0 else None

    # ─────────────────────────────────────────
    #  查询 / 校验
    # ─────────────────────────────────────────

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """
        列出全部快照（按版本倒序，JSON 安全）。

        Returns
        -------
        List[Dict] : [{version, timestamp, tag, checksum, file}]
        """
        with self._lock:
            index = self._read_index()
            versions = index.get("versions", {})
        items = []
        for v_str, meta in versions.items():
            items.append({
                "version": int(v_str),
                "timestamp": meta.get("timestamp"),
                "tag": meta.get("tag", ""),
                "checksum": meta.get("checksum", ""),
                "file": meta.get("file", ""),
            })
        items.sort(key=lambda x: x["version"], reverse=True)
        return items

    def verify_snapshot(self, version: int) -> bool:
        """
        校验指定版本快照完整性。

        Parameters
        ----------
        version : int
            版本号

        Returns
        -------
        bool : True=完整
        """
        try:
            snap = self.load_version(version)
            return snap.verify()
        except StateStoreError:
            return False

    # ─────────────────────────────────────────
    #  删除 / 保留
    # ─────────────────────────────────────────

    def delete_version(self, version: int) -> bool:
        """
        删除指定版本快照（head 自动回退）。

        Parameters
        ----------
        version : int
            版本号

        Returns
        -------
        bool : True=删除成功
        """
        with self._lock:
            index = self._read_index()
            versions = index.get("versions", {})
            if str(version) not in versions:
                return False
            path = self._snapshot_path(version)
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            del versions[str(version)]
            if int(index.get("head", 0)) == version:
                head_versions = [int(v) for v in versions.keys() if int(v) < version]
                index["head"] = max(head_versions) if head_versions else 0
            index["versions"] = versions
            self._write_index(index)
        logger.info(f"[state_store] 快照已删除: v{version}")
        return True

    def delete_old(self, keep: Optional[int] = None) -> int:
        """
        只保留最近 N 份快照，删除更旧的。

        Parameters
        ----------
        keep : int | None
            保留份数（None 用初始化配置 keep）

        Returns
        -------
        int : 删除的快照数量
        """
        keep = self._keep if keep is None else int(keep)
        if keep <= 0:
            raise StateStoreError(f"keep 必须 > 0，实际 {keep}")
        snaps = self.list_snapshots()  # 版本倒序
        if len(snaps) <= keep:
            return 0
        deleted = 0
        for item in snaps[keep:]:
            if self.delete_version(item["version"]):
                deleted += 1
        if deleted:
            logger.info(f"[state_store] 保留策略清理: 删除 {deleted} 份旧快照")
        return deleted

    def _apply_keep(self) -> None:
        """保存后自动执行保留策略。"""
        try:
            self.delete_old(self._keep)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[state_store] 保留策略执行失败: {e}")

    # ─────────────────────────────────────────
    #  摘要
    # ─────────────────────────────────────────

    def get_summary(self) -> Dict[str, Any]:
        """存储摘要（JSON 安全）。"""
        with self._lock:
            index = self._read_index()
        versions = index.get("versions", {})
        latest = None
        if versions:
            v_str = str(int(index.get("head", 0)))
            meta = versions.get(v_str) or versions[max(versions, key=int)]
            latest = {
                "version": int(v_str),
                "tag": meta.get("tag", ""),
                "timestamp": meta.get("timestamp"),
            }
        return {
            "store_id": self._store_id,
            "store_dir": str(self._store_dir),
            "head": int(index.get("head", 0)),
            "snapshot_count": len(versions),
            "keep": self._keep,
            "latest_snapshot": latest,
        }

    def __repr__(self) -> str:
        return (
            f"StateStore(store={self._store_id!r}, "
            f"head={self.get_latest_version()}, "
            f"count={len(self.list_snapshots())})"
        )


# ═══════════════════════════════════════════════
#  实盘状态采集 / 回写
# ═══════════════════════════════════════════════

def capture_live_state(
    engine: Any = None,
    portfolio: Any = None,
    broker: Any = None,
    scheduler: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    从实盘组件采集状态为 JSON 安全 dict（供 StateStore.save 使用）。

    全部为"已发生"事实：引擎状态/账户/持仓/订单/调度器统计。
    组件缺失时对应字段置空 dict 并标注 not_connected，不抛错。

    Parameters
    ----------
    engine : LiveEngine | None
        实盘引擎（get_status）
    portfolio : LivePortfolio | None
        持仓管理（get_account / get_all_positions）
    broker : BaseBroker | None
        券商（get_account / get_positions / get_orders）
    scheduler : LiveScheduler | None
        调度器（get_summary）
    extra : dict | None
        自定义附加字段

    Returns
    -------
    Dict : 采集到的状态 dict
    """
    data: Dict[str, Any] = {
        "captured_at": time.time(),
        "engine": _capture_engine(engine),
        "account": _capture_account(portfolio, broker),
        "positions": _capture_positions(portfolio, broker),
        "orders": _capture_orders(broker),
        "scheduler": _capture_scheduler(scheduler),
    }
    if extra is not None:
        data["extra"] = dict(extra)
    return data


def _capture_engine(engine: Any) -> Dict[str, Any]:
    """采集引擎状态（只读已发生统计）。"""
    if engine is None:
        return {"not_connected": True}
    try:
        if hasattr(engine, "get_status"):
            return engine.get_status()
        return {"not_connected": True}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _capture_account(portfolio: Any, broker: Any) -> Dict[str, Any]:
    """采集账户资金（优先 portfolio，其次 broker）。"""
    try:
        if portfolio is not None and hasattr(portfolio, "get_account"):
            return portfolio.get_account()
        if broker is not None and hasattr(broker, "get_account"):
            return broker.get_account()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    return {"not_connected": True}


def _capture_positions(portfolio: Any, broker: Any) -> List[Dict[str, Any]]:
    """采集持仓明细（优先 portfolio）。"""
    try:
        if portfolio is not None and hasattr(portfolio, "get_all_positions"):
            return [_position_to_dict(p) for p in portfolio.get_all_positions()]
        if broker is not None and hasattr(broker, "get_positions"):
            return [_position_to_dict(p) for p in broker.get_positions()]
    except Exception as e:  # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {e}"}]
    return []


def _capture_orders(broker: Any) -> List[Dict[str, Any]]:
    """采集订单（broker 支持 get_orders 时；否则空列表）。"""
    if broker is None:
        return []
    try:
        if hasattr(broker, "get_orders"):
            orders = broker.get_orders()
            result = []
            for o in orders:
                if hasattr(o, "to_dict"):
                    result.append(o.to_dict())
                else:
                    result.append(_order_to_dict(o))
            return result
    except Exception as e:  # noqa: BLE001
        return [{"error": f"{type(e).__name__}: {e}"}]
    return []


def _capture_scheduler(scheduler: Any) -> Dict[str, Any]:
    """采集调度器任务总览。"""
    if scheduler is None:
        return {"not_connected": True}
    try:
        if hasattr(scheduler, "get_summary"):
            return scheduler.get_summary()
        return {"not_connected": True}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _position_to_dict(pos: Any) -> Dict[str, Any]:
    """Position → JSON 安全 dict（兼容真实 Position 与通用对象）。"""
    g = lambda name, default=None: getattr(pos, name, default)  # noqa: E731
    quantity = g("quantity", 0)
    avg_price = g("avg_cost", g("avg_price", 0.0))
    market_price = g("market_price", g("current_price", 0.0))
    market_value = g("market_value", quantity * market_price)
    cost = g("cost_value", g("cost", quantity * avg_price))
    return {
        "symbol": g("symbol"),
        "quantity": quantity,
        "avg_price": avg_price,
        "current_price": market_price,
        "market_value": market_value,
        "cost": cost,
        "unrealized_pnl": g("unrealized_pnl", market_value - cost),
        "realized_pnl": g("realized_pnl", 0.0),
        "pnl_ratio": g("pnl_ratio", 0.0),
    }


def _order_to_dict(order: Any) -> Dict[str, Any]:
    """Order（非标准）→ dict 兜底。"""
    g = lambda name, default=None: getattr(order, name, default)  # noqa: E731
    side = g("side")
    return {
        "order_id": g("order_id"),
        "symbol": g("symbol"),
        "side": side.value if hasattr(side, "value") else side,
        "quantity": g("quantity", 0.0),
        "price": g("price"),
        "status": g("status", ""),
        "filled_qty": g("filled_qty", 0.0),
        "filled_price": g("filled_price", 0.0),
        "created_at": g("created_at"),
        "updated_at": g("updated_at"),
    }


def restore_live_state(
    snapshot: StateSnapshot,
    portfolio: Any = None,
    broker: Any = None,
    scheduler: Any = None,
    strict: bool = False,
) -> Dict[str, Any]:
    """
    将快照状态回写实盘组件（崩溃恢复落地）。

    恢复策略:
      - portfolio：reset() 清空 → 按快照重建持仓（apply_trade BUY）→ update_cash 精确回写现金 → 回写市值
      - broker：支持 restore_state(state) 时调用（duck-typing），否则跳过
      - scheduler：v1 只记录，不重建任务（任务由代码重启后重新注册）
      - 组件缺失时安全跳过；strict=True 时缺失组件视为失败

    防未来: 恢复只回写快照中"已发生"的持仓/资金，不生成任何交易信号。

    Parameters
    ----------
    snapshot : StateSnapshot
        待恢复快照（应先通过完整性校验）
    portfolio : LivePortfolio | None
        持仓管理（主恢复目标）
    broker : BaseBroker | None
        券商（可选）
    scheduler : LiveScheduler | None
        调度器（v1 仅记录）
    strict : bool
        严格模式：缺失组件抛 StateStoreError

    Returns
    -------
    Dict : {restored_portfolio, restored_broker, restored_scheduler, restored_positions}
    """
    state = snapshot.state
    restored: Dict[str, Any] = {
        "version": snapshot.version,
        "timestamp": snapshot.timestamp,
        "restored_portfolio": False,
        "restored_broker": False,
        "restored_scheduler": False,
        "restored_positions": 0,
        "notes": [],
    }

    # ── portfolio 恢复 ──
    if portfolio is None:
        if strict:
            raise StateStoreError("strict 模式下 portfolio 缺失，无法恢复")
        restored["notes"].append("portfolio 未接入，跳过")
    else:
        try:
            restored["restored_positions"] = _restore_portfolio(portfolio, state)
            restored["restored_portfolio"] = True
        except Exception as e:  # noqa: BLE001
            if strict:
                raise StateStoreError(f"portfolio 恢复失败: {e}") from e
            restored["notes"].append(f"portfolio 恢复失败: {type(e).__name__}: {e}")

    # ── broker 恢复（可选 duck-typing）──
    if broker is not None and hasattr(broker, "restore_state"):
        try:
            broker.restore_state(state)
            restored["restored_broker"] = True
        except Exception as e:  # noqa: BLE001
            if strict:
                raise StateStoreError(f"broker 恢复失败: {e}") from e
            restored["notes"].append(f"broker 恢复失败: {type(e).__name__}: {e}")
    elif broker is not None:
        restored["notes"].append("broker 无 restore_state，跳过（账户以 portfolio 为准）")

    # ── scheduler 恢复（v1 仅记录）──
    if scheduler is not None:
        restored["notes"].append(
            "scheduler 任务不重建（重启后由代码重新注册），仅保留统计信息"
        )
    restored["restored_scheduler"] = False

    logger.info(
        f"[state_store] 状态恢复完成: v{snapshot.version}, "
        f"portfolio={restored['restored_portfolio']}, "
        f"positions={restored['restored_positions']}"
    )
    return restored


def _restore_portfolio(portfolio: Any, state: Dict[str, Any]) -> int:
    """
    重建 portfolio：reset → 按快照 BUY 重建持仓 → 回写现金 → 回写市值。

    Returns
    -------
    int : 恢复的持仓数量
    """
    from myquant.models.trade import OrderSide, Trade

    account = state.get("account", {})
    positions = state.get("positions", [])
    if isinstance(positions, dict):
        positions = list(positions.values()) if positions else []

    # 1. 重置
    if hasattr(portfolio, "reset"):
        portfolio.reset()

    # 2. 重建持仓（BUY 成交价 = avg_price，保证数量与成本价正确）
    count = 0
    for p in positions:
        if not isinstance(p, dict) or "error" in p:
            continue
        symbol = p.get("symbol")
        quantity = float(p.get("quantity", 0))
        avg_price = float(p.get("avg_price", p.get("avg_cost", 0.0)) or 0.0)
        if not symbol or quantity <= 0 or avg_price <= 0:
            continue
        if hasattr(portfolio, "apply_trade"):
            portfolio.apply_trade(Trade(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                price=avg_price,
            ))
            count += 1

    # 3. 精确回写现金（快照现金为准，覆盖重建产生的扣款）
    if hasattr(portfolio, "update_cash") and "cash" in account:
        portfolio.update_cash(
            float(account.get("cash", portfolio.initial_capital)),
            float(account.get("frozen_cash", 0.0)),
        )

    # 4. 回写市值
    for p in positions:
        if not isinstance(p, dict) or "error" in p:
            continue
        symbol = p.get("symbol")
        price = p.get("current_price")
        if symbol and price is not None and hasattr(portfolio, "update_market_price"):
            portfolio.update_market_price(symbol, float(price))

    return count


# ═══════════════════════════════════════════════
#  attach_snapshot - 定时快照便捷对接（任务22 调度器）
# ═══════════════════════════════════════════════

def attach_snapshot(
    scheduler: Any,
    store: StateStore,
    engine: Any = None,
    portfolio: Any = None,
    broker: Any = None,
    snapshot_interval: float = 300.0,
    tag: str = "auto",
    run_immediately: bool = False,
    task_id: Optional[str] = None,
) -> str:
    """
    将定时快照挂载到任务22 调度器：按固定间隔自动保存实盘状态。

    Parameters
    ----------
    scheduler : LiveScheduler
        目标调度器
    store : StateStore
        状态存储
    engine / portfolio / broker : 实盘组件（快照数据源）
    snapshot_interval : float
        快照间隔（秒），默认 300（5 分钟）
    tag : str
        快照标签，默认 "auto"
    run_immediately : bool
        是否立即执行一次（默认 False，等待首个间隔）
    task_id : str | None
        任务 ID

    Returns
    -------
    str : 任务 ID
    """

    def _snapshot_job(**kw) -> None:  # noqa: ARG001
        state = capture_live_state(
            engine=engine, portfolio=portfolio, broker=broker, scheduler=scheduler,
        )
        snap = store.save(state, tag=tag)
        logger.info(
            f"[state_store] 定时快照完成: v{snap.version} "
            f"positions={len(state.get('positions', []))}"
        )

    return scheduler.add_interval_task(
        _snapshot_job,
        name="状态快照",
        interval_seconds=snapshot_interval,
        run_immediately=run_immediately,
        task_id=task_id,
    )
