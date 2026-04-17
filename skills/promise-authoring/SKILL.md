---
name: promise-authoring
description: Run the full Promise-driven development workflow for AI coding. Use when Codex needs to define or revise system truth before implementation, translate PRD or Markdown requirements into a single System Promise with field/function/verify layers, decide which layer must move first, author or repair `.promise` files, or enforce Promise lint/format/check gates inside a repository.
---

# Promise Authoring

Use one `System Promise` as the source of truth for the whole development flow, not just as a validation syntax.

## Core Rule

Keep this hierarchy intact:

- `field layer > function layer > derived artifacts`
- `field layer -> function layer -> verify layer -> derived artifacts -> evidence`

When the user asks for a feature or change, decide which layer inside the single `System Promise` must move first. Do not jump to implementation if the Promise is still wrong or incomplete.

## Decide The Layer First

Use this decision rule:

- Change `field` first when the request adds or changes:
  - business objects
  - fields
  - states
  - invariants
  - forbidden implicit state
- Change `function` first when the request adds or changes:
  - actions
  - triggers
  - preconditions
  - reads / writes
  - rejects
  - side effects
- Change `verify` first when the request adds or changes:
  - proof of invariants
  - proof of state transitions
  - regression guards
  - delivery criteria
- Change code only after the necessary Promise layers inside the single graph are explicit.

If implementation seems to require a hidden flag, helper state, or undeclared field, stop and push that change back into the Promise layer.

## Run The Workflow

1. Discover the source of truth.

- Prefer an existing `.promise` file when it already exists.
- If the repository only has split Markdown Promise docs, merge them into one `.promise` graph before extending the system.
- If the repository already has implementation but no Promise, reconstruct one `System Promise` from the code carefully, then treat it as the new source of truth.
- If the repository is working on Promise self-hosting or tool self-bootstrap, prefer the Promise source under `tooling/` over secondary docs.

2. Author in Promise order.

- Write or revise one graph in the order `meta`, then `field`, then `function`, then `verify`.
- Keep `field` focused on truth: objects, fields, states, invariants, and forbidden implicit state.
- Keep `function` focused on behavior: triggers, preconditions, reads, writes, ensures, rejects, forbids.
- Keep `verify` focused on proof: what is being proven, by which method, with which scenarios and regression guards.

3. Validate before touching downstream layers.

- Run formatting first so the file becomes canonical.
- Run lint next so references and structural boundaries are sound.
- Run `lint --profile core` or `check --profile core --json` when the task is explicitly constraining the system to the minimal Promise Core subset.
- Run `check --json` when another tool, gate, or agent needs a machine-readable report.
- Run `tooling verify --json` when you need to confirm the repo source, bundled skill, and installed skill are still synchronized.

4. Implement only after Promise is stable.

- If the user asks for code, let code follow the Promise.
- If code and Promise disagree, fix Promise first when the truth changed; fix code first when implementation drifted.

5. Treat completion as verified Promise, not written code.

- A task is not done because code exists.
- A task is done when the Promise is explicit, implementation stays within bounds, and verification proves the promise.

## Use The CLI

- Prefer a repo-local `./promise` CLI if the repository already ships one.
- Otherwise use the bundled [scripts/promise](scripts/promise) launcher from this skill.
- Use these commands:

```bash
./promise format path/to/file.promise --write
./promise format path/to/file.promise --check
./promise lint path/to/file.promise
./promise lint path/to/file.promise --profile core
./promise lint path/to/file.promise --json
./promise check path/to/file.promise --json
./promise check path/to/file.promise --profile core --json
./promise graph path/to/file.promise --html /tmp/promise-graph.html
./promise tooling verify --json
```

- Use `format --check` as a CI-style formatting gate.
- Use `lint --json` when another tool or agent needs a machine-readable lint report.
- Use `check --json` when another tool or agent needs the full parse + lint report with the projected spec.
- Use `graph --html` when you want a self-contained HTML page that visualizes the current Promise graph; on large graphs it should switch to an overview/composite viewer that still keeps an aggregate visual graph on screen instead of forcing every node into one canvas.
- Use `tooling verify --json` when maintaining the Promise toolchain itself or after syncing the packaged skill.

## Fix Problems By Layer

- If parse fails, fix syntax, quoting, or indentation first.
- If lint fails, fix references, dependencies, reads, writes, or state transitions.
- If the Promise is structurally valid but semantically wrong, revise its `field`, `function`, or `verify` layer instead of hiding the gap in implementation code.
- If a change request pressures implementation to invent undeclared state, rewrite the Promise rather than embedding the state in code.

## Translate Markdown Or PRD Into Promise

When the repository starts from prose instead of DSL:

1. Extract system truth and write `field` blocks first.
2. Extract system behavior and write `function` blocks second.
3. Extract proof obligations and write `verify` blocks third.
4. Run `format`, `lint`, and `check`.

Do not mirror PRD structure blindly. Reorganize the material into one `System Promise` with clear layers.

## References

- Read [references/promise-language.md](references/promise-language.md) when you need DSL syntax, command behavior, or block examples.
- Read [references/promise-core.md](references/promise-core.md) when the task is about the minimal subset or which parts of the system must stay in Core.
- Read [references/promise-cli.promise](references/promise-cli.promise) and [references/promise-tooling-readme.md](references/promise-tooling-readme.md) when the task is about Promise CLI self-bootstrap, command surface, or step runtime behavior.
- Read [references/promise-standard.md](references/promise-standard.md) when you need the normative rules for the single `System Promise` and its field/function/verify layers.
- Read [references/promise-architecture.md](references/promise-architecture.md) when the task involves Promise Kernel, plugin enforcement, Orchestrator Agent flow, or CI gate design.
- Read [references/task-example.system.promise.md](references/task-example.system.promise.md), [references/task-example.promise](references/task-example.promise), and [references/task-example.spec.json](references/task-example.spec.json) when you need a concrete end-to-end example.

## Bundled Resources

- [scripts/promise](scripts/promise): Portable Promise CLI entrypoint.
- `scripts/promise_cli/`: Parser, formatter, linter, and JSON reporting logic.
