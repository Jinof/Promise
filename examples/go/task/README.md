# Go Task Example

This module validates Go artifacts generated from `examples/task/task.promise`.

It contains two generated packages:

- `default`: generated with built-in Promise to Go type mappings. `TaskID` remains a Go named type.
- `mapped`: generated with `../../task/go-type-map.json`. `TaskID` maps to the configured concrete Go type.

Run:

```bash
cd examples/go/task
make verify
```

To inspect the generated outputs directly:

```bash
../../../promise compile ../../task/task.promise --target go --out default
../../../promise compile ../../task/task.promise --target go --type-map ../../task/go-type-map.json --out mapped
go test ./...
```
