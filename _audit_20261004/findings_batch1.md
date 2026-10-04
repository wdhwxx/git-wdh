# 第1批 详细发现（仓库画像、架构与代码、依赖与CI/CD、安全基线）

## 阅读说明

本文件为审计工作底稿草案，记录的是对 D:\Projects\Pythonwdhmm 在 2026-10-04 文件系统快照下的只读取证结果，不构成鉴证结论，不含任何保证性意见。

**证据等级口径（A–E，依 v3.0 提示词第九章 9.2 全局唯一表）**：A=直接可核验物（代码/配置/日志原文，路径与行号可逐字复核，含 hash 可验）；B=文档/配置/截图等间接材料；C=基于材料的合理推断（含仅目录级观察）；D=材料缺失；E=单方陈述未经佐证（含无真实性声明的 hash）。本批发现以 A 级为主，个别引用项目内文档自述的条目为 B 级并已注明。

**「依赖单方声明」的含义**：指某项事实的成立只能依据项目方自己撰写的文档、注释或整改清单，审计方无法从代码执行路径、配置消费点或运行产物中独立验证。此类事实不作为控制有效性的证据，只能作为「项目方自述与代码实现是否一致」的比较基准；一旦自述与代码冲突，以代码为准。

**本批共 8 条发现**（P1-01 ~ P1-08），覆盖仓库画像、架构与代码、依赖与 CI/CD、安全基线四类。本次审计未取得 git commit hash，重要性水平、抽样口径与材料真实性声明均未提供，故整体按降级口径执行：路径与行号以快照文本为准，不作版本追溯。

---

### KP-P1-01 下单与风控/实盘控制接口返回伪造成功

- **维度**：架构与代码 / 交易安全
- **位置**：`myquant/api/routes.py:515,521,553,559,622-637,642`
- **证据等级**：B（源码原文逐字可复核：端点直接构造成功响应体，无任何下游调用）
- **优先级 / 风险等级**：P0 严重
- **事实**：`POST /api/order` 在 622-637 行按下单请求拼装返回对象，`status="submitted"`、`filled_quantity=0`，message 为「订单已提交（v1占位，未实际对接券商）」；`/api/order/{order_id}/cancel`（642 行）直接返回 `{"order_id":..., "status":"cancelled"}`；`/api/live/start`（515）、`/api/live/stop`（521）返回 `{"status":"starting"}` / `{"status":"stopping"}`；`/api/risk/pause`（553）返回 `{"status":"PAUSED"}` 并携带自造 order_id 式时间戳字符串。所有端点均以 `ApiResponse.success(...)` 返回 HTTP 200，无券商、实盘引擎或风控管理器调用，返回体形如 `return ApiResponse.success(data={"status": "PAUSED", "reason": reason}, message="交易已暂停（v1占位）")`。599-603 行另见同类包裹：数据质量失败时返回 `ApiResponse.error(code=500, ...)` 而 HTTP 状态仍为 200。
- **推断**：调用方无法从响应码或响应体区分「指令已真实生效」与「仅回显占位」，接口语义与实际副作用脱钩，属于对外的虚假成功反馈；在实盘接线完成后该占位若未被移除，将直接演变为静默空转。
- **未知**：是否已有真实下单调用方或对账任务依赖该响应；券商通道接线计划与验收口径；是否存在网关层拦截该类端点。
- **风险影响**：客户或运维据「已暂停交易」认为风险已阻断而继续放量，或据 `submitted` 认为持仓已建立，导致实际无持仓/无风控的状态下做出资金决策，直接影响客户资产安全与交易安全，并使事后追溯无据可查。
- **可能性**：控制完全缺失（接口对交易控制类动作返回伪造成功，无任何真实执行与校验）
- **根因**：技术——占位实现以真实成功语义对外暴露，未做「未接线即拒绝」的失败关闭设计。
- **整改建议**：在 `myquant/api/routes.py` 中为 `/api/order`、`/api/order/{id}/cancel`、`/api/live/start`、`/api/live/stop`、`/api/risk/pause`、`/api/risk/resume` 增加「未接线」判定，返回 HTTP 501 或 503 并给出明确错误体（如 `error="NotImplemented"`），禁止返回 `ApiResponse.success`；仅当实盘引擎/风控管理器实例存在且返回真实受理票据时才返回成功。
- **验证方法（DoD）**：对六个端点各发一次请求，断言 HTTP 状态码非 2xx 且响应体不含 `"status": "submitted"`/`"PAUSED"`/`"cancelled"` 成功语义；代码层检索 `routes.py` 确认这些路径不再出现 `ApiResponse.success`；补充单元测试覆盖未接线时的拒绝分支。
- **最坏情形**：实盘运行中操作员以为已一键暂停，风控实际未生效，继续持仓并在极端行情中承受未受控回撤；或以为下单成功而未监控真实成交，形成账实不符。
- **是否需法律顾问确认**：否（属工程控制缺陷，未直接涉及特定法规适用性）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-02 回测API返回硬编码绩效常量

- **维度**：架构与代码 / 绩效数据真实性
- **位置**：`myquant/api/routes.py:462-481`
- **证据等级**：B（源码原文逐字可复核，常量与调用参数无关）
- **优先级 / 风险等级**：P0 严重
- **事实**：`POST /api/backtest/run`（454 行起）在 461 行注明「v1占位：后续迭代调用回测引擎」，随后直接构造 `BacktestMetrics(total_return=0.0523, annual_return=0.125, sharpe_ratio=1.25, max_drawdown=0.085, win_rate=0.55, total_trades=23, calmar_ratio=1.47, sortino_ratio=1.82)`，并组装 `BacktestResponse(status="completed", equity_curve=[], trades=[], ...)` 后写入 `_backtest_results`；481 行以 `ApiResponse.success(..., message="回测任务已提交")` 返回 HTTP 200。指标值不随 `request.strategy_name`、`request.symbol` 变化，结果对象被缓存后可由 483-489 行 `GET /api/backtest/{backtest_id}` 再次取回。
- **推断**：任何输入都得到同一组「看起来合格」的绩效指标（夏普 1.25、胜率 55%），且状态为 `completed`，系统对外呈现了完整的、可被引用的绩效结论，而该结论无任何数据与撮合支撑；`equity_curve`/`trades` 为空本可作为矛盾线索，但不易被非技术使用者识别。
- **未知**：是否有下游系统或人员已实际读取该接口数据用于策略决策或对外披露；是否存在历史导出的绩效报告；是否有网关禁止该接口对外暴露。
- **风险影响**：可审计性丧失——绩效结论无计算过程与数据口径支撑，无法回溯复算；若被用于策略筛选或资金分配决策，将导致基于虚构指标配置真实资金，直接影响客户资产安全。
- **可能性**：控制完全缺失（回测接口无真实引擎调用，硬编码结果直接以完成态返回）
- **根因**：技术——占位数据未做未实现标识与访问隔离，且以 `completed` 语义对外发布。
- **整改建议**：在 `myquant/api/routes.py` 的 `run_backtest` 中移除硬编码 `BacktestMetrics`，改为调用回测引擎；在引擎接线完成前，端点返回 HTTP 501/503 并在响应体标注 `status="not_implemented"`，同时禁止写入 `_backtest_results`。
- **验证方法（DoD）**：以两组不同 `strategy_name`/`symbol` 调用 `/api/backtest/run`，断言返回指标不完全相同或返回未实现错误；检索 `routes.py` 确认 `0.0523`、`1.25`、`0.55` 等常量不再出现于响应构造路径；单元测试断言 `status != "completed"` 于未接线态。
- **最坏情形**：以虚构绩效作为实盘放量依据，真实策略上线后绩效与预期严重背离，造成客户资金实质亏损并引发受托责任争议。
- **是否需法律顾问确认**：否（属数据真实性与工程控制缺陷；若该接口数据曾用于对外披露，则由管理层另行评估）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-03 镜像无.dockerignore，.env与quant.db入镜像且容器root

- **维度**：安全基线 / 镜像与部署
- **位置**：`docker/Dockerfile:7,40,47,50`
- **证据等级**：C（Dockerfile 原文可复核，且经全仓检索 `**/.dockerignore` 返回 0 个文件，两处独立证据互证）
- **优先级 / 风险等级**：P0 严重
- **事实**：`docker/Dockerfile:50` 使用 `COPY . .` 将构建上下文整体复制入镜像；40 行先 `COPY pyproject.toml README.md ./`，47 行 `RUN pip install --no-cache-dir -e ".[live,dev,web]"`（含 dev 依赖），7 行 `FROM python:3.12-slim` 未指定 `USER`，全程以 root 构建与运行。全仓搜索 `.dockerignore` 无任何文件命中，因此构建上下文内的敏感与本地化产物（按 `.env.example:1-13` 约定在建库时复制生成的 `.env`，含 `BROKER_PASSWORD`、`TUSHARE_TOKEN`、`SMTP_PASSWORD`、`POSTGRES_PASSWORD` 等占位键）以及实际存在的 `database/quant.db` 均无排除规则。快照中 `.env` 未实际存在（仅 `.env.example`），数据库文件 `database/quant.db` 实际存在。
- **推断**：镜像层一旦含密钥文件，任何持有镜像者均可 `docker save`/`docker history` 提取；SQLite 数据库入镜像还会把历史业务数据（持仓、成交、账户）固化为可分发副本，且每次重建都携带旧数据。
- **未知**：镜像仓库是否私有及访问控制范围；生产部署时是否由 CI 注入真实 `.env` 后再构建（若注入则风险由「模板占位」升级为「真实密钥随镜像分发」）；是否有其他层（如 compose）覆盖运行用户。
- **风险影响**：密钥泄露可导致券商账户、数据源与邮件通道被冒用，直接威胁客户资产；业务数据库副本外泄违反数据保密要求；root 运行放大容器逃逸与横向移动影响，削弱合规连续性。
- **可能性**：控制完全缺失（无忽略规则、无降权用户、敏感文件随上下文入镜像）
- **根因**：技术——镜像构建缺少上下文白名单与最小权限约束。
- **整改建议**：在项目根新增 `.dockerignore`，至少排除 `.env`、`.env.*`、`database/`、`logs/`、`outputs/`、`datasets/`、`实盘状态/`、`实盘信号/`、`backups/`、`.git/`、`__pycache__/`；在 `docker/Dockerfile` 中创建非 root 用户并 `USER` 切换，`pip install` 只保留运行所需 extras（去掉 `dev`）。
- **验证方法（DoD）**：构建后执行 `docker run --rm <image> sh -c "ls -a /app | grep -E '^.env$|quant.db'"` 断言无输出；`docker run --rm <image> id -u` 断言非 0；`docker history` 或 `docker save` 后扫描层内容，确认无密钥字符串命中。
- **最坏情形**：镜像被推送至共享仓库或外发，券商密码与含真实持仓的历史数据库同时泄露，造成资金损失、客户信息泄露与监管问责。
- **是否需法律顾问确认**：是（涉及个人信息与交易数据出境/共享时的合规适用性需法律意见）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-04 .env与认证/限流链路未连通，密钥不生效

- **维度**：安全基线 / 配置与鉴权链路
- **位置**：`myquant/core/settings.py:112-113,273`；`myquant/api/middleware/auth.py:117`；`scripts/运行API.py:118-136`
- **证据等级**：C（三处源码独立互证：不加载、不读取配置层、仅读进程环境变量）
- **优先级 / 风险等级**：P1 高
- **事实**：`settings.py:109-113` 的 `_load_env` 仅在业务代码实例化 `Settings` 时才调用 `load_dotenv(str(env_path), override=False)`，而 273-275 行 `get_settings()` 每次新建实例、无模块级自动加载；`auth.py:117` 认证密钥取自 `os.environ.get(env_var, "")`（`env_var` 默认 `QUANT_API_KEYS`，见 80 行），不经过 `settings.get()`；`scripts/运行API.py` 导入链为 `myquant.api.cli` 与 `myquant.core.logger`，全程未导入 `settings`；`routes.py:329-336` 直接以 `os.environ.get("QUANT_CORS_ORIGINS"...)`、`QUANT_RATE_LIMIT_MAX` 等构造认证与限流器。全仓检索 `myquant/api` 下 `get_settings`/`load_dotenv` 均无命中。
- **推断**：以 `python scripts/运行API.py` 或 uvicorn 启动时，`.env` 中的 `QUANT_API_KEYS`、`QUANT_CORS_ORIGINS` 等键不会被读入进程环境，认证密钥集为空；据 `auth.py:124-128`，此时若认证开启将拒绝全部请求，若以 `--disable-auth` 规避则鉴权形同虚设，形成「文档称已配置、实际不生效」的落差。同时也说明密钥管理实际依赖外部注入，项目内 `.env` 机制在 API 场景失效。
- **未知**：生产环境实际注入方式（系统环境变量/容器 secrets/编排 env_file）；是否存在其他入口代为实现 `settings` 加载；是否已有因密钥未生效而关闭认证的运维记录。
- **风险影响**：要么全体接口不可用（业务中断），要么为恢复可用而关闭认证，使对外 API 无鉴权暴露，客户资产相关接口可被任意调用；配置与生效状态不一致亦破坏可审计性。
- **可能性**：控制存在未执行（`.env` 与鉴权链路已实现但入口未触发加载）
- **根因**：技术——配置加载入口未与 API 启动路径绑定，鉴权组件绕过统一配置层直读环境变量。
- **整改建议**：在 `scripts/运行API.py` 启动早期显式调用 `from myquant.core.settings import get_settings; get_settings()` 触发 `_load_env`；或改为模块级单例并在 `myquant/api/routes.py:create_app` 开头调用一次 `settings.get("QUANT_API_KEYS")`；同时把 `auth.py` 的密钥来源改为经 `settings` 读取，统一配置入口。
- **验证方法（DoD）**：在仓库根放置含 `QUANT_API_KEYS=test_key_1` 的 `.env`，仅以 `python scripts/运行API.py` 启动，断言日志出现「认证中间件已启用: keys=1」且携带 `X-API-Key: test_key_1` 的请求通过、缺失时返回 401；补充集成测试断言 `.env` 中的 CORS/限流键实际生效。
- **最坏情形**：为让服务可用而长期关闭鉴权，API 暴露于内网或公网，攻击者可调用下单/风控/资金分配类端点，造成资金被操纵。
- **是否需法律顾问确认**：否（属技术配置链路缺陷）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-05 限流仅单进程内存、CORS默认全开

- **维度**：安全基线 / 边界防护
- **位置**：`myquant/api/routes.py:304-316,333-336`；`myquant/api/middleware/rate_limit.py:97,193`
- **证据等级**：B（源码原文逐字可复核）
- **优先级 / 风险等级**：P2 中
- **事实**：`routes.py:304-308` 取 `os.environ.get("QUANT_CORS_ORIGINS", "*")`，未设置时 `cors_origins == ["*"]`，309 行据此置 `allow_credentials=False`，310-316 行以 `allow_methods=["*"]`、`allow_headers=["*"]` 注册 `CORSMiddleware`；注释虽写「默认全开放，生产建议设置」，但默认值即为全开放。限流侧 `rate_limit.py:97` 使用 `self._request_counts: Dict[str, int] = defaultdict(int)`、193 行 `self._window_start: Dict[str, float] = defaultdict(time.time)`，计数与窗口均存于进程内字典；`routes.py:333-336` 以环境变量构造 `FixedWindowLimiter`（默认 100 次/60 秒）并单实例注册中间件。
- **推断**：多 worker（`scripts/运行API.py:141-147` 支持 `--workers`）或多副本部署时，每个进程各自计数，实际放行上限为「进程数 × 100」，限流不再是全局约束；内存态在重启后归零，攻击者可利用重启窗口重置配额。CORS 默认 `*` 使任意站点可在浏览器上下文发起跨域请求读取响应（`allow_credentials=False` 仅阻断 Cookie 携带，不阻断无凭据调用）。
- **未知**：生产部署的 worker 数与副本数；是否有反向代理层实施全局限流与 CORS 收紧；`QUANT_CORS_ORIGINS` 在生产是否已设置。
- **风险影响**：限流保护在扩容后失效，接口可被高频调用（含资金分配计算等重资源端点），形成拒绝服务与配额滥用；CORS 全开扩大浏览器侧攻击面，削弱接口边界防护与审计可信度。
- **可能性**：部分执行偶发失效（限流存在但仅进程内生效；CORS 可控但默认值即最宽）
- **根因**：技术——限流状态存储未外置为共享后端，安全相关配置以「不安全值」作为默认。
- **整改建议**：在 `myquant/api/routes.py` 中将 CORS 默认值改为显式白名单（无 `QUANT_CORS_ORIGINS` 时不添加 CORS 中间件或仅允许 `http://127.0.0.1:<dashboard端口>`）；在 `myquant/api/middleware/rate_limit.py` 增加基于 Redis（项目已有 `myquant/core/cache.py` 与 `cache` extras）的共享限流后端，供多 worker 共用计数。
- **验证方法（DoD）**：以 `--workers 4` 启动，对同一 IP 连续发起 401 次请求，断言第 101 次起返回 429 且四进程合计放行数不超过 100；未设置 `QUANT_CORS_ORIGINS` 时用跨域预检请求断言响应不含 `Access-Control-Allow-Origin: *`。
- **最坏情形**：攻击者绕过限流高频调用接口，服务不可用或敏感计算被批量窃取，交易时段内监控与风控查询中断。
- **是否需法律顾问确认**：否（属技术边界防护缺陷）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-06 CI无安全与密钥扫描门禁，依赖未锁定

- **维度**：依赖与 CI/CD
- **位置**：`.github/workflows/ci.yml:39-339`；`pyproject.toml:31-40`
- **证据等级**：C（CI 全文与依赖清单原文可复核，两处互证「无安全门禁 + 无锁定」）
- **优先级 / 风险等级**：P2 中
- **事实**：`ci.yml` 共 339 行，作业为 `lint`（ruff 基线护栏，65-79 行以 `RUFF_BASELINE: "560"` 计数并允许历史债 `|| true` 吸收）、`test`、配置校验、`data-clean`（304-339）等，全部为功能与规范类检查；全文无依赖漏洞扫描（pip-audit/safety/Dependabot）、无密钥扫描（gitleaks/trufflehog）、无 SAST 与镜像扫描步骤。依赖侧 `pyproject.toml:31-40` 中 `dev` 组仅 `pytest==9.1.1`、`black==26.5.1` 为精确固定，`ruff>=0.15.0,<0.17.0`、`pytest-cov>=5.0.0`、`pytest-benchmark>=4.0.0` 为区间；`web`/`live`/`gui` extras（27-53 行）均为 `>=` 下限，仓库内无 `requirements*.lock`、`poetry.lock`、`uv.lock` 类锁定文件。
- **推断**：CI 只能保证代码规范与既有测试通过，无法阻止带已知漏洞的依赖版本或误提交的密钥进入主干；`>=` 区间使同一提交在不同时间构建得到不同依赖集合，构建不可复现，审计难以界定「当时运行的是哪些第三方代码」。
- **未知**：是否有平台侧（GitHub Dependabot 告警、secret scanning）默认开启；部署时是否另行使用内部镜像源与审计清单；历史提交中是否已存在真实密钥。
- **风险影响**：供应链漏洞与密钥泄露缺少阻断点，风险在合并时不可见；依赖漂移使生产与审计基线不一致，削弱可审计性与合规连续性。
- **可能性**：控制完全缺失（CI 无安全类门禁，依赖未锁定）
- **根因**：流程——CI 门禁设计仅覆盖质量维度，未纳入安全与可复现性要求。
- **整改建议**：在 `.github/workflows/ci.yml` 新增 `security` 作业，执行 `pip-audit`（对已安装环境）与 `gitleaks detect --no-git`，并以 `continue-on-error: false` 阻断；在 `pyproject.toml` 中新增锁定文件产物（如 `uv lock`/`pip-compile` 生成的 `requirements.lock`）并在 CI 以 `--require-hashes` 或锁定文件安装。
- **验证方法（DoD）**：提交一个含已知 CVE 依赖版本与一个假密钥串的测试分支，断言 CI 失败且失败作业名分别为 `security` 的依赖扫描与密钥扫描步骤；`pip install -r requirements.lock` 在两次运行中产出相同 `pip freeze` 输出。
- **最坏情形**：被投毒的依赖版本随主干进入实盘环境，运行期执行未审计代码并访问券商凭据，造成资金损失且无法从审计记录定位引入点。
- **是否需法律顾问确认**：否（属流程与供应链控制缺陷）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-07 异常吞噬规模大（约52处），静默降级

- **维度**：架构与代码 / 错误处理
- **位置**：`myquant/api/routes.py:94-96,260-263`；`myquant/database/connection.py:136-139`
- **证据等级**：C（同类模式在多文件、多层级重复出现，可交叉印证规模与一致性）
- **优先级 / 风险等级**：P2 中
- **事实**：`routes.py:88-96` 以 `try: from fastapi import ... except ImportError: FASTAPI_AVAILABLE = False` 仅记录 `logger.warning`，随后 99-117 行定义 `FastAPI`/`get`/`post` 占位类使装饰器静默失效；260-263 行 `create_app` 在 `not FASTAPI_AVAILABLE` 时直接 `return FastAPI(title=title, version=version)`，返回一个无路由的占位应用而不抛异常，注释自述「便于在无fastapi环境下导入和测试」；`connection.py:136-139` 在 `close_all` 中对每个连接执行 `except Exception: pass`，注释说明「已关闭/连接异常的连接忽略」。按该模式在全仓检索 `except Exception`/`except ImportError` 且主体为 `pass`、仅记日志或静默降级的分支，累计约 52 处。
- **推断**：依赖缺失、连接异常、配置加载失败（如 `settings.py:139-145` 仅告警）等错误均不产生失败信号，调用方看到的是「成功但空结果」；在交易系统中，降级路径与正常路径的对外表现不可区分，异常会被掩盖至统计数据或资金结果中。
- **未知**：52 处中各分支的实际可达性与影响面（本次未逐处评估）；是否有监控系统消费告警日志以补偿静默降级；是否存在已知因静默降级导致的运行事故。
- **风险影响**：故障不可观测，问题在实盘运行中长时间潜伏；一旦在交易时段触发（如数据库连接异常被吞），可能出现持仓/成交记录缺失或风控状态失真，影响客户资产安全与事后追责能力。
- **可能性**：部分执行偶发失效（错误处理存在但以静默方式执行，具体触发依赖运行环境）
- **根因**：技术——以「不中断流程」为优先的错误处理约定，缺少分级与显式失败策略。
- **整改建议**：在 `myquant/api/routes.py` 将 `FASTAPI_AVAILABLE=False` 的降级改为启动即 `raise RuntimeError`（或 `/api/health` 汇报 `degraded` 并置非 200）；在 `myquant/database/connection.py:136-139` 的 `except Exception` 中至少 `logger.warning` 记录连接标识与异常；对全仓 52 处按「交易/风控路径必须显式失败、非关键清理路径可告警」分类并回写清单。
- **验证方法（DoD）**：在无 fastapi 的隔离环境中导入 `create_app`，断言抛出明确异常而非返回占位实例；构造关闭已关闭连接场景，断言日志出现告警记录；提交一份 52 处静默分支的分类清单（保留告警/改为抛出），并在 CI 以 grep 规则阻断新增无日志 `except Exception: pass`。
- **最坏情形**：实盘运行中数据库写入被静默吞掉，成交与持仓未落库，崩溃恢复时按错误状态重建持仓，形成账实不符与不可逆的资金差错。
- **是否需法律顾问确认**：否（属技术错误处理缺陷）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认

---

### KP-P1-08 文档声明与代码实现三处矛盾

- **维度**：仓库画像 / 文档一致性与可审计性
- **位置**：`config/live_config.example.yaml:41-42,184,192,220`；`config/backtest_config.example.yaml:41`；`docs/因子与机器学习档案/P2整改任务清单.md:19,64,68`
- **证据等级**：C（矛盾双方均有原文可复核：文档声明与代码消费点/日志输出相互印证）
- **优先级 / 风险等级**：P3 低
- **事实**：矛盾一（股票池）：`backtest_config.example.yaml:41` 设 `type: "three_factor"`，37 行注释称其为「三因子交集（动态选股，与实盘一致）」，而 `myquant/backtest/config_builder.py:122-129` 对该类型只打印「三因子选股尚未实现」并回退读取 `stock_pool.codes`，代码注释标为 `TODO(阶段3/4)`。矛盾二（实盘模式）：`live_config.example.yaml:20` 为 `trade_mode: "paper"`，同文件 38 行却为 `mode: "LIVE"`，`myquant/live/run_mode.py:116-121` 判定该组合为「配置不一致」并保守按 SIM 处理，40-42 行声明引擎读取 `qmt_path`/`account`，但模板中该两键被注释，实盘校验（run_mode.py:226-227）要求 `broker.account` 非空，模板直接启用即不满足。矛盾三（完成声明）：`P2整改任务清单.md:19` 记 data_versioner「已完成」且「待提交」，64 行「全量回归待跑」，67-68 行仍宣告 Gate 3「全部完成 ✅ —— 进入模拟盘 ≥ 3 个月观察期」；`live_config.example.yaml:184,192,220` 三段自述「【预留·未消费】本段暂无代码消费」，但 `tests/unit/test_config_live_check.py:193-195` 仅校验这些标注字符串存在，未校验配置是否真被消费。
- **推断**：示例配置与代码实际能力脱节，使用者按模板启用 `three_factor` 会静默回退到自定义列表（可能为空并抛错），按模板启用真钱模式会与 paper 模式冲突或被保守降级；「已完成」与「回归待跑」并列削弱文档作为审计基准的可信度，凡依赖文档判断控制有效性的结论均须回到代码复核。
- **未知**：是否存在未纳入本次快照的更新文档；实盘模板在生产是否被直接复制使用；`three_factor` 与 signal_output/notification 的实际排期。
- **风险影响**：运维与策略人员依据错误文档操作，可能以非预期股票池或非预期运行模式启动实盘；文档与代码不一致使审计证据链降级为单方声明，削弱可审计性与合规连续性。
- **可能性**：已有事件（示例配置与代码判定路径已形成确定性冲突，非潜在风险）
- **根因**：治理——文档回写未与代码变更同步，且缺少「文档声明可被自动化校验」的机制。
- **整改建议**：在 `config/backtest_config.example.yaml:41` 将默认改为 `preset_filtered` 或保留 `three_factor` 但注明「未实现，将回退 codes」；在 `config/live_config.example.yaml` 将 38 行改为 `mode: "SIM"` 并补齐 `account`/`qmt_path` 的环境变量占位键；在 `docs/因子与机器学习档案/P2整改任务清单.md` 将 Gate 3 状态改为「待全量回归通过后确认」，与 64 行一致。
- **验证方法（DoD）**：扩展 `tests/unit/test_config_live_check.py`，断言 `live_config.example.yaml` 的 `live.trade_mode` 与 `broker.mode` 组合通过 `run_mode.check_mode_consistency()` 返回空列表，且 `broker.account` 键存在；断言 `backtest_config.example.yaml` 的 `stock_pool.type` 在 `config_builder` 已实现类型集合内；CI 的配置校验作业纳入这两项断言。
- **最坏情形**：以文档为准在真钱模式启动而实际运行于非预期模式或非预期股票池，产生非授权标的交易与额外损失，且事后以文档为证导致责任界定困难。
- **是否需法律顾问确认**：否（属文档治理缺陷，未涉及法规适用性判断）
- **管理层回复**：待管理层回复
- **整改 DoD 完成情况**：未开始
- **复评日期与结果**：待定
- **残留风险与豁免人**：待确认
