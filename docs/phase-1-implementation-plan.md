# DingPan 第一阶段实施清单

## 文档目的

本文档用于把 [design-final-decision.md](/Users/james/Documents/dev/projects/dingpan/docs/design-final-decision.md) 中的第一阶段决议拆成可执行任务。

范围只包含：
- `gpt54` 接入
- 模型目录与 provider 路由重构
- `analysis_cache` 追踪字段
- 前端模型可见范围收紧
- 报告空态语义区分

不包含：
- 个性化报告
- 用户画像
- 对话系统
- Next.js 前端迁移

---

## 一、第一阶段目标

交付后系统应满足：

1. `gemini` 继续可用。
2. `gpt54` 成为真正可运行的模型选项。
3. 前端只展示可运行模型。
4. 报告生成链路按 `model_id` 正确路由到对应 provider。
5. `analysis_cache` 能记录实际使用的 provider、模型名、请求追踪 ID。
6. 报告页能区分“尚未生成”和“生成失败”。

---

## 二、任务拆分

## 任务 1：数据库 schema 与 seed data 调整

目标：
- 让 `model_pricing` 成为第一阶段可用的模型目录事实源
- 让 `analysis_cache` 具备 provider 追踪能力

涉及文件：
- [src/database.py](/Users/james/Documents/dev/projects/dingpan/src/database.py)

改动项：
- `model_pricing` 新增字段：
  - `upstream_model_name TEXT`
  - `is_runnable BOOLEAN NOT NULL DEFAULT 0`
- `analysis_cache` 新增字段：
  - `actual_provider TEXT NOT NULL DEFAULT ''`
  - `actual_model_name TEXT NOT NULL DEFAULT ''`
  - `provider_response_id TEXT NOT NULL DEFAULT ''`
- `personalized_analysis` 唯一键暂不处理，保持第一阶段最小范围
- `_ensure_schema_migrations()` 增加上述字段的兼容迁移
- 更新 `MODEL_PRICING_SEED`

新的 seed 方向：

| model_id | provider | display_name | upstream_model_name | points | is_active | is_runnable | sort_order |
|----------|----------|--------------|---------------------|--------|-----------|-------------|------------|
| `gemini` | `gemini` | `Gemini 3 Flash Preview` | `gemini-3-flash-preview` | 1 | 1 | 1 | 1 |
| `deepseek` | `deepseek` | `DeepSeek` | `NULL` | 1 | 0 | 0 | 2 |
| `qwen` | `qwen` | `通义千问` | `NULL` | 1 | 0 | 0 | 3 |
| `glm` | `glm` | `GLM` | `NULL` | 2 | 0 | 0 | 4 |
| `gpt54` | `openai` | `GPT-5.4` | `gpt-5.4` | 3 | 1 | 1 | 5 |
| `claude` | `claude` | `Claude` | `NULL` | 3 | 0 | 0 | 6 |

验收标准：
- 新库初始化后表结构正确
- 老库启动后能自动补齐新增字段
- `model_pricing` 中只存在 `gemini` 和 `gpt54` 两个用户可见且可运行模型

---

## 任务 2：配置层 provider 化

目标：
- 去掉“只有 Gemini 才是正式配置”的结构偏差
- 为 OpenAI/sub2api 接入提供运行时配置

涉及文件：
- [src/config.py](/Users/james/Documents/dev/projects/dingpan/src/config.py)
- [README.md](/Users/james/Documents/dev/projects/dingpan/README.md)

改动项：
- 在 `Settings` 中新增：
  - `openai_api_key`
  - `openai_base_url`
  - `openai_reasoning_effort`
- 暂不把 `OPENAI_MODEL` 作为主事实源，实际模型名由 `model_pricing.upstream_model_name` 提供
- 保留现有 Gemini 字段，避免第一阶段过度重构
- README 增补 OpenAI/sub2api 所需环境变量说明

建议环境变量：
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_REASONING_EFFORT`

默认值建议：
- `OPENAI_BASE_URL=https://subapi.233clouds.com/v1`
- `OPENAI_REASONING_EFFORT=xhigh`

验收标准：
- `load_settings()` 能稳定读到 OpenAI 配置
- 缺失 OpenAI 配置时，仅在运行 `gpt54` 时失败，不影响 `gemini`

---

## 任务 3：Provider 基类与返回结构统一

目标：
- 为 Gemini 和 OpenAI 提供统一返回结构
- 让业务层拿到的不只是文本，还包括追踪信息

涉及文件：
- [src/providers/base.py](/Users/james/Documents/dev/projects/dingpan/src/providers/base.py)
- [src/providers/gemini.py](/Users/james/Documents/dev/projects/dingpan/src/providers/gemini.py)

改动项：
- 在 `base.py` 中新增 provider 返回结果结构，例如：
  - `ProviderResult.text`
  - `ProviderResult.actual_provider`
  - `ProviderResult.actual_model_name`
  - `ProviderResult.provider_response_id`
- `ModelProvider.generate()` 的返回值从 `str` 升级为结构化结果
- Gemini provider 适配新返回结构

说明：
- 如果 Gemini SDK 拿不到稳定的响应 ID，可以先填空字符串
- 这一层一定要先统一，否则 `analysis_cache` 的追踪字段无法通用落地

验收标准：
- Gemini provider 仍能正常返回 JSON 文本
- 业务层可以拿到 `actual_provider` 与 `actual_model_name`

---

## 任务 4：新增 OpenAIProvider

目标：
- 接入自建 `sub2api`
- 支持 `gpt54` 生成完整共享报告

涉及文件：
- `src/providers/openai.py` 新建
- 依赖可能需要补充到 [requirements.txt](/Users/james/Documents/dev/projects/dingpan/requirements.txt)

改动项：
- 新增 `OpenAIProvider`
- 基于 OpenAI 兼容接口调用 `responses` 风格 API
- provider 构造参数至少包含：
  - `api_key`
  - `base_url`
  - `model_name`
  - `reasoning_effort`
- 统一输出 `ProviderResult`
- 仅负责“调用并返回结果”，不负责业务解析

依赖说明：
- 如果当前环境没有 OpenAI Python SDK，需要补依赖
- 如决定不用 SDK 而直接走 HTTP，请保持 provider 层封装，不把 HTTP 细节泄露到业务层

验收标准：
- `gpt54` 能返回可 `json.loads()` 的纯 JSON 文本
- provider 能带出 `actual_provider=openai`
- provider 能记录 `actual_model_name=gpt-5.4`
- 若上游返回响应 ID，能写入 `provider_response_id`

---

## 任务 5：registry 改为数据库驱动

目标：
- 去掉按 `if/elif` 硬编码 `model_id` 的旧路由
- 让 registry 从 `model_pricing` 动态解析模型目录

涉及文件：
- [src/providers/registry.py](/Users/james/Documents/dev/projects/dingpan/src/providers/registry.py)
- 可能新增辅助查询函数到 [src/database.py](/Users/james/Documents/dev/projects/dingpan/src/database.py)

改动项：
- `get_provider()` 不再接受 `api_key/model_name/fallback_model_name`
- 改为接受：
  - `db_path`
  - `settings`
  - `model_id`
- 运行时从 `model_pricing` 查：
  - `provider`
  - `upstream_model_name`
  - `is_runnable`
- 根据 `provider` 选择对应 Provider 类
- 对 `is_runnable = 0` 或模型不存在的情况给出明确错误

建议行为：
- 查不到模型：`Unsupported or unknown model_id`
- 模型不可运行：`Model is not runnable`
- provider 未实现：`Provider not implemented`

验收标准：
- `gemini` 和 `gpt54` 都能通过同一入口拿到 provider
- registry 不再依赖硬编码的模型分支

---

## 任务 6：收窄 analyze 层接口

目标：
- 让 `src/analyze.py` 只处理 prompt 和 parse
- provider 选择与配置下沉到 registry

涉及文件：
- [src/analyze.py](/Users/james/Documents/dev/projects/dingpan/src/analyze.py)

改动项：
- `analyze_market_data()` 签名改为：
  - `model_id`
  - `market_data`
  - `news_list`
  - 可选传入 `db_path/settings` 或由模块内部读取
- 去掉：
  - `api_key`
  - `model_name`
  - `fallback_model_name`
- 调用 provider 后，不只返回 `AnalysisResult`
- 建议补一个分析包装结果，例如同时返回：
  - `analysis`
  - `actual_provider`
  - `actual_model_name`
  - `provider_response_id`

说明：
- 如果不想大改现有 `AnalysisResult`，可以新增 `AnalyzeOutput` 包装对象

验收标准：
- `analyze.py` 不再知道具体是 Gemini 还是 OpenAI
- 追踪字段能继续传递到写库层

---

## 任务 7：改造 main.py 与 generate.py 调用链

目标：
- 让所有分析入口都通过新的 provider 路由与返回结构

涉及文件：
- [main.py](/Users/james/Documents/dev/projects/dingpan/main.py)
- [generate.py](/Users/james/Documents/dev/projects/dingpan/generate.py)

改动项：
- `main.py` 改成按 `settings.model_id` 走新 `analyze_market_data()`
- `generate.py` 改成仅传 `model_id`
- `generate.py` 的 `upsert_analysis_cache()` 新增三个追踪字段写入
- 失败日志与 `error_message` 中加入：
  - `model_id`
  - `provider`
  - `actual_model_name` 或 `upstream_model_name`

说明：
- 第一阶段仍使用统一的 `generate_target_timeout_seconds`
- 不急着做 provider 级超时配置

验收标准：
- `generate.py` 跑 `gemini` 不回归
- `generate.py --model gpt54` 能生成并写库
- 失败记录能带足排查信息

---

## 任务 8：前端模型查询条件收紧

目标：
- 用户只能看到真正可运行模型

涉及文件：
- [app.py](/Users/james/Documents/dev/projects/dingpan/app.py)
- [templates/dashboard.html](/Users/james/Documents/dev/projects/dingpan/templates/dashboard.html)
- [templates/settings_placeholder.html](/Users/james/Documents/dev/projects/dingpan/templates/settings_placeholder.html)

改动项：
- 所有模型查询改为：

```sql
SELECT ...
FROM model_pricing
WHERE is_active = 1 AND is_runnable = 1
ORDER BY sort_order ASC, model_id ASC
```

- 涉及页面：
  - Dashboard
  - Settings
  - 更新默认模型接口校验

验收标准：
- 前端只显示 `gemini`、`gpt54`
- 用户无法将默认模型或订阅模型设置为不可运行模型

---

## 任务 9：报告空态区分“未生成”和“失败”

目标：
- 让报告页与用户感知对齐真实状态

涉及文件：
- [app.py](/Users/james/Documents/dev/projects/dingpan/app.py)
- [templates/report_empty.html](/Users/james/Documents/dev/projects/dingpan/templates/report_empty.html)
- 可选新增专门的失败模板，或在同模板中区分状态

改动项：
- `report_latest_page` 和 `report_page` 查询缓存时，不只看 `status='success'`
- 需要识别：
  - 无记录
  - 最新记录 `failed`
- 页面文案区分：
  - 无记录：报告正在生成中
  - failed：当前模型分析暂时不可用，请稍后重试

建议模板入参：
- `cache_status`
- `model_id`
- `trade_date`

验收标准：
- 用户能区分“还没生成”与“生成失败”
- 不暴露底层异常细节给用户

---

## 任务 10：文档与运行说明收尾

目标：
- 保证后续开发和部署知道如何启用 `gpt54`

涉及文件：
- [README.md](/Users/james/Documents/dev/projects/dingpan/README.md)
- 可选新增运维说明文档

改动项：
- 更新模型说明
- 增加 OpenAI/sub2api 环境变量示例
- 增加第一阶段可用模型说明
- 增加基础验证命令示例

建议补充命令：

```bash
python generate.py --model gemini --limit 1 --dry-run
python generate.py --model gpt54 --limit 1 --dry-run
```

验收标准：
- 新人只看 README 就能理解如何启用 `gpt54`

---

## 三、建议实施顺序

建议顺序如下：

1. 先改 `src/database.py`
2. 再改 `src/config.py`
3. 再统一 `src/providers/base.py`
4. 新增 `src/providers/openai.py`
5. 重构 `src/providers/registry.py`
6. 收口 `src/analyze.py`
7. 改 `main.py`、`generate.py`
8. 改 `app.py` 的模型查询与报告空态
9. 最后更新模板与 README

原因：
- 先把 schema 和 provider 基础打稳
- 再改业务链路
- 最后处理用户可见层

---

## 四、第一阶段测试清单

## 4.1 数据迁移验证

- 新库初始化后字段完整
- 老库启动后自动补齐：
  - `model_pricing.upstream_model_name`
  - `model_pricing.is_runnable`
  - `analysis_cache.actual_provider`
  - `analysis_cache.actual_model_name`
  - `analysis_cache.provider_response_id`

## 4.2 模型目录验证

- Dashboard 模型下拉只显示 `gemini`、`gpt54`
- Settings 默认模型选择器只显示 `gemini`、`gpt54`
- API 拒绝选择不可运行模型

## 4.3 Gemini 回归验证

- `python generate.py --model gemini --limit 1 --dry-run`
- 报告生成成功
- 追踪字段能写入或安全留空

## 4.4 GPT-5.4 验证

- `python generate.py --model gpt54 --limit 1 --dry-run`
- 能通过 sub2api 生成结果
- 输出是合法 JSON
- 写库记录包含：
  - `actual_provider=openai`
  - `actual_model_name=gpt-5.4`

## 4.5 失败路径验证

- 刻意不给 `OPENAI_API_KEY` 时运行 `gpt54`
- 确认写入 `failed`
- 报告页展示“当前模型分析暂时不可用”
- `gemini` 路径不受影响

---

## 五、已知实现取舍

第一阶段明确不做：

- provider 级超时配置
- provider 自动降级
- “即将上线”模型展示
- 个性化报告字段拆分
- 用户画像
- 对话与流式输出

这些都属于后续阶段，不能在第一阶段顺手混入。

---

## 六、完成定义

第一阶段只有在以下条件同时满足时，才算完成：

1. 生产数据库可兼容迁移。
2. `gemini` 无回归。
3. `gpt54` 可从前端选择并进入生成链路。
4. `generate.py` 能按 `model_id` 路由 provider。
5. `analysis_cache` 追踪字段落库。
6. 报告页空态语义清晰。
7. README 已更新。
