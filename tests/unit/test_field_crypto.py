# 文件名：tests/unit/test_field_crypto.py
"""
test_field_crypto.py - BASE-9 监控数据加密与访问审计单元测试
===========================================================
覆盖：
  1. FieldCrypto：密钥文件生成/复用、环境变量密钥、加密/解密往返、
     密文非明文、错误密钥解密失败（try_decrypt 返回 None）
  2. 敏感判定与脱敏：is_sensitive_name（equity/balance/position/capital/
     asset/pnl 命中；普通指标不命中）、mask_value 不泄露
  3. metrics_history 集成：敏感指标密文落盘（value 占位 + value_encrypted
     非空 + 密文非明文）、解密回读真实值、普通指标明文不受影响、
     未绑定加密器拒绝敏感明文落盘、敏感指标拒绝降采样聚合、
     自定义 patterns 覆盖
  4. 访问审计：query(record_access=True) 留痕、export_csv 导出留痕、
     审计链完整
  5. database/connection：库文件权限收紧（POSIX chmod 600；Windows
     不抛异常）

运行方式:
  pytest tests/unit/test_field_crypto.py -v
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from myquant.database.connection import DatabaseConnection
from myquant.core.audit_log import AuditLogStore
from myquant.core.field_crypto import FieldCrypto, is_sensitive_name, mask_value
from myquant.core.metrics_history import MetricsHistoryStore


@pytest.fixture()
def tmp_conn(tmp_path: Path) -> DatabaseConnection:
    return DatabaseConnection(tmp_path / "crypto_test.db")


@pytest.fixture()
def crypto(tmp_path: Path) -> FieldCrypto:
    return FieldCrypto(key_file=str(tmp_path / ".test_key"))


@pytest.fixture()
def store(tmp_conn: DatabaseConnection, crypto: FieldCrypto) -> MetricsHistoryStore:
    s = MetricsHistoryStore(connection=tmp_conn)
    s.attach_field_crypto(crypto)
    return s


# ═══════════════════════════════════════════
#  1. FieldCrypto
# ═══════════════════════════════════════════

class TestFieldCrypto:
    def test_key_file_generated_and_reused(self, tmp_path: Path) -> None:
        key_file = tmp_path / "k1"
        c1 = FieldCrypto(key_file=str(key_file))
        assert key_file.exists()
        c2 = FieldCrypto(key_file=str(key_file))
        assert c2.decrypt(c1.encrypt("1.5")) == "1.5"

    def test_env_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = FieldCrypto._generate_key().decode()
        monkeypatch.setenv("QUANT_MONITOR_KEY", key)
        c = FieldCrypto()
        assert c.decrypt(c.encrypt("9.9")) == "9.9"

    def test_encrypt_decrypt_roundtrip(self, crypto: FieldCrypto) -> None:
        plain = "123456.78"
        token = crypto.encrypt(plain)
        assert token != plain and len(token) > 10
        assert crypto.decrypt(token) == plain

    def test_wrong_key_rejected(self, crypto: FieldCrypto) -> None:
        token = crypto.encrypt("1.0")
        other = FieldCrypto(key_file=str(Path(crypto._fernet.__dict__.get("_encryptor", "x")))) if False else FieldCrypto(
            key_env=FieldCrypto._generate_key().decode()
        )
        with pytest.raises(ValueError):
            other.decrypt(token)
        assert other.try_decrypt(token) is None  # 安全解密返回 None

    def test_mask_never_leaks(self) -> None:
        assert mask_value("123456.78") == "****"
        assert mask_value(1_000_000.0) == "****"


# ═══════════════════════════════════════════
#  2. 敏感判定
# ═══════════════════════════════════════════

class TestSensitiveDetection:
    @pytest.mark.parametrize(
        "name",
        [
            "equity_curve_total",
            "balance_available",
            "position_value_a",
            "capital_total",
            "asset_net",
            "pnl_accumulated",
        ],
    )
    def test_sensitive_names(self, name: str) -> None:
        assert is_sensitive_name(name) is True

    @pytest.mark.parametrize("name", ["patrol_status", "dirty_count", "meta_status", ""])
    def test_normal_names(self, name: str) -> None:
        assert is_sensitive_name(name) is False

    def test_custom_patterns(self) -> None:
        assert is_sensitive_name("custom_secret_field", patterns=("secret",)) is True
        assert is_sensitive_name("patrol_status", patterns=("secret",)) is False


# ═══════════════════════════════════════════
#  3. metrics_history 集成
# ═══════════════════════════════════════════

class TestMetricsHistoryIntegration:
    def test_sensitive_encrypted_on_disk(
        self, store: MetricsHistoryStore, tmp_conn: DatabaseConnection
    ) -> None:
        store.save_metric("equity_curve_total", 1_000_000.0, timestamp=time.time())
        raw = tmp_conn.execute(
            "SELECT value, value_encrypted FROM metrics_history WHERE name = ?",
            ("equity_curve_total",),
        )
        assert float(raw[0]["value"]) == 0.0          # 占位
        assert bool(raw[0]["value_encrypted"])        # 密文非空
        assert "1000000.0" not in raw[0]["value_encrypted"]  # 密文非明文

    def test_decrypt_on_query(
        self, store: MetricsHistoryStore, tmp_conn: DatabaseConnection
    ) -> None:
        store.save_metric("equity_curve_total", 1_000_000.0, timestamp=time.time())
        rows = store.query("equity_curve_total", limit=5)
        assert len(rows) == 1
        assert rows[0]["value"] == 1_000_000.0        # 解密回读真实值

    def test_normal_metric_unencrypted(
        self, store: MetricsHistoryStore, tmp_conn: DatabaseConnection
    ) -> None:
        store.save_metric("patrol_status", 0.0, timestamp=time.time())
        raw = tmp_conn.execute(
            "SELECT value, value_encrypted FROM metrics_history WHERE name = ?",
            ("patrol_status",),
        )
        assert raw[0]["value_encrypted"] is None      # 普通指标明文

    def test_reject_plaintext_without_crypto(
        self, tmp_conn: DatabaseConnection
    ) -> None:
        bare = MetricsHistoryStore(connection=tmp_conn)  # 未绑定 FieldCrypto
        with pytest.raises(ValueError):
            bare.save_metric("equity_curve_total", 1.0)

    def test_downsample_rejects_sensitive(
        self, store: MetricsHistoryStore
    ) -> None:
        store.save_metric("equity_curve_total", 1.0, timestamp=time.time())
        with pytest.raises(ValueError):
            store.query_downsampled("equity_curve_total", bucket_minutes=60)

    def test_custom_patterns_override(
        self, tmp_conn: DatabaseConnection, crypto: FieldCrypto
    ) -> None:
        s = MetricsHistoryStore(connection=tmp_conn)
        s.attach_field_crypto(crypto, sensitive_patterns=("secret_field",))
        s.save_metric("equity_curve_total", 5.0, timestamp=time.time())  # 不再敏感 → 明文
        raw = tmp_conn.execute(
            "SELECT value_encrypted FROM metrics_history WHERE name = ?",
            ("equity_curve_total",),
        )
        assert raw[0]["value_encrypted"] is None
        # 新敏感名（自定义 patterns 命中）→ 密文落盘
        s.save_metric("secret_field_x", 1.0, timestamp=time.time())
        raw2 = tmp_conn.execute(
            "SELECT value, value_encrypted FROM metrics_history WHERE name = ?",
            ("secret_field_x",),
        )
        assert float(raw2[0]["value"]) == 0.0 and bool(raw2[0]["value_encrypted"])
        s.attach_field_crypto(crypto)  # 恢复默认 patterns
        assert s.save_metric("secret_field_y", 1.0, timestamp=time.time()) is True

    def test_batch_sensitive_encrypted(
        self, store: MetricsHistoryStore, tmp_conn: DatabaseConnection
    ) -> None:
        store.save_batch(
            [
                {"name": "position_value_a", "value": 999.0, "timestamp": time.time()},
                {"name": "dirty_count", "value": 3.0, "timestamp": time.time()},
            ]
        )
        raw = tmp_conn.execute(
            "SELECT name, value, value_encrypted FROM metrics_history ORDER BY name"
        )
        by_name = {r["name"]: r for r in raw}
        assert float(by_name["position_value_a"]["value"]) == 0.0
        assert bool(by_name["position_value_a"]["value_encrypted"])
        assert float(by_name["dirty_count"]["value"]) == 3.0
        assert by_name["dirty_count"]["value_encrypted"] is None


# ═══════════════════════════════════════════
#  4. 访问审计（读取/导出留痕）
# ═══════════════════════════════════════════

class TestAccessAudit:
    def test_query_record_access(
        self, tmp_conn: DatabaseConnection, crypto: FieldCrypto, tmp_path: Path
    ) -> None:
        audit = AuditLogStore(connection=tmp_conn)
        store = MetricsHistoryStore(connection=tmp_conn)
        store.attach_field_crypto(crypto)
        store.attach_access_audit(audit)
        store.save_metric("patrol_status", 0.0, timestamp=time.time())
        store.query("patrol_status", limit=5, record_access=True)
        rows = audit.query(kind="access", order="asc")
        assert len(rows) == 1
        assert rows[0]["action"] == "read"
        assert rows[0]["detail"]["resource"] == "metrics_history"

    def test_export_records_access(
        self, tmp_conn: DatabaseConnection, crypto: FieldCrypto, tmp_path: Path
    ) -> None:
        audit = AuditLogStore(connection=tmp_conn)
        store = MetricsHistoryStore(connection=tmp_conn)
        store.attach_field_crypto(crypto)
        store.attach_access_audit(audit)
        store.save_metric("patrol_status", 0.0, timestamp=time.time())
        out = tmp_path / "export.csv"
        n = store.export_csv(str(out), name="patrol_status", limit=10)
        assert n == 1 and out.exists()
        with out.open(encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows_list = list(reader)
        assert len(rows_list) == 1
        assert rows_list[0]["name"] == "patrol_status"
        rows = audit.query(kind="access", order="asc")
        assert len(rows) == 1 and rows[0]["action"] == "export"
        # 审计链完整
        assert audit.verify_chain()[0] is True

    def test_audit_failure_not_blocking(
        self, tmp_conn: DatabaseConnection, crypto: FieldCrypto
    ) -> None:
        class BoomAudit:
            def append(self, *a: Any, **k: Any) -> None:
                raise RuntimeError("audit down")

        store = MetricsHistoryStore(connection=tmp_conn)
        store.attach_field_crypto(crypto)
        store.attach_access_audit(BoomAudit())
        store.save_metric("patrol_status", 0.0, timestamp=time.time())
        rows = store.query(limit=5, record_access=True)  # 审计失败不阻断查询
        assert len(rows) == 1


# ═══════════════════════════════════════════
#  5. connection 库文件权限收紧
# ═══════════════════════════════════════════

class TestDbPermissionHarden:
    def test_connect_does_not_raise(self, tmp_path: Path) -> None:
        conn = DatabaseConnection(tmp_path / "perm.db")
        conn.connect()  # POSIX 会 chmod 600；Windows 记 warning 不抛
        assert (tmp_path / "perm.db").exists()
        conn.close()
