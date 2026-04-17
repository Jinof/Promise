# Task Function Layer Extract

这是一份从单一 [Task System Promise](/Users/jinof/source/Promise/examples/task/system.promise.md) 中抽取出来的功能层视图，不是独立的治理源。

## 基本信息

- Promise 名称：Task Function Promise
- 领域 / 模块：Task
- 版本：v1
- 状态：active

## 依赖的字段层

- `Task Field Promise v1`

## 目标

定义任务系统的两个行为：创建任务、完成任务。

## 行为清单

| 行为 | 触发条件 | 前置条件 | 读取集合 | 写入集合 | 成功结果 |
| --- | --- | --- | --- | --- | --- |
| `CreateTask` | 用户提交新任务 | `title` 非空 | 无 | `id`, `title`, `status`, `completedAt`, `createdAt`, `updatedAt` | 生成一个 `todo` 状态的新任务 |
| `CompleteTask` | 用户标记任务完成 | 任务存在且当前状态为 `todo` | `id`, `status` | `status`, `completedAt`, `updatedAt` | 任务状态变为 `done` |

## 行为定义

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

#### 幂等性 / 一致性

- 对已处于 `done` 的任务再次执行 `CompleteTask`，必须拒绝或显式返回无变更，不能产生第二套状态语义。

#### 禁止行为

- 不允许只写 `status` 而漏写 `completedAt`。
- 不允许通过额外布尔值表达“完成态”。

## 错误与拒绝条件

- `title` 为空时拒绝创建任务。
- 目标任务不存在时拒绝完成任务。
- 目标任务已完成时不得重复进入新的隐含状态。
