# Promise Architecture

这份文档回答的问题不是“Promise 是什么”，而是：

`Promise 如何真正变成一种可以强制执行的 AI Coding 基础设施。`

## 结论

推荐路线不是只做插件，也不是一开始就只做独立产品。

推荐路线是：

- 用 `Promise Kernel` 作为唯一内核
- 用 `Coding Agent Plugin` 作为开发时入口
- 用 `Promise Orchestrator Agent` 作为流程级入口

一句话说：

`共享内核，双入口，逐步加强门禁。`

## 为什么不能只靠一种形态

### 只做插件的问题

插件最适合贴近 Coding Agent 工作流，能在写代码前后提供约束。

但插件的天然限制是：

- 可以被关闭
- 可以被绕过
- 很难统一治理不同 Agent
- 很难成为组织级交付门禁

所以插件更像：`开发时约束层`

### 只做独立产品的问题

独立 Agent 更适合接管任务、派发实现、回收结果、统一验收。

但如果一开始直接做成重编排产品，问题是：

- 集成成本高
- 反馈回路慢
- 早期很难验证开发者是否真的愿意按 Promise 工作
- 没有贴近 Coding Agent 的细粒度上下文约束

所以独立 Agent 更像：`流程级门禁层`

## 推荐架构

```text
                +---------------------------+
                |   Promise Orchestrator    |
                |        Agent              |
                +-------------+-------------+
                              |
                              v
                    +---------+---------+
                    |    Promise Kernel |
                    +----+---------+----+
                         |         |
                         |         |
                         v         v
             +-----------+--+   +--+----------------+
             | Coding Agent |   | Verification / CI |
             |   Plugin     |   |   Gateways        |
             +--------------+   +-------------------+
```

这个结构里，真正重要的不是 Plugin 或 Agent 本身，而是中间的 `Promise Kernel`。

因为只有 Kernel 才应该拥有以下权力：

- 定义 Promise 的机器结构
- 判断 Promise 是否自洽
- 判断代码是否越界
- 判断验证是否足以证明承诺成立

## 核心组件

## 1. Promise Kernel

`Promise Kernel` 是整个系统的规则引擎和语义中枢。

它不负责写代码，也不负责直接和用户聊天。它负责把单一 `System Promise` 变成机器可检查、可派发、可验证的对象。

### Kernel 的职责

- 解析单一 Promise graph 中的字段层、功能层、验证层
- 维护统一 Schema
- 做 Promise graph 内部的一致性检查
- 生成给 Coding Agent 的实现任务
- 生成给验证系统的校验任务
- 返回结构化违规信息

### Kernel 的输入

- `system.promise` 或等价的 `promise_graph`
- 代码仓库上下文
- 实现结果或 diff
- 测试结果 / 验证证据

### Kernel 的输出

- `promise_graph`
- `implementation_task`
- `verification_plan`
- `violations`
- `delivery_decision`

### Kernel 必须强制的规则

- 功能层不得引用字段层未声明的字段、状态或语义
- 实现代码不得引入 Promise 之外的隐含业务状态
- 验证层必须覆盖关键字段不变量和关键功能边界
- 没有足够验证证据时，不得给出已完成判定

## 2. Coding Agent Plugin

`Coding Agent Plugin` 是 Promise 体系最轻量、最贴近开发者的一层。

它的目标不是拥有最终裁决权，而是让 Agent 在最容易出错的地方被约束住。

### Plugin 的职责

- 在开始编码前检查单一 Promise graph 是否齐全
- 把 Promise 注入 Coding Agent 的上下文
- 在代码生成后请求 Kernel 做越界检查
- 在提交前提示缺失验证或 Promise 冲突
- 让开发者尽量在本地工作流里完成 Promise 驱动开发

### Plugin 最适合做的拦截

- 单一 Promise graph 缺少字段层就不允许进入实现态
- 功能层引用未定义字段时阻止继续
- 生成 diff 后扫描出新增隐含状态并告警
- 验证层缺失时标记“未完成”

### Plugin 不应该独自承担的事

- 组织级交付裁决
- 多 Agent 编排
- 跨仓库统一治理
- 最终上线审批

## 3. Promise Orchestrator Agent

`Promise Orchestrator Agent` 是更强控制力的产品形态。

它接收用户任务，但不直接把任务理解成“去写代码”，而是理解成：

`先建立承诺，再委派实现，再回收验证，再决定是否交付。`

### Orchestrator 的职责

- 接收产品或工程任务
- 生成或补全单一 `System Promise` 草案
- 调用 Kernel 做一致性检查
- 向外部 Coding Agent 派发实现任务
- 回收代码结果和验证证据
- 根据 Kernel 判定是否允许交付

### Orchestrator 适合承担的门禁

- 没有唯一的 `System Promise`，不允许进入编码阶段
- 没有通过验证，不允许进入完成状态
- Promise 与实现冲突时，要求回到 Promise 修订
- 多次任务执行结果必须能追溯到同一组 Promise 版本

## 强制力来自哪里

真正的强制保障，不是“Agent 很听话”，而是系统把关键动作变成可拒绝的状态机。

至少要有三层强制：

## 1. 输入门禁

在进入实现前，必须存在：

- 唯一的 `System Promise`
- 字段层
- 功能层
- 最小可执行的验证层

否则任务状态不能从 `draft` 进入 `implementable`。

## 2. 过程门禁

在实现阶段，系统持续检查：

- 是否新增未声明字段
- 是否新增未声明状态
- 是否出现超出读写边界的实现
- 是否通过缓存、标志位、注释语义制造隐含状态

否则任务状态不能从 `implementing` 进入 `verifiable`。

## 3. 交付门禁

在交付阶段，系统必须检查：

- 验证层是否被执行
- 关键承诺是否有证据支撑
- 是否存在未关闭违规项

否则任务状态不能从 `verifiable` 进入 `delivered`。

## 机器协议

如果 Promise 只停留在 Markdown，它适合人读，但不适合强制执行。

所以系统需要一层机器协议。Markdown 可以继续保留，但背后必须能投影成结构化对象。

## 1. Promise Spec

建议定义统一的结构化对象：

```json
{
  "fieldPromises": [],
  "functionPromises": [],
  "verificationPromises": [],
  "meta": {
    "domain": "task",
    "version": "v1"
  }
}
```

### Field Promise 的最小结构

```json
{
  "object": "Task",
  "fields": [],
  "states": [],
  "invariants": [],
  "forbiddenImplicitState": []
}
```

### Function Promise 的最小结构

```json
{
  "action": "CompleteTask",
  "preconditions": [],
  "reads": [],
  "writes": [],
  "forbidden": []
}
```

### Verification Promise 的最小结构

```json
{
  "claim": "status=done implies completedAt exists",
  "verifies": ["Task.status", "Task.completedAt"],
  "method": "unit",
  "evidence": []
}
```

## 2. Kernel API

不管以后是本地库、服务还是 MCP，都建议把内核接口稳定下来。

### `parsePromiseSpec`

输入：

- Markdown 或结构化 Promise 文档

输出：

- 标准化 `promise_graph`

### `checkPromiseConsistency`

输入：

- `promise_graph`

输出：

- 一致性错误
- 缺失约束
- 未覆盖风险

### `compileImplementationTask`

输入：

- `promise_graph`
- 仓库上下文

输出：

- 发给 Coding Agent 的实现任务
- 明确允许与禁止事项

### `inspectImplementation`

输入：

- `promise_graph`
- 代码 diff 或结果文件

输出：

- 违规列表
- 风险等级
- 是否允许进入验证阶段

### `evaluateVerification`

输入：

- `promise_graph`
- 测试结果
- 验证证据

输出：

- 承诺覆盖情况
- 未证明项
- 最终交付判定

## 入口协作协议

## 1. Plugin 到 Kernel

Plugin 调用 Kernel 时，核心任务是“拿规则，不拿裁决之外的业务权力”。

标准流程：

1. 收集仓库中的 Promise 文档
2. 调用 `parsePromiseSpec`
3. 调用 `checkPromiseConsistency`
4. 若通过，则调用 `compileImplementationTask`
5. 把实现任务注入 Coding Agent
6. 代码生成后调用 `inspectImplementation`
7. 需要时调用 `evaluateVerification`

## 2. Orchestrator 到 Kernel

Orchestrator 调用 Kernel 时，核心任务是“拿到裁决依据并驱动状态流转”。

标准流程：

1. 接收用户任务
2. 创建或补全 Promise 草案
3. 调用 `checkPromiseConsistency`
4. 生成实现任务并派发给 Coding Agent
5. 回收代码结果与测试证据
6. 调用 `inspectImplementation`
7. 调用 `evaluateVerification`
8. 根据结果推进或拒绝交付

## 3. Orchestrator 到 Coding Agent

Orchestrator 发给 Coding Agent 的内容，不应该是模糊需求，而应该是结构化任务包。

建议任务包至少包含：

- Promise 版本
- 允许修改的文件范围
- 必须满足的字段不变量
- 必须满足的功能边界
- 必须补齐的验证要求
- 禁止引入的隐含状态

## 推荐状态机

为了让“强制保障”不是口头描述，建议把任务定义成显式状态机：

```text
draft
  -> promised
  -> implementable
  -> implementing
  -> verifiable
  -> delivered

任何阶段发现 Promise 冲突或验证不足：
  -> blocked
```

### 状态解释

- `draft`：只有原始任务，还没有承诺结构
- `promised`：唯一的 `System Promise` 已定义，且字段层、功能层已显式存在
- `implementable`：Kernel 检查通过，可以发给 Coding Agent
- `implementing`：正在实现
- `verifiable`：实现已回收，等待验证裁决
- `delivered`：承诺已被证明成立
- `blocked`：Promise 冲突、实现越界或验证不足

## 产品决策建议

## 第一阶段

先做 `Coding Agent Plugin`，但只把它当成薄入口。

目标是：

- 验证开发者是否愿意按 Promise 工作
- System Promise 模板是否足够自然
- 验证 Kernel 的核心检查规则是否有价值

这一阶段不要把大量精力花在重产品编排上。

## 第二阶段

开始做 `Promise Orchestrator Agent`。

目标是：

- 接管任务入口
- 统一派发给不同 Coding Agent
- 增加版本追踪、证据管理和交付门禁

这一阶段开始形成真正的产品壁垒。

## 第三阶段

把 Promise 变成 CI 和交付系统的一部分。

目标是：

- PR 或 merge 前自动检查 Promise 一致性
- 自动验证承诺覆盖率
- 把“已完成”从主观判断改成系统裁决

## 为什么这条路线更稳

- 插件让你最快看到真实使用反馈
- 共享内核避免未来重写
- Orchestrator 让 Promise 从建议升级成门禁
- CI 接入让 Promise 从工具升级成工程制度

## 最小可行版本

如果现在只做 MVP，我建议只做四个能力：

1. Promise Markdown / DSL 到结构化 Spec 的解析
2. 字段层与功能层的一致性检查
3. 基于 diff 的隐含状态检测
4. 验证层覆盖检查与完成判定

只要这四件事成立，Plugin 和 Orchestrator 都能长出来。

## 一句总结

Promise 的强制保障，不应该寄托在“某个 Agent 是否自觉遵守”。

它应该建立在：

- 有统一内核
- 有结构化协议
- 有显式状态机
- 有输入、过程、交付三层门禁

在这个前提下：

- 插件负责把 Promise 带进 Coding 工作流
- Orchestrator 负责把 Promise 变成交付门禁
- Kernel 负责裁定什么叫“承诺成立”
