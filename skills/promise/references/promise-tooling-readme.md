# Promise CLI Self-Bootstrap

这里放的是 Promise 工具自身的 Promise 源文件，而不是示例。

入口文件是：

- [promise-cli.promise](/Users/jinof/source/Promise/tooling/promise-cli.promise)

它的作用不是演示语法，而是作为 Promise CLI 的自举约束：

1. 先用 Promise 描述 `parse / format / lint / check / compile / graph / impact / tooling verify`
2. 再用 Promise 显式描述 `path / tooling verify / --target / --out / --type-map / --intent / --json / --profile / --write / --check / --html` 这些输入面
3. 再用 Promise 显式描述每个命令的 step plan
4. 再让实际 CLI 从这份 Promise 生成 argparse 子命令、选项和 dispatch step plan
5. 再用测试验证“CLI 暴露的命令集合、选项集合、执行步骤”与 Promise 中声明的一致
6. `compile --target go` 从 Promise Spec 生成 Go contract package，包括声明类型的 Go 命名类型，并允许通过 `--type-map` 插件把 primitive 或声明类型映射到实际 Go 类型，而不是让 Go 成为第二个 truth source
7. `graph` 不只负责小图渲染，也要显式处理大规模 Promise graph 的 `full` 与 `overview/composite` 视图切换，并在 composite 模式下保留聚合图面而不是退化成纯摘要页
8. `impact` 从 intent 树出发，展示选中 intent 的上游链路、直接映射 Promise Item、下游影响项和相关 intent

推荐验证方式：

```bash
./promise check tooling/promise-cli.promise --json
./promise compile examples/task/task.promise --target go --out /tmp/promise-go-task
./promise compile examples/task/task.promise --target go --type-map examples/task/go-type-map.json --out /tmp/promise-go-task
make -C examples/go/task verify
./promise graph examples/task/task.promise --html /tmp/task-graph.html
./promise impact examples/task/task.promise --intent PreserveTaskLifecycleTruth --json
./promise tooling verify --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tooling verify` 会检查三层同步关系：

- `src/promise_cli/*` 是否和 repo skill 里的脚本镜像一致
- `docs/*`、`tooling/*` 是否和 repo skill references 一致
- repo skill bundle 是否和 `/Users/jinof/.codex/skills/promise` 的安装副本一致

如果从仓库根目录运行，它会做完整的三层检查。
如果从安装后的 skill 目录运行，它会退化成当前 skill bundle 自检和 `quick_validate` 校验，因为那时已经不再处于完整 repo 上下文。

如果后面新增 CLI 命令，应该先改这里的 Promise，再改实现和测试。
