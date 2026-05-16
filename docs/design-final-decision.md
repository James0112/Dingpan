# DingPan 多模型接入与个性化报告最终决议稿

## 文档目的

本文档是 DingPan 多模型接入、个性化报告、对话与用户画像方案的最终决议稿。

用途：
- 作为后续实施的唯一基线文档
- 替代主方案与 v2-v4 补充文档中的分散讨论结论
- 明确哪些规则已经冻结，哪些能力分阶段交付

不再包含：
- 备选方案比较
- 已否决路径
- 讨论过程中的临时表述

---

## 一、项目目标

本方案要解决四件事：

1. 在现有 Gemini 之外，接入可运行的 `gpt5.4` 模型。
2. 保持用户选择的 `model_id` 在共享分析、个性化分析、对话中的一致性。
3. 将报告拆分为“共享分析 + 个性化建议”，提升复用率并降低生成成本。
4. 逐步引入用户画像和对话能力，但不让复杂能力阻塞第一阶段上线。

---

## 二、冻结的设计决议

### 2.1 事实源

1. `model_pricing` 是模型目录唯一产品事实源。
2. 环境变量只负责 provider 运行时配置，如 `api_key`、`base_url`、`reasoning_effort`。
3. 代码只维护 provider 工厂注册，不维护模型清单。
4. `model_pricing.provider` 存代码路由类型，不存厂商名。

### 2.2 模型与报告

5. 用户选择的 `model_id` 贯穿共享分析、个性化分析、对话。
6. 共享分析按 `(stock_code, trade_date, model_id)` 隔离，每个模型一份公版。
7. 个性化报告按 `(user_id, stock_code, trade_date, model_id)` 隔离，切模型视为不同报告。
8. 共享层输出 10 个字段：`market_review`、`technical_signals`、`technical_analysis`、`fund_flow_analysis`、`news_impact`、`news_sentiment`、`bias`、`support_price`、`resistance_price`、`risk_notes`。
9. 个性化层输出 2+1 个字段：`executive_summary`、`action_advice`、`personal_risk_notes`。
10. 第一阶段不做跨 provider 自动降级，生成失败记录为 `failed`。

### 2.3 用户画像

11. 第四阶段前，AI 不得自动写入用户画像，必须经用户确认。
12. 用户手填内容优先级最高，AI 不可静默覆盖。
13. `context_version` 采用整数自增，用于驱动个性化缓存过期判断。

### 2.4 前端与阶段边界

14. 前端只展示 `is_active = 1 AND is_runnable = 1` 的模型。
15. 第二阶段不依赖 Next.js 才能交付，先用 Jinja2 + 简单 JS 跑通个性化报告。
16. 对话会话允许多会话，不加唯一约束。

---

## 三、系统边界与职责分工

### 3.1 数据库职责

数据库中的 `model_pricing` 负责描述：
- `model_id`
- `provider`
- `upstream_model_name`
- `display_name`
- `points_per_call`
- `is_active`
- `is_runnable`
- `sort_order`

这张表决定：
- 哪些模型是产品层已知模型
- 哪些模型对用户可见
- 哪些模型后端允许实际运行
- 每个 `model_id` 对应哪个 provider 和上游模型名

### 3.2 环境变量职责

环境变量负责 provider 运行时配置，不承载产品目录信息。

示例：
- `GEMINI_API_KEY`
- `GEMINI_MODEL`
- `GEMINI_FALLBACK_MODEL`
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `OPENAI_REASONING_EFFORT`

原则：
- 数据库决定“有哪些模型”
- 环境变量决定“这些 provider 如何连出去”

### 3.3 代码职责

代码层只维护 provider 类型到实现类的注册关系，例如：

- `gemini -> GeminiProvider`
- `openai -> OpenAIProvider`
- 后续可扩展 `deepseek -> DeepSeekProvider`

代码不维护 `model_id` 清单，不在代码里硬编码 `gpt5.4`、`gemini` 之类的模型目录。

---

## 四、模型目录与展示规则

### 4.1 provider 字段标准化

`model_pricing.provider` 的值统一使用代码路由类型：

| model_id | provider |
|----------|----------|
| `gemini` | `gemini` |
| `deepseek` | `deepseek` |
| `qwen` | `qwen` |
| `glm` | `glm` |
| `gpt5.4` | `openai` |
| `claude` | `claude` |

未接入模型也写预期的 provider 类型，后续只需补对应 Provider 类。

### 4.2 `is_active` 与 `is_runnable`

`model_pricing` 新增：
- `upstream_model_name TEXT`
- `is_runnable BOOLEAN DEFAULT 0`

当前前端展示规则冻结为：

```sql
WHERE is_active = 1 AND is_runnable = 1
```

第一阶段不做“即将上线”展示态，用户只能选到真正可运行的模型。

---

## 五、Provider 架构决议

### 5.1 Registry 机制

registry 启动时从数据库读取模型目录，再根据 `provider` 字段选择 Provider 类，根据 `upstream_model_name` 和环境变量构建实例。

路线如下：

1. 读取 `model_id`
2. 从 `model_pricing` 查到 `provider` 与 `upstream_model_name`
3. 从环境变量读取对应 provider 配置
4. 通过 provider 工厂实例化具体 Provider

### 5.2 analyze 接口收口

`analyze_market_data()` 收窄为业务接口，不再接受 provider 细节参数。

目标签名：

```python
def analyze_market_data(model_id, market_data, news_list)
```

业务层只知道 `model_id`，不知道：
- `api_key`
- `base_url`
- `upstream_model_name`
- `reasoning_effort`

这些细节由 registry 和 provider 内部处理。

### 5.3 OpenAI Provider

第一阶段新增 `OpenAIProvider`，用于通过自建 `sub2api` 调用 OpenAI 兼容接口。

要求：
- `base_url` 指向部署侧配置的 OpenAI 兼容入口
- 走 `responses` 风格接口
- 输出需标准化为可直接 `json.loads()` 的纯 JSON 字符串

---

## 六、共享报告与个性化报告

### 6.1 共享层

共享层仍由批量任务生成，按模型分别缓存。

缓存键：
- `(stock_code, trade_date, model_id)`

共享层字段：
- `market_review`
- `technical_signals`
- `technical_analysis`
- `fund_flow_analysis`
- `news_impact`
- `news_sentiment`
- `bias`
- `support_price`
- `resistance_price`
- `risk_notes`

### 6.2 个性化层

个性化层基于共享分析结论和用户画像生成。

缓存键：
- `(user_id, stock_code, trade_date, model_id)`

个性化字段：
- `executive_summary`
- `action_advice`
- `personal_risk_notes`

### 6.3 拆分原则

共享层负责：
- 客观市场事实
- 指标推导
- 技术结论
- 通用市场风险

个性化层负责：
- 结合用户仓位和风格的总体摘要
- 面向该用户的操作建议
- 面向该用户的风险提示

---

## 七、报告缓存与追踪字段

### 7.1 `analysis_cache` 追踪字段

第一阶段直接为 `analysis_cache` 增加：

```sql
actual_provider TEXT,
actual_model_name TEXT,
provider_response_id TEXT
```

含义：
- `actual_provider`：实际执行时使用的 provider 类型
- `actual_model_name`：实际执行时使用的上游模型名
- `provider_response_id`：上游返回的请求追踪 ID

Gemini 和 OpenAI 统一写入，允许为空。

### 7.2 错误语义

报告空态必须区分两种情况：

1. 尚未生成
2. 生成失败

语义定义：

- 无记录：视为“尚未生成”
- 有记录且 `status = 'failed'`：视为“生成失败”

用户侧文案原则：
- 尚未生成：提示报告正在生成中
- 生成失败：提示当前模型分析暂时不可用

日志和错误信息必须包含：
- `model_id`
- `provider`
- `upstream_model_name`

---

## 八、用户画像设计

### 8.1 `user_profiles`

第二阶段引入 `user_profiles`，第一版只保留极简字段：

```sql
user_profiles (
    user_id INTEGER PRIMARY KEY,
    risk_preference TEXT,
    trading_style TEXT,
    position_notes TEXT,
    custom_notes TEXT,
    context_version INTEGER DEFAULT 1,
    updated_by TEXT DEFAULT 'user',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

字段说明：
- `risk_preference`：风险偏好
- `trading_style`：交易风格
- `position_notes`：持仓概况
- `custom_notes`：其他偏好
- `context_version`：每次确认修改后递增
- `updated_by`：最近一次整体修改来源

### 8.2 `updated_by` 语义

`updated_by` 只表示最近一次整体修改来源，不表示字段级来源。

当前支持：
- `user`
- `ai`
- `admin`

如未来需要字段级来源追踪，再另行扩展设计。

### 8.3 画像写入规则

冻结规则：
- 第二阶段仅支持用户手动编辑画像
- 第四阶段前，AI 不得直接写画像
- 第四阶段引入 AI 建议更新时，也必须经过用户确认后才落库

---

## 九、个性化报告 API 决议

第二阶段实现 3 个接口：

1. `POST /api/personalized-analysis/generate`
2. `GET /api/personalized-analysis/status`
3. `GET /api/personalized-analysis`

职责如下：

- `generate`：幂等触发生成任务
- `status`：返回当前状态
- `GET result`：获取个性化分析结果

### 9.1 状态机

个性化分析状态机冻结为五态：

```text
missing -> generating -> ready
                     -> failed
ready -> stale
stale -> generating -> ready
```

其中：
- `missing`：还没有个性化记录
- `generating`：正在生成
- `ready`：已生成可用
- `failed`：生成失败
- `stale`：因画像版本变更而过期

### 9.2 过期判断

`personalized_analysis` 记录生成时的 `context_version`。

页面判断逻辑：
1. 读取当前 `user_profiles.context_version`
2. 与 `personalized_analysis.context_version` 比较
3. 不一致则视为 `stale`

---

## 十、对话系统决议

### 10.1 会话模型

对话系统允许多会话，不加唯一约束。

`conversations` 结构方向：

```sql
conversations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    conversation_type TEXT DEFAULT 'general',
    stock_code TEXT,
    title TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

规则：
- `conversation_type` 取值 `general` / `stock`
- `stock_code` 在 `stock` 会话中填写
- 同一用户、同一模型、同一股票可有多个会话
- `general` 会话同样允许多个

### 10.2 Prompt 注入规则

- `stock` 会话：注入该股票共享分析结论
- `general` 会话：不注入股票级共享分析

### 10.3 产品语义

UI 默认打开最近会话，允许用户：
- 新建会话
- 切换历史会话

数据层不提前做单会话限制，后续如有必要再由 UI 或业务层收紧。

---

## 十一、阶段实施计划

### 阶段一：GPT-5.4 接入

内容：
- `gpt5.4` 接入
- `model_pricing.provider` 标准化
- `model_pricing` 新增 `upstream_model_name`、`is_runnable`
- registry 改为数据库驱动
- 新增 `OpenAIProvider`
- `analyze_market_data()` 收窄签名
- `analysis_cache` 增加追踪字段
- 前端模型查询改为 `is_active = 1 AND is_runnable = 1`
- 报告页区分“未生成”和“失败”

交付标准：
- `gemini` 继续可用
- `gpt5.4` 可选且能生成共享报告
- 不可运行模型不进入用户主路径

### 阶段二：个性化报告

内容：
- 新增 `user_profiles`
- 用户手动编辑画像
- 新增 `personalized_analysis`
- 提供三接口个性化 API
- Jinja2 页面先渲染共享部分
- 页面内嵌 JS 轮询个性化状态并补齐个性化字段

交付标准：
- 同一份共享报告可叠加不同用户的个性化建议
- 用户修改画像后，个性化内容可失效并重算

### 阶段二.五：Next.js 前端迁移

内容：
- 将前端迁移为独立 Next.js 项目
- 报告页和设置页优先迁移
- 后端保持 API 契约稳定

交付标准：
- 前端迁移完成
- 不改变后端业务边界

### 阶段三：对话系统

内容：
- 引入 `conversations`
- 引入 `messages`
- 支持 `general` / `stock` 两类会话
- `stock` 会话注入共享分析结论

交付标准：
- 用户能按模型发起对话
- 会话按模型隔离

### 阶段四：AI 建议更新画像

内容：
- 从对话中提取画像更新建议
- 用户确认后才写入 `user_profiles`

交付标准：
- 画像可自动演化
- 最终修改权仍在用户

---

## 十二、第一阶段实施范围清单

第一阶段只做多模型接入，不提前实现第二阶段及以后能力。

明确包含：
- `model_pricing` schema 变更
- seed data 更新
- provider 路由重构
- `OpenAIProvider`
- `analysis_cache` 追踪字段
- 生成失败/未生成语义区分
- 前端模型筛选条件收紧

明确不包含：
- 个性化报告生成
- 用户画像编辑
- 对话系统
- Next.js 前端迁移
- 跨 provider 自动降级

---

## 十三、实施注意事项

1. `provider` 字段语义必须从第一阶段开始统一，避免后续重复迁移。
2. `gpt5.4` 的 `model_id` 与上游 `gpt-5.4` 必须保持解耦。
3. 任何用户可见模型都必须满足 `is_runnable = 1`。
4. provider 失败时必须留下足够的追踪信息，便于排查自建 `sub2api` 问题。
5. 第二阶段之前，不允许把个性化逻辑偷偷塞回共享 prompt。

---

## 十四、文档状态

状态：已冻结  
适用范围：多模型接入、个性化报告、对话、用户画像四阶段实施  
后续变更方式：如需修改本决议，必须新增补充文档并明确替换条款
