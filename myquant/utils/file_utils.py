# 文件名：myquant/utils/file_utils.py
"""
file_utils.py - 路径处理、文件读写、压缩解压
=============================================
功能清单:
  1. 项目根路径自动探测
  2. 目录创建 / 清理
  3. JSON / CSV / TXT / Parquet 读写
  4. 压缩解压 (zip / gzip / tar.gz)
  5. 文件锁 (跨进程安全)
  6. 原子写入 (防止写一半崩溃)
  7. 文件哈希校验

依赖: os, pathlib, json, csv, gzip, zipfile, tarfile, hashlib, tempfile
可选: pandas (CSV/Parquet), fcntl/portalocker (文件锁)
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ─────────────────────────────────────────────
# 项目根路径探测
# ─────────────────────────────────────────────

def find_project_root(marker: str = "requirements.txt", max_depth: int = 5) -> Path:
    """
    从当前文件向上搜索项目根目录
    通过 marker 文件识别 (默认 requirements.txt)

    Returns
    -------
    Path : 项目根目录

    Raises
    ------
    FileNotFoundError : 未找到标记文件
    """
    current = Path(__file__).resolve().parent
    for _ in range(max_depth):
        if (current / marker).exists():
            return current
        if current == current.parent:
            break
        current = current.parent
    # fallback: 当前工作目录
    return Path.cwd()


PROJECT_ROOT = find_project_root()


# ─────────────────────────────────────────────
# 目录操作
# ─────────────────────────────────────────────

def ensure_dir(path: Union[str, Path]) -> Path:
    """确保目录存在，不存在则递归创建"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def clean_dir(path: Union[str, Path], pattern: str = "*") -> int:
    """
    清空目录下匹配文件 (保留子目录结构可选)
    返回删除文件数
    """
    p = Path(path)
    if not p.exists():
        return 0
    count = 0
    for f in p.glob(pattern):
        if f.is_file():
            f.unlink()
            count += 1
    return count


def get_project_path(*parts: str) -> Path:
    """基于项目根拼接路径"""
    return PROJECT_ROOT.joinpath(*parts)


# ─────────────────────────────────────────────
# 文件读写
# ─────────────────────────────────────────────

def read_text(path: Union[str, Path], encoding: str = "utf-8") -> str:
    """读取文本文件"""
    return Path(path).read_text(encoding=encoding)


def write_text(
    path: Union[str, Path],
    content: str,
    encoding: str = "utf-8",
    atomic: bool = True,
) -> None:
    """
    写入文本文件
    atomic=True 时先写临时文件再 rename，防止写入中断导致数据损坏
    """
    p = Path(path)
    ensure_dir(p.parent)
    if atomic:
        fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding=encoding) as f:
                f.write(content)
            shutil.move(tmp_path, str(p))
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    else:
        p.write_text(content, encoding=encoding)


def read_json(path: Union[str, Path]) -> Any:
    """读取JSON文件"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(
    path: Union[str, Path],
    data: Any,
    indent: int = 2,
    ensure_ascii: bool = False,
    atomic: bool = True,
) -> None:
    """写入JSON文件"""
    content = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii, default=str)
    write_text(path, content, atomic=atomic)


def read_csv(
    path: Union[str, Path],
    encoding: str = "utf-8",
    has_header: bool = True,
) -> List[Dict[str, str]]:
    """
    读取CSV为字典列表
    大文件场景建议直接用 pandas.read_csv
    """
    with open(path, "r", encoding=encoding, newline="") as f:
        if has_header:
            reader = csv.DictReader(f)
        else:
            reader = csv.reader(f)
            return [row for row in reader]
        return [row for row in reader]


def write_csv(
    path: Union[str, Path],
    data: List[Dict[str, Any]],
    fieldnames: Optional[List[str]] = None,
    encoding: str = "utf-8",
) -> None:
    """字典列表写入CSV"""
    if not data:
        write_text(path, "", encoding=encoding)
        return
    if fieldnames is None:
        fieldnames = list(data[0].keys())
    ensure_dir(Path(path).parent)
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)


def read_csv_pandas(path: Union[str, Path], **kwargs):
    """
    使用pandas读取CSV (需安装pandas)
    支持所有 pd.read_csv 参数透传
    """
    import pandas as pd
    return pd.read_csv(path, **kwargs)


def write_csv_pandas(path: Union[str, Path], df, **kwargs) -> None:
    """使用pandas写入CSV"""
    ensure_dir(Path(path).parent)
    df.to_csv(path, **kwargs)


def read_parquet(path: Union[str, Path]):
    """读取Parquet文件 (需 pyarrow 或 fastparquet)"""
    import pandas as pd
    return pd.read_parquet(path)


def write_parquet(path: Union[str, Path], df, **kwargs) -> None:
    """写入Parquet文件"""
    ensure_dir(Path(path).parent)
    df.to_parquet(path, **kwargs)


# ─────────────────────────────────────────────
# 压缩 / 解压
# ─────────────────────────────────────────────

def compress_zip(
    source: Union[str, Path],
    output: Union[str, Path],
    include_dir: bool = False,
) -> Path:
    """
    压缩文件或目录为 .zip

    Parameters
    ----------
    source : 待压缩的文件或目录
    output : 输出zip路径
    include_dir : True=包含顶层目录名, False=直接打包内容
    """
    source = Path(source)
    output = Path(output)
    ensure_dir(output.parent)

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        if source.is_file():
            zf.write(source, source.name)
        elif source.is_dir():
            if include_dir:
                for f in source.rglob("*"):
                    if f.is_file():
                        arcname = f.relative_to(source.parent)
                        zf.write(f, arcname)
            else:
                for f in source.rglob("*"):
                    if f.is_file():
                        arcname = f.relative_to(source)
                        zf.write(f, arcname)
    return output


def decompress_zip(
    source: Union[str, Path],
    output_dir: Union[str, Path],
) -> Path:
    """解压 .zip"""
    output_dir = Path(output_dir)
    ensure_dir(output_dir)
    with zipfile.ZipFile(source, "r") as zf:
        zf.extractall(output_dir)
    return output_dir


def compress_gzip(
    source: Union[str, Path],
    output: Optional[Union[str, Path]] = None,
) -> Path:
    """单文件 gzip 压缩"""
    source = Path(source)
    if output is None:
        output = source.with_suffix(source.suffix + ".gz")
    output = Path(output)
    ensure_dir(output.parent)
    with open(source, "rb") as f_in:
        with gzip.open(output, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    return output


def decompress_gzip(
    source: Union[str, Path],
    output: Optional[Union[str, Path]] = None,
) -> Path:
    """单文件 gzip 解压"""
    source = Path(source)
    if output is None:
        output = source.with_suffix("")  # 去掉 .gz
    output = Path(output)
    ensure_dir(output.parent)
    with gzip.open(source, "rb") as f_in:
        with open(output, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    return output


def compress_tar(
    source: Union[str, Path],
    output: Union[str, Path],
    mode: str = "gz",
) -> Path:
    """压缩为 .tar.gz / .tar.bz2 / .tar.xz"""
    source = Path(source)
    output = Path(output)
    ensure_dir(output.parent)
    with tarfile.open(output, f"w:{mode}") as tf:
        tf.add(source, arcname=source.name)
    return output


def decompress_tar(
    source: Union[str, Path],
    output_dir: Union[str, Path],
) -> Path:
    """解压 tar 包"""
    output_dir = Path(output_dir)
    ensure_dir(output_dir)
    with tarfile.open(source, "r:*") as tf:
        tf.extractall(output_dir)
    return output_dir


# ─────────────────────────────────────────────
# 备份（L2修复：结果备份.py 内置逻辑下沉 myquant）
# ─────────────────────────────────────────────

def backup_files(
    sources: List[tuple],
    output_dir: Union[str, Path],
    backup_name: str,
    compress: bool = False,
) -> Dict[str, Any]:
    """
    备份文件/目录列表到输出目录（zip 或目录复制）

    供 scripts/结果备份.py 等薄入口调用（L2修复：原内置逻辑下沉，
    消除"引用不存在的 backup_files"断链）。

    Parameters
    ----------
    sources : List[(Path, str)]
        (源路径, 归档相对路径) 列表
    output_dir : 输出根目录（自动创建）
    backup_name : 备份名（目录名或 zip 文件名前缀）
    compress : True=打包 zip，False=目录复制

    Returns
    -------
    Dict[str, Any] : {"path": 备份路径, "size": 字节数, "mode": "zip"|"dir"}
    """
    output_dir = Path(output_dir)
    ensure_dir(output_dir)
    if compress:
        zip_path = output_dir / f"{backup_name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, rel_path in sources:
                src = Path(src)
                if src.is_dir():
                    for file_path in src.rglob("*"):
                        if file_path.is_file():
                            arcname = f"{backup_name}/{rel_path}/{file_path.relative_to(src)}"
                            zf.write(file_path, arcname)
                else:
                    zf.write(src, f"{backup_name}/{rel_path}")
        # KP-P5-06: 备份内写 MANIFEST.sha256（每文件 sha256，供还原比对）
        manifest_lines: List[str] = []
        for src, rel_path in sources:
            src = Path(src)
            if src.is_dir():
                for file_path in src.rglob("*"):
                    if file_path.is_file():
                        rel = f"{rel_path}/{file_path.relative_to(src).as_posix()}"
                        manifest_lines.append(f"{file_sha256(file_path)}  {rel}")
            else:
                manifest_lines.append(f"{file_sha256(src)}  {rel_path}")
        with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"{backup_name}/MANIFEST.sha256", "\n".join(sorted(manifest_lines)))
        return {"path": zip_path, "size": zip_path.stat().st_size, "mode": "zip"}
    dest_dir = output_dir / backup_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for src, rel_path in sources:
        src = Path(src)
        dest = dest_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
            total += sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        else:
            shutil.copy2(src, dest)
            total += dest.stat().st_size
    # KP-P5-06: 备份内写 MANIFEST.sha256（每文件 sha256，供还原比对）
    _write_manifest(dest_dir)
    return {"path": dest_dir, "size": total, "mode": "dir"}

# ─────────────────────────────────────────────
# 备份 MANIFEST / 校验 / 还原（KP-P5-06）
# ─────────────────────────────────────────────

def _write_manifest(backup_dir: Path) -> None:
    """写备份目录 MANIFEST.sha256（备份内每文件 sha256）。"""
    lines: List[str] = []
    for f in sorted(backup_dir.rglob("*")):
        if f.is_file() and f.name != "MANIFEST.sha256":
            rel = f.relative_to(backup_dir).as_posix()
            lines.append(f"{file_sha256(f)}  {rel}")
    manifest_path = backup_dir / "MANIFEST.sha256"
    manifest_path.write_text("\n".join(lines), encoding="utf-8")


def _manifest_digests(manifest_path: Path) -> List[tuple]:
    """解析 MANIFEST.sha256 为 [(sha256, rel), ...]。"""
    entries: List[tuple] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition("  ")
        if digest and rel:
            entries.append((digest, rel))
    return entries


def verify_backup(backup_path: Union[str, Path]) -> Dict[str, Any]:
    """校验备份完整性：读 MANIFEST.sha256 逐文件比对 sha256（抽样还原比对）。

    Parameters
    ----------
    backup_path : 备份目录（含 MANIFEST.sha256）或 zip 文件。

    Returns
    -------
    Dict : {"ok": bool, "checked": int, "mismatches": [{"path", "reason"}]}
    """
    backup_path = Path(backup_path)
    mismatches: List[Dict[str, str]] = []
    checked = 0
    if backup_path.is_dir():
        manifest = backup_path / "MANIFEST.sha256"
        if not manifest.exists():
            return {"ok": False, "checked": 0, "mismatches": [{"path": "MANIFEST.sha256", "reason": "缺失"}]}
        for digest, rel in _manifest_digests(manifest):
            f = backup_path / rel
            if not f.is_file():
                mismatches.append({"path": rel, "reason": "文件缺失"})
            elif file_sha256(f) != digest:
                mismatches.append({"path": rel, "reason": "哈希不一致"})
            checked += 1
    elif backup_path.is_file() and zipfile.is_zipfile(backup_path):
        with zipfile.ZipFile(backup_path) as zf:
            names = zf.namelist()
            mrel = next((n for n in names if n.endswith("MANIFEST.sha256")), None)
            if mrel is None:
                return {"ok": False, "checked": 0, "mismatches": [{"path": "MANIFEST.sha256", "reason": "缺失"}]}
            base = mrel[: -len("MANIFEST.sha256")]
            for digest, rel in _manifest_digests_from_text(zf.read(mrel).decode("utf-8")):
                arc = f"{base}{rel}"
                if arc not in names:
                    mismatches.append({"path": rel, "reason": "文件缺失"})
                elif hashlib.sha256(zf.read(arc)).hexdigest() != digest:
                    mismatches.append({"path": rel, "reason": "哈希不一致"})
                checked += 1
    else:
        return {"ok": False, "checked": 0, "mismatches": [{"path": str(backup_path), "reason": "非备份目录/zip"}]}
    return {"ok": not mismatches, "checked": checked, "mismatches": mismatches}


def _manifest_digests_from_text(text: str) -> List[tuple]:
    """从 MANIFEST 文本解析 [(sha256, rel), ...]。"""
    entries: List[tuple] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition("  ")
        if digest and rel:
            entries.append((digest, rel))
    return entries


def restore_backup(backup_path: Union[str, Path], target_root: Union[str, Path]) -> Dict[str, Any]:
    """按 MANIFEST.sha256 将备份还原到目标根目录（相对路径回写）。

    Parameters
    ----------
    backup_path : 备份目录或 zip 文件。
    target_root : 还原目标根目录（如项目根）。

    Returns
    -------
    Dict : {"ok": bool, "restored": int, "errors": [str]}
    """
    backup_path = Path(backup_path)
    target_root = Path(target_root)
    target_root.mkdir(parents=True, exist_ok=True)
    restored = 0
    errors: List[str] = []
    if backup_path.is_dir():
        manifest = backup_path / "MANIFEST.sha256"
        if not manifest.exists():
            return {"ok": False, "restored": 0, "errors": ["MANIFEST.sha256 缺失"]}
        for digest, rel in _manifest_digests(manifest):
            src = backup_path / rel
            dst = target_root / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                restored += 1
            except Exception as e:
                errors.append(f"{rel}: {e}")
    elif backup_path.is_file() and zipfile.is_zipfile(backup_path):
        with zipfile.ZipFile(backup_path) as zf:
            mrel = next((n for n in zf.namelist() if n.endswith("MANIFEST.sha256")), None)
            if mrel is None:
                return {"ok": False, "restored": 0, "errors": ["MANIFEST.sha256 缺失"]}
            base = mrel[: -len("MANIFEST.sha256")]
            for digest, rel in _manifest_digests_from_text(zf.read(mrel).decode("utf-8")):
                arc = f"{base}{rel}"
                if arc not in zf.namelist():
                    errors.append(f"{rel}: zip 内缺失")
                    continue
                dst = target_root / rel
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(zf.read(arc))
                    restored += 1
                except Exception as e:
                    errors.append(f"{rel}: {e}")
    else:
        return {"ok": False, "restored": 0, "errors": ["非备份目录/zip"]}
    return {"ok": not errors, "restored": restored, "errors": errors}


# ─────────────────────────────────────────────
# 文件哈希 / 校验
# ─────────────────────────────────────────────

def file_md5(path: Union[str, Path], chunk_size: int = 8192) -> str:
    """计算文件MD5"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(path: Union[str, Path], chunk_size: int = 8192) -> str:
    """计算文件SHA256"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────
# 文件锁 (跨进程)
# ─────────────────────────────────────────────

@contextmanager
def file_lock(lock_path: Union[str, Path], timeout: float = 10.0):
    """
    跨进程文件锁 (基于 fcntl / msvcrt)
    用于防止多进程同时写同一文件

    Usage:
        with file_lock("/tmp/my.lock"):
            # 安全写入...
    """
    import sys
    lock_path = Path(lock_path)
    ensure_dir(lock_path.parent)

    f = open(lock_path, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield f
    finally:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


# ─────────────────────────────────────────────
# FileUtils 统一入口类
# ─────────────────────────────────────────────

class FileUtils:
    """
    文件操作统一封装类
    提供链式 / 面向对象风格调用

    Usage:
        fu = FileUtils(base_dir="./data")
        fu.save_json({"key": "val"}, "config/test.json")
        data = fu.load_json("config/test.json")
        fu.compress("raw/", "backup/raw_20250101.zip")
    """

    def __init__(self, base_dir: Union[str, Path] = "."):
        self.base = Path(base_dir).resolve()
        ensure_dir(self.base)

    def _resolve(self, rel_path: str) -> Path:
        return self.base / rel_path

    # ── 读写 ──
    def save_json(self, data: Any, rel_path: str, **kw) -> Path:
        p = self._resolve(rel_path)
        write_json(p, data, **kw)
        return p

    def load_json(self, rel_path: str) -> Any:
        return read_json(self._resolve(rel_path))

    def save_csv(self, data: List[Dict], rel_path: str, **kw) -> Path:
        p = self._resolve(rel_path)
        write_csv(p, data, **kw)
        return p

    def load_csv(self, rel_path: str, **kw) -> List[Dict]:
        return read_csv(self._resolve(rel_path), **kw)

    def save_text(self, content: str, rel_path: str) -> Path:
        p = self._resolve(rel_path)
        write_text(p, content)
        return p

    def load_text(self, rel_path: str) -> str:
        return read_text(self._resolve(rel_path))

    # ── 目录 ──
    def ensure(self, rel_path: str) -> Path:
        return ensure_dir(self._resolve(rel_path))

    def list_files(self, rel_path: str = ".", pattern: str = "*") -> List[Path]:
        d = self._resolve(rel_path)
        if not d.exists():
            return []
        return sorted(d.glob(pattern))

    def file_exists(self, rel_path: str) -> bool:
        return self._resolve(rel_path).exists()

    def file_size(self, rel_path: str) -> int:
        """文件大小 (bytes)"""
        p = self._resolve(rel_path)
        return p.stat().st_size if p.exists() else 0

    # ── 压缩 ──
    def compress(self, rel_source: str, rel_output: str, fmt: str = "zip") -> Path:
        src = self._resolve(rel_source)
        out = self._resolve(rel_output)
        if fmt == "zip":
            return compress_zip(src, out)
        elif fmt in ("gz", "tar.gz"):
            return compress_tar(src, out, mode="gz")
        elif fmt == "gzip":
            return compress_gzip(src, out)
        raise ValueError(f"不支持的压缩格式: {fmt}")

    def decompress(self, rel_source: str, rel_output: str) -> Path:
        src = self._resolve(rel_source)
        out = self._resolve(rel_output)
        suffix = "".join(src.suffixes)
        if ".zip" in suffix:
            return decompress_zip(src, out)
        elif ".tar" in suffix:
            return decompress_tar(src, out)
        elif ".gz" in suffix:
            return decompress_gzip(src, out)
        raise ValueError(f"无法识别的压缩格式: {suffix}")

    # ── 哈希 ──
    def md5(self, rel_path: str) -> str:
        return file_md5(self._resolve(rel_path))

    def sha256(self, rel_path: str) -> str:
        return file_sha256(self._resolve(rel_path))

    def __repr__(self) -> str:
        return f"FileUtils(base='{self.base}')"


# ─────────────────────────────────────────────
# 自测
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  file_utils.py 自测")
    print("=" * 60)

    # 使用临时目录测试
    test_dir = Path(tempfile.mkdtemp(prefix="quant_test_"))
    print(f"\n[1] 测试目录: {test_dir}")

    fu = FileUtils(base_dir=test_dir)

    # 2) JSON 读写
    test_data = {"symbol": "600519", "price": 1800.5, "date": "2025-06-15"}
    fu.save_json(test_data, "test/data.json")
    loaded = fu.load_json("test/data.json")
    assert loaded == test_data, "JSON读写不一致"
    print(f"[2] JSON读写 ✓  {loaded}")

    # 3) CSV 读写
    csv_data = [
        {"date": "2025-01-02", "open": "10.5", "close": "10.8"},
        {"date": "2025-01-03", "open": "10.8", "close": "11.2"},
    ]
    fu.save_csv(csv_data, "test/prices.csv")
    loaded_csv = fu.load_csv("test/prices.csv")
    assert len(loaded_csv) == 2
    print(f"[3] CSV读写 ✓  {len(loaded_csv)} rows")

    # 4) 文本读写
    fu.save_text("Hello Quant!", "test/hello.txt")
    assert fu.load_text("test/hello.txt") == "Hello Quant!"
    print("[4] 文本读写 ✓")

    # 5) 压缩解压
    fu.compress("test/", "backup/test_backup.zip", fmt="zip")
    assert fu.file_exists("backup/test_backup.zip")
    fu.decompress("backup/test_backup.zip", "restored/")
    assert fu.file_exists("restored/data.json")
    print("[5] ZIP压缩/解压 ✓")

    # 6) 文件哈希
    md5_val = fu.md5("test/hello.txt")
    print(f"[6] MD5: {md5_val}")

    # 7) 文件列表
    files = fu.list_files("test/", "*")
    print(f"[7] test/ 下文件: {[f.name for f in files]}")

    # 清理
    shutil.rmtree(test_dir, ignore_errors=True)
    print(f"\n[8] 临时目录已清理")

    print("\n" + "=" * 60)
    print("  ✓ file_utils.py 自测完成")
    print("=" * 60)