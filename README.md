# HTTP 语言协商解释 API

供国际化网关调用的语言协商微服务：接收客户端 `Accept-Language` 头、服务端支持的
BCP 47 语言标签列表与默认语言，依据 RFC 9110 权重规则与 RFC 4647 lookup 逐级截断
匹配，选出最终语言，并返回**每一个候选标签的接受 / 淘汰轨迹**，方便网关侧排障与
可观测。当标准 lookup 没有可接受候选时，还可通过可选的 `locale_fallbacks`
源标签 → 下一标签有向链做定向回退。此外，`/api/v1/negotiate/group` 提供**群体
协商**：多位参与者各自给出 `Accept-Language`，逐人复用同一套匹配语义构成权重
矩阵，具体范围 `q=0` 全局否决，按最低个人权重 → 总权重 → 服务端顺序选出共同
接受的语言。`/api/v1/negotiate/coverage-plan` 提供**覆盖规划**：给定带非负
traffic 的 `Accept-Language` 流量样本与 1～12 个候选新增语言，穷举所有不超过
`max_additions`（1～3）个新增的候选子集，复用同一套协商语义计算每个子集可将多少
流量从默认兜底转为正常命中，选出改善流量最大的方案（同分依次取新增数更少、
候选输入序更早）。`/api/v1/negotiate/q-stability` 提供**偏好权重稳定区间分析**：
请求体与单人协商完全相同，服务端先给出基准协商结果，再对去重后的每个语言范围单独
扫描 RFC 允许的 `0.000`～`1.000` 三位小数 q 网格，求出保持 `selected_language` 与
`fallback` 状态均不变的最大连续区间，并报告越过边界后会得到什么结果。

技术栈：Python 3.12 · FastAPI · Pydantic v2 · Uvicorn，依赖在容器构建时安装。

## 单条命令启动

```bash
docker compose up -d --build
```

服务随后在 **http://localhost:8000** 提供访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查（`GET`，返回 `{"status":"ok"}`） |
| http://localhost:8000/api/v1/negotiate | 语言协商接口（`POST`） |
| http://localhost:8000/api/v1/negotiate/q-stability | 偏好权重稳定区间分析接口（`POST`） |
| http://localhost:8000/api/v1/negotiate/group | 群体语言协商接口（`POST`） |
| http://localhost:8000/api/v1/negotiate/coverage-plan | 候选语言覆盖规划接口（`POST`） |
| http://localhost:8000/docs | Swagger UI 交互式文档 |
| http://localhost:8000/openapi.json | OpenAPI 3 描述 |

停止：`docker compose down`

## 调用示例

```bash
curl -X POST http://localhost:8000/api/v1/negotiate \
  -H 'Content-Type: application/json' \
  -d '{
    "accept_language": "zh-TW;q=0.95,zh;q=0.9,en;q=0,*;q=0.5",
    "supported_languages": ["en-US", "zh-CN", "zh-TW", "ja-JP"],
    "default_language": "en-US"
  }'
```

响应（节选）：

```json
{
  "selected_language": "zh-TW",
  "selected_supported_index": 2,
  "fallback": false,
  "fallback_reason": null,
  "parsed_ranges": [
    {"range": "zh-tw", "q": 0.95, "position": 1},
    {"range": "zh", "q": 0.9, "position": 2},
    {"range": "en", "q": 0.0, "position": 3},
    {"range": "*", "q": 0.5, "position": 4}
  ],
  "ignored_duplicates": [],
  "candidates": [
    {
      "tag": "en-US",
      "supported_index": 0,
      "truncation_chain": ["en-us", "en"],
      "matched_range": "en",
      "q": 0.0,
      "status": "excluded",
      "detail": "匹配到的范围 'en' q=0，候选被显式排除",
      "events": [ {"truncated_to": "en-us", "outcome": "no-match"},
                  {"truncated_to": "en", "outcome": "match",
                   "matched_range": "en", "q": 0.0, "header_position": 3},
                  {"truncated_to": "*", "outcome": "skipped",
                   "detail": "存在更具体的语言范围匹配，通配符未参与"} ]
    }
  ]
}
```

### 请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `accept_language` | string \| null | 否 | 原始 `Accept-Language` 头；缺失、`null` 或空白串视为“无有效范围” |
| `supported_languages` | string[] | 是 | 服务端支持的 BCP 47 标签，至少 1 个、不可重复（大小写不敏感）；**顺序即稳定优先级** |
| `default_language` | string | 是 | 默认语言，必须包含在 `supported_languages` 中（大小写不敏感） |
| `locale_fallbacks` | object<string,string> \| null | 否 | 语言回退有向链：源标签 → 下一标签。仅在标准 lookup 没有可接受候选时启用 |

### `locale_fallbacks` 定向回退

`locale_fallbacks` 是一张「源标签 → 下一标签」的有向图，请求时以 JSON 对象给出，
例如：

```json
"locale_fallbacks": {"gsw": "de", "de": "zh-CN", "it": "it-IT", "it-IT": "zh-TW"}
```

触发与选择规则：

1. **仅在标准 lookup 无可接受候选时触发**；只要存在正常选出的候选，链回退完全不
   参与，响应行为与未提供该字段时一致。
2. 取头中所有**严格正 q 的具体范围**（通配符 `*` 不参与），按 **q 权重降序**、
   权重相同按**头部出现顺序**排列，依次尝试：
   - 若该范围标签在 `locale_fallbacks` 中没有出边，则跳过该范围；
   - 否则沿链逐跳前进，链上**第一个属于 `supported_languages` 的节点**即为命中
     （大小写不敏感，如 `gsw → de → zh-CN`）；
   - 若该受支持节点被**任一 `q=0` 具体范围**排除（按 lookup 逐级截断前缀判定，
     如 `zh;q=0` 同样排除 `zh-CN`），则跳过并继续沿链前进；
   - 一条链走到尽头仍无可用节点时，继续尝试下一个源范围。
3. **通配符 `*;q=0` 不阻止链选择**：只有「具体」q=0 范围能排除链上候选。
4. 命中后响应增加 `locale_fallback` 对象，给出**触发回退的源范围、实际路径与最终
   标签**；链未触发时该字段**整体缺省**（不会返回 `null`），以保持旧响应结构：

```json
{
  "triggered": true,
  "source_range": "gsw",
  "source_q": 0.9,
  "source_position": 1,
  "path": ["gsw", "de", "zh-cn"],
  "selected_language": "zh-CN"
}
```

> 链命中时 `fallback` 仍为 `false`（不是默认语言兜底）；被选中的候选 `status`
> 为 `selected`，其 `detail` 与顶层 `message` 会说明走过的完整路径。
> 若提供了链但所有源范围都走不到未被排除的受支持语言，则仍回退默认语言
> （`fallback=true`、`fallback_reason=no-acceptable-candidate`），`message`
> 中会额外说明链也未找到可用语言。

```bash
curl -X POST http://localhost:8000/api/v1/negotiate \
  -H 'Content-Type: application/json' \
  -d '{
    "accept_language": "gsw;q=0.9,fr;q=0.5",
    "supported_languages": ["en-US", "zh-CN", "zh-TW", "ja-JP"],
    "default_language": "en-US",
    "locale_fallbacks": {"gsw": "de", "de": "zh-TW", "fr": "ja-JP"}
  }'
```

标准 lookup 中 `gsw`、`fr` 都不命中任何受支持标签，于是按 q 降序先沿
`gsw → de → zh-TW` 找到受支持的 `zh-TW`（响应节选）：

```json
{
  "selected_language": "zh-TW",
  "selected_supported_index": 2,
  "fallback": false,
  "fallback_reason": null,
  "locale_fallback": {
    "triggered": true,
    "source_range": "gsw",
    "source_q": 0.9,
    "source_position": 1,
    "path": ["gsw", "de", "zh-tw"],
    "selected_language": "zh-TW"
  }
}
```

`locale_fallbacks` 的校验（任一不过均返回 **HTTP 422**，`loc` 为
`["body","locale_fallbacks"]`）：

- 必须是 JSON 对象，键和值都必须是合法 BCP 47 标签；
- 源标签不可重复（**大小写不敏感**）；
- 不允许**环路**：从任一源出发沿链前进时不能重复访问节点（在到达受支持标签之前的
  自环也算环路）；
- 不允许**悬空链**：每条链都必须能到达某个 `supported_languages`。

### 响应要点

- `selected_language`：最终选中的服务端标签（保持服务端原始大小写）。
- `fallback` / `fallback_reason`：
  - `no-valid-range`：请求头缺失或没有任何有效范围 → 回退默认语言；
  - `no-acceptable-candidate`：头合法但所有候选都被 `q=0` 排除或未命中 → 回退默认语言。
- `parsed_ranges`：解析出的语言范围（小写、去重后的结果，含位置与 q 值）。
- `ignored_duplicates`：重复范围中被忽略的后续出现（保留第一次出现）。
- `candidates[]`：每个候选的完整轨迹：
  - `truncation_chain`：逐级截断链，如 `zh-Hant-TW → zh-Hant → zh`；
  - `events[]`：每一级截断的 `match` / `no-match`，以及通配符 `*` 的
    `match` / `skipped`（存在更具体匹配时通配符不参与）；
  - `matched_range`、`q`：最终命中的客户端范围及权重；
  - `status`：`selected` / `eligible` / `excluded`（q=0）/ `unmatched`（未命中）/
    `no-range`（头无有效范围）。
- `locale_fallback`：**仅当 `locale_fallbacks` 链实际选中语言时出现**，记录触发源
  范围（`source_range`/`source_q`/`source_position`）、实际路径 `path`（小写标签，
  含起点与终点）与最终标签 `selected_language`；未触发时该键缺省。

## 偏好权重稳定区间分析（POST /api/v1/negotiate/q-stability）

当需要回答「某个语言范围的 q 在多大范围内调整，协商结果不会变？」时（例如评估
客户端权重抖动、AB 调参的敏感度），使用稳定区间分析接口。**请求体与单人协商接口
完全相同**（同样支持 `locale_fallbacks`，校验规则也一致）。服务端先按现有规则
跑一次基准协商，再对**去重后的每个语言范围**单独扫描 RFC 9110 允许的
**`0.000`～`1.000` 三位小数 q 网格**（共 1001 个点），扫描某一范围时**其余范围
（含重复出现、通配符）的原始 q 与拼写保持不变**，求出：

- **包含该范围原 q 的最大连续区间**，使得区间内每个网格点的 `selected_language`
  与 `fallback` 状态都与基准完全一致；
- 当区间外仍存在网格点时，给出**紧邻边界的第一个网格点**重新协商后的新
  `selected_language` 与 `fallback`（该点的结果必然与基准不同）。

通配符、RFC 4647 逐级截断匹配、`q=0` 排除与 `locale_fallbacks` 链全部**原样复用
`/negotiate` 的协商逻辑**；非法 `Accept-Language` 头沿用现有 **422** 响应。

```bash
curl -X POST http://localhost:8000/api/v1/negotiate/q-stability \
  -H 'Content-Type: application/json' \
  -d '{
    "accept_language": "zh-CN,zh;q=0.9,en;q=0.8,*;q=0.5",
    "supported_languages": ["en-US", "zh-CN", "zh-TW", "ja-JP"],
    "default_language": "en-US"
  }'
```

响应（节选）：

```json
{
  "baseline": {
    "selected_language": "zh-CN",
    "selected_supported_index": 1,
    "fallback": false,
    "fallback_reason": null
  },
  "ranges": [
    {
      "range": "zh-cn",
      "position": 1,
      "original_q": 1.0,
      "lower_bound": 0.9,
      "upper_bound": 1.0,
      "lower_outside": {"q": 0.899, "selected_language": "zh-TW", "fallback": false},
      "upper_outside": null
    },
    {
      "range": "zh",
      "position": 2,
      "original_q": 0.9,
      "lower_bound": 0.0,
      "upper_bound": 1.0,
      "lower_outside": null,
      "upper_outside": null
    },
    {
      "range": "en",
      "position": 3,
      "original_q": 0.8,
      "lower_bound": 0.0,
      "upper_bound": 0.999,
      "lower_outside": null,
      "upper_outside": {"q": 1.0, "selected_language": "en-US", "fallback": false}
    },
    {
      "range": "*",
      "position": 4,
      "original_q": 0.5,
      "lower_bound": 0.0,
      "upper_bound": 1.0,
      "lower_outside": null,
      "upper_outside": null
    }
  ]
}
```

上例中：`zh-CN` 的 q 只要不低于 `zh;q=0.9`（含 0.9 的服务端顺序决胜）就始终选中
`zh-CN`，降到 `0.899` 时赢家变为 `zh-TW`；`zh` 与 `*` 不参与冠军争夺，q 在整个
网格上变化都不改变结果；`en` 升到 `1.000` 时会反超选出 `en-US`。

### 稳定区间响应字段

`baseline` 即**完整的基准单人协商结果**，结构与 `POST /api/v1/negotiate` 逐字段
一致（`locale_fallback` 同样仅在链实际触发时出现）。`ranges[]` 按头部顺序为
**去重后保留的每个范围**（含通配符 `*`）给出一条记录：

| 字段 | 说明 |
| --- | --- |
| `range` | 去重后的语言范围（小写；可能为 `*`） |
| `position` | 该范围在原始 `Accept-Language` 头中的位置（从 1 开始，重复范围只报第一次出现） |
| `original_q` | 原始请求中的 q；恒有 `lower_bound ≤ original_q ≤ upper_bound` |
| `lower_bound` / `upper_bound` | 稳定区间的下界 / 上界（**含端点**，均为合法三位小数网格点） |
| `lower_outside` | 紧邻下界之外的网格点（`lower_bound - 0.001`）的新结果；下界已是 `0.000` 时为 `null` |
| `upper_outside` | 紧邻上界之外的网格点（`upper_bound + 0.001`）的新结果；上界已是 `1.000` 时为 `null` |

`lower_outside` / `upper_outside` 非 `null` 时只包含 `q`、`selected_language`、
`fallback` 三个字段，且该点的语言或兜底状态必然与基准不同——这正是区间「最大」的
含义。

### 分析规则要点

1. **基准优先**：先用未改动的请求头按现有规则协商一次；只有
   `selected_language` 与 `fallback` **两者都不变**才算区间内的稳定点
   （`fallback_reason`、`message`、候选轨迹等解释性字段不作为判稳条件）。
2. **逐范围独立扫描**：每次只替换一个范围对应头部元素的 q 值，其余逗号分隔元素
   **原样保留**；重复出现的范围仍由基准协商的「保留第一次出现」规则去重，因此只为
   第一次出现生成区间报告。
3. **`q=0` 不跳过**：`0.000` 是合法网格点并参与扫描。若原 q 就是 0（该范围处于
   排除状态），稳定区间可以恰好是单点 `0.000`，此时 `upper_outside.q` 为
   `0.001`；反之通配符/具体范围降到 `0.000` 导致全员被排除时，`lower_outside`
   会显示回退默认语言（`fallback: true`）。
4. **通配符与截断匹配**：扫描得到的每次协商都完整走现有 RFC 4647 lookup 与
   q=0 排除逻辑，因此「具体范围优先于 `*`」「最长前缀命中」「服务端顺序决胜」
   等规则在边界点上自然生效。
5. **无有效范围**：请求头缺失/`null`/空白时 `ranges` 为空数组，`baseline` 为
   默认兜底结果。
6. **校验**：请求体校验与单人接口完全一致；非法头返回与 `/negotiate` 相同的
   **422** 结构（`loc` 为 `["body","accept_language"]`，`type` 为
   `accept_language.syntax_error`）。

## 群体协商（POST /api/v1/negotiate/group）

当需要为一组用户（如同一会议、家庭账号）选出**共同可接受**的单一语言时，使用群体
协商接口。每位参与者给出自己的 `Accept-Language` 头，服务端**逐人复用与单人接口
完全相同的 RFC 9110/4647 匹配语义**，构成「参与者 × 候选」权重矩阵后统一决策。

```bash
curl -X POST http://localhost:8000/api/v1/negotiate/group \
  -H 'Content-Type: application/json' \
  -d '{
    "participants": [
      {"id": "alice", "accept_language": "zh;q=0.9,en;q=0.8"},
      {"id": "bob",   "accept_language": "zh;q=0.7"},
      {"id": "carol"}
    ],
    "supported_languages": ["en-US", "zh-CN", "zh-TW", "ja-JP"],
    "default_language": "en-US"
  }'
```

响应（节选）：

```json
{
  "selected_language": "zh-CN",
  "selected_supported_index": 1,
  "fallback": false,
  "fallback_reason": null,
  "participants": [
    {"id": "carol", "position": 3, "abstained": true,
     "parsed_ranges": [], "ignored_duplicates": []}
  ],
  "candidates": [
    {
      "tag": "zh-CN",
      "supported_index": 1,
      "min_q": 0.7,
      "total_q": 2.6,
      "vetoed": false,
      "vetoed_by": [],
      "status": "selected",
      "weights": [
        {"participant_id": "alice", "q": 0.9, "matched_range": "zh", "veto": false},
        {"participant_id": "bob",   "q": 0.7, "matched_range": "zh", "veto": false},
        {"participant_id": "carol", "q": 1.0, "matched_range": null, "veto": false}
      ]
    }
  ]
}
```

### 群体请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `participants` | object[] | 是 | 至少 1 人；`id` 非空且请求内不可重复；`accept_language` 可缺省/`null` |
| `supported_languages` | string[] | 是 | 与单人接口相同（顺序即稳定优先级） |
| `default_language` | string | 是 | 与单人接口相同（必须在支持列表中） |

### 群体决策规则

1. **逐人计权**：每位参与者对每个候选标签按单人接口相同的 lookup 语义得到一个
   权重（具体范围最长前缀命中 → 该范围 q；否则通配符 q；都未命中 → 0）。
2. **弃权**：`accept_language` 缺失/`null`/空白的参与者视为接受任意语言——对所有
   候选计 q=1 且不参与否决（其 `abstained=true`）。
3. **全局否决**：任一参与者以**具体范围 `q=0`** 命中的候选被全局否决
   （`vetoed=true`，`vetoed_by` 列出否决者）。通配符 `*;q=0` 只把该参与者的权重
   计 0，**不构成否决**。
4. **决胜顺序**：在未否决且最低个人权重 > 0 的候选中，依次比较
   **最低个人权重 → 总权重 → `supported_languages` 顺序**。
5. **回退**：没有任何被所有人共同接受的候选时，返回 `default_language`，
   `fallback=true`、`fallback_reason=no-common-acceptable-candidate`；若默认语言
   本身被具体范围 `q=0` 否决，`message` 会额外说明。
6. **可解释性**：`candidates[].weights[]` 给出每位参与者对该候选的实际命中范围
   （`matched_range`）与权重，`vetoed_by` 给出否决者名单，便于向用户解释决策。

校验（均返回 **HTTP 422**）：`participants` 为空数组、参与者 `id` 重复或缺失、
某参与者的 `accept_language` 语法非法（`loc` 形如
`["body","participants",1,"accept_language"]`，`type` 为
`accept_language.syntax_error`），以及沿用单人接口的
`supported_languages` / `default_language` 校验。

## 覆盖规划（POST /api/v1/negotiate/coverage-plan）

当运营侧想评估「新增哪些语言能覆盖更多流量」时，使用覆盖规划接口。请求给出
当前支持列表、默认语言、一组带非负 `traffic` 的 `Accept-Language` 流量样本，
以及 1～12 个候选新增语言和 `max_additions`（1～3）。服务端**对每个候选子集
复用与单人接口完全相同的协商语义**，计算可把多少流量从默认兜底转为正常命中，
穷举选出最优方案。

```bash
curl -X POST http://localhost:8000/api/v1/negotiate/coverage-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "supported_languages": ["en-US", "zh-CN"],
    "default_language": "en-US",
    "samples": [
      {"accept_language": "fr-FR,fr;q=0.9", "traffic": 320.5},
      {"accept_language": "de-DE,de;q=0.8", "traffic": 210},
      {"accept_language": "zh-CN,zh;q=0.9", "traffic": 500},
      {"accept_language": "it;q=0.7", "traffic": 45}
    ],
    "candidates": ["fr-FR", "de-DE", "it-IT"],
    "max_additions": 2
  }'
```

响应（节选）：

```json
{
  "selected_additions": ["fr-FR", "de-DE"],
  "baseline_fallback_traffic": 575.5,
  "improved_traffic": 530.5,
  "message": "穷举所有不超过 2 个新增的候选组合后，新增 fr-FR、de-DE 可将 530.5 的默认兜底流量转为正常命中（同分按新增数更少、候选输入序更早取舍）",
  "samples": [
    {"position": 1, "traffic": 320.5,
     "baseline_selected_language": "en-US", "baseline_fallback": true,
     "planned_selected_language": "fr-FR", "planned_fallback": false,
     "improved": true},
    {"position": 3, "traffic": 500.0,
     "baseline_selected_language": "zh-CN", "baseline_fallback": false,
     "planned_selected_language": "zh-CN", "planned_fallback": false,
     "improved": false}
  ]
}
```

### 覆盖规划请求体

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `supported_languages` | string[] | 是 | 当前支持的 BCP 47 标签，至少 1 个、不可重复（大小写不敏感） |
| `default_language` | string | 是 | 默认语言，必须包含在 `supported_languages` 中（大小写不敏感） |
| `samples` | object[] | 是 | 至少 1 个样本；`accept_language` 可缺省/`null`，`traffic` 为非负数值 |
| `candidates` | string[] | 是 | 1～12 个候选新增语言；不可重复，且不得已在 `supported_languages` 中（均大小写不敏感） |
| `max_additions` | int | 是 | 每个方案最多新增的语言数，1～3 |

### 规划规则

1. **基线**：每个样本用现有 `supported_languages` 按单人接口语义协商；回退到
   默认语言的样本流量计入 `baseline_fallback_traffic`。
2. **穷举**：枚举候选的所有大小为 0～`max_additions` 的子集（新增语言按候选
   输入顺序**追加**到支持列表末尾，现有条目的同分优先级不变），重新协商每个
   基线兜底样本；由「默认兜底」转为「正常命中」的样本流量之和即该子集的
   改善流量。
3. **取舍**：改善流量最大者胜出；同分依次取**新增数更少**、**候选输入序更早**
   的方案。空子集参与竞争，因此任何组合都无改善时返回
   `selected_additions: []`、`improved_traffic: 0`。
4. **逐样本明细**：`samples[]` 给出每个样本规划前后选中的语言
   （`baseline_selected_language` / `planned_selected_language`）、是否回退
   （`baseline_fallback` / `planned_fallback`）以及是否改善（`improved`，
   即「基线兜底 → 规划后正常命中」）。`improved=true` 样本的流量之和恒等于
   `improved_traffic`。
5. 缺省/`null`/空白的 `accept_language` 视为无有效范围：恒定走默认兜底，
   计入 `baseline_fallback_traffic`，且任何新增都无法改善它。

校验（均返回 **HTTP 422**）：`samples` 为空、`traffic` 为负数、`candidates`
为空或超过 12 个、`max_additions` 超出 1～3、候选标签非法/重复/已在支持列表中、
某样本的 `accept_language` 语法非法（`loc` 形如
`["body","samples",1,"accept_language"]`，`type` 为
`accept_language.syntax_error`），以及沿用单人接口的
`supported_languages` / `default_language` 校验。

## 协商规则

1. **q 权重**：遵循 RFC 9110，缺省 `q=1`；合法写法为 `0`–`1`、最多 3 位小数
   （如 `0.9`、`1.000`）。非法 q 值、未知参数、非法 BCP 47 标签等语法错误一律返回
   **HTTP 422**。
2. **q=0 排除**：命中 `q=0` 范围的候选标记为 `excluded`，不参与选择。
3. **通配符 `*`**：只在候选没有任何具体范围命中时兜底；具体范围永远优先于 `*`。
4. **重复范围**：同一范围（大小写不敏感）出现多次时**保留第一次出现**及其 q 值，
   后续出现进入 `ignored_duplicates`。
5. **逐级截断匹配（RFC 4647 lookup）**：候选标签从右向左每次去掉一个子标签
   （`zh-Hant-TW → zh-Hant → zh`，单例子标签如扩展 `-u-`、私有标记 `-x-`
   不会作为链尾），最长前缀命中的范围决定候选权重；比较大小写不敏感。
6. **排序**：先按客户端权重从高到低；权重相同则按 `supported_languages` 中的先后
   顺序（稳定、可预期）。
7. **回退**：请求头缺失/空白/无有效范围，或没有任何可接受候选时，返回
   `default_language` 并置 `fallback=true`。若默认语言本身被 `q=0` 显式排除，
   `message` 中会额外说明。
8. **`locale_fallbacks` 定向链**：仅在第 7 条的“默认语言回退”之前、标准 lookup
   无可接受候选时介入；按正 q 具体范围降序、同 q 按头部顺序沿链寻找首个未被
   具体 `q=0` 范围排除的受支持语言，命中时 `fallback=false` 并返回
   `locale_fallback`（源范围、路径、最终标签）。映射本身的重复源、非法标签、
   环路与悬空链一律 **422**。

### 422 错误示例

```bash
curl -i -X POST http://localhost:8000/api/v1/negotiate \
  -H 'Content-Type: application/json' \
  -d '{"accept_language":"en;q=2","supported_languages":["en"],"default_language":"en"}'
```

```json
{
  "detail": [
    {
      "loc": ["body", "accept_language"],
      "msg": "第 1 个语言范围的 q 值非法: '2'",
      "type": "accept_language.syntax_error"
    }
  ]
}
```

请求体自身的 Pydantic 校验错误（如默认语言不在支持列表、标签重复/非法、字段缺失）
同样返回标准 422 结构。`locale_fallbacks` 的重复源、非法标签、环路或悬空链错误的
`loc` 为 `["body","locale_fallbacks"]`，例如：

```json
{
  "detail": [
    {
      "loc": ["body", "locale_fallbacks"],
      "msg": "Value error, locale_fallbacks 从 'de' 出发的链在 'fr' 处悬空：无法到达任何受支持语言",
      "type": "value_error"
    }
  ]
}
```

## 配置

所有配置均为可选项，默认值可直接本地使用：

| 配置 | 位置 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 端口映射 | `docker-compose.yml` | `8000:8000` | 左侧改宿主机端口，如 `18000:8000` |
| `UVICORN_WORKERS` | compose 环境变量 | `2` | Uvicorn worker 进程数 |
| `LOG_LEVEL` | compose 环境变量 | `info` | 日志级别（`critical`/`error`/`warning`/`info`/`debug`/`trace`） |

容器内服务固定监听 `8000` 端口；镜像以非 root 用户运行，内置 `/health`
Docker healthcheck。

不使用 Compose 时等价的单容器启动方式：

```bash
docker build -t language-negotiator .
docker run --rm -p 8000:8000 -e UVICORN_WORKERS=2 language-negotiator
```

## 本地开发（不用容器）

需要 Python 3.11+：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q          # 142 个单元/接口测试
```

## 项目结构

```
app/
  main.py          # FastAPI 路由、异常处理（422）、健康检查
  negotiation.py   # Accept-Language 解析与逐级截断协商核心
  schemas.py       # Pydantic v2 请求/响应模型
tests/             # pytest 单元测试与 TestClient 接口测试
Dockerfile         # python:3.12-slim，依赖构建期安装，非 root 运行
docker-compose.yml # 单服务编排，8000 端口 + healthcheck
requirements.txt   # 运行时固定版本依赖
```
