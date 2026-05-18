# Promise Language

这份文档定义 Promise 的文本 DSL。

目标不是替代 [docs/promise-standard.md](/Users/jinof/source/Promise/docs/promise-standard.md)，而是把单一 `System Promise` 映射成一个更适合 CLI 解析、lint 和编排的语言层。

## 设计目标

- 让 Promise 关系可以被机器稳定解析
- 保留足够强的人类可读性
- 尽量贴近现有 `Promise Spec` 结构
- 不依赖外部解析器生成器

## 文件结构

一个 `.promise` 文件表达一个系统或模块的唯一 Promise graph，由六类块组成：

```text
meta:
intent <Name> priority <must|should|may>:
type <Name> kind <Kind> base <Base>:
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

intent TaskSystemIntent priority must:
  statement "The task system must keep task lifecycle truth explicit, simple, and verifiable."
  rationale "This system-level intent anchors the human purpose behind the Task Promise graph."
  status active
  root true
  source "human prompt 2026-05-14"
  maps TaskFieldPromise relation constrains

type TaskID kind id base string:
  summary "Stable identity for a task."
  format opaque
  generated true

field TaskFieldPromise for Task:
  summary "Defines the Task object."
  field id type TaskID required true nullable false default null semantic "Unique identifier." mutable false system true readers * writers system.create
  state todo meaning "Task is not yet complete." terminal false initial true transitions done
  invariant Task.done_requires_completedAt statement "When Task.status is done, Task.completedAt must exist." refs Task.status,Task.completedAt when "Task.status == Task.status.done" must "Task.completedAt != null"
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

### `intent`

块头：

```text
intent <IntentName> priority <must|should|may>:
```

块内支持：

- `statement "<text>"`
- `rationale "<text>"`
- `status <active|changed|deprecated>`
- `root <true|false>`
- `source "<text>"`
- `parent <IntentName> relation <Relation> [note "<text>"]`
- `maps <PromiseItemRef> relation <Relation> [note "<text>"]`

`intent` 记录人类核心诉求，并把诉求映射到具体 Promise Item。一个 intent 可以 `maps` 多个 Promise Item，一个 Promise Item 也可以被多个 intent 映射。

所有 intent 组成一棵多叉树：

- 存在 intent 时必须恰好一个 `root true`
- root intent 代表系统级诉求，不能声明 `parent`
- 非 root intent 必须声明且只能声明一个 `parent`
- `parent` 不能形成环

`Relation` 当前支持：`motivates`、`constrains`、`explains`、`verifies`、`conflicts`、`refines`、`supports`。

### `type`

块头：

```text
type <TypeName> kind <Kind> base <BaseType>:
```

块内支持：

- `summary "<text>"`
- `format <token-or-text>`
- `generated <true|false>`

类型声明属于字段层真相，用来给字段提供可复用的语义类型。`base` 当前支持内建 primitive：`string`、`text`、`integer`、`number`、`boolean`、`datetime`、`json`、`path`。字段 `type` 可以引用已声明类型，也可以继续使用 primitive 或 inline enum，例如 `enum(todo|done)`。

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

### Promise Expression Grammar

`invariant` 的 `when` 和 `must` 使用 Promise expression grammar。当前 DSL 仍要求表达式放在引号内，但引号内的内容会被解析成 AST 并做类型检查，而不是作为普通文本处理。

支持的表达式形态：

```text
Task.status == Task.status.done
Task.status = done
Task.completedAt != null
Task.priority in [Task.priority.high,Task.priority.urgent]
Task.retryCount <= 3
not Task.archived
Task.status == Task.status.done and Task.completedAt != null
Task.status == Task.status.done or Task.status == Task.status.blocked
```

语法结构：

```text
expr        := or_expr
or_expr     := and_expr ("or" and_expr)*
and_expr    := not_expr ("and" not_expr)*
not_expr    := "not" not_expr | comparison
comparison  := value (("==" | "=" | "!=" | "<" | "<=" | ">" | ">=" | "in") value)?
value       := field_ref | enum_ref | literal | list | "(" expr ")"
field_ref   := Identifier "." Identifier
enum_ref    := Identifier | Identifier "." Identifier | Identifier "." Identifier "." Identifier
literal     := string | number | boolean | null
list        := "[" value ("," value)* "]"
```

类型检查规则：

- 字段引用必须指向当前 field block 的对象字段，例如 `Task.status`。
- enum 或 state 字面量必须来自字段声明。`Task.status.archived` 会在 `archived` 未声明时报错。
- nullable 字段可以与 `null` 比较；非 nullable 字段可以声明 `!= null` 作为非空承诺，但不能声明必须等于 `null`。
- `and`、`or`、`not` 只能组合 boolean 表达式。
- `<`、`<=`、`>`、`>=` 当前只支持 numeric 字段。
- `in` 右侧必须是 list，list 中每个值都必须能与左侧字段比较。
- `=` 是兼容旧 Promise 的等号写法，格式化时保留原文；新 Promise 推荐使用 `==`。

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

当前 CLI 支持八个命令：

```bash
./promise parse examples/task/task.promise
./promise lint examples/task/task.promise
./promise lint examples/core/task-core.promise --profile core
./promise lint examples/task/task.promise --json
./promise format examples/task/task.promise
./promise format examples/task/task.promise --write
./promise format examples/task/task.promise --check
./promise check examples/task/task.promise --json
./promise compile examples/task/task.promise --target go --out /tmp/promise-go-task
./promise compile examples/task/task.promise --target go --type-map examples/task/go-type-map.json --out /tmp/promise-go-task
make -C examples/go/task verify
./promise graph examples/task/task.promise --html /tmp/task-graph.html
./promise impact examples/task/task.promise --intent PreserveTaskLifecycleTruth --json
./promise check tooling/promise-cli.promise --json
./promise tooling verify --json
```

## 输出

- `parse` 会输出 JSON 格式的 `Promise Spec`
- `lint` 会检查引用、依赖、状态迁移、字段类型和重复定义等结构问题；加 `--profile core` 时还会检查是否超出最小 Promise Core 子集；加 `--json` 时会输出结构化 lint 报告。结构错误会返回失败，覆盖告警会保留为 `warning` 而不是逼迫作者机械填空
- `format` 会输出 canonical DSL；加 `--write` 时会原地覆盖文件；加 `--check` 时只检查是否已格式化
- `check --json` 会输出结构化检查结果，包含 `ok`、`profile`、`issues`、`errorCount`、`warningCount`、`spec` 和 `error`
- `compile --target go --out <dir>` 会从 Promise Spec 生成 Go contract package，包括类型、状态枚举、invariant validator 和状态迁移 guard；在不能生成具体断言前，不生成 verify 测试骨架
- `compile --target go --type-map <json> --out <dir>` 会加载类型映射插件，把 Promise primitive 或声明类型映射到具体 Go 类型
- `graph` 会生成单文件 HTML Promise graph；加 `--html` 时会写入目标页面，否则输出到 stdout；当图规模过大时会自动切到 `overview/composite` 复合视图，用聚合图面加 explorer 保持一屏可读，而不是把所有节点硬塞到一个 full graph 画布里
- `impact` 会输出 intent 树、选中 intent 的上游链路、直接映射 Promise Item、下游影响项和共享影响项的相关 intent；加 `--json` 时输出结构化报告
- `tooling verify --json` 会输出 Promise 工具链的一致性报告，检查 repo 源码、repo skill bundle 和已安装 skill 是否同步

## 类型映射插件

Promise 类型声明只表达语义，不直接锁死某个编程语言的类型。实际编译到 Go 时，可以通过 `--type-map` 提供 JSON 插件：

```json
{
  "target": "go",
  "types": {
    "TaskID": {
      "type": "uuid.UUID",
      "import": "github.com/google/uuid"
    }
  },
  "primitives": {
    "datetime": {
      "type": "civil.DateTime",
      "import": "cloud.google.com/go/civil"
    }
  }
}
```

- `types` 映射 Promise 声明类型，例如 `TaskID`
- `primitives` 映射 Promise primitive，例如 `datetime`
- 没有被插件覆盖的类型仍使用 CLI 内置默认映射
- 当声明类型被插件覆盖时，Go target 使用插件指定的实际类型，不再生成本地 `type <Name> <Base>` 声明
- [examples/go/task](/Users/jinof/source/Promise/examples/go/task) 提供了一个可运行 Go module，用默认映射和 `--type-map` 映射各生成一套 contract package，并用 Go tests 验证生成结果

## 当前限制

- 这是一个最小语言，不是最终版
- 当前 lint 主要做结构一致性检查，不做深层语义推理
- 当前覆盖告警使用启发式判断“是否值得补 invariant/forbid”，还没有完整的语义充分性分析
- 当前 parser 假设缩进稳定且语法显式
