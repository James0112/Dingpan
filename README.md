# DingPan

盯盘侠当前是一个最小 MVP：
- 保留原有的每日邮件日报
- 提供多用户 Web 端注册、登录、自选股与报告页
- 支持 Web Push 订阅与按用户设定时间发送日报通知

## 功能

- AKShare 拉取 A 股日线、资金流、个股新闻
- 本地计算 MA5/MA10/MA20、MACD、量能状态
- Gemini 3 Flash Preview / GPT-5.4 输出结构化 JSON 分析
- Jinja2 渲染深色 HTML 邮件
- Resend API 发送认证邮件与报告邮件
- SQLite 持久化用户、订阅、共享分析缓存、Push 订阅
- FastAPI SSR 页面：登录、注册、Dashboard、报告页
- Web Push 订阅、测试推送、按用户设定时间发送通知

## 项目结构

```text
dingpan/
├── app.py
├── generate.py
├── send_reports.py
├── send_push.py
├── scripts/generate_vapid_keys.py
├── .github/workflows/daily.yml
├── src/
├── templates/
├── static/
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
OPENAI_API_KEY=your_openai_api_key
OPENAI_BASE_URL=https://subapi.233clouds.com/v1
OPENAI_REASONING_EFFORT=xhigh
RECEIVER_EMAIL=your_receiver@example.com
JWT_SECRET=change-this-in-production
SITE_URL=http://127.0.0.1:8000
DB_PATH=data/dingpan.db
RESEND_API_KEY=re_xxxxxxxx
MAIL_FROM_AUTH=auth@mail.manuflow.net
MAIL_FROM_REPORTS=reports@mail.manuflow.net
```

程序启动时会自动加载 `.env`，如果系统环境变量和 `.env` 同时存在，优先使用系统环境变量。

也可以直接在终端 export：

```bash
export GEMINI_API_KEY="your_gemini_api_key"
export OPENAI_API_KEY="your_openai_api_key"
export OPENAI_BASE_URL="https://subapi.233clouds.com/v1"
export OPENAI_REASONING_EFFORT="xhigh"
export RECEIVER_EMAIL="first@example.com,second@example.com"
export JWT_SECRET="change-this-in-production"
export SITE_URL="http://127.0.0.1:8000"
export RESEND_API_KEY="re_xxxxxxxx"
export MAIL_FROM_AUTH="auth@mail.manuflow.net"
export MAIL_FROM_REPORTS="reports@mail.manuflow.net"
```

可选变量：

```bash
export STOCK_CODE="603212"
export STOCK_NAME="你的股票名称"
export COST_PRICE="你的持仓成本"
export DRY_RUN="true"
export DISABLE_PROXY="true"
export MODEL_ID="gemini"
export GEMINI_MODEL="gemini-3-flash-preview"
export GEMINI_FALLBACK_MODEL="gemini-2.5-flash-lite"
export GENERATE_TARGET_TIMEOUT_SECONDS="180"
```

第一阶段当前可运行模型：

- `gemini`
- `gpt5.4`

其中：
- `gemini` 实际调用 `gemini-3-flash-preview`
- `gpt5.4` 实际调用 `gpt-5.4`

Web Push 变量：

```bash
export VAPID_PUBLIC_KEY="..."
export VAPID_PRIVATE_KEY="..."
export VAPID_CLAIMS_EMAIL="your@email.com"
```

VAPID 密钥可以本地生成：

```bash
python scripts/generate_vapid_keys.py
```

2. 邮件模式本地预览：

```bash
python main.py --preview
```

这会生成 `output/dingpan_report_YYYYMMDD.html`，并尝试在本地浏览器打开。
如果是在交易日北京时间 15:00 之后做本地预览或 dry run，程序会优先使用当日收盘数据，方便手动测试。

默认会清理 `HTTP_PROXY` / `HTTPS_PROXY` 等代理环境变量，避免 AKShare 请求被本机代理拦截。如果你确实需要代理访问外部服务，可以显式设置：

```bash
DISABLE_PROXY=false python main.py --preview
```

3. 本地只跑不发信：

```bash
DRY_RUN=true python main.py
```

4. 启动 Web 端：

```bash
uvicorn app:app --reload
```

打开：

- `http://127.0.0.1:8000/`
- 注册 / 登录
- Dashboard 添加自选股
- `generate.py` 生成共享分析
- Dashboard / Report 查看报告

5. 生成共享分析缓存：

```bash
python generate.py
```

常用参数：

```bash
python generate.py --stock 603212
python generate.py --model gemini --limit 1 --dry-run
python generate.py --model gpt5.4 --limit 1 --dry-run
python generate.py --date 2026-05-07
python generate.py --dry-run
python generate.py --today-if-trading-day
```

6. 本地测试 Web Push：

前提：
- `.env` 里已经配置 `VAPID_*`
- 浏览器允许当前站点通知权限
- Dashboard 上已经开启推送

测试步骤：

```bash
# 手动发测试推送（推荐先从页面按钮测）
```

Dashboard 上点击：
- `开启推送`
- `发送测试推送`

也可以跑按时间派发脚本：

```bash
python send_push.py
```

仅查看当前时间窗口会命中哪些用户：

```bash
python send_push.py --dry-run
```

指定交易日：

```bash
python send_push.py --date 2026-05-07
```

说明：
- `已开启推送` 代表浏览器订阅成功
- `发送测试推送` 或 `python send_push.py` 成功，说明通知已经送达浏览器推送服务
- macOS / Chrome 当前前台窗口不一定弹出明显横幅，可能只进通知中心

7. 当前 MVP 关于“定时推送”的真实含义：

- 代码已经支持“按用户设置的时间筛选并推送”
- 但需要外部调度器定时执行 `send_push.py`
- 例如每 5 分钟执行一次，脚本内部会判断哪些用户此刻到点、且当天还没推过

8. 多用户每日报告邮件：

- 认证邮件和报告邮件都通过 Resend 发送
- 用户在 Settings 页面开启“每日报告邮件”后，系统会使用注册邮箱接收日报
- 邮件发送时间与当前 `daily_push_time` 共用

手动派发：

```bash
python send_reports.py
```

仅查看当前时间窗口会命中哪些用户/股票：

```bash
python send_reports.py --dry-run
```

指定交易日：

```bash
python send_reports.py --date 2026-05-08
```

手动测试某个交易日但不占用次日正式日报去重：

```bash
python send_reports.py --date 2026-05-08 --delivery-type manual_test
```

## 部署建议

线上最小部署建议：

- 1 个 `uvicorn` / `gunicorn+uvicorn` Web 进程
- 1 个定时任务执行 `generate.py`
- 1 个定时任务执行 `send_reports.py`
- 1 个定时任务执行 `send_push.py`
- Nginx 反向代理并启用 HTTPS

推荐使用仓库内的 `deploy/systemd/` 单元文件，而不是直接写一个长期常驻的 `simple` service。
`generate.py` 和 `send_reports.py` 都应配置为 `Type=oneshot`，并加上 `TimeoutStartSec`，避免一次外部接口阻塞把后续所有 timer 调度堵死。

### 最小调度建议

共享分析缓存：

```cron
30 15 * * 1-5 cd /path/to/dingpan && /path/to/.venv/bin/python generate.py --force --today-if-trading-day >> logs/generate_close.log 2>&1
20 7 * * 1-5 cd /path/to/dingpan && /path/to/.venv/bin/python generate.py --force >> logs/generate_refresh.log 2>&1
```

日报 Push：

```cron
*/5 * * * * cd /path/to/dingpan && /path/to/.venv/bin/python send_push.py >> logs/send_push.log 2>&1
```

日报邮件：

```cron
45 7 * * 1-5 cd /path/to/dingpan && /path/to/.venv/bin/python send_reports.py --force >> logs/send_reports.log 2>&1
```

说明：
- 建议收盘后 `15:30` 生成当天交易日缓存
- 建议次日早上 `07:20` 再补刷新一次前一交易日缓存，补齐隔夜新闻
- `send_push.py` 建议每 5 分钟跑一次
- 脚本内部有用户时间窗口判断和当日去重，不会无限重复推
- `generate.py` 现在默认对单只股票设置 `180` 秒硬超时；若某只股票卡在 AKShare、新闻或 Gemini 请求，该股票会记为失败并继续处理其他股票，不会让整个生成进程无限挂起

## GitHub Actions 部署

如果你要托管到 GitHub 仓库，建议把隐私相关内容都放到 `Settings -> Secrets and variables -> Actions`：

Secrets:
- `GEMINI_API_KEY`
- `RESEND_API_KEY`
- `RECEIVER_EMAIL`

Variables:
- `STOCK_CODE`
- `STOCK_NAME`
- `COST_PRICE`
- `GEMINI_MODEL`（可选，默认已是 `gemini-3-flash-preview`）
- `GEMINI_FALLBACK_MODEL`（可选）

这样仓库代码里不需要暴露你的股票、持仓成本和收件人信息，后续即使公开仓库也不会泄漏这些配置。

然后确认 Resend 已验证 `mail.manuflow.net` 域名，并配置了 `MAIL_FROM_REPORTS`。工作流会在北京时间工作日 08:00 左右运行。你也可以在 GitHub 仓库的 `Actions` 页面手动触发：

- 默认 `send_email = false`，只生成 HTML artifact，适合在线测试
- 勾选 `send_email = true`，则手动触发时直接发邮件
- 手动触发属于 dry run/测试路径，若在交易日北京时间 15:00 之后执行，会优先尝试使用当日收盘数据

## 部署前你需要准备的内容

- 一个可用的 Gemini API Key
- 一个已在 Resend 验证通过的发信域
- Resend API Key
- 一个或多个接收日报的邮箱地址，逗号分隔
- 股票代码、股票名称、持仓成本，建议只放 `.env` 或 GitHub Variables

## 已知限制

- `tool_trade_date_hist_sina` 若未及时覆盖到未来年份，程序会退化为按工作日判断，节假日可能需要人工留意
- AKShare 上游字段如果变动，可能需要更新字段映射
- 新闻模块失败时会自动降级为空，不阻断主流程
- 资金流接口偶发失败时，单只股票共享分析可能写入 `failed`
- 当前还不是完整 PWA 安装形态，重点先验证 Web 访问与通知 MVP
