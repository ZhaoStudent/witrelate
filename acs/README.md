# 在 ACPs 平台网页上提交 Agent 的 ACS

对应页面：<https://wt.ioa.pub/registry/#/my-agents/create>

## 三个文件

| 文件 | 对应角色 | 监听端口 |
|---|---|---|
| `incitement.acs.json` | 煽动性言论识别 Partner | 8443 |
| `harm.acs.json` | 社会危害性评估 Partner | 8444 |
| `leader.acs.json` | 法律咨询主控 | 8445 |

内容取自项目里 wit 实际生成的 ACS 草稿（`~/witrelate/.wit/<角色>/acps/acs.draft.json`），
**不是照规范手写的**，所以和代码真实暴露的能力一致。

## ⚠️ 关于「设为私有」——ACS 规范里没有这个字段

官方建议「ACS 里面是否对外公开的字段选 private」。**这个字段在 ACS 里不存在。**
查了 6 处来源，全部指向同一结论：

| # | 来源 | 怎么查的 | 结果 |
|---|---|---|---|
| 1 | **ACS 规范 v02.02 原文** | 从 `AIP-PUB/ACPs-community`（tag `v2.2.0`，最新版）取 `acps-specs/03-ACPs-spec-ACS/ACPs-spec-ACS.md` | `visibility`/`private`/`public`/`公开`/`私有` **出现 0 次**；全文「对外」7 次全是"对外**提供服务端点**"的网络含义 |
| 2 | SDK 官方模型 | 读 `acps_sdk.acs.AgentCapabilitySpec.model_fields` | **19 个字段，无任何可见性字段** |
| 3 | 官方 ACS 示例 | 平台 `GET /api/v1/agent/public/acs_example`，去注释后深度遍历所有键 | 无可见性字段 |
| 4 | 平台后端契约 | wt 63 接口 / www 55 接口的 OpenAPI 全量比对 | `AgentCreate` = {name, version, description, logo_url, acs, is_ontology}；`AgentDetailResponse` 有 `is_active`/`is_disabled`/`is_deleted`/`approval_status`，**没有任何可见性字段**。两份 OpenAPI 全文 `private` 出现 **0 次** |
| 5 | 平台创建表单 | 扒前端分包 `agent_form-CoQfke8g.js` | 只有 6 项：logo 上传 / name / version / description / is_ontology(单选) / acs(文本框)，**无可见性控件** |
| 6 | 线上真实数据 | `/api/v1/agent/public/recent?limit=100` 拉 100 个已过审 Agent，统计键并集 | 顶层键 29 个 + ACS 键 20 个，**无一个**匹配 `visib\|publi\|privat\|open\|access\|scope\|restrict` |

### 那官方说的 `private` 到底从哪来？—— AAC 规范 §8.6

这三个字确实在 ACPs 官方体系里，只是**不在 ACS 里，而在 AAC（智能体访问控制）规范里**：

`acps-specs/10-ACPs-spec-AAC/ACPs-spec-AAC.md`，第 8.6 节「ACS 与 ADP 边界」原文：

> （1）ACS **可声明能力的静态开放程度**；
> （2）ADP 可根据静态开放程度和查询条件做 discovery 过滤；
> （5）**`public`、`restricted`、`private` 等静态可见性字段**不得被解释为运行时一定允许或一定拒绝。

也就是说：**协议层"设想"了 ACS 可以声明静态可见性，但 ACS 规范自身从未定义这个字段，
平台也没有实现它。** 这是规范和实现之间的一处空缺。
（对照：ADP 规范里检索过滤用的字段是 `active`、`protocolVersion`、`endPoints.transport`、
`capabilities.streaming`、`skills.tags` —— 全是已经存在的字段，没有可见性字段。）

### 平台的「可见不可见」只由**状态**决定

不是由 ACS 字段决定的：

```
DRAFT      ← 保存后停在这里：公开列表里查不到、不触发审核、不分配 AIC
PENDING    ← 点了提交审核：人工审核中，仍然查不到
APPROVED   ← 审核通过：出现在公开列表 /agent/public/recent 与 Discovery 里
REJECTED   ← 被拒
```

实测佐证：我们在 www 侧的三个 Agent 现在都是待审状态，用
`/api/v1/agent/public/recent?limit=100` 拉 100 条，**一条都查不到自己的**。

**所以「只给自己测试」的实际做法是：在创建页点「保存」，不要点「提交审核」。**
停在 DRAFT 就是别人看不到的状态。代价是**拿不到 AIC 和证书，wit 起不了服务**，
只能验证表单流程，验证不了真实互联。

如果必须让它真跑起来（AIC + 证书 + 起服务），就只能提交审核，通过后必然公开可见。
补救手段是事后再下线：页面上有「取消审核 / 删除」，
对应接口 `POST /agent/client/{id}/cancel` 与 `DELETE /agent/client/{id}`。

### 我在 ACS 里怎么表达这个意图

顶层不能加非规范字段（会给人工审核添噪音）。但规范里的 `entityMeta` 明确写着
「具体格式和内容由 Agent Provider 自定义」，平台自己的 100 个 Agent 里有 58 个在用
（例如 `{"kind": "user-aic", "application": "dingdang"}`）。

所以三份 ACS 都加了：

```json
"entityMeta": {
  "kind": "test-agent",
  "visibility": "private",
  "purpose": "acps-demo-self-test",
  "note": "仅本项目自测使用，不对外提供服务。"
}
```

**合规、零风险，但它不改变平台行为**——只是一个声明。
真正让别人看不到你的，是上面说的 DRAFT 状态。

### 建议这样回问官方（他们大概能看到我们看不到的东西）

```
老师您好，按您的建议我们准备单独建一个测试智能体，不占用叮当。
想确认一下「ACS 里面是否对外公开的字段」具体在哪里：我们在
https://wt.ioa.pub/registry/#/my-agents/create 这个创建页上只看到
logo / 名称 / 版本 / 描述 / 是否本体 / ACS 这 6 项，ACS 文本框里按
ACS-v02.02 规范也没有可见性字段。请问：
（1）是需要在 ACS 里手动加某个字段名吗？取值是 private 还是别的？
（2）还是指「创建后只保存、不提交审核」这种草稿状态？
（3）如果平台确实支持 private，字段名和取值能否给一下？
```

问清楚之前，**不要为了"选 private"而往 ACS 顶层塞非规范字段**——
`acs` 在接口里是自由对象，塞了不会报错，但没有代码会读它，只会给人工审核添噪音。

### 顺带一个好消息：性能测试不需要外人

官方不让用叮当测性能，核心原因是"别拿别人的智能体压测"。但我们的项目
**本身就是 1 个 Leader + 2 个 Partner**，互调的**两端都在我们手里**：

- Leader(8445) → Partner(8443/8444) 这条 ACPs 调用链，发起方和接收方都是自己的，
- 所以压测/自测完全可以自己打自己，**不需要任何外部智能体**。

也就是说：即便 private 这个字段最终确认不存在，**也不影响做性能测试**。
private 只影响"别人能不能在 Discovery 里搜到你"，不影响你自己互调。

## 网页上怎么填

页面上的字段（按平台接口契约 `POST /api/v1/agent/client` 的 body）：

| 页面字段 | 填什么 |
|---|---|
| 名称 | 与 ACS 里的 `name` 一致，如 `煽动性言论识别 Partner` |
| 版本 | `1.0.0` |
| 描述 | 可留空（ACS 里已有更详细的） |
| logo_url | 留空（我们没有域名，图片无处安放） |
| **ACS** | 粘贴对应 json 文件的**全文** |
| is_ontology | 不勾 |

`name` 和 `version` 在页面和 ACS 里各出现一次，**两处保持一致**，不要一边改名。

## ACS 里哪些字段不用你操心

| 字段 | 说明 |
|---|---|
| `aic` | 空字符串。**由注册服务分配**，不要在页面上编 |
| `active` | `false`。由注册服务维护，审核通过后才变 true |
| `lastModifiedTime` | 我填了当前时间，注册服务会覆盖 |

## 三个必须知道的事

### 1. 官网示例带 `//` 注释，标准 JSON 解析器不接受

平台自己返回的 `acs_example` 是**带注释**的文本，但那多半只是给人看的文档。
网页提交走的是 `JSON.parse`，所以**注释会导致解析失败**。
我给你的三份都是**纯净 JSON，无注释**，可以直接粘贴。

### 2. `domainRegistrations` 我故意省略了 —— 这是最大的不确定点

规范注释里写着：

> ACS 中所有 URL（endpoint、provider.url、documentationUrl、webAppUrl 等）的域名
> 必须属于此列表中某个主域名的子域名或与之相同。

而我们的 endpoint 是**裸 IP** `https://150.109.154.16:8443/rpc`，没有域名可归属，
所以这一项**没法如实填写**，我选择整体省略。

参考：我们在 `www.ioa.pub` 侧用完全相同的写法（无 `domainRegistrations`、裸 IP）
提交三个 Agent 时，平台**接受了**，进入了待审队列。所以这条不一定是硬门槛。
但如果这次审核以「缺域名归属材料」打回，只有两条路：买个域名做备案，或走 WHOIS 登记。

### 3. 这个网页（wt）和我们的项目（www）**不是同一套账号库**

已实测确认：

| 用户名 | `wt.ioa.pub/registry-server` | `www.ioa.pub/registry`（wit 用的） |
|---|---|---|
| `legal-acps-demo` | 不存在 | 存在 |
| `studentzhao`（你网页登录用的） | — | 不存在 |

所以：

- 你在**这个网页**上注册的 Agent，会挂在**你自己的 `studentzhao` 账号**名下，
  和我们在 www 侧已经提交的三个 Agent 是**两套独立记录**，互不覆盖也互不替代。
- 我们的 wit 代码连的是 **www**，所以网页这边注册出来的 Agent，**wit 默认用不上**。

**如果你的目标是「先在网页上把流程跑通、看到自己的 Agent 上线」**，那这样注册完全可行，
而且比等 www 侧审核更可控。

**如果想让 wit 也能用**，需要把项目切到 wt 这套 registry —— wit 支持自定义地址
（`AcpsConfig.custom(...)`，项目里 `acps_config.py` 已预留 `ACPS_USE_CUSTOM=1` 通道），
但要先探出 wt 的 4 个服务地址。**审核通过后告诉我，我来探。**

## 建议的提交顺序

**先只提交 1 个**（推荐 `incitement`，它的能力最完整，含两个 skill），观察平台反馈：

1. 是否要求域名归属材料？
2. 是否要求实名认证？
3. 审核是自动还是人工、要多久？

拿到这些答案，再决定剩下两个怎么填、以及要不要买个域名。

> 但如果你只是想把流程跑通、不想公开，就**只点「保存」，不点「提交审核」**——
> 见上一节。那样拿不到 AIC 和证书，wit 起不了服务，属于「只看流程」的测试。

## 提交后

把平台的返回（状态、报错、或审核结论）发我。若通过，我们接着做两件事：
把 wit 切到 wt 的 registry，或者继续等 www 侧那三个。
