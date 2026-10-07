# 文件名：myquant/monitoring/config_as_code.py
"""
config_as_code.py - 监控即代码完善（D-7）
==========================================
仪表盘、告警规则、Runbook、采集配置（config/monitoring/*.yaml）全部 yaml
版本化（P0-6 完成态）；**变更关联监控**（规则变更经独立告警通道留痕可追溯）
与**回滚**（恢复上一版本内容）。

依据：《量化系统监控模块施工总清单_V1.2.md》D-7（规则变更可回滚、可追溯）。

本模块提供 MonitoringConfigRegistry：
  - discover()               : 盘点 config/monitoring 下 4 类监控配置项
  - initialize()             : 对现有配置项打初始版本 v1（幂等，不改内容）
  - apply(item, content)     : 应用新配置 → 版本 +1、留存上一版本快照、审计、
                              变更关联监控告警（category=config，可追溯）
  - rollback(item)           : 回滚到上一版本 → 恢复内容、版本 +1、审计、告警
  - history(item)            : 该配置项完整变更史（JSON 可序列化）
  - get_version(item)        : 当前版本与 sha256

防未来铁律：版本号、applied_at 等一律系统时钟当前时刻，不引用未来数据。
变更关联监控走独立告警通道（category=config，与业务告警隔离），审计
kind=config_as_code 失败降级不阻断。

scripts/监控配置管理.py 为薄入口。

依赖: 标准库 + myquant（alerting / audit_log / settings / time_utils）
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from myquant.core.logger import get_logger

logger = get_logger("monitoring.config_as_code")

try:
    from myquant.core.constants import SeverityLevel
except Exception:  # noqa: BLE001 —— 常量模块不可用时的降级占位
    class SeverityLevel:  # type: ignore[no-redef]
        INFO = "INFO"
        WARNING = "WARNING"
        CRITICAL = "CRITICAL"


# ═══════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════
# 项目根探测：业务模块默认路径一律基于项目根（对齐 #254 根治语义），
# 避免 CWD 差异导致配置目录/版本账本落错位置（scripts/ 下运行同样正确）
def _resolve_project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "pyproject.toml").is_file() or (parent / "myquant").is_dir():
            return parent
    return here.parents[1]  # 兜底：monitoring → myquant → 项目根


_PROJECT_ROOT: Path = _resolve_project_root()
# 默认路径语义：相对项目根（拼接在 __init__ 中完成）
DEFAULT_CONFIG_DIR: str = "config/monitoring"
DEFAULT_LEDGER_PATH: str = "docs/监控档案/monitoring_config_ledger.json"
AUDIT_KIND = "config_as_code"
CHANGE_CATEGORY = "config"
CHANGE_SOURCE = "config_as_code"
# 版本化管理的 4 类监控配置项（P0-6）
KNOWN_ITEMS: List[str] = ["dashboard", "alert_rules", "runbooks", "collectors"]
VERSION_META_KEY = "_meta"


def _sha256(content: str) -> str:
    """内容 sha256 指纹（版本追溯用）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class MonitoringConfigRegistry:
    """
    监控即代码版本注册表（D-7）。

    管理 config/monitoring 下 4 类监控配置的版本、变更关联监控与回滚：
      - 每个配置项一个版本序列，逐次 apply 版本 +1，留存上一版本内容快照；
      - 每次变更（apply/rollback/initialize）写审计（kind=config_as_code）
        并触发独立告警通道（category=config）关联留痕（可追溯）；
      - rollback 恢复上一版本内容快照（不依赖 git）。

    隔离/防未来：全部时间戳用系统时钟（clock 可注入测试）；只消费已发生事实。
    """

    def __init__(
        self,
        config_dir: Optional[str] = None,
        ledger_path: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
        alert_manager: Any = None,
    ) -> None:
        """
        Parameters
        ----------
        config_dir : str | None
            监控配置目录（缺省 config/monitoring）。
        ledger_path : str | None
            版本账本 JSON 路径（缺省 docs/监控档案/monitoring_config_ledger.json）。
        clock : Callable[[], float] | None
            当前时间戳函数（测试注入）；None 用 time.time。
        alert_manager : AlertManager | None
            变更关联监控告警管理器；None 惰性取 get_alert_manager()。
        """
        self._config_dir: Path = (
            Path(config_dir) if config_dir else _PROJECT_ROOT / "config" / "monitoring"
        )
        self._ledger_path: Path = (
            Path(ledger_path) if ledger_path
            else _PROJECT_ROOT / "docs" / "监控档案" / "monitoring_config_ledger.json"
        )
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        self._alert_manager: Any = alert_manager
        self._audit_log: Any = None
        self._ledger: Dict[str, Any] = self._load_ledger()

    # ── 账本读写 ────────────────────────────────────────────

    def _load_ledger(self) -> Dict[str, Any]:
        """读取版本账本（不存在/损坏时返回空账本，不抛）。"""
        if not self._ledger_path.exists():
            return {"items": {}}
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and "items" in data else {"items": {}}
        except Exception as e:  # noqa: BLE001 —— 账本损坏降级为空账本
            logger.warning("[config_as_code] 账本读取失败（按空账本处理）: %s", e)
            return {"items": {}}

    def _save_ledger(self) -> None:
        """账本落盘（JSON 可序列化；父目录自动建）。"""
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._ledger_path, "w", encoding="utf-8") as f:
            json.dump(self._ledger, f, ensure_ascii=False, indent=2)

    # ── 配置项盘点 ──────────────────────────────────────────

    def _file_for(self, item: str) -> Optional[Path]:
        """定位配置项文件：<item>.yaml 优先，其次 <item>.example.yaml。"""
        if not self._config_dir.is_dir():
            return None
        live = self._config_dir / f"{item}.yaml"
        if live.exists():
            return live
        example = self._config_dir / f"{item}.example.yaml"
        if example.exists():
            return example
        return None

    def discover(self) -> Dict[str, Dict[str, Any]]:
        """
        盘点 4 类监控配置项（P0-6：仪表盘/告警规则/Runbook/采集配置）。

        Returns
        -------
        dict : {item: {"path"/"exists"/"sha256"/"version"/"filename"}}
        """
        result: Dict[str, Dict[str, Any]] = {}
        for item in KNOWN_ITEMS:
            f = self._file_for(item)
            if f is None:
                result[item] = {"path": None, "exists": False,
                                "sha256": None, "version": None, "filename": None}
                continue
            content = f.read_text(encoding="utf-8")
            ver = self.get_version(item)
            result[item] = {
                "path": str(f),
                "exists": True,
                "sha256": _sha256(content),
                "version": ver.get("current_version"),
                "filename": f.name,
            }
        return result

    # ── 版本查询 ────────────────────────────────────────────

    def get_version(self, item: str) -> Dict[str, Any]:
        """当前版本信息：{current_version, sha256, applied_at, changed_by}。"""
        self._validate_item(item)
        entry = self._ledger.get("items", {}).get(item, {})
        entries = entry.get("entries", [])
        if not entries:
            return {"current_version": None, "sha256": None,
                    "applied_at": None, "changed_by": None}
        last = entries[-1]
        return {
            "current_version": last.get("version"),
            "sha256": last.get("sha256"),
            "applied_at": last.get("applied_at"),
            "changed_by": last.get("changed_by"),
        }

    def history(self, item: str) -> List[Dict[str, Any]]:
        """该配置项完整变更史（按版本升序，JSON 可序列化）。"""
        self._validate_item(item)
        return list(self._ledger.get("items", {}).get(item, {}).get("entries", []))

    def _validate_item(self, item: str) -> None:
        """配置项必须属于 KNOWN_ITEMS（防未知项破坏账本）。"""
        if item not in KNOWN_ITEMS:
            raise ValueError(
                f"未知监控配置项 {item!r}（须为 {KNOWN_ITEMS} 之一）"
            )

    # ── 版本化初始化 ────────────────────────────────────────

    def initialize(self, changed_by: str = "system") -> int:
        """
        对 config/monitoring 下现有配置项打初始版本 v1（幂等，不改内容）。

        已完成版本化的项跳过；返回本次新初始化的项数。
        """
        count = 0
        for item in KNOWN_ITEMS:
            if self.get_version(item).get("current_version") is not None:
                continue
            f = self._file_for(item)
            if f is None:
                continue
            content = f.read_text(encoding="utf-8")
            self._record_entry(item, version=1, action="init", sha256=_sha256(content),
                               prev_content=None, changed_by=changed_by,
                               summary="初始版本（现有配置入库版本化）")
            count += 1
        self._save_ledger()
        return count

    # ── 应用/回滚 ───────────────────────────────────────────

    def apply(
        self, item: str, content: str, changed_by: str = "operator",
        summary: str = "",
    ) -> Dict[str, Any]:
        """
        应用新配置（版本 +1，留存上一版本内容快照）。

        写盘 config/<item>.yaml → 账本记录 → 审计 → 变更关联监控告警。

        Parameters
        ----------
        item : str
            配置项（KNOWN_ITEMS 之一）。
        content : str
            新配置内容（非空，防御式校验）。
        changed_by : str
            变更发起方（可追溯）。
        summary : str
            变更说明（可追溯）。

        Returns
        -------
        dict : 应用结果（{item/version/sha256/applied_at}）。
        """
        self._validate_item(item)
        raw = content or ""
        if not raw.strip():
            raise ValueError("配置内容为空，拒绝应用（防御式）")
        content = raw  # 保留原文（含尾部换行/缩进），YAML 内容不剥除

        prev = self.get_version(item)
        prev_content: Optional[str] = None
        if prev.get("current_version") is not None:
            # 读取当前文件内容作为上一版本快照（回滚用）
            f = self._file_for(item)
            if f is not None:
                prev_content = f.read_text(encoding="utf-8")

        new_version = (int(prev["current_version"]) if prev.get("current_version") else 0) + 1
        new_sha = _sha256(content)

        # 写盘实时配置 <item>.yaml
        target = self._config_dir / f"{item}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        self._record_entry(item, version=new_version, action="apply", sha256=new_sha,
                           prev_content=prev_content, changed_by=changed_by,
                           summary=summary or "应用新配置")
        self._save_ledger()
        self._audit("apply", item, {"version": new_version, "changed_by": changed_by})
        self._notify_change("apply", item, new_version)
        return {
            "item": item, "version": new_version, "sha256": new_sha,
            "applied_at": self._clock(), "changed_by": changed_by,
        }

    def rollback(self, item: str, changed_by: str = "operator") -> Dict[str, Any]:
        """
        回滚到上一版本（恢复上一版本内容快照）。

        Returns
        -------
        dict : 回滚结果；无可回滚的上一版本时返回 {"rolled_back": False}。
        """
        self._validate_item(item)
        entry = self._ledger.get("items", {}).get(item, {})
        entries = entry.get("entries", [])
        if len(entries) < 2:
            return {"rolled_back": False, "item": item, "reason": "无上一版本可回滚"}
        last = entries[-1]  # 最后一次变更记录（apply 携带其前快照）
        prev_content = last.get("prev_content")
        if prev_content is None:
            return {"rolled_back": False, "item": item,
                    "reason": "最后一次变更无上一版本快照"}
        restored_version = int(last.get("version", 1)) - 1
        new_version = int(entry["current_version"]) + 1
        # 回滚写回最后一次变更前的内容快照
        target = self._config_dir / f"{item}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(prev_content, encoding="utf-8")

        self._record_entry(
            item, version=new_version, action="rollback",
            sha256=_sha256(prev_content), prev_content=None,
            changed_by=changed_by,
            summary=f"回滚到上一版本 v{restored_version}",
        )
        self._save_ledger()
        self._audit("rollback", item, {"version": new_version, "changed_by": changed_by})
        self._notify_change("rollback", item, new_version)
        return {
            "rolled_back": True, "item": item, "version": new_version,
            "restored_version": restored_version, "changed_by": changed_by,
        }

    def _record_entry(
        self, item: str, version: int, action: str, sha256: str,
        prev_content: Optional[str], changed_by: str, summary: str,
    ) -> None:
        """写入一条变更史记录并更新当前版本号。"""
        items = self._ledger.setdefault("items", {})
        item_ledger = items.setdefault(item, {})
        item_ledger["current_version"] = version
        entries = item_ledger.setdefault("entries", [])
        entries.append({
            "version": version,
            "action": action,
            "sha256": sha256,
            "prev_content": prev_content,
            "applied_at": self._clock(),
            "changed_by": changed_by,
            "summary": summary,
        })

    # ── 变更关联监控 / 审计 ──────────────────────────────────

    def _notify_change(self, action: str, item: str, version: int) -> None:
        """变更关联监控：规则变更经独立告警通道留痕（category=config，可追溯）。

        与业务告警隔离（category=config），dedup_key=config:<action>:<item>:
        <version> 保证同一次变更只告警一次（防抖），同 key 复用窗口内去重。
        """
        manager = self._alert_manager
        if manager is None:
            try:
                from myquant.monitoring.alerting import get_alert_manager
                manager = get_alert_manager()
            except Exception as e:  # noqa: BLE001 —— 无告警管理器时降级日志
                logger.warning("[config_as_code] 告警管理器不可用（变更仅日志留痕）: %s", e)
                return
        try:
            manager.alert(
                level=SeverityLevel.INFO,
                message=f"监控配置变更 [{action}] {item} → v{version}",
                source=CHANGE_SOURCE, category=CHANGE_CATEGORY,
                details={"action": action, "item": item, "version": version},
                deduplication_key=f"config:{action}:{item}:{version}",
            )
        except Exception as e:  # noqa: BLE001 —— 变更告警失败不阻断变更
            logger.warning("[config_as_code] 变更关联监控告警失败: %s", e)

    def _audit(self, action: str, item: str, detail: Dict[str, Any]) -> None:
        """审计留痕（kind=config_as_code；失败降级不阻断）。"""
        if self._audit_log is None:
            try:
                from myquant.monitoring.audit_log import AuditLogStore
                self._audit_log = AuditLogStore()
            except Exception as e:  # noqa: BLE001
                logger.warning("[config_as_code] 审计存储不可用（降级内存留痕）: %s", e)
        if self._audit_log is None:
            return
        try:
            self._audit_log.append(
                AUDIT_KIND, f"config:{action}:{item}", "system", dict(detail or {})
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[config_as_code] 审计留痕失败: %s", e)
