# DingPan

盯盘侠每日盯盘邮件工具。它会在交易日上午生成上一交易日的 A 股分析邮件，并通过 QQ 邮箱 SMTP 推送。

## 功能

- AKShare 拉取 A 股日线、资金流、个股新闻
- 本地计算 MA5/MA10/MA20、MACD、量能状态
- Gemini 2.5 Flash 输出结构化 JSON 分析
- Jinja2 渲染深色 HTML 邮件
- QQ 邮箱 SMTP 发送
- GitHub Actions 定时执行并保存 HTML artifact

## 项目结构

```text
dingpan/
├── .github/workflows/daily.yml
├── src/
├── templates/email_template.html
├── main.py
├── requirements.txt
└── README.md
```

## 本地运行

1. 安装依赖：

```bash
pip install -r requirements.txt
```

2. 配置环境变量。你可以直接 export，也可以在项目根目录创建 `.env`：

```bash
GEMINI_API_KEY=your_gemini_api_key
QQ_EMAIL=your_sender@qq.com
QQ_EMAIL_AUTH_CODE=your_qq_smtp_auth_code
RECEIVER_EMAIL=your_receiver@example.com
```

程序启动时会自动加载 `.env`，如果系统环境变量和 `.env` 同时存在，优先使用系统环境变量。

也可以直接在终端 export：

```bash
export GEMINI_API_KEY="your_gemini_api_key"
export QQ_EMAIL="your_sender@qq.com"
export QQ_EMAIL_AUTH_CODE="your_qq_smtp_auth_code"
export RECEIVER_EMAIL="first@example.com,second@example.com"
```

可选变量：

```bash
export STOCK_CODE="603212"
export STOCK_NAME="你的股票名称"
export COST_PRICE="你的持仓成本"
export DRY_RUN="true"
export DISABLE_PROXY="true"
export GEMINI_MODEL="gemini-3-flash-preview"
export GEMINI_FALLBACK_MODEL="gemini-2.5-flash-lite"
```

3. 本地预览：

```bash
python main.py --preview
```

这会生成 `output/dingpan_report_YYYYMMDD.html`，并尝试在本地浏览器打开。
如果是在交易日北京时间 15:00 之后做本地预览或 dry run，程序会优先使用当日收盘数据，方便手动测试。

默认会清理 `HTTP_PROXY` / `HTTPS_PROXY` 等代理环境变量，避免 AKShare 请求被本机代理拦截。如果你确实需要代理访问外部服务，可以显式设置：

```bash
DISABLE_PROXY=false python main.py --preview
```

4. 本地只跑不发信：

```bash
DRY_RUN=true python main.py
```

## GitHub Actions 部署

如果你要托管到 GitHub 仓库，建议把隐私相关内容都放到 `Settings -> Secrets and variables -> Actions`：

Secrets:
- `GEMINI_API_KEY`
- `QQ_EMAIL`
- `QQ_EMAIL_AUTH_CODE`
- `RECEIVER_EMAIL`

Variables:
- `STOCK_CODE`
- `STOCK_NAME`
- `COST_PRICE`
- `GEMINI_MODEL`（可选，默认已是 `gemini-3-flash-preview`）
- `GEMINI_FALLBACK_MODEL`（可选）

这样仓库代码里不需要暴露你的股票、持仓成本和收件人信息，后续即使公开仓库也不会泄漏这些配置。

然后确认 QQ 邮箱已经开启 SMTP 服务，并拿到授权码。工作流会在北京时间工作日 08:00 左右运行。你也可以在 GitHub 仓库的 `Actions` 页面手动触发：

- 默认 `send_email = false`，只生成 HTML artifact，适合在线测试
- 勾选 `send_email = true`，则手动触发时直接发邮件
- 手动触发属于 dry run/测试路径，若在交易日北京时间 15:00 之后执行，会优先尝试使用当日收盘数据

## 部署前你需要准备的内容

- 一个可用的 Gemini API Key
- 一个开启了 SMTP 的 QQ 邮箱
- QQ 邮箱授权码
- 一个或多个接收日报的邮箱地址，逗号分隔
- 股票代码、股票名称、持仓成本，建议只放 `.env` 或 GitHub Variables

## 已知限制

- `tool_trade_date_hist_sina` 若未及时覆盖到未来年份，程序会退化为按工作日判断，节假日可能需要人工留意
- AKShare 上游字段如果变动，可能需要更新字段映射
- 新闻模块失败时会自动降级为空，不阻断主流程
