.PHONY: generate test verify

GOCACHE ?= /tmp/promise-go-build-cache

generate:
	../../../promise compile ../../task/task.promise --target go --out default
	../../../promise compile ../../task/task.promise --target go --type-map ../../task/go-type-map.json --out mapped

test:
	GOCACHE=$(GOCACHE) go test ./...

verify: generate test
