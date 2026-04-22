# Task System Promise

这是一份单一 `System Promise` 示例。`Task` 的字段层、功能层和验证层都属于同一个 Promise graph。

## 基本信息

- 系统名称：Task
- 领域 / 模块：Task
- 版本：v1
- 状态：active
- Owner：product / engineering

## 目标

约束任务系统中 `Task` 对象的真相、允许行为和兑现证明，确保代码只能从这份 Promise 派生。

## 非目标

- 不讨论 UI 布局
- 不讨论具体框架
- 不讨论持久化技术选型

## 字段层

### 核心对象

| 对象 | 说明 | 生命周期归属 |
| --- | --- | --- |
| `Task` | 一个待完成事项 | 从创建到归档 |

### 字段定义

### 对象：`Task`

| 字段 | 类型 | 必填 | 默认值 | 可空 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `id` | string | 是 | 无 | 否 | 任务唯一标识 |
| `title` | string | 是 | 无 | 否 | 任务标题 |
| `status` | enum | 是 | `todo` | 否 | 任务状态 |
| `completedAt` | datetime | 否 | `null` | 是 | 任务完成时间 |
| `createdAt` | datetime | 是 | 系统生成 | 否 | 任务创建时间 |
| `updatedAt` | datetime | 是 | 系统生成 | 否 | 最近更新时间 |

### 字段语义

- `title`：表达任务内容，不允许为空字符串。
- `status`：表示任务所处业务状态，而不是 UI 状态。
- `completedAt`：只在任务已完成时有值，用来表达完成事实。
- `updatedAt`：任何合法字段变更后都必须刷新。

### 状态定义

| 状态 | 含义 | 是否终态 | 允许迁移到 |
| --- | --- | --- | --- |
| `todo` | 待完成 | 否 | `done` |
| `done` | 已完成 | 否 | `todo` |

### 不变量

- `status = done` 时，`completedAt` 必须存在。
- `status = todo` 时，`completedAt` 必须为 `null`。
- `createdAt` 不可被业务功能回写。
- `id` 一旦创建不可修改。

### 禁止隐含状态

- 不允许在代码里引入 `isCompleted` 之类未声明字段作为真实业务状态。
- 不允许用缓存标志、临时变量或前端显示态替代 `status` 的业务语义。

## 功能层

### 行为清单

| 行为 | 触发条件 | 前置条件 | 读取集合 | 写入集合 | 成功结果 |
| --- | --- | --- | --- | --- | --- |
| `CreateTask` | 用户提交新任务 | `title` 非空 | 无 | `id`, `title`, `status`, `completedAt`, `createdAt`, `updatedAt` | 生成一个 `todo` 状态的新任务 |
| `CompleteTask` | 用户标记任务完成 | 任务存在且当前状态为 `todo` | `id`, `status` | `status`, `completedAt`, `updatedAt` | 任务状态变为 `done` |

### 行为：`CreateTask`

#### 触发条件

- 用户提交创建请求。

#### 前置条件

- `title` 非空。

#### 读取边界

- 不依赖既有任务状态。

#### 写入边界

- 允许写入 `Task` 的所有初始化字段。
- 不允许写入未声明字段。

#### 处理结果

- 成功时，新建一个 `Task`。
- 新任务的 `status` 必须是 `todo`。
- 新任务的 `completedAt` 必须是 `null`。

#### 禁止行为

- 不允许创建后直接进入 `done`。
- 不允许跳过 `createdAt` / `updatedAt` 的系统写入。

### 行为：`CompleteTask`

#### 触发条件

- 用户发起完成任务请求。

#### 前置条件

- 目标任务存在。
- 目标任务当前状态为 `todo`。

#### 读取边界

- 允许读取 `id`、`status`。
- 不允许依赖未声明的辅助状态。

#### 写入边界

- 允许写入 `status`、`completedAt`、`updatedAt`。
- 不允许修改 `id`、`createdAt`、`title`。

#### 处理结果

- 成功时，`status` 必须变为 `done`。
- `completedAt` 必须写入当前完成时间。
- `updatedAt` 必须刷新。

#### 禁止行为

- 不允许只写 `status` 而漏写 `completedAt`。
- 不允许通过额外布尔值表达“完成态”。

## 验证层

### 验证矩阵

| 承诺 | 层面 | 验证方式 | 证据 |
| --- | --- | --- | --- |
| `status = done` 时 `completedAt` 必须存在 | field | unit | 完成任务后的对象断言 |
| `status = todo` 时 `completedAt` 必须为 `null` | field | unit | 新建任务后的对象断言 |
| `CreateTask` 只能创建 `todo` 状态任务 | function | integration | 创建接口或服务返回值断言 |
| `CompleteTask` 只能写入允许字段 | function | unit / review | 对写入结果和持久化字段做断言 |
| 不允许出现未声明状态 | field | unit / static-check | 枚举约束与异常路径断言 |

### 场景：`CreateTask creates a valid todo task`

#### 对应承诺

- 新任务必须以 `todo` 状态创建。
- 新任务的 `completedAt` 必须为 `null`。

#### 前置数据

- 输入 `title = "Buy milk"`。

#### 操作

- 调用创建任务行为。

#### 预期结果

- 返回任务存在 `id`。
- `status = todo`。
- `completedAt = null`。
- `createdAt`、`updatedAt` 存在。

#### 失败判定

- 默认创建 `done` 状态任务。
- 遗漏时间字段。

### 场景：`CompleteTask keeps field invariants`

#### 对应承诺

- 完成任务后 `status = done`。
- 完成任务后 `completedAt` 必须存在。

#### 前置数据

- 一个状态为 `todo` 的已存在任务。

#### 操作

- 调用完成任务行为。

#### 预期结果

- `status` 从 `todo` 变为 `done`。
- `completedAt` 被写入。
- `updatedAt` 被刷新。
- `title`、`id`、`createdAt` 不变。

#### 失败判定

- 只改 `status` 不改 `completedAt`。
- 修改非授权字段。

## 派生产物

- task implementation code
- task tests
- task api contract

## 变更规则

- 新增字段、状态或不变量时，先改字段层。
- 新增行为边界时，先改功能层。
- 新增证明要求时，先改验证层。
