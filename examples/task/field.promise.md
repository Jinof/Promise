# Task Field Layer Extract

这是一份从单一 [Task System Promise](/Users/jinof/source/Promise/examples/task/system.promise.md) 中抽取出来的字段层视图，不是独立的治理源。

## 基本信息

- Promise 名称：Task Field Promise
- 领域 / 模块：Task
- 版本：v1
- 状态：active

## 目标

定义任务对象 `Task` 的字段、状态、语义和不变量。

## 核心对象

| 对象 | 说明 | 生命周期归属 |
| --- | --- | --- |
| `Task` | 一个待完成事项 | 从创建到归档 |

## 字段定义

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

### 派生字段

本对象没有派生字段。

### 读写边界

| 字段 | 可读取方 | 可写入方 | 备注 |
| --- | --- | --- | --- |
| `id` | 所有读取方 | 系统创建逻辑 | 创建后只读 |
| `title` | 所有读取方 | 创建 / 编辑功能 | |
| `status` | 所有读取方 | 状态变更功能 | |
| `completedAt` | 所有读取方 | 状态变更功能 | 与 `status` 联动 |
| `createdAt` | 所有读取方 | 系统创建逻辑 | 创建后只读 |
| `updatedAt` | 所有读取方 | 系统维护逻辑 | 每次合法变更后刷新 |

### 禁止隐含状态

- 不允许在代码里引入 `isCompleted` 之类未声明字段作为真实业务状态。
- 不允许用缓存标志、临时变量或前端显示态替代 `status` 的业务语义。

## 全局约束

- 同一个 `Task` 在任一时刻只能处于一个业务状态。
- 任一状态迁移都必须保持字段不变量成立。
