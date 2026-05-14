package taskexample_test

import (
	"errors"
	"testing"
	"time"

	defaultcontract "promise-go-task-example/default"
	mappedcontract "promise-go-task-example/mapped"
)

func TestDefaultGeneratedTaskContract(t *testing.T) {
	now := time.Unix(1_700_000_000, 0).UTC()
	task := defaultcontract.Task{
		ID:        defaultcontract.TaskID("task-1"),
		Title:     "Write Promise",
		Status:    defaultcontract.TaskStatusTodo,
		CreatedAt: now,
		UpdatedAt: now,
	}

	if err := defaultcontract.ValidateTaskPromise(task); err != nil {
		t.Fatalf("todo task should satisfy Promise invariants: %v", err)
	}

	task.Status = defaultcontract.TaskStatusDone
	if err := defaultcontract.ValidateTaskPromise(task); !errors.Is(err, defaultcontract.ErrTaskInvariantViolation) {
		t.Fatalf("done task without completedAt should violate Promise invariants, got %v", err)
	}

	task.CompletedAt = &now
	if err := defaultcontract.ValidateTaskPromise(task); err != nil {
		t.Fatalf("done task with completedAt should satisfy Promise invariants: %v", err)
	}
}

func TestDefaultGeneratedTransitionGuards(t *testing.T) {
	if !defaultcontract.CanTransitionTaskStatus(defaultcontract.TaskStatusTodo, defaultcontract.TaskStatusDone) {
		t.Fatal("todo should transition to done")
	}
	if !defaultcontract.CanTransitionTaskStatus(defaultcontract.TaskStatusDone, defaultcontract.TaskStatusTodo) {
		t.Fatal("done should transition to todo")
	}
	if defaultcontract.CanTransitionTaskStatus(defaultcontract.TaskStatusTodo, defaultcontract.TaskStatus("archived")) {
		t.Fatal("todo should not transition to undeclared archived state")
	}
}

func TestMappedGeneratedTaskContract(t *testing.T) {
	now := time.Unix(1_700_000_000, 0).UTC()
	task := mappedcontract.Task{
		ID:        "task-1",
		Title:     "Write Promise",
		Status:    mappedcontract.TaskStatusTodo,
		CreatedAt: now,
		UpdatedAt: now,
	}

	if err := mappedcontract.ValidateTaskPromise(task); err != nil {
		t.Fatalf("mapped todo task should satisfy Promise invariants: %v", err)
	}
}
