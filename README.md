# Promise

Promise 是一套面向 AI Coding 的开发范式，用来替代传统 PRD。

它关注的不是“需求描述”，而是“产品承诺”：

- 系统里什么是真的
- 系统允许做什么
- 代码必须遵守哪些边界
- 我们如何证明承诺已经兑现

## 核心主张

- 一个系统只有一份 `System Promise`
- `field / function / verify` 不是三份并列真相源，而是一份 Promise 的三个层面
- 代码、测试、Schema、接口契约、迁移和 Agent 任务，都是 Promise 的派生产物
- 完成标准不是“代码写完”，而是“Promise 被兑现并有证据证明”

## 一个 Promise，三个层面

### 1. 字段层

字段层是 `System Promise` 的最高优先级部分，用来定义系统真相。

它负责定义：

- 核心对象
- 字段
- 状态
- 语义
- 约束
- 不变量
- 禁止隐含状态

字段层回答的问题是：`系统里到底有什么，以及这些东西的含义是什么。`

### 2. 功能层

功能层定义系统行为，但不能突破字段层的边界。

它负责定义：

- 行为名称
- 触发条件
- 前置条件
- 读取哪些字段
- 写入哪些字段
- 允许的结果
- 禁止行为

功能层回答的问题是：`系统在什么条件下，可以对已定义的真相做什么。`

### 3. 验证层

验证层定义证明义务。它不创造新的系统真相，而是说明如何证明前两层承诺成立。

它负责定义：

- 要验证的承诺
- 验证方式
- 输入条件
- 预期结果
- 边界场景
- 失败判定

验证层回答的问题是：`我们如何证明承诺成立，而不是凭感觉认为代码差不多可以。`

## 代码与其它产物

代码实现不是单独的 Promise 文档层。

它只是 `System Promise` 的派生产物。通常被 Promise 派生出来的产物包括：

- code
- tests
- schema
- api contracts
- migrations
- agent tasks

这些产物都必须满足两个约束：

- 不能偷偷创造 Promise 之外的隐含状态
- 不能通过实现细节绕过 Promise 的语义边界

## 三种关系要分开看

### 1. 逻辑唯一性

`one system -> one promise graph`

这意味着：

- 一个系统只能有一个逻辑上的语义源
- 可以物理拆成多个模块或区块，但最终必须汇总成唯一的 Promise graph
- 下游工具只认这一个 graph，不认多个并列真相源

### 2. 治理层级

`字段层 > 功能层 > 派生产物`

含义是：

- 功能层不能突破字段层边界
- 代码和其它产物不能突破功能层边界
- 如果某个行为需要新的状态，必须先修改字段层

### 3. 交付链路

`产品意图 -> System Promise -> 派生产物 -> 验证证据`

在 `System Promise` 内部，推荐编写顺序仍然是：

`字段层 -> 功能层 -> 验证层`

### 完成标准

`Promise 被兑现并被验证`

这意味着“写完代码”不是完成，“Promise 的关键承诺有证据支撑”才是完成。

## Promise 和 PRD 的区别

传统 PRD 往往把三件事混在一起：

- 为什么做
- 系统应该是什么
- 系统怎么实现

Promise 只接管中间这层，也就是：

- 系统对象是什么
- 字段语义是什么
- 行为能读写什么
- 哪些状态绝对不能被偷偷引入
- 如何证明承诺已经兑现

所以更准确地说：

- `产品意图 / brief` 仍然可以存在，但可以很短
- `System Promise` 成为真正治理实现的唯一源

一句话说：

`Brief 负责方向，Promise 负责真相与边界。`

## 面向 AI Coding 的工作流

1. 先写简短意图，说明目标、范围和成功标准。
2. 再更新唯一的 `System Promise`，先写字段层，再写功能层，再写验证层。
3. 跑 `format / lint / check`，确认 Promise graph 自洽。
4. 让 AI 基于这份 Promise 生成代码、测试、Schema 或其它产物。
5. 执行验证，产出能映射回承诺的证据。
6. 如果实现过程中发现需要新状态，回到字段层，而不是在代码里偷偷补。

## AI Coding 约束

当你把 Promise 交给 AI 生成产物时，应当默认以下规则成立：

- 未在字段层中声明的字段、状态、语义，不允许在产物里隐式创建。
- 未在功能层中声明的读写行为，不允许在产物里私自扩展。
- 如果字段层与功能层冲突，以字段层为准。
- 如果验证层无法证明承诺成立，视为尚未完成。

## 推荐目录

```text
promises/
  <domain>.promise

src/
tests/
```

其中：

- `<domain>.promise` 是这个系统或模块的唯一 Promise graph
- 如果需要更适合人读的长文说明，可以维护一个由同一 Promise graph 派生出的 `system.promise.md`

## 文档模板

仓库里提供了一个单一 `System Promise` 模板：

- [templates/system.promise.md](/Users/jinof/source/Promise/templates/system.promise.md)

另外保留了三份拆分层模板，作为从同一 Promise graph 中抽取审阅视图时的兼容材料：

- [templates/field.promise.md](/Users/jinof/source/Promise/templates/field.promise.md)
- [templates/function.promise.md](/Users/jinof/source/Promise/templates/function.promise.md)
- [templates/verification.promise.md](/Users/jinof/source/Promise/templates/verification.promise.md)

## 标准文档

如果你想看 Promise 范式的正式规范，而不是介绍性说明，入口在这里：

- [docs/promise-standard.md](/Users/jinof/source/Promise/docs/promise-standard.md)

这份标准固定了：

- 核心公理
- 四层关系
- `MUST / SHOULD / MAY` 规则
- 完成定义
- 变更规则
- 反模式和合规检查

## Promise Core

如果你想先抓住 Promise 最小不可再删的内核，再让其它能力建立在上面，入口在这里：

- [docs/promise-core.md](/Users/jinof/source/Promise/docs/promise-core.md)
- [examples/core/task-core.promise](/Users/jinof/source/Promise/examples/core/task-core.promise)
- [examples/core/promise-tooling-core.promise](/Users/jinof/source/Promise/examples/core/promise-tooling-core.promise)
- [tooling/promise-cli.promise](/Users/jinof/source/Promise/tooling/promise-cli.promise)

这部分定义了：

- Promise 的最小子集
- 什么属于 Core，什么属于增强层
- 如何基于 Core 让 Promise tooling 自举

## Tool Self-Bootstrap

如果你要看的不是“示例怎么写”，而是“Promise 工具怎么用 Promise 约束自己”，入口在这里：

- [tooling/promise-cli.promise](/Users/jinof/source/Promise/tooling/promise-cli.promise)
- [tooling/README.md](/Users/jinof/source/Promise/tooling/README.md)

这部分定义了：

- Promise CLI 自身的 `parse / format / lint / check / graph / tooling verify` 承诺
- 工具自身的显式状态载体，例如 `specJson`、`issueCount`、`parseError`
- 工具自身的显式输入面，例如 `path`、`tooling verify`、`--json`、`--profile`、`--write`、`--check`、`--html`
- 工具自身的 step runtime plan
- 图工具的大规模 Promise graph 复合展示策略，例如 `full` 与 `overview/composite` 的切换，以及在 composite 模式下仍保留聚合图面
- 如何验证真实 CLI 暴露的命令集合、选项集合、执行步骤与 Promise 中声明的一致
- 如何验证 repo 源码、repo skill bundle 和全局安装 skill 保持同步

## Machine Schema

如果你想把 Promise 交给程序解析、校验或编排，机器入口在这里：

- [schemas/promise-spec.schema.json](/Users/jinof/source/Promise/schemas/promise-spec.schema.json)
- [examples/task/promise.spec.json](/Users/jinof/source/Promise/examples/task/promise.spec.json)

这套 Schema 定义了：

- 顶层 `Promise Spec`
- `fieldPromises` / `functionPromises` / `verificationPromises` 的结构
- 可被后续 Kernel、lint 和编排系统消费的引用锚点

## Promise Language

如果你想用一门更适合 CLI 和编排系统消费的文本语言来写 Promise，入口在这里：

- [docs/promise-language.md](/Users/jinof/source/Promise/docs/promise-language.md)
- [examples/task/task.promise](/Users/jinof/source/Promise/examples/task/task.promise)

当前原型 CLI 支持：

- `./promise parse examples/task/task.promise`
- `./promise lint examples/task/task.promise`
- `./promise lint examples/core/task-core.promise --profile core`
- `./promise lint examples/task/task.promise --json`
- `./promise format examples/task/task.promise`
- `./promise format examples/task/task.promise --write`
- `./promise format examples/task/task.promise --check`
- `./promise check examples/task/task.promise --json`
- `./promise graph examples/task/task.promise --html /tmp/task-graph.html`
- `./promise check tooling/promise-cli.promise --profile core --json`
- `./promise tooling verify --json`

## 架构设计

如果要把 Promise 真正做成“可强制执行”的系统，而不是一套建议，架构稿在这里：

- [docs/architecture.md](/Users/jinof/source/Promise/docs/architecture.md)

这份文档定义了：

- 为什么应该采用“共享内核 + 插件入口 + Orchestrator 入口”
- `Promise Kernel`、`Coding Agent Plugin`、`Promise Orchestrator Agent` 的职责边界
- 机器协议、状态机和推荐演进路线

## 示例

仓库里还提供了一个最小示例：

- [examples/task/system.promise.md](/Users/jinof/source/Promise/examples/task/system.promise.md)
- [examples/task/task.promise](/Users/jinof/source/Promise/examples/task/task.promise)

这个示例展示了：

- 如何在一份 `System Promise` 里同时表达字段层、功能层和验证层
- 如何把这份 Promise 投影成可执行的 `.promise` DSL
- 如何用验证层证明实现真的符合承诺

## 一句总结

Promise 不是“把 PRD 换个名字”。

Promise 是把产品设计改写成一套可以约束 AI、驱动实现、并被验证证明的承诺系统。
