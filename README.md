# Capital Tracker

金融资本资讯助手：围绕指定机构，检索国内与国际的投资、融资、基金募资、并购退出、证券持仓及资金流动资讯，整合来源，通过大模型分析并生成 PDF 报告。支持多搜索后端回退和多主题并发。

## 环境变量

在运行程序的终端中设置环境变量，可参考 [`.env.example`](.env.example)。程序**不会自动加载 `.env` 文件**，请自行导出或通过运行环境注入；不要将真实密钥提交到仓库。

| 环境变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 模型 API 密钥；启用模型分析时需要。未配置时生成原始检索证据报告。 |
| `DEEPSEEK_BASE_URL` | 模型服务地址，默认 `https://api.deepseek.com`；可配置兼容 OpenAI 接口的服务。 |
| `DEEPSEEK_MODEL` | 模型名称，项目默认 `deepseek-v4-flash`；请填写所用服务实际支持的模型名。 |
| `BRAVE_API_KEY` | 可选，启用 Brave Search。 |
| `PARALLEL_SEARCH_API_KEY` | 可选，启用 Parallel Search。 |
| `SERPER_API_KEY` | 可选，启用 Serper。 |
| `TAVILY_API_KEY` | 可选，启用 Tavily。 |
| `SEARCH_PROXY` | 可选，搜索服务使用的代理地址。 |
| `SEC_USER_AGENT` | 可选，启用 SEC 披露检索时填写应用名称及真实联系邮箱，例如 `CapitalTracker/1.0 you@example.com`。 |
| `EMAIL_SENDER` | 可选，邮件发送账号。 |
| `EMAIL_PASSWORD` | 可选，邮件服务密码或 SMTP 授权码；配合 `EMAIL_SENDER` 和命令行 `--email` 使用。 |

搜索默认依次尝试 DDGS news → Brave → Parallel → Serper → Tavily → DDGS text。DDGS 无需 API Key，其他搜索后端未配置密钥时自动跳过，无需全部配置。

```sh
export DEEPSEEK_API_KEY='你的模型 API 密钥'
export DEEPSEEK_BASE_URL='https://api.deepseek.com'
export DEEPSEEK_MODEL='你所用服务支持的模型名'
# 按需配置搜索 API Key，例如：
export BRAVE_API_KEY='你的 Brave API 密钥'
```

## 运行

需要 Python 3.12+ 和 WeasyPrint 所需的系统 Pango 库（Apple Silicon macOS 可用 `/opt/homebrew/bin/brew install pango`）。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run.py --tracker investment --topics 'BlackRock' 'Temasek'
.venv/bin/python run.py --tracker investment_china --topics '易方达' '高毅资产'
```

报告默认保存到 `reports/international/` 和 `reports/china/`；不指定 `--tracker` 时运行国内、国际两类。更多参数见 `run.py --help`。

## 改造成 Skill

你可以自行使用 **Codex / Claude Code** 阅读本项目，将检索、证据整合、分析和报告生成流程抽象成可复用的 Skill，并按自己的机构名单、研究维度和输出格式调整。例如：

> 请将本项目的金融资本资讯追踪流程封装成可复用的 Skill，包含使用说明、环境变量配置、命令入口和报告输出约定，复用现有代码，密钥通过环境变量读取。

当前项目提供 Python 命令行工具，尚未附带现成的 Skill。报告基于检索资料及模型分析，重要金额、事件与持仓变化请核对原始披露。
