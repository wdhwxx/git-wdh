# 文件名：myquant/monitoring/audit_log.py
"""
audit_log.py - 审计只追加存储（BASE-7 审计只追加与归档校验）
================================================================
依据《量化系统监控模块施工总清单_V1.2》BASE-7（§3.2）：
  - 审计日志只追加（append-only），定期归档并计算校验和，防篡改
    （个人系统降级版，非完整哈希链）
  - orders/fills/alerts 全落库留痕（本模块提供通用审计库 + AlertStore
    真实接入；orders/fills 属交易账，交易域落库时调 append(kind=...))
  - 验收：审计日志不可被常规改写；归档校验通过

降级哈希链设计：
  - audit_log 每行含 seq（链序号从 1 递增，UNIQUE）+ row_hash +
    prev_hash（上一条 row_hash，首条 64 个 '0'）
  - row_hash = SHA-256(kind|action|actor|detail_json|created_ts|seq)，
    规范化序列（detail_json 排序 key 序列化）
  - 业务层无 UPDATE/DELETE API（只读查询 + append-only 追加）：
    常规直接改库即断链，verify_chain() 按 seq 顺序重算比对可检出篡改位置
  - 归档：旧行按原 seq/prev_hash/row_hash 整体 COPY 到 audit_archive
    （哈希不改写，主链不可变），主表删除；manifest 记录归档批
    row_count/first_seq/last_seq/checksum（= 归档行 row_hash 按序聚合
    SHA-256），verify_archive() 重算聚合校验和比对 → 归档校验通过

防未来铁律：created_ts 一律取"记录时刻"（time.time() 当前时点），
只记录已发生的事件，不预测未来；审计写失败记 error 不阻断告警主流程。

依赖: json, time, hashlib, logging, typing, myquant.database.connection / schema
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

from myquant.database.connection import DatabaseConnection, get_connection
from myquant.database.schema import (
    CREATE_AUDIT_ARCHIVE_MANIFEST_SQL,
    CREATE_AUDIT_ARCHIVE_SQL,
    CREATE_AUDIT_LOG_INDEX_SQL,
    CREATE_AUDIT_LOG_SQL,
)

logger = logging.getLogger("myquant.monitoring.audit_log")


# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════

_GENESIS_HASH: str = "0" * 64        # 链首 prev_hash（64 个 '0'）
_MAX_DETAIL_JSON_BYTES: int = 8192   # detail 序列化大小上限（防超大审计行）
_QUERY_LIMIT_MAX: int = 10_000       # 查询条数上限（防误拉全表）


def _sha256_hex(text: str) -> str:
    """SHA-256 hex（哈希链 / 归档校验和基础）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AuditLogStore:
    """
    审计只追加存储（SQLite，降级哈希链，append-only）。

    Usage:
        audit = AuditLogStore()                     # 全局默认库
        audit.append("alert", "create", actor="scheduler", detail={"alert_id": "..."})
        ok, broken_seq, checked = audit.verify_chain()
        manifest = audit.archive(before_ts=time.time() - 30 * 86400)
        ok, expected, actual, n = audit.verify_archive(manifest["id"])
    """

    def __init__(
        self,
        db_path: Optional[Union[str, Any]] = None,
        connection: Optional[DatabaseConnection] = None,
    ):
        """
        Parameters
        ----------
        db_path : str | Path | None
            数据库路径；None 则用全局默认（项目根/database/quant.db）
        connection : DatabaseConnection | None
            注入连接（测试用临时库）；与 db_path 互斥，优先 connection
        """
        if connection is not None:
            self._conn: DatabaseConnection = connection
        else:
            self._conn = get_connection(db_path)
        self._ensure_schema()

    # ── 建表自检（幂等，与迁移 DDL 逐字一致）──

    def _ensure_schema(self) -> None:
        """确保审计三表与索引存在（幂等，启动/实例化时自检）。"""
        with self._conn.transaction():
            self._conn.execute_update(CREATE_AUDIT_LOG_SQL)
            for sql in CREATE_AUDIT_LOG_INDEX_SQL:
                self._conn.execute_update(sql)
            self._conn.execute_update(CREATE_AUDIT_ARCHIVE_SQL)
            self._conn.execute_update(CREATE_AUDIT_ARCHIVE_MANIFEST_SQL)
        logger.debug("审计表自检完成（CREATE IF NOT EXISTS）")

    # ── 哈希链核心 ──

    @staticmethod
    def _dump_detail(detail: Optional[Dict[str, Any]]) -> str:
        """detail → 排序 key JSON（规范化序列，保证同构 dict 哈希一致）。"""
        return json.dumps(detail or {}, sort_keys=True, ensure_ascii=False)

    @classmethod
    def _row_hash(
        cls,
        kind: str,
        action: str,
        actor: str,
        detail_json: str,
        created_ts: float,
        seq: int,
    ) -> str:
        """规范化序列 SHA-256（降级哈希链行指纹）。"""
        canonical = (
            f"{kind}|{action}|{actor}|{detail_json}|{created_ts!r}|{seq}"
        )
        return _sha256_hex(canonical)

    def _tail(self) -> tuple:
        """取链尾：当前最大 seq 及其 row_hash（空表为 (0, GENESIS_HASH)）。"""
        with self._conn.transaction():
            rows = self._conn.execute(
                "SELECT seq, row_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            )
        if not rows:
            return 0, _GENESIS_HASH
        return int(rows[0]["seq"]), str(rows[0]["row_hash"])

    # ── 写：只追加 ──

    def append(
        self,
        kind: str,
        action: str,
        actor: str = "system",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        追加一条审计记录（append-only，链尾衔接）。

        Parameters
        ----------
        kind : str
            审计类别（alert/order/fill/metric/config 等）
        action : str
            动作（create/update/ack/resolve/archive 等）
        actor : str
            操作者（系统组件名或用户标识）
        detail : dict | None
            明细（dict，排序 key 序列化；超大报错）

        Returns
        -------
        dict : {"seq", "row_hash", "prev_hash", "created_ts"}

        Raises
        ------
        ValueError: kind/action/actor 为空、detail 非 dict、detail 超限时抛出
        """
        kind_s = (kind or "").strip()
        if not kind_s:
            raise ValueError("审计追加失败: kind 不能为空")
        action_s = (action or "").strip()
        if not action_s:
            raise ValueError("审计追加失败: action 不能为空")
        actor_s = (actor or "").strip()
        if not actor_s:
            raise ValueError("审计追加失败: actor 不能为空")
        if detail is not None and not isinstance(detail, dict):
            raise ValueError(f"审计追加失败: detail 必须为 dict，收到 {type(detail).__name__}")
        detail_json = self._dump_detail(detail)
        if len(detail_json.encode("utf-8")) > _MAX_DETAIL_JSON_BYTES:
            raise ValueError(
                f"审计追加失败: detail 序列化超限 "
                f"({len(detail_json.encode('utf-8'))} > {_MAX_DETAIL_JSON_BYTES} 字节)"
            )

        created_ts = time.time()  # 记录时刻（当前时点，防未来）
        with self._conn.transaction():
            prev_seq, prev_hash = self._tail()
            seq = prev_seq + 1
            row_hash = self._row_hash(
                kind_s, action_s, actor_s, detail_json, created_ts, seq
            )
            self._conn.execute_update(
                """
                INSERT INTO audit_log
                (kind, action, actor, detail_json, seq, prev_hash, row_hash, created_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind_s, action_s, actor_s, detail_json, seq, prev_hash, row_hash, created_ts),
            )
        logger.debug(f"[audit] #{seq} {kind_s}/{action_s} actor={actor_s}")
        return {
            "seq": seq,
            "row_hash": row_hash,
            "prev_hash": prev_hash,
            "created_ts": created_ts,
        }

    def log_access(
        self,
        resource: str,
        action: str,
        actor: str = "system",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        监控库访问审计留痕（BASE-9：读取/导出纳入审计，kind=access）。

        Parameters
        ----------
        resource : str
            被访问资源（metrics_history/alerts/quant.db 等）
        action : str
            访问动作（read/export/query 等）
        actor : str
            访问者（组件名/用户标识）
        detail : dict | None
            访问明细（过滤条件/导出行数等）

        Returns
        -------
        dict : append 返回的 {seq/row_hash/prev_hash/created_ts}

        Raises
        ------
        ValueError: 参数非法时抛出（同 append 防御式校验）
        """
        return self.append(
            "access",
            action,
            actor=actor,
            detail={"resource": resource, **(detail or {})},
        )

    # ── 读：只读查询（无 update/delete API，append-only）──

    def query(
        self,
        kind: Optional[str] = None,
        actor: Optional[str] = None,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        limit: int = 1000,
        order: str = "desc",
    ) -> List[Dict[str, Any]]:
        """
        只读查询审计记录。

        Parameters
        ----------
        kind / actor : str | None
            精确过滤；None=不限
        start_ts / end_ts : float | None
            时间范围（epoch 秒，闭区间）
        limit : int
            条数上限（>0，<=10000）
        order : str
            "desc"=倒序（默认）；"asc"=升序

        Returns
        -------
        list[dict] : 每项 {id/kind/action/actor/detail/seq/prev_hash/row_hash/created_ts}
        """
        if limit <= 0:
            raise ValueError(f"limit 必须为正整数，收到 {limit}")
        if limit > _QUERY_LIMIT_MAX:
            raise ValueError(f"limit 超上限 {_QUERY_LIMIT_MAX}，收到 {limit}")
        if order not in ("asc", "desc"):
            raise ValueError(f"order 必须为 asc/desc，收到 {order!r}")

        clauses: List[str] = []
        params: List[Any] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        if start_ts is not None:
            clauses.append("created_ts >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("created_ts <= ?")
            params.append(float(end_ts))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT id, kind, action, actor, detail_json, seq, prev_hash, "
            f"row_hash, created_ts FROM audit_log{where}"
            f" ORDER BY seq {order} LIMIT ?"
        )
        params.append(limit)
        with self._conn.transaction():
            rows = self._conn.execute(sql, params)
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                detail = json.loads(r["detail_json"] or "{}")
            except ValueError:
                logger.error("审计行 detail_json 解析失败: seq=%s", r["seq"])
                detail = {}
            out.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "action": r["action"],
                    "actor": r["actor"],
                    "detail": detail,
                    "seq": r["seq"],
                    "prev_hash": r["prev_hash"],
                    "row_hash": r["row_hash"],
                    "created_ts": r["created_ts"],
                }
            )
        return out

    def count(self) -> int:
        """主表审计记录数。"""
        with self._conn.transaction():
            row = self._conn.execute("SELECT COUNT(*) AS c FROM audit_log")
        return int(row[0]["c"])

    # ── 篡改检测 ──

    def verify_chain(self) -> tuple:
        """
        校验哈希链完整性（防常规改写）。

        Returns
        -------
        tuple : (ok: bool, broken_seq: Optional[int], checked: int)
            ok=True 链完整；ok=False 返回首个断链 seq（篡改位置），
            checked 为已校验行数
        """
        with self._conn.transaction():
            rows = self._conn.execute(
                "SELECT seq, kind, action, actor, detail_json, created_ts, "
                "prev_hash, row_hash FROM audit_log ORDER BY seq ASC"
            )
        checked = 0
        expect_prev = _GENESIS_HASH
        for r in rows:
            seq = int(r["seq"])
            recomputed = self._row_hash(
                str(r["kind"]),
                str(r["action"]),
                str(r["actor"]),
                str(r["detail_json"]),
                float(r["created_ts"]),
                seq,
            )
            if recomputed != str(r["row_hash"]):
                return False, seq, checked  # 行内容被改
            if str(r["prev_hash"]) != expect_prev:
                return False, seq, checked  # 链条衔接被破坏（插入/删除）
            expect_prev = str(r["row_hash"])
            checked += 1
        return True, None, checked

    # ── 归档与校验 ──

    def archive(self, before_ts: float) -> Dict[str, Any]:
        """
        归档 created_ts < before_ts 的审计行（append-only 归档，主链不改写）。

        流程（单事务）：按 seq 序 COPY 旧行到 audit_archive（保留原
        seq/prev_hash/row_hash 哈希不改写）→ 写 manifest（row_count/
        first_seq/last_seq/checksum=归档行 row_hash 按序聚合 SHA-256）
        → 删除主表已归档行。主链剩余记录 prev_hash 衔接不受影响
        （被删除的是链头段）。

        Parameters
        ----------
        before_ts : float
            归档截止（epoch 秒，只归档该时刻之前的旧行；当前时点回溯）

        Returns
        -------
        dict : manifest {id/archive_cutoff_ts/row_count/first_seq/last_seq/checksum/created_ts}

        Raises
        ------
        ValueError: before_ts 非法
        """
        cutoff = float(before_ts)
        if cutoff != cutoff or cutoff in (float("inf"), float("-inf")):
            raise ValueError(f"before_ts 非法: {before_ts!r}")
        created_ts = time.time()  # 归档时刻（当前时点）
        with self._conn.transaction():
            old = self._conn.execute(
                "SELECT kind, action, actor, detail_json, seq, prev_hash, "
                "row_hash, created_ts FROM audit_log "
                "WHERE created_ts < ? ORDER BY seq ASC",
                (cutoff,),
            )
            if not old:
                return {
                    "id": None,
                    "archive_cutoff_ts": cutoff,
                    "row_count": 0,
                    "first_seq": None,
                    "last_seq": None,
                    "checksum": None,
                    "created_ts": created_ts,
                }
            seqs = [int(r["seq"]) for r in old]
            row_hashes = [str(r["row_hash"]) for r in old]
            self._conn.executemany(
                """
                INSERT INTO audit_archive
                (kind, action, actor, detail_json, seq, prev_hash, row_hash, created_ts, archived_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(r["kind"]),
                        str(r["action"]),
                        str(r["actor"]),
                        str(r["detail_json"]),
                        int(r["seq"]),
                        str(r["prev_hash"]),
                        str(r["row_hash"]),
                        float(r["created_ts"]),
                        created_ts,
                    )
                    for r in old
                ],
            )
            checksum = _sha256_hex("|".join(row_hashes))  # 归档聚合校验和
            self._conn.execute_update(
                """
                INSERT INTO audit_archive_manifest
                (archive_cutoff_ts, row_count, first_seq, last_seq, checksum, created_ts)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (cutoff, len(seqs), seqs[0], seqs[-1], checksum, created_ts),
            )
            manifest_id = int(
                self._conn.execute(
                    "SELECT id FROM audit_archive_manifest ORDER BY id DESC LIMIT 1"
                )[0]["id"]
            )
            # 只删主表已归档行（链头段；剩余链不受影响）
            self._conn.execute_update(
                "DELETE FROM audit_log WHERE created_ts < ?",
                (cutoff,),
            )
        logger.info(
            f"[audit] 归档完成: {len(seqs)} 行 (seq {seqs[0]}~{seqs[-1]}), "
            f"checksum={checksum[:12]}..."
        )
        return {
            "id": manifest_id,
            "archive_cutoff_ts": cutoff,
            "row_count": len(seqs),
            "first_seq": seqs[0],
            "last_seq": seqs[-1],
            "checksum": checksum,
            "created_ts": created_ts,
        }

    def verify_archive(self, manifest_id: int) -> tuple:
        """
        校验归档批校验和（重算归档行 row_hash 按序聚合 SHA-256 比对）。

        Returns
        -------
        tuple : (ok: bool, expected: str | None, actual: str | None, row_count: int)
        """
        with self._conn.transaction():
            rows = self._conn.execute(
                "SELECT archive_cutoff_ts, row_count, first_seq, last_seq, "
                "checksum FROM audit_archive_manifest WHERE id = ?",
                (int(manifest_id),),
            )
            if not rows:
                raise ValueError(f"归档清单不存在: manifest_id={manifest_id}")
            m = rows[0]
            archived = self._conn.execute(
                "SELECT row_hash FROM audit_archive "
                "WHERE seq >= ? AND seq <= ? ORDER BY seq ASC",
                (int(m["first_seq"]), int(m["last_seq"])),
            )
        row_hashes = [str(r["row_hash"]) for r in archived]
        actual = _sha256_hex("|".join(row_hashes)) if row_hashes else None
        expected = str(m["checksum"]) if m["checksum"] else None
        return actual == expected, expected, actual, len(row_hashes)


# ═══════════════════════════════════════════════
#  自测（python -m myquant.monitoring.audit_log）
# ═══════════════════════════════════════════════

def _run_self_test() -> int:
    """独立自测：临时库跑通追加/链校验/篡改检测/归档校验全链路，0=通过。"""
    import tempfile
    from pathlib import Path

    logger.info("AuditLogStore 自测开始")
    passed = 0
    failed = 0

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            logger.info("  ✓ %s", label)
        else:
            failed += 1
            logger.error("  ✗ %s %s", label, detail)

    with tempfile.TemporaryDirectory() as tmp:
        conn = DatabaseConnection(str(Path(tmp) / "audit_test.db"))
        try:
            audit = AuditLogStore(connection=conn)

            # 建表自检
            from myquant.database.schema import TABLE_NAMES, table_exists

            check("建表自检", table_exists(conn, TABLE_NAMES["audit_log"]))
            check("归档表自检", table_exists(conn, TABLE_NAMES["audit_archive"]))
            check("清单表自检", table_exists(conn, TABLE_NAMES["audit_archive_manifest"]))

            # 追加 + 哈希链
            rec1 = audit.append("alert", "create", actor="scheduler", detail={"alert_id": "A1"})
            rec2 = audit.append("alert", "ack", actor="operator", detail={"alert_id": "A1"})
            rec3 = audit.append("metric", "archive", detail={"kind": "raw"})
            check("追加返回链信息", rec1["seq"] == 1 and rec1["prev_hash"] == "0" * 64)
            check("链尾衔接", rec2["prev_hash"] == rec1["row_hash"] and rec3["prev_hash"] == rec2["row_hash"])
            check("主表 3 条", audit.count() == 3)

            # 只读查询
            rows = audit.query(kind="alert", order="asc")
            check("按 kind 过滤", len(rows) == 2 and rows[0]["action"] == "create")
            rows = audit.query(actor="operator")
            check("按 actor 过滤", len(rows) == 1 and rows[0]["kind"] == "alert")
            check("detail 反序列化", audit.query(limit=1)[0]["detail"] == {"kind": "raw"})

            # 链校验（干净链）
            ok, broken, checked = audit.verify_chain()
            check("干净链校验通过", ok and checked == 3, f"ok={ok}, broken={broken}")

            # 篡改检测：直接改库（模拟常规改写）→ 断链检出
            with conn.transaction():
                conn.execute_update(
                    "UPDATE audit_log SET detail_json = ? WHERE seq = ?",
                    ('{"alert_id": "A1", "tampered": true}', 2),
                )
            ok, broken, checked = audit.verify_chain()
            check("篡改检出（seq=2）", (not ok) and broken == 2, f"broken={broken}")
            # 恢复供后续归档测试
            with conn.transaction():
                conn.execute_update(
                    "UPDATE audit_log SET detail_json = ? WHERE seq = ?",
                    ('{"alert_id": "A1"}', 2),
                )
            ok, broken, _ = audit.verify_chain()
            check("恢复后链校验通过", ok)

            # 归档 + 归档校验
            old_ts = time.time() - 30 * 86400.0
            for i in range(3):
                conn.execute_update(
                    "INSERT INTO audit_log (kind, action, actor, detail_json, seq, "
                    "prev_hash, row_hash, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "old",
                        "create",
                        "system",
                        "{}",
                        10 + i,
                        "0" * 64,
                        f"{'a' * 62}{i:02d}",
                        old_ts + i,
                    ),
                )
            manifest = audit.archive(before_ts=time.time() - 20 * 86400.0)
            check("归档返回 manifest", manifest["row_count"] == 3 and manifest["first_seq"] == 10)
            check("归档后主表清理", audit.count() == 3)
            ok, expected, actual, n = audit.verify_archive(manifest["id"])
            check("归档校验通过", ok and n == 3, f"expected={expected}, actual={actual}")
            # 篡改归档行 → 校验失败
            with conn.transaction():
                conn.execute_update(
                    "UPDATE audit_archive SET row_hash = ? WHERE seq = ?",
                    ("y" * 64, 11),
                )
            ok, _, _, _ = audit.verify_archive(manifest["id"])
            check("归档篡改检出", not ok)

            # 防御式校验
            try:
                audit.append("", "create")
                check("空 kind 报错", False)
            except ValueError:
                check("空 kind 报错", True)
            try:
                audit.append("alert", "create", detail="not-dict")  # type: ignore[arg-type]
                check("detail 非 dict 报错", False)
            except ValueError:
                check("detail 非 dict 报错", True)
            try:
                audit.query(limit=0)
                check("limit=0 报错", False)
            except ValueError:
                check("limit=0 报错", True)
        finally:
            conn.close_all()

    logger.info("自测结果: %d 通过, %d 失败", passed, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-5s %(message)s",
    )
    raise SystemExit(_run_self_test())
