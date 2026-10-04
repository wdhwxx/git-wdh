# -*- coding: utf-8 -*-
"""T-34 KP-P5-08 崩溃恢复在途订单：快照订单对账 + schema_version + auto_recover 显式必填。"""
import io
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "live_config.example.yaml"


class FakeBroker:
    """最小券商替身：get_orders/cancel_order/restore_state。"""

    def __init__(self, orders=None):
        self._orders = list(orders or [])
        self._cancelled: list = []
        self.restored = False

    def get_orders(self):
        return list(self._orders)

    def cancel_order(self, order_id):
        self._cancelled.append(order_id)
        return True

    def restore_state(self, state):
        self.restored = True


def _snapshot(orders, schema_version=1):
    from myquant.live.state_store import StateSnapshot

    return StateSnapshot(
        version=1,
        timestamp=0.0,
        checksum="",
        metadata={"schema_version": schema_version},
        state={"positions": [], "cash": 1000.0, "orders": orders},
    )


def test_restore_reconciles_unclaimed_orders_cancelled():
    """快照在途订单未在券商账本领回 → cancel + 告警留痕。"""
    from myquant.live.state_store import restore_live_state

    broker = FakeBroker(orders=[])  # 券商侧无任何订单
    res = restore_live_state(_snapshot([{"order_id": "O1"}, {"order_id": "O2"}]), broker=broker)
    assert res["reconciled_orders"] == 2
    assert set(broker._cancelled) == {"O1", "O2"}
    assert any("未在券商账本领回" in n for n in res["notes"])


def _order(oid):
    """带 order_id 属性的订单替身（broker.get_orders 返回 Order 对象）。"""
    return type("Ord", (), {"order_id": oid})()


def test_restore_no_reconcile_when_orders_claimed():
    """快照订单券商已领回 → 不 cancel。"""
    from myquant.live.state_store import restore_live_state

    broker = FakeBroker(orders=[_order("O1")])  # 券商已领回 O1
    res = restore_live_state(_snapshot([{"order_id": "O1"}]), broker=broker)
    assert res["reconciled_orders"] == 0
    assert broker._cancelled == []


def test_save_writes_schema_version():
    """save 快照 metadata 写 schema_version。"""
    from myquant.live.state_store import StateStore

    tmp = tempfile.mkdtemp()
    store = StateStore(root=tmp, store_id="t34")
    snap = store.save({"positions": []}, tag="t")
    assert snap.metadata.get("schema_version") == 1
    loaded = store.load_latest()
    assert loaded.metadata.get("schema_version") == 1


def test_load_rejects_future_schema_version():
    """不兼容（高于当前）schema_version → SnapshotVersionError。"""
    from myquant.live.state_store import SnapshotVersionError, StateStore

    tmp = tempfile.mkdtemp()
    store = StateStore(root=tmp, store_id="t34")
    store.save({"positions": []}, tag="t")
    # 篡改 metadata schema_version 为未来版本
    import json

    latest = store.load_latest()
    path = store._snapshot_path(latest.version)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["metadata"]["schema_version"] = 999
    path.write_text(json.dumps(raw), encoding="utf-8")
    # 重写索引 checksum 校验绕过（校验针对 state，不针对 metadata）
    with pytest.raises(SnapshotVersionError):
        store.load_latest()


def test_create_live_engine_auto_recover_required():
    """auto_recover_on_start 缺失 → ValueError（显式必填）。"""
    from myquant.live.engine import create_live_engine

    cfg = yaml_load()
    cfg.setdefault("state_store", {}).pop("auto_recover_on_start", None)
    with pytest.raises(ValueError, match="auto_recover_on_start"):
        create_live_engine(cfg)


def test_create_live_engine_auto_recover_honored():
    """auto_recover_on_start 显式 true/false 生效。"""
    from myquant.live.engine import create_live_engine

    cfg = yaml_load()
    cfg.setdefault("state_store", {})["auto_recover_on_start"] = True
    assert create_live_engine(cfg)._auto_recover is True
    cfg["state_store"]["auto_recover_on_start"] = False
    assert create_live_engine(cfg)._auto_recover is False


def yaml_load():
    import yaml

    return yaml.safe_load(io.open(EXAMPLE, "r", encoding="utf-8"))
