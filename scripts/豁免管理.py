# 文件名：scripts/豁免管理.py
"""
豁免管理 - 薄入口（P1-14 / #365）
================================
业务逻辑在 myquant/leakage_gate/exemption.py（按治理规则归 myquant）。
审批状态机 pending → approved → revoked；仅 approved 且未过期豁免压制违规。

用法::

  python scripts/豁免管理.py list                        # 列全部豁免（按状态分组）
  python scripts/豁免管理.py grant --rule-id R03 --valid-from 2026-08-15 \
      --valid-until 2026-10-31 --reason "滚动对照合法截面" --target strat_a
  python scripts/豁免管理.py approve EX-2026-10-10-001 --approver 张三
  python scripts/豁免管理.py revoke EX-2026-10-10-001

台账受控持久化于 database/exemptions.json（重启不丢）；grant 后默认 pending，
须 approve 才生效；有效期上限 90 天；gate-error 不可豁免。
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myquant.leakage_gate import exemption as ex  # noqa: E402


def _print_list(store: ex.ExemptionStore) -> None:
    """按状态分组打印全部豁免。"""
    rows = store.load()
    if not rows:
        print("（豁免台账为空）")
        return
    by_status: dict[str, list[ex.Exemption]] = {}
    for e in rows:
        by_status.setdefault(e.status, []).append(e)
    for status in ("pending", "approved", "revoked"):
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n== {status}（{len(group)}）==")
        for e in group:
            target = e.target or "全局"
            print(f"  {e.exemption_id}  rule={e.rule_id}  目标={target}  "
                  f"{e.valid_from}~{e.valid_until}  approver={e.approver or '-'}  "
                  f"actor={e.actor or '-'}")
            print(f"      原因: {e.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(description="回测真实性门禁豁免管理（P1-14）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列全部豁免")

    p_grant = sub.add_parser("grant", help="登记豁免申请（pending，须 approve 生效）")
    p_grant.add_argument("--rule-id", required=True,
                         help="规则 ID（白名单 R01a/R01b/R02/R03/R04/R05/R07/R09/R10/R11）")
    p_grant.add_argument("--valid-from", required=True, help="生效日 YYYY-MM-DD")
    p_grant.add_argument("--valid-until", required=True, help="到期日 YYYY-MM-DD（≤90 天）")
    p_grant.add_argument("--reason", required=True, help="豁免原因（为何合法/保守近似）")
    p_grant.add_argument("--target", default="", help="目标策略名（缺省=全局适用）")
    p_grant.add_argument("--actor", default="ops", help="申请人（默认 ops）")

    p_approve = sub.add_parser("approve", help="审批通过一条 pending 豁免")
    p_approve.add_argument("exemption_id", help="豁免 ID")
    p_approve.add_argument("--approver", required=True, help="审批人（须与申请人分离）")

    p_revoke = sub.add_parser("revoke", help="撤销一条豁免（软删除留痕）")
    p_revoke.add_argument("exemption_id", help="豁免 ID")

    args = parser.parse_args()
    store = ex.get_store()

    if args.cmd == "list":
        _print_list(store)
        return 0

    if args.cmd == "grant":
        vid = f"EX-{args.valid_from.replace('-', '')}-{uuid.uuid4().hex[:6].upper()}"
        e = ex.Exemption(
            exemption_id=vid,
            reason=args.reason,
            rule_id=args.rule_id,
            approver="",
            valid_from=args.valid_from,
            valid_until=args.valid_until,
            target=args.target,
            actor=args.actor,
        )
        stored = store.grant(e, actor=args.actor)
        print(f"已登记豁免申请（pending，须 approve 生效）: {stored.exemption_id}")
        return 0

    if args.cmd == "approve":
        ok = store.approve(args.exemption_id, approver=args.approver)
        if not ok:
            print(f"未找到 pending 豁免: {args.exemption_id}", file=sys.stderr)
            return 1
        print(f"已批准: {args.exemption_id}（approver={args.approver}）")
        return 0

    if args.cmd == "revoke":
        ok = store.revoke(args.exemption_id)
        if not ok:
            print(f"未找到可撤销豁免: {args.exemption_id}", file=sys.stderr)
            return 1
        print(f"已撤销（软删除留痕）: {args.exemption_id}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
