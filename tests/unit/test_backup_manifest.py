# 文件名：tests/unit/test_backup_manifest.py
"""
test_backup_manifest.py - 备份与恢复测试（KP-P5-06 / T-32）
=================================================================
覆盖修复：
  1. backup_files 备份内写 MANIFEST.sha256（目录与 zip 两种模式）。
  2. verify_backup 校验 MANIFEST 完整性（篡改/缺失文件判失败）。
  3. restore_backup 按 MANIFEST 还原。
  4. collect_backup_sources 默认源扩到 db/config/state。
  5. cleanup_old_backups 白名单（.keep 标记备份不清理）。

依赖: pytest, myquant.utils.file_utils, scripts.结果备份
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from myquant.utils.file_utils import (
    backup_files,
    restore_backup,
    verify_backup,
)


def _make_source(root: Path) -> tuple[Path, str]:
    out = root / "outputs"
    out.mkdir(parents=True)
    (out / "report.csv").write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    return out, "outputs"


class TestBackupManifest:
    def test_目录模式写MANIFEST(self, tmp_path) -> None:
        """DoD：backup_files 目录模式备份内含 MANIFEST.sha256 且可校验通过。"""
        src, rel = _make_source(tmp_path / "src")
        res = backup_files([(src, rel)], tmp_path / "bk", "backup_test")
        assert res["mode"] == "dir"
        manifest = Path(res["path"]) / "MANIFEST.sha256"
        assert manifest.exists()
        v = verify_backup(res["path"])
        assert v["ok"] and v["checked"] >= 1

    def test_zip模式写MANIFEST(self, tmp_path) -> None:
        """DoD：backup_files zip 模式 zip 内含 MANIFEST.sha256 且可校验通过。"""
        src, rel = _make_source(tmp_path / "src")
        res = backup_files([(src, rel)], tmp_path / "bk", "backup_test", compress=True)
        assert res["mode"] == "zip"
        with zipfile.ZipFile(res["path"]) as zf:
            assert any(n.endswith("MANIFEST.sha256") for n in zf.namelist())
        v = verify_backup(res["path"])
        assert v["ok"] and v["checked"] >= 1

    def test_篡改判失败(self, tmp_path) -> None:
        """DoD：篡改备份文件后 verify_backup 判失败。"""
        src, rel = _make_source(tmp_path / "src")
        res = backup_files([(src, rel)], tmp_path / "bk", "backup_test")
        # 篡改备份内文件
        target = next((Path(res["path"]).rglob("*.csv")))
        target.write_text("tampered", encoding="utf-8")
        v = verify_backup(res["path"])
        assert not v["ok"]
        assert any(m["reason"] == "哈希不一致" for m in v["mismatches"])

    def test_restore还原(self, tmp_path) -> None:
        """DoD：restore_backup 按 MANIFEST 还原到目标根。"""
        src, rel = _make_source(tmp_path / "src")
        res = backup_files([(src, rel)], tmp_path / "bk", "backup_test")
        target = tmp_path / "restored"
        r = restore_backup(res["path"], target)
        assert r["ok"] and r["restored"] >= 1
        assert (target / "outputs" / "report.csv").read_text(encoding="utf-8").startswith("a,b,c")


class TestBackupScriptSources:
    def test_默认源含db_config_state(self, tmp_path, monkeypatch) -> None:
        """DoD：collect_backup_sources 默认纳入 database/config/实盘状态/实盘信号。"""
        import sys
        proj = tmp_path / "proj"
        (proj / "outputs").mkdir(parents=True)
        (proj / "outputs" / "x.csv").write_text("x", encoding="utf-8")
        (proj / "database").mkdir(parents=True)
        (proj / "database" / "quant.db").write_bytes(b"db")
        (proj / "config").mkdir()
        (proj / "config" / "settings.yaml").write_text("k: v", encoding="utf-8")
        (proj / "config" / "settings.example.yaml").write_text("k: v", encoding="utf-8")
        (proj / "实盘状态").mkdir()
        (proj / "实盘信号").mkdir()
        sys.path.insert(0, str(proj.parent))
        import scripts.结果备份 as sb  # type: ignore[import-not-found]
        sources = sb.collect_backup_sources(proj, include_db=False, include_config=False, all_mode=False)
        rels = {rel for _, rel in sources}
        assert "database/quant.db" in rels
        assert any(rel.startswith("config/") for rel in rels)
        assert "实盘状态" in rels
        assert "实盘信号" in rels
        # example 模板不备份
        assert not any("example" in rel for rel in rels)

    def test_清理白名单(self, tmp_path) -> None:
        """DoD：带 .keep 锁定标记的备份不被清理。"""
        import sys
        sys.path.insert(0, str(tmp_path.parent))
        import scripts.结果备份 as sb  # type: ignore[import-not-found]
        bd = tmp_path / "bk"
        bd.mkdir()
        now = datetime.now().timestamp()
        locked = bd / "backup_locked"
        locked.mkdir()
        (locked / ".keep").write_text("", encoding="utf-8")
        old = bd / "backup_old"
        old.mkdir()
        import os
        for d in (locked, old):
            os.utime(d, (now - 1000, now - 1000))
        deleted = sb.cleanup_old_backups(bd, keep=0)
        assert deleted == 1
        assert locked.exists()  # 锁定备份保留
        assert not old.exists()  # 未锁定备份被清理
