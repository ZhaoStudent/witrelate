# Leader / Partner 角色交换：可行性与实用性评判

> 调研日期 2026-09-24。结论全部有出处，出处分三层：**协议规范 → SDK 实现 → 平台现实**。  
> 三层要一致才算定论（本项目在 `private` 字段上已经吃过"规范写了但平台没实现"的亏）。

## 0. 一句话结论

**你的想法里最核心的那半句是错的，但结论反而是好消息。**

- ❌ 「把 Leader 权限交给 Partner，用完再归还」——**不存在这个动作**。ACP 里 Leader/Partner 是  
  **每次交互（Session）的角色**，不是**每个 Agent 的固定身份**。没有"权限转移"这种东西，  
  所以也没有"归还"这一步。
- ✅ 「那个繁重的 Partner 直接去调别的 Partner」——**协议原生支持**，叫  
  **多跳委托（Delegation）** / **Agent 发起的委托链（AAC Profile D）**，  
  且 wit 已实现、平台不需要额外服务。

换句话说：你想做的事，协议早就设计好了，只是**换了个名字**——不叫"角色交换"，  
叫"那个 Agent 自己发起一个新的交互"。而且它在官方规范里被列为**推荐默认模式**。

---

## 1. 协议层依据（最权威）

### 1.1 Leader / Partner 是"交互级"角色，不是"Agent 级"身份

`docs/acps-specs/07-ACPs-spec-AIP.md` §3.1「角色定义」（第 29-35 行）原文：

> （1）领导者（Leader）：在智能体交互中，领导者是指发布任务并组织交互的智能体。  
> **在一次完整的交互中，只能有一个领导者。**  
> （2）参与者（Partner）：在智能体交互中，参与者是指接受任务并提供服务的智能体。

关键在「**在一次完整的交互中**」这个限定语——约束的是**单次交互**，不是**单个 Agent 的一生**。  
所以：

| 说法                                          | 是否成立        |
| ------------------------------------------- | ----------- |
| 一个 Agent 在交互 X 里是 Partner，同时在交互 Y 里是 Leader | ✅ 成立        |
| 在一次交互里有两个 Leader                            | ❌ 禁止        |
| 把某次交互的 Leader 身份"过户"给原来的 Partner            | ❌ 规范里没有这个操作 |
| 让某个 Partner 新建一次交互、自己当 Leader               | ✅ 成立，且这就是正解 |

**佐证（Session 结构里没有 owner 字段）**——同文档 §4.2「会话（Session）」（第 89-134 行）：

> 会话对象**只存在于 Leader**，不参与智能体之间的通信，只有 `sessionId` 作为标识，出现在所有与会话相关的消息中。  
> `id: string;` —— **由 Leader 为新会话生成**，可以使用 UUID 格式确保全局唯一性

Session 结构体只有 `id / taskResults / taskCommands / createdAt / updatedAt`——**没有 leader 字段、没有 owner 字段、没有成员表**。  
而 Session 对象本身"不参与通信"，只把 `sessionId` 放进消息里。

这意味着：**"谁是 Leader"不是登记出来的，是"谁生成了 sessionId"自然决定的。**  
没有任何平台接口可以"改"一个会话的 Leader——因为根本没地方存这个信息。  
（对照 wit 实现：`AipTaskStore.ensure_session(session_id, owner_aic)`——`owner_aic` 是**运行时参数**。）

### 1.2 但直连模式下，Partner 之间确实不能直接说话

同文档 §3.2「交互模式」（第 41-46 行）原文：

> **直连模式**下，由 Leader 创建并维护一个 Session……该 Session 中 Leader 与每个 Partner 直接进行交互，  
> **而 Partner 之间无交互**。

这正好对应你的痛点：**你的三个 Agent 现在用的就是直连模式**（`AcpsDirect(modes=("rpc",))`），  
所以"Partner 需要跟其他 Partner 交流"在直连模式下**是明文禁止的**。

规范给了两条出路（§3.2 的另外两种模式）：

| 模式       | 机制                                                       | 当前平台可用性               |
| -------- | -------------------------------------------------------- | --------------------- |
| **群组模式** | 建 MQ 群组，同群组内**所有成员可自由收发消息**，"同一群组内的智能体均可通过消息分发模块发送和接收消息" | ❌ **官方部署未开放**（见第 3 节） |
| **混合模式** | 直连 + 群组混用                                                | ❌ 同上，依赖群组             |

以及第三条路（不是交互模式，而是**委托**）：

| 路径       | 机制                                               | 当前平台可用性               |
| -------- | ------------------------------------------------ | --------------------- |
| **多跳委托** | 那个 Partner **自己作为新交互的 Leader**，用委托机制去调其他 Partner | ✅ **可用，且不需要任何额外基础设施** |

### 1.3 §3.3 交互网络：一个 Agent 本来就可以同时在多个"关系"里

同文档 §3.3（第 67-71 行）原文：

> 智能体以直连与群组两种模式与其他智能体建立连接，**部分智能体可同时隶属于不同群组并复用消息队列服务，  
> 进而构成动态的智能体交互网络**。

规范明确承认"一个 Agent 同时处在多个交互关系里"是**正常形态**，并称之为"动态的智能体交互网络"。  
你的设想正是这个形态的一个特例。

### 1.4 AAC：协议不但支持，还把它列为"推荐默认"

`docs/acps-specs/10-ACPs-spec-AAC.md` §9.4「Profile D：Agent 发起的多跳委托」（第 691-727 行）原文：

> ```text
> Agent0 -> Agent1 -> Agent2 -> ... -> PartnerN
> ```

这**逐字**就是你描述的场景：一个 Agent 发起，链路中间再经过别的 Agent。

§10.1「委托链路模式」（第 750-766 行）给出的两条路线：

| 模式        | 说明                                            | 适用场景                                  |
| --------- | --------------------------------------------- | ------------------------------------- |
| 固定链路      | 初始授权上下文里明确后续 actor / Partner 路径，运行时只能沿指定链路继续  | 高敏感、强合规、指定处理路径                        |
| **动态下一跳** | 初始上下文给出**边界约束**，当前节点可在边界内**选择下一跳**，由 STS 逐跳裁决 | **Agent 自主规划、能力发现、任务分解、多 Partner 协作** |

> 推荐默认模式为：**动态下一跳 + 边界约束 + 逐跳裁决**

「适用场景」那一栏几乎是你需求的照抄。**协议作者把"你想做的事"当成了推荐做法。**

### 1.5 三条必须遵守的红线

| # | 红线                                                                                                                  | 出处                       | 对你的含义                                                                            |
| - | ------------------------------------------------------------------------------------------------------------------- | ------------------------ | -------------------------------------------------------------------------------- |
| 1 | **禁止冒充**："AAC 默认只支持 delegation，不支持 impersonation。允许：Agent 代表 primary subject，经授权调用下游。**禁止：Agent 伪装成另一个 Agent 或真人**" | AAC §14.4（第 1082-1101 行） | 那个 Partner **不能"戴上 Leader 的帽子"**——不能冒用 Leader 的 AIC / 证书 / 身份去发请求。它必须用**自己的身份**发 |
| 2 | **链路深度受限**：裁决要问"该 Skill 是否允许二次或多次转委托"、"chain depth 是否在允许范围内"                                                        | AAC §9.4                 | 委托不能无限套娃。需要自己设最大深度                                                               |
| 3 | **权限逐跳收窄**：`new_token.scope <= subject_token.scope`，"给 Partner1 的 token 不得直接用于 Partner2"                            | AAC §10.5（第 836-849 行）   | 下级拿到的权限不能比上级大                                                                    |

**第 1 条是理解你想法本质的钥匙**：协议区分"**代表某人做事**"（合法）和"**变成某人**"（禁止）。  
你说"让这个 Partner 暂时成为 Leader"，如果字面执行——冒用 Leader 身份——那就违规了。  
正确形态是"**这个 Partner 自己发起一次新交互**"。

---

## 2. 实现层证据（wit 到底做了什么）

wit 是**编译分发**（源码不可读，只有 `.pyi` 存根 + `_bytecode/*.pyc`），  
所以下面全部来自**运行时反射**——比读文档可靠。

### 2.1 wit 里没有"你是 Leader 还是 Partner"这种固定属性

| 证据                                                                  | 说明                                        |
| ------------------------------------------------------------------- | ----------------------------------------- |
| `WitRuntime.aip_delegate(state, *, partner_aic, request)`           | 发起委托。第一个参数是 **运行时 state**，不是"角色"          |
| `WitRuntime.aip_group(state, *, action, group_id, partners=...)`    | 群组管理，同样是运行时 state                         |
| `WitRuntime.aip_group_task(state, *, action, partner_aics=[...])`   | 群组内对**多个** Partner 发任务，`partner_aics` 是列表 |
| `AipTaskStore.create_outbound(..., local_aic=..., partner_aic=...)` | **`local_aic` 是参数**——"谁发起"由运行时决定          |
| `AipTaskStore.ensure_session(session_id, owner_aic)`                | **`owner_aic` 是参数**——"谁是会话所有者"由运行时决定      |

结论：**在 wit 里，"能不能当 Leader"不是编译期身份，而是"手上有没有 state + 有没有挂对应工具"。**  
你的两个 Partner 现在是 Partner，只因为**没给它们挂委托工具**，不是因为框架限制。

### 2.2 决定性的那个"开关"

```python
# wit/hooks/acps/delegation.py
class AipDelegationHooks:
    """Give a Wit agent high-level AIP discovery and delegation tools."""
    def __init__(self, *, delivery: str = 'rpc', notify_on_states: list[str] | tuple[str, ...] | None = None)
```

**docstring 的用词是 "a Wit agent"，不是 "a Leader agent"。**

它给任何 wit agent 注入三个工具（`acps_discover` / `acps_delegate` / `acps_task`）。  
而项目 `legal_acps/agents.py` 里**只有 `build_leader` 挂了它**：

```python
def build_leader(model):        return WitAgent(..., hooks=[AipDelegationHooks(delivery="rpc")], ...)
def build_incitement_partner(model): return WitAgent(..., capabilities=[...], protocols=[AcpsDirect(modes=("rpc",))])   # 没挂 hooks
def build_harm_partner(model):       return WitAgent(..., capabilities=[...], protocols=[AcpsDirect(modes=("rpc",))])   # 没挂 hooks
```

**所以"那个 Partner 能不能自己去找别的 Partner"这件事，在代码上就是"要不要给它也加一行 hooks"。**

> ⚠️ 注意：`AipDelegationHooks` 注入的是**给大模型用的工具**，也就是说  
> **"要不要找别人帮忙"是由模型自己决定调用的**。  
> 好处是灵活；坏处是**不可预测**——同一段输入，模型这次调了、下次可能不调。  
> 若要可控（Demo 演示尤其需要），应改用代码直接调 `runtime.aip_delegate(...)`，  
> 或用 `wrap_tool_call` 强制/禁止。

### 2.3 同一个进程可以同时既被调用、又主动调别人

| 类                 | 服务端能力                                       | 客户端能力                                   |
| ----------------- | ------------------------------------------- | --------------------------------------- |
| `AipCoordinator`  | `handle_jsonrpc_request(request, peer=...)` | `delegate(state, partner_aic=..., ...)` |
| `AipDirectClient` | `notify(...)`                               | `call(...)` / `stream(...)`             |

两者在同一个 `AipCoordinator` 上，**不存在"要当 Leader 就得先停止当 Partner"的约束**。  
它原来的 Partner 监听器可以一直开着（继续接 Leader 的活），同时并行发起对新 Partner 的调用。

### 2.4 准入控制（安全阀）

| 类                                                                                   | 作用             |
| ----------------------------------------------------------------------------------- | -------------- |
| `AipPartnerAdmissionHooks.decide_admission(request) -> AipPartnerAdmissionDecision` | 被调方决定"接不接这个任务" |
| `AipGroupAdmissionHooks(handler)`                                                   | 群组准入           |

这就是防止"某个 Partner 拉着一堆人不放"的机制。

---

## 3. 平台层现实（最关键的一节，也是唯一说"不行"的一节）

### 3.1 群组模式：官方部署**没有开放**

运行 `AcpsConfig.ioa_pub(provider=...)` 实测返回：

```
registry_url      = 'https://www.ioa.pub/registry'
registry_mtls_url = 'https://www.ioa.pub:9002'
ca_url            = 'https://www.ioa.pub/ca-server'
discovery_url     = 'https://www.ioa.pub/discovery/acps-adp-v2/discover'
mq_url            = None      ← 群组消息队列：没有
mq_group_api_url  = None      ← 群组 ACL 服务：没有
```

三重交叉验证：

| 验证                           | 结果                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------- |
| `AcpsConfig.ioa_pub()` 实测    | `mq_url = None`、`mq_group_api_url = None`                                                |
| wit 内置校验常量                   | `"ACPS_PROFILE=ioa_pub owns its infrastructure URLs"`（官方 profile 自己掌管这些地址，**不该手动覆盖**）    |
| 两个平台 OpenAPI 全量              | www 55 个接口 / wt 67 个接口，**没有任何 group / mq / amqp / acl / token-exchange / delegation 接口** |
| 官方文档页 `wt.ioa.pub/docs.html` | 「群组」「MQ」「AMQP」「RabbitMQ」「5671」「委托」「Token Exchange」**出现 0 次**                             |

### 3.2 想自己开群组也不行（硬约束来自 wit 校验常量）

```
"AcpsConfig mq_url and mq_group_api_url must be configured together"     ← 必须成对
"Strict AcpsConfig mq_url must use AMQPS port 5671"                      ← 必须 5671 + AMQPS
"AcpsConfig mq_url must be an AMQP(S) URL with explicit port and /acps vhost"
"AcpsConfig mq_group_api_url must be an origin without a path"
```

`mq_group_api_url` 指向的是**平台的群组 ACL 服务**——`MqGroupAclClient.add_member(leader_aic, group_id, member_aic)`  
是调**平台接口**来配 ACL 的。所以即使自己搭一个 RabbitMQ 4.2+，**ACL 那半边仍然要平台配合**。  
→ **群组模式在当前条件下不可落地。** 协议定义了、wit 实现了客户端、但官方服务端没提供。

（这跟 `private` 字段是同一类情况：**规范/框架有这个能力 ≠ 平台开放了它**。  
所以判断任何 ACPs 能力，必须落到"平台开没开"这一层。）

### 3.3 多跳委托：**不需要平台额外服务**

wit 的委托走的是 `AcpsDirect` 的 rpc + **mTLS 身份**（`MtlsPeerEvidence`），  
**不走 STS、不走 delegation token**（字节码里搜 `token_exchange` / `sts` 零命中）。

含义：

- ✅ **不需要任何平台新服务**就能跑通
- ⚠️ 但 AAC §10 那一整套 delegation token 约束（`acps_chain_depth`、`acps_allowed_partner_aics`、  
  `scope` 逐跳收窄）**在 wit 实现里没有强制**。也就是说：**协议给了规矩，实现没上枷锁**。
- ⚠️ 因此 §1.5 那三条红线（不冒充、限深度、限权限）**要你自己在应用层守住**——  
  框架不会替你拦。

---

## 4. 三种方案对比

|                 | 方案 A：Leader 中转<br />（现状） | 方案 B：多跳委托<br />（你的想法）                            | 方案 C：群组模式           |
| --------------- | ------------------------ | ------------------------------------------------ | ------------------- |
| **协议支持**        | ✅ AIP 直连模式               | ✅✅ AAC Profile D + §10.1 推荐默认                    | ✅ AIP §3.2          |
| **wit 支持**      | ✅ 已在用                    | ✅ `AipDelegationHooks` / `aip_delegate`          | ✅ 类都在               |
| **平台开放**        | ✅                        | ✅（不需要平台服务）                                       | ❌ **mq_url = None** |
| **改动量**         | 0                        | **小**：给 Partner 加 `hooks=[...]` + 改 instructions | 大：自建 MQ + 求平台开 ACL  |
| **Partner 间通信** | 间接（经 Leader）             | 直接（新交互）                                          | 直接（同群组广播）           |
| **多轮协商**        | 差（每轮都要回 Leader）          | 好                                                | 最好（天然多方）            |
| **可观测性**        | **最好**（单链路）              | 中（子会话需自己串 `sessionId`）                           | 差（广播，谁说了啥要捞 MQ）     |
| **调试难度**        | 低                        | 中                                                | 高                   |
| **死循环风险**       | 无                        | **有**（A→B→A）                                     | 有                   |
| **答辩亮点**        | 无                        | **强**（对齐规范 §10.1 推荐模式）                           | 强但跑不起来              |

---

## 5. 实用性评判：值得做，但要分清"真需求"和"听起来酷"

### 5.1 先问一句：你需要的是"多轮协商"还是"单向传结果"？

这是决定要不要做方案 B 的**唯一标准**：

| 你的真实需求                                                       | 正确做法                                                   |
| ------------------------------------------------------------ | ------------------------------------------------------ |
| Partner A 的结果 → 给 Partner B 用（**单向依赖，一次就够**）                 | **方案 A（Leader 中转）就够了**。方案 B 只是把一次转发挪了个地方，收益接近零，却引入新故障点 |
| Partner A 和 Partner B 要**来回讨论 N 轮**才能收敛（如：一个提危害初判、另一个反驳、再修正） | **方案 B 有真实价值**——省掉 N 次"回 Leader 再转发"的往返                |
| 需要 3 个以上 Partner **同时**感知彼此的中间结论                             | 方案 B；或等平台开群组模式（方案 C）                                   |

**我的判断**：按你描述的法律研判场景（煽动识别 → 危害评估），  
这更像**单向依赖链**而不是多轮协商。**如果是这样，方案 B 的收益低于成本。**

### 5.2 如果真要做方案 B，必须自己守住的三件事

框架不替你兜底（见 §3.3），所以：

1. **递归深度硬上限**：在应用层计数，超过 N（建议 ≤ 2~3）层就拒绝继续委托。  
   否则 A→B→A→B 会烧掉你的 DeepSeek 额度，而且推理模型每轮成本很高。
2. **禁止冒充**：那个 Partner 发请求时用的是**自己的 AIC / 证书**（wit 自动如此，别去手动指定别人的身份）。  
   这也是 AAC §14.4 的硬要求。
3. **可观测链**：子交互会生成**新的 `sessionId`**。要在 `output/` 里把  
   "父会话 → 子会话"的映射记下来，否则出问题时无法复盘。ACP 的 Session 对象只在各自 Leader 本地，  
   平台不会替你串这条链。

### 5.3 一个容易被忽略的设计陷阱

方案 B 在**权限语义上是有代价的**：

- 方案 A（Leader 中转）：Leader 是**唯一的授权决策点**。它知道全链路在干什么，  
  可以统一做风控、留痕、否决。
- 方案 B（多跳委托）：控制权**下放**给了那个 Partner。它自己决定找谁、发什么、  
  链走多深。**这既是灵活性来源，也是失控来源。**

AAC §10.1 之所以把"动态下一跳"配上"**边界约束 + 逐跳裁决**"，就是为了对冲这个代价。  
而 §3.3 说了，wit 没实现逐跳裁决。所以**你等于在没有刹车的情况下拿到了方向盘**。  
对 Demo 来说可以接受（用户就你一个），但如果对外提供服务，这是要认真补的。

---

## 6. 建议（按优先级）

1. **先把"角色交换"这个说法从设计里删掉**。它不存在，也没必要存在。  
   正确表述是：**"某个高负载 Partner 可发起子交互，以 Leader 身份自主编排下游 Partner"**。  
   这个表述既准确，又正好命中 AAC §10.1 的推荐模式——**答辩时这是加分项**。
2. **先做方案 A 的显式化**：把"谁依赖谁"在 Leader 的 instructions 里写清楚  
   （先派 A，拿到结果再派 B）。这一步成本≈0，且立刻让链路可复现。**先把这个跑顺。**


1. **方案 B 作为"可选的进阶开关"**：给那个繁重 Partner 挂 `AipDelegationHooks`，  
   但**用代码控制触发条件**（不要纯靠模型自觉），并加深度上限。  
   这样你既能讲"支持多跳委托编排"，又不会在演示时失控。
2. **群组模式暂时别碰**。它不是你的问题（Partner 间通信）的唯一解，  
   而且官方没开（`mq_url=None`）。**可以写进"未来工作"**——因为 AIP §3.2 确实定义了它，  
   等平台开放就能无缝升级。
3. **向官方确认两件事**（附建议问法）：
   - 「AIP §3.2 的群组模式需要 MQ Server，wit 的 `AcpsConfig.ioa_pub()` 返回 `mq_url=None`。  
     ioa.pub 是否提供群组 MQ 服务？如果提供，地址和 ACL 服务端点是什么？」
   - 「AAC §9.4 Profile D 的多跳委托，平台是否有 STS 做逐跳裁决？  
     wit 的实现走 mTLS 而不走 delegation token，这是有意简化还是待补齐？」

---

## 7. 一句话给团队

**你们的规则不是"缺一个功能"，而是"用错了词"。**  
协议里已经有这个能力（AAC Profile D），wit 也已经实现了（`AipDelegationHooks`），  
不需要平台开新服务——**但也不需要急着做**。真正的判断依据是：  
你的 Partner 之间是**单向传结果**还是**多轮协商**。  
如果是前者，Leader 中转就是正确答案，别为了"看起来更厉害"给自己加变量。
