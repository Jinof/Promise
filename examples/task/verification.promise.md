# Task Verification Layer Extract

这是一份从单一 [Task System Promise](/Users/jinof/source/Promise/examples/task/system.promise.md) 中抽取出来的验证层视图，不是独立的治理源。

## 基本信息

- Promise 名称：Task Verification Promise
- 领域 / 模块：Task
- 版本：v1
- 状态：active

## 验证目标

证明 `Task Field Promise v1` 与 `Task Function Promise v1` 已被实现并保持成立。

## 依赖层

- 字段层：`Task System Promise / field layer / v1`
- 功能层：`Task System Promise / function layer / v1`

## 验证矩阵

| 承诺 | 类型 | 验证方式 | 证据 |
| --- | --- | --- | --- |
| `status = done` 时 `completedAt` 必须存在 | field | unit | 完成任务后的对象断言 |
| `status = todo` 时 `completedAt` 必须为 `null` | field | unit | 新建任务后的对象断言 |
| `CreateTask` 只能创建 `todo` 状态任务 | function | integration | 创建接口或服务返回值断言 |
| `CompleteTask` 只能写入允许字段 | function | unit / review | 对写入结果和持久化字段做断言 |
| 不允许出现未声明状态 | field | unit / static-check | 枚举约束与异常路径断言 |

## 场景验证

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

#### 禁止回归

- 不允许默认创建 `done` 状态任务。
- 不允许遗漏时间字段。

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

#### 禁止回归

- 不允许只改 `status` 不改 `completedAt`。
- 不允许修改非授权字段。

### 场景：`CompleteTask rejects invalid transitions`

#### 对应承诺

- 已完成任务不能再次进入新的隐含状态。

#### 前置数据

- 一个状态为 `done` 的任务。

#### 操作

- 再次调用完成任务行为。

#### 预期结果

- 操作被拒绝，或明确返回无变更。
- 不会生成新的业务状态。

#### 禁止回归

- 不允许通过重试产生第二次完成时间语义。

## 失败判定

满足以下任一条件，即视为承诺未兑现：

- 出现未声明字段或未声明状态。
- `status` 与 `completedAt` 的关系不一致。
- 功能写入超出声明边界。
- 测试无法给出对应承诺的明确证据。
