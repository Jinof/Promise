# Verification Layer Extract Template

这是一份拆分视图模板，只适合从单一 `System Promise` 中抽取验证层审阅材料。

系统的唯一治理源应当是 [system.promise.md](/Users/jinof/source/Promise/templates/system.promise.md)，而不是把验证层单独当成一份并列 Promise。

## 基本信息

- Promise 名称：
- 领域 / 模块：
- 版本：
- 状态：draft / active / deprecated
- Owner：

## 验证目标

说明这份验证层抽取视图要证明哪些承诺成立。

## 依赖层

- 字段层：
- 功能层：

## 验证矩阵

把承诺和验证方法直接对应起来。

| 承诺 | 类型 | 验证方式 | 证据 |
| --- | --- | --- | --- |
| | field / function | unit / integration / e2e / static-check / review | |

## 场景验证

### 场景：`<ScenarioName>`

#### 对应承诺

- 

#### 前置数据

- 

#### 操作

- 

#### 预期结果

- 

#### 禁止回归

- 

## 边界场景

列出容易破坏 Promise 的边界条件。

- 空值 / 缺失值
- 非法状态迁移
- 重复提交
- 并发写入
- 派生字段失真

## 失败判定

满足以下任一条件，即视为承诺未兑现：

- 字段语义与结果不一致
- 写入超出功能层声明边界
- 出现未声明状态
- 验证无法提供明确证据

## 交付要求

- 验证必须能重复执行
- 验证结果必须能指向具体承诺
- “代码看起来没问题”不能作为通过条件
