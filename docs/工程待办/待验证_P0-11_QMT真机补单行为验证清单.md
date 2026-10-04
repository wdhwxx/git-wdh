# P0-11 真机验证预留备注 — QMT 补单对账行为

> 状态：**代码层防御已完成（蓝图 #112），真机行为待券商终端开通后验证**。
> 创建日期：2026-10-04 ｜ 优先级：真钱 P0（上实盘前必须勾完）

---

## 背景

`qmt_broker.query_broker_orders` 契约：未连接/查询异常/SDK 返回 `None` 时一律返回
`None`，对账层（`live/order_manager.py`）见 `None` 即跳过补发（保守，不真实重发）。

原缺陷：实现里 `return list(result or [])` 把 SDK 的 `None` 吞成 `[]`，对账层误判
"券商端无此单" → 对本地已 SUBMITTED 单幂等补发 → **重复真实报单 / 重复仓位**。

代码修复（已提交）：SDK 返回 `None` 时透传 `None` 并打 WARNING。

---

## 真机开通后的验证清单（逐条勾选）

### 前置
- [ ] QMT 客户端已登录、`xtquant` 可用（`python -c "from xtquant import xttrader"` 不报错）
- [ ] 账户为**模拟盘 / 小额资金**，严禁用主资金账号做下列实验

### 用例 1：正常查询返回
- [ ] 提交一笔真实报单（任意标的、限价、远离成交价确保挂单）
- [ ] 调用 `query_broker_orders(symbol)` → 应返回**非空 list**，含刚下的单
- [ ] 预期日志：无 `query_stock_orders 返回 None` WARNING

### 用例 2：SDK 返回 None 的异常路径（核心）
- [ ] 触发限流/瞬时故障路径（短时间高频调用 `query_stock_orders` 或断网后立即查询）
- [ ] 预期：方法返回 `None`（不是 `[]`），日志出现
      `[qmt_broker] query_stock_orders 返回 None（查询失败/无法确认），保守返回 None，调用方不补发`
- [ ] 对账层行为：本地 SUBMITTED 单被记 `skipped`，**不触发 resubmit_order**
- [ ] 券商端委托列表核对：无重复报单

### 用例 3：断连路径
- [ ] 断开 QMT 连接后调用 `query_broker_orders` → 返回 `None`，日志 `QMT 未连接`

### 通过标准
- 用例 2 是本项的验收点：**任何查询失败都不得被当成"券商端无此单"**。
- 全部勾选后，在本文件末尾签字日期，并在蓝图追加一条 P0-11 真机验证闭环记录。

---

## 相关文件
- 修复点：`myquant/live/brokers/qmt_broker.py::query_broker_orders`（约 L1740）
- 消费方：`myquant/live/order_manager.py` 对账补发段（None 跳过 / 非空才比对 order_id）
- 审核出处：`docs/鲲鹏量化交易系统_完整性独立审核报告_20261004.md` §P0-11
