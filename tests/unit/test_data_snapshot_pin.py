# -*- coding: utf-8 -*-
# 文件名：tests/unit/test_data_snapshot_pin.py
"""
P1-12 数据快照 pin 单元测试
==========================================================
覆盖：
  - provider.compute_data_snapshot：目录文件内容哈希（L0 纯函数，tmp_path 隔离）
  - provider.verify_sidecar：首次自动 pin / 一致通过 / 新增或改文件不一致 /
    sidecar 损坏按不一致 fail-closed / 模块缓存 + reset 隔离
  - checks.data_pit：applicable=True 下指纹缺失 / 不一致 → critical；
    applicable=False（内存预加载 / live）跳过不判
  - RealismManifest._derive_status：applicable 且指纹 None → unverified
"""
from __future__ import annotations

from pathlib import Path

from myquant.data_access.provider import (
    SNAPSHOT_SIDECAR_NAME,
    compute_data_snapshot,
    reset_data_snapshot,
    verify_sidecar,
)
from myquant.realism_gate import RealismManifest
from myquant.realism_gate.checks.data_pit import check_data_pit


def _make_data_dir(base: Path, names: dict) -> Path:
    """在 base 下写假数据文件（{文件名: 字节内容}），返回 base。

    compute_data_snapshot 只看后缀 + 内容哈希，不解析 parquet 格式，
    故写任意字节即可。
    """
    for name, content in names.items():
        (base / name).write_bytes(content)
    return base


class TestComputeSnapshot:
    """compute_data_snapshot L0 纯函数。"""

    def test_empty_dir_zero_count(self, tmp_path: Path) -> None:
        """空目录 file_count=0，但指纹仍稳定可算。"""
        snap = compute_data_snapshot(tmp_path)
        assert snap["file_count"] == 0
        assert snap["fingerprint_hash"]

    def test_hash_stable_and_changes_on_content(self, tmp_path: Path) -> None:
        """同内容哈希稳定；改文件内容后指纹变化。"""
        _make_data_dir(tmp_path, {"A.parquet": b"x" * 100, "B.csv": b"yy"})
        s1 = compute_data_snapshot(tmp_path)
        s2 = compute_data_snapshot(tmp_path)
        assert s1["fingerprint_hash"] == s2["fingerprint_hash"]
        assert s1["file_count"] == 2
        _make_data_dir(tmp_path, {"A.parquet": b"z" * 100})
        s3 = compute_data_snapshot(tmp_path)
        assert s3["fingerprint_hash"] != s1["fingerprint_hash"]

    def test_ignores_non_data_files(self, tmp_path: Path) -> None:
        """只收 .parquet/.csv，忽略 md/tmp 等其他文件。"""
        _make_data_dir(tmp_path, {"A.parquet": b"x", "readme.md": b"#", ".tmp": b"t"})
        snap = compute_data_snapshot(tmp_path)
        assert snap["file_count"] == 1

    def test_snapshot_id_is_short_hash(self, tmp_path: Path) -> None:
        """snapshot_id 为 fingerprint_hash 前 12 字符。"""
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        snap = compute_data_snapshot(tmp_path)
        assert snap["snapshot_id"] == snap["fingerprint_hash"][:12]


class TestSidecarVerify:
    """verify_sidecar 首次 pin / 一致 / 不一致 / 损坏 / 缓存隔离。"""

    def test_first_pin_auto_writes(self, tmp_path: Path) -> None:
        """sidecar 不存在 → 首次自动 pin（first_pin=True，matched=True）。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        result = verify_sidecar(tmp_path)
        assert result["first_pin"] is True
        assert result["matched"] is True
        assert (tmp_path / SNAPSHOT_SIDECAR_NAME).exists()

    def test_second_run_matched(self, tmp_path: Path) -> None:
        """sidecar 已存在且内容未变 → matched=True、first_pin=False。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        verify_sidecar(tmp_path)  # 首次 pin（写 sidecar）
        reset_data_snapshot(tmp_path)  # 改目录后重扫（缓存是性能优化，须 reset）
        result = verify_sidecar(tmp_path)
        assert result["first_pin"] is False
        assert result["matched"] is True

    def test_mismatch_on_new_file(self, tmp_path: Path) -> None:
        """pin 后新增数据文件 → matched=False，diff.added 含新文件名。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        verify_sidecar(tmp_path)
        _make_data_dir(tmp_path, {"B.parquet": b"new"})
        reset_data_snapshot(tmp_path)  # 目录变了，重扫对比 sidecar 基线
        result = verify_sidecar(tmp_path)
        assert result["matched"] is False
        assert "B.parquet" in result["diff"]["added"]

    def test_mismatch_on_modified_file(self, tmp_path: Path) -> None:
        """pin 后改某文件内容 → matched=False，diff.changed 含该文件。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"old"})
        verify_sidecar(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"changed-content"})
        reset_data_snapshot(tmp_path)
        result = verify_sidecar(tmp_path)
        assert result["matched"] is False
        assert "A.parquet" in result["diff"]["changed"]

    def test_corrupt_sidecar_mismatch(self, tmp_path: Path) -> None:
        """sidecar 损坏（非法 JSON）→ 按不一致 fail-closed，不静默当基线。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        verify_sidecar(tmp_path)
        (tmp_path / SNAPSHOT_SIDECAR_NAME).write_text("{not json", encoding="utf-8")
        reset_data_snapshot(tmp_path)
        result = verify_sidecar(tmp_path)
        assert result["matched"] is False

    def test_cache_hit_then_reset_detects_change(self, tmp_path: Path) -> None:
        """模块缓存：同进程同目录不重复全量哈希；reset 后重扫才能发现新改动。"""
        reset_data_snapshot(tmp_path)
        _make_data_dir(tmp_path, {"A.parquet": b"x"})
        verify_sidecar(tmp_path)
        _make_data_dir(tmp_path, {"B.parquet": b"new"})
        # 未 reset：缓存命中（仍按首次基线判 matched）
        cached = verify_sidecar(tmp_path)
        assert cached["matched"] is True
        # reset 缓存后重扫：发现新增文件
        reset_data_snapshot(tmp_path)
        fresh = verify_sidecar(tmp_path)
        assert fresh["matched"] is False


class TestDataPitSnapshot:
    """data_pit 指纹判据（P1-12 恢复 #346 占位）。"""

    @staticmethod
    def _manifest(applicable: bool, fp: object = None, mismatch: bool = False) -> RealismManifest:
        m = RealismManifest()
        m.set_field("data.snapshot_applicable", applicable)
        m.set_field("fingerprint_hash", fp)
        m.set_field("data.snapshot_mismatch", mismatch)
        return m

    def test_applicable_fp_none_critical(self) -> None:
        """快照适用但指纹缺失 → critical 违规。"""
        violations = check_data_pit(self._manifest(applicable=True, fp=None))
        assert any(v.field_path == "fingerprint_hash" and v.critical for v in violations)

    def test_applicable_mismatch_critical(self) -> None:
        """快照适用且校验不一致 → critical 违规。"""
        violations = check_data_pit(self._manifest(applicable=True, fp="abc", mismatch=True))
        assert any(
            v.field_path == "data.snapshot_mismatch" and v.critical for v in violations
        )

    def test_not_applicable_skipped(self) -> None:
        """不适用目录快照（内存预加载 / live）：即使指纹缺失 + 不一致也不判。"""
        violations = check_data_pit(self._manifest(applicable=False, fp=None, mismatch=True))
        assert not any(
            v.field_path in ("fingerprint_hash", "data.snapshot_mismatch") for v in violations
        )


class TestStatusDerive:
    """manifest._derive_status：快照适用 + 指纹缺失 → unverified（方案 §4.5）。"""

    def test_unverified_when_applicable_and_fp_none(self) -> None:
        m = RealismManifest()
        m.set_field("data.snapshot_applicable", True)
        m.set_field("fingerprint_hash", None)
        assert m._derive_status() == "unverified"

    def test_ok_when_applicable_with_fp(self) -> None:
        m = RealismManifest()
        m.set_field("data.snapshot_applicable", True)
        m.set_field("fingerprint_hash", "abc")
        assert m._derive_status() == "ok"
