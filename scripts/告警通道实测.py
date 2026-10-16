# -*- coding: utf-8 -*-
"""
C-6 告警通道实测：飞书 Webhook 送达验证
=======================================
项目：订单与对账规范 V1.1 施工（鲲鹏量化交易系统）
职责：按交接文档 C-6，验证告警通道（飞书 Webhook）真实送达人工端。

模式：
  single      发 1 条 CRITICAL（C-6 主验收：确认人工端收到）
  concurrent  并发发 N 条 CRITICAL（通道稳定性压测，默认 10 条）
  probe       直连探测：以飞书 interactive 格式直接 POST 一条，验证 Webhook URL 本身可用

用法（conda_wdh 环境）：
  D:\\ProgramData\\anaconda3\\envs\\conda_wdh\\python.exe scripts/告警通道实测.py --mode single
  D:\\ProgramData\\anaconda3\\envs\\conda_wdh\\python.exe scripts/告警通道实测.py --mode concurrent --count 10
  D:\\ProgramData\\anaconda3\\envs\\conda_wdh\\python.exe scripts/告警通道实测.py --mode probe

说明：
  - webhook URL 来自配置装配（config/live_config.yaml notification.channels.feishu.webhook），
    不在此脚本硬编码（Webhook URL 属敏感信息，live_config.yaml 已被 .gitignore 忽略）。
  - WebhookAlertChannel 为后台线程队列发送（P1-13），send() 立即返回；本脚本轮询
    通道 sent/failed 计数直至稳定，以反映真实投递结果。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from myquant.monitoring.alerting import (  # noqa: E402
    Alert,
    SeverityLevel,
    get_alert_manager,
)


def _feishu_channel(manager):
    """取装配后的 feishu Webhook 通道；不存在时返回 None。"""
    return manager.get_channel("feishu")


def _print_channels(manager) -> None:
    print("== 装配后的告警通道 ==")
    for name in manager.list_channels():
        ch = manager.get_channel(name)
        extra = ""
        if hasattr(ch, "webhook_url"):
            url = getattr(ch, "webhook_url", "")
            shown = url if str(url).startswith("http") else f"<占位/无效: {url!r}>"
            extra = (
                f" webhook_type={getattr(ch, 'webhook_type', '?')!r} url={shown}"
            )
        print(
            f"  - {name}: {type(ch).__name__} "
            f"(min_level={getattr(ch, 'min_level', '?')}){extra}"
        )
    print(f"  共 {len(manager.list_channels())} 个通道")


def _wait_stable(ch, timeout: float = 30.0, settle: float = 1.5):
    """轮询通道 sent/failed 计数直至稳定（后台线程投递完成）。"""
    def snap():
        s = ch.stats or {}
        return (s.get("sent_count", 0), s.get("failed_count", 0))

    last = snap()
    stable_since = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        cur = snap()
        if cur == last:
            if time.time() - stable_since >= settle:
                return cur
        else:
            last = cur
            stable_since = time.time()
    return last


def _build_critical(message: str, seq: int = 0, tag: str = "single") -> Alert:
    return Alert(
        level=SeverityLevel.CRITICAL,
        message=message,
        source="c6_alert_channel_test",
        category="system",
        details={
            "task": "C-6 告警通道实测",
            "seq": seq,
            "tag": tag,
            "mode": "订单与对账规范 V1.1 施工",
        },
        deduplication_key=f"c6-{tag}-{seq}-{time.time():.0f}",
    )


def run_probe(manager) -> int:
    ch = _feishu_channel(manager)
    if ch is None:
        print("[FAIL] 装配中不存在 feishu 通道，直连探测无法继续")
        return 2
    url = str(getattr(ch, "webhook_url", ""))
    if not url.startswith("http"):
        print(f"[FAIL] feishu 通道 webhook_url 无效: {url!r}")
        return 2
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🚨 C-6 直连探测"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "**探测消息**：C-6 告警通道实测（绕过 manager，"
                        "直连验证 Webhook URL 可用性）"
                    ),
                }
            ],
        },
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            status = resp.getcode()
        print(f"HTTP {status} 响应: {body}")
        parsed = json.loads(body)
        ok = parsed.get("code") == 0 or parsed.get("StatusCode") == 0
        if ok:
            print("[OK] 直连探测送达成功（飞书已接受消息）")
            return 0
        print("[FAIL] 飞书返回错误（Webhook URL 或 payload 有问题）")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 直连探测异常: {e}")
        return 1


def run_single(manager) -> int:
    alert = _build_critical("C-6 告警通道实测：单条 CRITICAL 送达验证")
    sent = manager.send_alert(alert, dedup_enabled=False)
    print(f"send_alert 返回 sent_count={sent}（同步入队成功数）")
    print(f"本轮通道尝试: {getattr(manager, '_last_channel_attempts', None)}")
    ch = _feishu_channel(manager)
    if ch is None:
        print("[FAIL] 未装配 feishu 通道 → 飞书告警无法发送（需修复装配）")
        return 2
    before = ch.stats or {}
    print(
        "feishu 通道投递前 stats: "
        f"sent={before.get('sent_count', 0)} failed={before.get('failed_count', 0)}"
    )
    final = _wait_stable(ch)
    print(
        f"feishu 通道投递后 stats: sent={final[0]} failed={final[1]}"
    )
    delta_sent = final[0] - before.get("sent_count", 0)
    if delta_sent > 0:
        print("[OK] CRITICAL 已通过 feishu Webhook 投递，请在飞书群确认人工端收到本条告警")
        return 0
    print("[FAIL] feishu 通道未成功投递（sent 计数未增长），见上方失败原因")
    return 1


def run_concurrent(manager, count: int) -> int:
    print(f"== 并发压测：{count} 条 CRITICAL ==")
    alerts = [
        _build_critical(f"C-6 并发压测 #{i + 1}/{count}", seq=i, tag="concurrent")
        for i in range(count)
    ]
    ch = _feishu_channel(manager)
    if ch is None:
        print("[FAIL] 未装配 feishu 通道 → 无法并发压测")
        return 2
    before = ch.stats or {}

    def do_send(a):
        try:
            return manager.send_alert(a, dedup_enabled=False)
        except Exception as e:  # noqa: BLE001
            print(f"  发送异常 seq={a.details.get('seq')}: {e}")
            return 0

    with ThreadPoolExecutor(max_workers=min(8, count)) as pool:
        results = list(pool.map(do_send, alerts))
    print(f"send_alert 并发返回（各线程同步入队成功数）: {results}")
    final = _wait_stable(ch, timeout=60.0, settle=2.0)
    print(
        f"feishu 通道投递后 stats: sent={final[0]} failed={final[1]} "
        f"（投递前 sent={before.get('sent_count', 0)} "
        f"failed={before.get('failed_count', 0)}）"
    )
    delta_sent = final[0] - before.get("sent_count", 0)
    print(f"本次新增成功投递 {delta_sent} 条 / 期望 {count} 条")
    ok = delta_sent == count
    if ok:
        print("[OK] 并发压测全部送达")
        return 0
    print(f"[FAIL] 并发压测未全部送达（{delta_sent}/{count}）")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="C-6 告警通道实测（飞书 Webhook）")
    parser.add_argument(
        "--mode", choices=["single", "concurrent", "probe"], default="single",
    )
    parser.add_argument(
        "--count", type=int, default=10, help="concurrent 模式的并发条数（默认 10）",
    )
    args = parser.parse_args()

    manager = get_alert_manager()
    _print_channels(manager)

    if args.mode == "probe":
        return run_probe(manager)
    if args.mode == "concurrent":
        return run_concurrent(manager, args.count)
    return run_single(manager)


if __name__ == "__main__":
    sys.exit(main())
