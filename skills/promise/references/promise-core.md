# Promise Core

这份文档定义 Promise 的最小子集。

目标不是替代完整 Promise 标准，而是给 Promise 提供一个足够小、足够稳、足够能自举的核心层。

一句话说：

`Promise Core = 最小可表达真相、行为、证明的单一 Promise 子集`

## 为什么需要 Core

完整 Promise 体系已经包含：

- 标准
- DSL
- Schema
- CLI
- Skill
- 架构和编排

这些都很有价值，但如果没有一个更小的内核，系统会变得：

- 难以讲清最小必须项
- 难以判断哪些能力是“本体”，哪些只是“增强”
- 难以自举

所以 Promise 需要一个核心层，满足两个条件：

1. 能表达系统真相、系统行为和验证证明
2. 能被更高层功能复用，而不是反过来依赖高层功能

## Core 原则

Promise Core 只保留三类最小能力：

1. 真相
2. 边界
3. 证明

如果某个能力不能直接服务这三件事，它就不属于 Core。

## Core 关系

Promise Core 仍然保持原来的三条主关系：

- 逻辑唯一性：`one system -> one promise graph`
- 治理层级：`field > function > derived artifacts`
- 交付链路：`field -> function -> verify -> derived artifacts -> evidence`

任何完整 Promise 功能，都必须能回落到这套关系上。

## Core 最小构成

一个 Promise Core 单元只要求一个最小 `System Promise`，并在其中具备以下结构：

### 1. `meta`

最小必需项：

- `title`
- `domain`
- `version`
- `status`
- `summary`

`owner` 和 `source` 可以继续保留，但不属于最小必需项。

### 2. `field`

最小必需项：

- `summary`
- `field`
- `invariant`
- `forbid`

可选项：

- `state`
- `constraint`
- `derived`

理由：

- 没有 `field`，就没有系统真相
- 没有 `invariant`，就没有可验证真相
- 没有 `forbid`，就无法阻止隐含状态

### 3. `function`

最小必需项：

- `summary`
- `trigger`
- `reads`
- `writes`
- `ensure`
- `forbid`

可选项：

- `depends`
- `precondition`
- `reject`
- `sideeffect`
- `idempotency`

理由：

- 没有 `reads` / `writes`，就没有边界
- 没有 `ensure`，就没有行为承诺
- 没有 `forbid`，就没有行为约束

### 4. `verify`

最小必需项：

- `claim`
- `verifies`
- `methods`
- `scenario`
- `fail`

最小场景项：

- `covers`
- `when`
- `then`

可选项：

- `given`
- `guard`
- `evidence`

理由：

- 没有 `scenario`，就没有具体证明方式
- 没有 `fail`，就没有明确失败判定

## Core 不包含什么

以下能力是增强层，不属于 Core：

- `constraint`
- `depends`
- `sideeffect`
- `idempotency`
- `given`
- `guard`
- `evidence`
- 更复杂的 Schema 限制
- 更复杂的 lint 规则
- Skill / Plugin / Orchestrator 描述

它们都可以建立在 Core 之上，但 Core 不依赖它们。

## Core DSL 子集

Promise DSL 的 Core 子集可以写成：

```text
meta:
  title "..."
  domain ...
  version ...
  status ...
  summary "..."

field <PromiseName> for <ObjectName>:
  summary "..."
  field ...
  invariant ...
  forbid ...

function <PromiseName> action <ActionName>:
  summary "..."
  trigger "..."
  reads ...
  writes ...
  ensure ...
  forbid ...

verify <PromiseName> kind <Kind>:
  claim "..."
  verifies ...
  methods ...
  scenario "...":
    covers ...
    when "..."
    then "..."
  fail "..."
```

当前 `promise` CLI 不需要新语法就能支持这个子集，因为 Core 本身就是现有 DSL 的真子集。

如果要把 Core 当成显式门禁来使用，可以直接运行：

```bash
./promise lint examples/core/task-core.promise --profile core
./promise check tooling/promise-cli.promise --profile core --json
```

这会把“是否超出最小子集”变成 CLI 可执行约束，而不只是文档约定。

## 自举策略

自举的意思不是“Promise 解释一切”，而是：

`Promise 的高层能力，必须能被 Promise Core 描述和约束。`

因此推荐的自举顺序是：

1. 先用 Promise Core 描述领域对象和关键动作
2. 再在其上增加完整 Promise 标准的增强项
3. 再在其上增加 Schema、CLI、Skill、Plugin、Orchestrator

也就是说：

`Promise Core -> Promise Standard -> Promise Tooling`

## 什么叫基于 Core

某个能力“基于 Core”，至少要满足：

1. 它不要求新的治理层级
2. 它不推翻 `field -> function -> verify`
3. 它能映射回 Core 的真相、边界或证明

如果不能映射回这三项，它就不是 Promise 的核心增强，而是旁支工具。

## 示例

仓库里提供了两个 Core 示例，以及一个当前实际用于约束 Promise CLI 的自举源文件：

- [examples/core/task-core.promise](/Users/jinof/source/Promise/examples/core/task-core.promise)
- [examples/core/promise-tooling-core.promise](/Users/jinof/source/Promise/examples/core/promise-tooling-core.promise)
- [tooling/promise-cli.promise](/Users/jinof/source/Promise/tooling/promise-cli.promise)

其中：

- `task-core.promise` 展示如何只用 Core 子集表达一个业务对象
- `promise-tooling-core.promise` 展示如何只用 Core 子集表达 Promise 工具自身的关键行为，作为最小自举示例
- `tooling/promise-cli.promise` 是当前仓库里真正拿来约束 Promise CLI 自身的 Promise 源文件，而不是示例

## 一句总结

Promise Core 不是功能更少的 Promise。

Promise Core 是 Promise 体系里最难被拿掉、拿掉后系统就不再成立的那一层。
