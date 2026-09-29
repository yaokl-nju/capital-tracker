# 金融资本资讯助手

仅保留国内与国际金融资本：投资、融资、基金募资、并购退出、证券持仓和资金流动。沿用原来的 `Tracker → 搜索整合 → 分析 → PDF` 架构，科技企业仍可作为投资/融资对象，但不再跟踪一般产品、论文或外交动态。

## 安装与运行

使用 Python 3.12 或更新版本，在项目目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

PDF 使用原有 WeasyPrint，另需系统 Pango。Apple Silicon macOS 应使用 ARM Homebrew：

```sh
/opt/homebrew/bin/brew install pango
```

避免混用 `/usr/local` 下的 Intel 库和 ARM Python。其他平台参见 [WeasyPrint 安装文档](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation)。

在终端设置所需环境变量（`.env.example` 仅为示例，程序不会自动加载 `.env`）：

```sh
export DEEPSEEK_API_KEY='你的密钥'
# 网络需要代理时才设置；也可使用系统的 HTTPS_PROXY 等标准环境变量。
export SEARCH_PROXY='http://127.0.0.1:7897'
# SEC 可选，需要提供应用名称和真实联系邮箱。
export SEC_USER_AGENT='CapitalTracker/1.0 531949813@qq.com'
```

```sh
.venv/bin/python run.py --tracker investment --topics 'BlackRock' 'Himalaya Capital'
.venv/bin/python run.py --tracker investment_china --topics 'E Fund' '高毅资产'
.venv/bin/python run.py --tracker all --max-workers 4
sh run.sh --topics 'Temasek' --max-workers 2
```

`investment` 为国际资本，`investment_china` 为国内及中国相关跨境资本；`all` 只运行这两类。不指定 `--tracker` 时默认 `all`。默认机构名单保留在 `run.py`。默认报告分别写入项目的 `reports/international/` 和 `reports/china/`；可用 `--output-dir` 覆盖。

## 搜索与分析

- 支持 DDGS、Brave、Parallel、Serper、Tavily，默认按 DDGS news → Brave → Parallel → Serper → Tavily → DDGS text 顺序回退，可在 `config/settings.py` 调整。DDGS news 和 text 作为独立后端，分别固定单一 Bing 新闻引擎和 DuckDuckGo 文本引擎，避免自动多引擎扇出。news 无有效结果或失败后尝试中间的搜索服务，最后才尝试 text；每个后端只调用一次。旧名称 `ddgs` 兼容为 text 别名。有密钥的后端分别使用 `BRAVE_API_KEY`、`PARALLEL_SEARCH_API_KEY`、`SERPER_API_KEY`、`TAVILY_API_KEY`；缺少密钥的后端自动跳过。Brave 在共享搜索服务内默认至少间隔 1 秒发起请求，避免并发工作线程形成突发；可按账户计划调整 `SEARCH_MIN_INTERVALS`，参见 [Brave 请求限速说明](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting)。
- Parallel 使用 [官方 Search API](https://docs.parallel.ai/search/search-quickstart) 的 `POST /v1/search`、`x-api-key` 认证及 `fast` 模式，复用现有 requests，不增加 SDK。按[高级参数文档](https://docs.parallel.ai/search/advanced-search-settings)设置结果数量、摘要长度和国家地区；发布日期与多段 excerpts 接入统一证据整合。时间范围通过搜索目标表达，并由公共逻辑过滤已知旧日期，保留未知日期资料；不启用可能排除无日期来源的硬日期限制。
- 国内检索使用中文地区与语言配置，并为默认国内机构补充中文别名。检索涵盖投资、股权/债务融资、基金募资、并购退出、证券持仓与跨境资金。
- 关键词沿用原版五个研究维度、每维度生成 5–8 条，分组后轮流取词以覆盖各维度；实际每主题最多检索 12 条、每条默认 5 条结果；整合证据默认不超过 40,000 字符。可在 `Config` 中调整。缓存按查询、时间窗口、结果数量隔离，并支持并发访问。
- 统一校验 URL、域名过滤、来源评分和去重。保留不同金额、币种、融资轮次、日期及增减持方向，避免把不同资本事件合并；同一披露链接的互补检索摘要合并保留。已知旧发布日期按搜索窗口过滤；未知日期保留并标记未核实时效。
- SEC 检索获取 `13F-HR`、`13F-NT`、`SC 13G`、`SC 13D` 及对应修订文件的元数据，最近 120 天内按日期排序。Himalaya、Blackstone 和显式 CIK 使用官方 submissions JSON（同时识别新版 `SCHEDULE 13D/13G`）；其他未映射机构使用原网页检索，按行匹配类型/日期/链接。修正了旧注册主体名称，并移除将多个机构或整个管理人误映射为单个子基金的条目。`13F-NT` 明确标注为通知，不能充当持仓表。缺少 `SEC_USER_AGENT` 或 SEC 请求失败时继续网页搜索。SEC 元数据目前**不含持仓明细和前后期对照**，不得据此推断新建仓或增减持。参见 [SEC Form 13F 数据说明](https://www.sec.gov/files/form_13f.pdf) 与 [官方 API 文档](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)。
- 报告沿用原版五个分析维度（核心持仓、行业/赛道、投资逻辑、新建仓/增持、中国相关资产）及原表格+列表格式。已核实时效的可分析资料不足 5 条时，补入相关的未知日期资料，在对应分析/表格注明“来源时效性未核实”；该阈值可用 `MIN_ANALYSIS_ITEMS` 调整，不为凑条数编造事实。模型失败时保留原始证据；个别引用链接未匹配时去除该链接并标注“来源链接未核实”，保留其余分析。该校验不等于逐项事实验证，重要事件仍需查阅原始披露。
- 没有检索结果与搜索系统失败分别处理；全部主题处理失败时抛出错误，不生成成功简报。部分主题失败时保留失败说明及其他主题结果。
- 保留 `DEEPSEEK_MODEL` 原默认值，可通过同名环境变量覆盖；`DEEPSEEK_BASE_URL` 也可覆盖。未配置模型密钥时使用固定检索词，并生成未经分析的原始证据报告。

## 异步与限流

CLI 使用异步主题编排，主题内的网页查询并行，SEC 与网页检索并行。保留原有 SDK，通过 `asyncio.to_thread` 执行其阻塞调用，无新增异步 HTTP 依赖。同步入口仍可使用；异步调用方使用 `process_topic_async`、`aggregate_async` 或 `run_topics_async`。

通用搜索与 SEC 使用两个独立的进程级限流器，各自最多 10 个请求在途、任意滚动 1 秒内最多启动 10 个请求，两路不占用对方额度。所有市场的通用搜索后端共用通用额度，所有 SEC 实例共用 SEC 额度；SEC 不再额外串行等待每次 1 秒。Brave 另保留账户请求间隔。`SEARCH_CONCURRENCY` 可调低通用查询并发，不能突破通用搜索 10 的上限；`--max-workers` 控制并行主题数。

默认请求/查询解析最多尝试 **1 次**，失败不重试同一请求；不同后端的回退保留。SDK 自动重试关闭，SEC API 失败不再重复查询旧网页。所有主动等待固定 **1 秒**，异步等待使用 `asyncio.sleep(1)`，不再采用随机等待。

## 邮件（可选）

源代码不再保存邮件授权码。配置 `EMAIL_SENDER` 与 `EMAIL_PASSWORD` 后，显式使用 `--email` 才会发送：

```sh
.venv/bin/python run.py --tracker investment --topics 'BlackRock' --email 'recipient@example.com'
```

历史上 `--email` 同时充当发件地址；配置 `EMAIL_SENDER` 时它只作为收件地址。未配置 `EMAIL_SENDER` 时仍兼容使用 `--email` 作为发件地址。SMTP 默认 QQ Mail / SSL 465。生成失败或邮件发送失败会明确报错；发送失败时 PDF 保留。

原配置文件曾包含邮件授权码；建议在邮件服务中撤销旧授权码后重新设置环境变量。

## 验证

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q run.py config core trackers tests scripts
.venv/bin/python -m pip check
.venv/bin/python run.py --help
sh run.sh --help
```

测试不访问真实服务、不发送邮件；包含真实 PDF 渲染，因此需要上述 Pango 依赖。在受限沙箱中如遇字体缓存不可写，可为验证命令设置 `XDG_CACHE_HOME=/tmp/finance-fontcache`。

可选真实验证（会调用已配置搜索服务，端到端验证还会调用模型；不发邮件）：

```sh
.venv/bin/python scripts/smoke_search.py --query 'BlackRock investment capital flows'
.venv/bin/python scripts/smoke_search.py --market china --query '易方达基金 资金流向 投资'
.venv/bin/python scripts/smoke_pipeline.py --topic 'E Fund' --market china
```

端到端验证限制为 1 个主题、2 条查询、每条 3 个结果，输出耗时，并保存 PDF 和 Markdown 摘要到 `reports/validation/`。真实搜索结果会随服务与时间变化，不能替代确定性回归测试。

## 当前限制

SEC 尚未覆盖所有默认机构的已核实 CIK，仍需有效联系标识且可能受访问限制；后续可扩充准确的主体映射和持仓 XML 对照。国内交易所、基金公告与资金流数据目前通过统一网页搜索获取，尚无专用结构化采集器。资料多数为搜索摘要，日期未知、转载、来源冲突与模型事实错误仍需要核对原始文件。
