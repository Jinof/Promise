# Promise CLI Self-Bootstrap

这里放的是 Promise 工具自身的 Promise 源文件，而不是示例。

入口文件是：

- [promise-cli.promise](/Users/jinof/source/Promise/tooling/promise-cli.promise)

它的作用不是演示语法，而是作为 Promise CLI 的自举约束：

1. 先用 Promise 描述 `parse / format / lint / check / graph / tooling verify`
2. 再用 Promise 显式描述 `path / tooling verify / --json / --profile / --write / --check / --html` 这些输入面
3. 再用 Promise 显式描述每个命令的 step plan
4. 再让实际 CLI 从这份 Promise 生成 argparse 子命令、选项和 dispatch step plan
5. 再用测试验证“CLI 暴露的命令集合、选项集合、执行步骤”与 Promise 中声明的一致
6. `graph` 不只负责小图渲染，也要显式处理大规模 Promise graph 的 `full` 与 `overview/composite` 视图切换，并在 composite 模式下保留聚合图面而不是退化成纯摘要页

推荐验证方式：

```bash
./promise check tooling/promise-cli.promise --profile core --json
./promise graph examples/task/task.promise --html /tmp/task-graph.html
./promise tooling verify --json
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

`tooling verify` 会检查三层同步关系：

- `src/promise_cli/*` 是否和 repo skill 里的脚本镜像一致
- `docs/*`、`tooling/*` 是否和 repo skill references 一致
- repo skill bundle 是否和 `/Users/jinof/.codex/skills/promise-authoring` 的安装副本一致

如果从仓库根目录运行，它会做完整的三层检查。
如果从安装后的 skill 目录运行，它会退化成当前 skill bundle 自检和 `quick_validate` 校验，因为那时已经不再处于完整 repo 上下文。

如果后面新增 CLI 命令，应该先改这里的 Promise，再改实现和测试。
