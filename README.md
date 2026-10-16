# Evident · 投资事件情报与证据时间线

> 题目 08：同一投资事件可能以公告、新闻、研报观点和市场传闻等形式持续演化。
> 本产品帮助用户看清**事件最初来源、事实变化、证据冲突、当前状态以及与关注标的的关系**。

## 一、启动方式

```bash
cd evident
pip install fastapi uvicorn requests   # 如未安装

# 三种数据模式（默认 auto）
export EVIDENT_DATA_MODE=auto          # real / mock / auto
export FUYAO_API_KEY=<你的key>          # real 模式必需；auto 无 key 时自动降级 mock

python3 run.py                         # 默认 0.0.0.0:8000
# 打开 http://localhost:8000
```

## 二、核心设计

### 数据流水线

```
Ingest(扶摇REST) → Atomize(证据原子) → Resolve(事件归并★) → Adjudicate(冲突/状态机) → Present(UI)
```

### 证据原子（核心数据单元）

| 字段 | 对应题目要求 |
|---|---|
| `claim_type`: fact / opinion / speculation / rumor | 区分**事实、观点、推测、传闻** |
| `source_tier`: official(1.0) / company(0.9) / media_major(0.7) / research(0.55) / media_general(0.4) / unattributed(0.2) | **证据权重**（权重表显式声明，UI 可见） |
| `event_time / disclose_time / fetch_time / update_time` | **四种时间**；`披露≠更新` 即"发布后被修改"的直接证据 |

### 事件归并（算法心脏）

普通做法按股票代码分组，会把"传闻→回应→公告"拆成三条记录，恰好丢掉"版本演化"。
本系统采用**可解释规则归并**：`标的重合 + 主题锚点相容 + 关键词Jaccard + 12天时间窗`，
每个归并输出 `resolve_basis` 依据；同标的不同事项（如收购传闻 vs 增持公告）被**主题锚点**切开，避免误合并。

### 状态机（子线索仲裁）

```
RUMORED → CONFIRMED(官方证实) / DENIED(官方否认) / CORRECTED(更正) / EXPIRED(超期) / DISPUTED(冲突未消解)
```

状态由**全量证据加权仲裁**得出（非取最后一条），且在**主题子线程**内分别裁定——
避免"增持公告的更正"错误覆盖"收购传闻的否认"。

### 数据健康度（合规关键）

扶摇文档原文："空列表也可能表示服务降级，不能据此断言期间没有异动。"
因此接口失败/降级/截断（`was_truncated`）时 UI 顶部显示**显式警告横幅**，绝不静默生成"正常"结论。

## 三、AI 的角色

- AI（Claude）参与：架构设计、全部代码生成、mock 语料构造、联调自测与 2 处 bug 修复
  （①过合并→主题锚点；②状态机跨子线索污染→子线程仲裁）。
- 人在环：设计决策、验收标准、算法取舍（可解释规则优先于黑盒相似度）。
- 产品运行时**不使用 LLM 做结论生成**——所有归类/归并/仲裁均为确定性规则，保证可审计、可复现。

## 四、数据来源

| 数据 | 接口 | 用途 |
|---|---|---|
| 个股分时新闻 | `GET /api/news/stock/timeline-list` | 证据主来源（含 source/publish_time_ms） |
| 异动解读 | `GET /api/news/anomaly/analysis` | 事件发现（first_publish_time / tag_code_update_time） |
| 快讯 | `GET /api/news/flash/list`（预留） | 高频演化跟踪（create/update 双时间戳） |
| 行情/估值 | 预留 | 标的关联展示 |

认证：`X-api-key` 头；key 仅经环境变量注入，不入仓库（见 `.gitignore`）。
iFinD MCP：沙箱内无凭据可调用，做适配器预留位，如实列为已知边界。

## 五、已知边界与未做事项

- 未做：研报观点库接入、LLM 语义级冲突检测（当前为线索词+数值口径规则）、多用户/账号体系、演示视频。
- 归并为规则策略：召回与精度在复杂语料下需继续调参；低置信归并已避免强合并。
- 真实模式下无 key 时会显式失败或降级并警告，不会伪造数据。

## 六、合规声明

不输出涨跌预测、收益承诺或买卖建议；事实/推断/不确定在 UI 上视觉区分；所有结论可回溯至原始记录 ID 与原文链接。
