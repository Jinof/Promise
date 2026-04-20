# Promise Language

这份文档定义 Promise 的文本 DSL。

目标不是替代 [docs/promise-standard.md](/Users/jinof/source/Promise/docs/promise-standard.md)，而是把单一 `System Promise` 映射成一个更适合 CLI 解析、lint 和编排的语言层。

## 设计目标

- 让 Promise 关系可以被机器稳定解析
- 保留足够强的人类可读性
- 尽量贴近现有 `Promise Spec` 结构
- 不依赖外部解析器生成器

## 文件结构

一个 `.promise` 文件表达一个系统或模块的唯一 Promise graph，由四类块组成：

```text
meta:
field <Name> for <Object>:
function <Name> action <Action>:
verify <Name> kind <Kind>:
```

缩进规则：

- 顶层块不缩进
- 块内属性缩进 2 个空格
- `scenario` 内部再缩进 2 个空格

## 最小示例

```text
meta:
  title "Task System Promise"
  domain task
  version v1
  status active
  owner product
  summary "Canonical System Promise for the Task example."

field TaskFieldPromise for Task:
  summary "Defines the Task object."
  field id type string required true nullable false default null semantic "Unique identifier." mutable false system true readers * writers system.create
  state todo meaning "Task is not yet complete." terminal false initial true transitions done
  invariant Task.done_requires_completedAt statement "When Task.status is done, Task.completedAt must exist." refs Task.status,Task.completedAt when "Task.status = done" must "Task.completedAt != null"
  forbid Task.no_duplicate_completion_flag statement "Do not introduce isCompleted outside declared fields." refs Task.status,Task.completedAt
```

## 语法

### `meta`

支持以下属性：

- `title`
- `domain`
- `version`
- `status`
- `owner`
- `summary`
- `source`

其中 `owner` 和 `source` 可重复。

### `field`

块头：

```text
field <PromiseName> for <ObjectName>:
```

块内支持：

- 说明：下文中的 `csv` 表示 `comma-separated list (CSV)`，也就是逗号分隔列表，例如 `user.create,user.edit`。
- `summary "<text>"`
- `field <name> type <type> required <true|false> nullable <true|false> default <value> semantic "<text>" ...`
- `state <value> meaning "<text>" terminal <true|false> initial <true|false> transitions <csv-or-->`
- `invariant <id> statement "<text>" [refs <csv>] [when "<text>"] [must "<text>"]`
- `constraint <id> statement "<text>" [refs <csv>]`
- `forbid <id> statement "<text>" [refs <csv>]`

字段定义可选属性：

- `mutable <true|false>`
- `system <true|false>`
- `readers <csv>`
- `writers <csv>`
- `derived <csv>`

### `function`

块头：

```text
function <PromiseName> action <ActionName>:
```

块内支持：

- `summary "<text>"`
- `depends <csv>`
- `trigger "<text>"`
- `precondition <id> statement "<text>" [refs <csv>]`
- `reads <csv-or-->`
- `writes <csv-or-->`
- `ensure <id> statement "<text>" [refs <csv>]`
- `reject <id> statement "<text>" [refs <csv>]`
- `sideeffect <id> statement "<text>" [refs <csv>]`
- `idempotency "<text>"`
- `forbid <id> statement "<text>" [refs <csv>]`

### `verify`

块头：

```text
verify <PromiseName> kind <field|function|cross-cutting>:
```

块内支持：

- `claim "<text>"`
- `verifies <csv>`
- `methods <csv>`
- `scenario "<name>":`
- `evidence "<text>"`
- `fail "<text>"`

### `scenario`

必须嵌套在 `verify` 块内。

支持：

- `covers <csv>`
- `given "<text>"`
- `when "<text>"`
- `then "<text>"`
- `guard "<text>"`

## CLI

当前 CLI 支持六个命令：

```bash
./promise parse examples/task/task.promise
./promise lint examples/task/task.promise
./promise lint examples/core/task-core.promise --profile core
./promise lint examples/task/task.promise --json
./promise format examples/task/task.promise
./promise format examples/task/task.promise --write
./promise format examples/task/task.promise --check
./promise check examples/task/task.promise --json
./promise graph examples/task/task.promise --html /tmp/task-graph.html
./promise check tooling/promise-cli.promise --profile core --json
./promise tooling verify --json
```

## 输出

- `parse` 会输出 JSON 格式的 `Promise Spec`
- `lint` 会检查引用、依赖、状态迁移和重复定义等结构问题；加 `--profile core` 时还会检查是否超出最小 Promise Core 子集；加 `--json` 时会输出结构化 lint 报告。结构错误会返回失败，覆盖告警会保留为 `warning` 而不是逼迫作者机械填空
- `format` 会输出 canonical DSL；加 `--write` 时会原地覆盖文件；加 `--check` 时只检查是否已格式化
- `check --json` 会输出结构化检查结果，包含 `ok`、`profile`、`issues`、`errorCount`、`warningCount`、`spec` 和 `error`
- `graph` 会生成单文件 HTML Promise graph；加 `--html` 时会写入目标页面，否则输出到 stdout；当图规模过大时会自动切到 `overview/composite` 复合视图，用聚合图面加 explorer 保持一屏可读，而不是把所有节点硬塞到一个 full graph 画布里
- `tooling verify --json` 会输出 Promise 工具链的一致性报告，检查 repo 源码、repo skill bundle 和已安装 skill 是否同步

## 当前限制

- 这是一个最小语言，不是最终版
- 当前 lint 主要做结构一致性检查，不做深层语义推理
- 当前覆盖告警使用启发式判断“是否值得补 invariant/forbid”，还没有完整的语义充分性分析
- 当前 parser 假设缩进稳定且语法显式
