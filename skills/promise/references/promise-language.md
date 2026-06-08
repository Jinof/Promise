# Promise Language

这份文档定义 Promise 的文本 DSL。

目标不是替代 [docs/promise-standard.md](/Users/jinof/source/Promise/docs/promise-standard.md)，而是把单一 `System Promise` 映射成一个更适合 CLI 解析、lint 和编排的语言层。

## 设计目标

- 让 Promise 关系可以被机器稳定解析
- 保留足够强的人类可读性
- 尽量贴近现有 `Promise Spec` 结构
- 不依赖外部解析器生成器

## 文件结构

一个 `.promise` 文件表达一个系统或模块的唯一 Promise graph，由九类块组成：

```text
meta:
resource <ResourceName> kind <Kind>:
term <TermName> kind <action|effect|constraint|scope>:
cycle <CycleName> kind <feedback>:
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

### `resource`

块头：

```text
resource <ResourceName> kind <actor|artifact|capability|concept|data|entity|external|system|ui|workflow>:
```

块内支持：

- `summary "<text>"`
- `alias <StableToken>`
- `maps <PromiseItemRef> relation <Relation> [note "<text>"]`

`resource` 定义 intent 层可被操作的资源目录。资源不是 field/function/verify 事实层；它是人类需求进入系统前的对象表。`maps` 可以把抽象资源连接到具体 Promise Item，便于 impact 和 graph 从“操作资源”继续追踪到 field、function 或 verify。

示例：

```text
resource User kind actor:
  summary "Human user operating the system."

resource Task kind entity:
  summary "Task resource operated by user intent."
  maps TaskFieldPromise relation constrains
```

### `term`

块头：

```text
term <TermName> kind <action|effect|constraint|scope>:
```

块内支持：

- `summary "<text>"`
- `alias <StableToken>`
- `parent <TermName>`
- `disjoint <csv>`
- `opposite <TermName>`
- `maps <PromiseItemRef> relation <Relation> [note "<text>"]`

`term` 定义 intent requirement atom 使用的受控词表。`action` 约束资源操作动词，`effect` 约束期望结果，`constraint` 约束稳定约束名，`scope` 约束操作边界。声明某一类 term 后，该类 requirement atom 必须引用已声明 term 或其 alias。`scope` term 可以通过 `parent` 声明层级，父 scope 与子 scope 视为重叠；`disjoint` 用来声明 scope 不重叠；`opposite` 记录反向词关系；`maps` 可以把词表项追踪到具体 Promise Item。

示例：

```text
term tenant kind scope:
  summary "Tenant-wide scope."

term user_workspace kind scope:
  summary "Workspace visible to one user."
  parent tenant
  disjoint admin_console

term export kind action:
  summary "Make a resource available outside the system."
  opposite import

term export_file kind effect:
  summary "A downloadable file is produced."

term authorized_user kind constraint:
  summary "Only an authorized user can operate the resource."
  maps AuthPolicyFieldPromise relation constrains
```

### `cycle`

块头：

```text
cycle <CycleName> kind <feedback>:
```

块内支持：

- `summary "<text>"`
- `rationale "<text>"`
- `edge <SourceRef> -> <TargetRef> relation <Relation> [note "<text>"]`

`cycle` 声明显式允许的 intent graph feedback 环。Promise tooling 会把 intent、resource、term、cycle 和被映射的 Promise Item 投影成 typed directed graph，然后用强连通分量检测非预期环。cycle 的节点集合由 `edge` 端点自动推导；结构性层级或 lowering 边仍然不能靠声明 cycle 来绕过。

示例：

```text
cycle ReviewFeedbackLoop kind feedback:
  summary "Review and revision intentionally feed each other."
  rationale "The loop models an intentional negotiation path."
  edge ReviewIntent -> ReviseIntent relation requires
  edge ReviseIntent -> ReviewIntent relation blocks
```

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
- `requires <RequirementId> actor <Resource> action <Action> resource <Resource> [over <Resource>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `forbids <RequirementId> actor <Resource> action <Action> resource <Resource> [over <Resource>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `prefers <RequirementId> actor <Resource> action <Action> resource <Resource> over <Resource> [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `accepts <RequirementId> actor <Resource> action <Action> resource <Resource> [over <Resource>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `requires <RequirementId> subject <Subject> predicate <Predicate> object <Object> [over <Object>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `forbids <RequirementId> subject <Subject> predicate <Predicate> object <Object> [over <Object>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `prefers <RequirementId> subject <Subject> predicate <Predicate> object <Object> over <Object> [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `accepts <RequirementId> subject <Subject> predicate <Predicate> object <Object> [over <Object>] [scope <Scope>] [effect <Effect>] [constraint <Constraint>] [priority <must|should|may>] [when "<text>"] [because "<text>"] [note "<text>"]`
- `conflicts <IntentName> severity <blocking|tension|advisory> reason "<text>" [resolution "<text>"] [note "<text>"]`
- `maps <PromiseItemRef> relation <Relation> [note "<text>"]`

`intent` 记录人类核心诉求，并把常见需求句法抽成严谨的 requirement atom。`statement` 和 `rationale` 保留人的原始语言，`requires`、`forbids`、`prefers`、`accepts` 记录机器可比较的需求原子。推荐形态是 `actor action resource`：intent 表达“谁对哪个资源做什么操作”。`scope` 缩小操作边界，`effect` 记录人类期望产生的结果，`constraint` 记录稳定约束名，`priority` 记录该 atom 的强度；未声明 priority 时，格式化后的 canonical DSL 会使用所在 intent 的 priority。一个 intent 可以 `maps` 多个 Promise Item，也可以先只声明 requirement atom 而不立刻绑定到具体 Promise Item；一个 Promise Item 也可以被多个 intent 映射。`conflicts` 用来确认、解释和解决 intent 层两个抽象人类意图之间的张力，而不是把冲突隐藏到 field/function/verify 这些具体 Promise Item 里。

Requirement atom 优先使用资源操作形态：

```text
requires UserExportsTask actor User action export resource Task scope user_workspace effect export_file constraint authorized_user priority must
forbids HiddenTaskState actor TaskLifecycle action hides resource TaskState
prefers CadLayout actor PromiseGraph action uses resource CadMatrixLayout over ForceLayout
accepts ConflictWarning actor Lint action reports resource IntentConflictCandidate
```

为了兼容更抽象的需求，也可以使用 `subject predicate object` 三元组承接常见人类需求语言：

```text
requires UserCanExport subject User predicate can object export_task
forbids HiddenState subject TaskLifecycle predicate hides object state
prefers CadLayout subject PromiseGraph.layout predicate uses object cad_matrix over force_layout
accepts ConflictWarning subject Lint predicate reports object intent_conflict_candidate
```

`actor`、`resource` 和 `over` 必须指向已定义 resource。`subject`、`predicate`、`object`、`scope`、`effect`、`constraint` 和 `over` 必须是稳定 token，而不是自由文本。若 `.promise` 声明了对应 kind 的 term，`action`、`scope`、`effect`、`constraint` 必须引用受控词表中的 term。需要解释来源、业务语境或自然语言细节时，用 `statement`、`rationale`、`because` 或 `note`。

所有 intent 组成一棵多叉树：

- 存在 intent 时必须恰好一个 `root true`
- root intent 代表系统级诉求，不能声明 `parent`
- 非 root intent 必须声明且只能声明一个 `parent`
- `parent` 不能形成环
- 非预期 intent graph 环会被检测；反向边会作为两节点 reciprocal cycle 报告
- `refines`、`contains`、`maps`、`constrains`、`verifies`、`supports` 等结构或 lowering relation 不能通过 `cycle` 声明覆盖
- 只有完整匹配实际边、且不包含结构/lowering relation 的 `cycle` 声明可以覆盖 feedback 类环
- 已声明但不再匹配实际 graph 环的 `cycle` 会作为 stale declaration 报告
- intent 必须至少声明一个 `maps` 或一个结构化 requirement atom
- `conflicts` 的目标必须是已声明 intent，不能指向自身
- 同一对 intent 的冲突只能声明一次
- `blocking` 冲突必须给出 `resolution`
- requirement id 不能重复
- `prefers` 必须声明 `over`
- requirement `priority` 必须是 `must`、`should` 或 `may`
- 使用 `actor/action/resource` 形态时，`actor`、`resource` 和 `over` 必须引用已声明 resource
- term kind 必须是 `action`、`effect`、`constraint` 或 `scope`
- term parent 不能形成环，parent、disjoint、opposite 和 maps 目标必须存在
- scope term 的 `parent` 表示覆盖关系，`disjoint` 表示不重叠关系

工具会自动检测未确认的 intent conflict candidate，并以 warning 报告。当前确定性检测规则包括：

- 两个非 root intent 的 requirement atom 具有相同 resource operation 或 `subject predicate object`，但一个 `requires`、另一个 `forbids`；若两者声明 scope，只有相同 scope、父子 scope 或未声明 scope 会被视为重叠，声明为 disjoint 的 scope 不产生冲突 candidate
- 两个非 root intent 对同一 action 或 `subject predicate` 声明相反偏好，例如 `prefers ... resource CadMatrixLayout over ForceLayout` 与 `prefers ... resource ForceLayout over CadMatrixLayout`
- 两个非 root intent 映射同一个 Promise Item，其中一个 `maps` relation 是 `conflicts` 或 `blocks`，另一个是正向 relation
- 两个非 root intent 映射到的 field invariant/constraint `must` 表达式对同一字段提出互斥要求，例如 `Mode.value == Mode.value.auto` 与 `Mode.value == Mode.value.manual`

自动检测到的 candidate 不等于已解决冲突；需要通过 `conflicts ... reason ... resolution ...` 明确确认和处理。

`Relation` 当前支持：`motivates`、`constrains`、`explains`、`verifies`、`conflicts`、`refines`、`supports`、`requires`、`blocks`。

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
