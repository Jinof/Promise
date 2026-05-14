from __future__ import annotations

import argparse
from dataclasses import dataclass
from dataclasses import asdict
import html as html_lib
import json as json_lib
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable

from promise_cli.dsl import LintIssue, PromiseParseError, format_spec, lint_spec, parse_file, to_json


ROOT = Path(__file__).resolve().parents[2]
CLI_INVOCATION_OBJECT = "PromiseCliInvocation"
ENUM_TYPE_RE = re.compile(r"^enum\(([^)]+)\)$")
STEP_RE = re.compile(r"^step\s*=\s*([A-Za-z0-9_-]+)$")
SKILL_NAME = "promise"
GRAPH_LANE_ORDER = ("system", "intent", "field", "function", "verify")
GRAPH_LANE_TITLES = {
    "system": "System",
    "intent": "Intent Layer",
    "field": "Field Layer",
    "function": "Function Layer",
    "verify": "Verify Layer",
}
FULL_GRAPH_NODE_LIMIT = 24
FULL_GRAPH_EDGE_LIMIT = 48
OVERVIEW_CLUSTER_PREVIEW_LIMIT = 16
OVERVIEW_RELATION_PREVIEW_LIMIT = 18
EXPLORER_PAGE_SIZE = 24
CLI_PROMISE_CANDIDATES = (
    ROOT / "tooling" / "promise-cli.promise",
    ROOT / "references" / "promise-cli.promise",
)
CLI_PROMISE_PATH = next((path for path in CLI_PROMISE_CANDIDATES if path.exists()), CLI_PROMISE_CANDIDATES[0])
GO_KEYWORDS = {
    "break",
    "default",
    "func",
    "interface",
    "select",
    "case",
    "defer",
    "go",
    "map",
    "struct",
    "chan",
    "else",
    "goto",
    "package",
    "switch",
    "const",
    "fallthrough",
    "if",
    "range",
    "type",
    "continue",
    "for",
    "import",
    "return",
    "var",
}
GO_INITIALISMS = {
    "api": "API",
    "html": "HTML",
    "http": "HTTP",
    "id": "ID",
    "json": "JSON",
    "url": "URL",
    "xml": "XML",
}
GO_OBSOLETE_GENERATED_FILES = ("promise_test.go",)
GO_PRIMITIVE_FIELD_TYPES = {
    "boolean": ("bool", None),
    "datetime": ("time.Time", "time"),
    "integer": ("int", None),
    "json": ("any", None),
    "number": ("float64", None),
    "path": ("string", None),
    "string": ("string", None),
    "text": ("string", None),
}


@dataclass
class CommandContract:
    action: str
    summary: str
    steps: list[str]
    invocation_field_names: list[str]
    exclusive_groups: list[set[str]]


@dataclass
class CliContract:
    invocation_fields: dict[str, dict[str, Any]]
    commands: dict[str, CommandContract]


StepFn = Callable[[dict[str, Any]], int | None]


def load_cli_contract(path: str | Path = CLI_PROMISE_PATH) -> CliContract:
    raw_contract = parse_file(path)
    invocation_fields = _collect_invocation_fields(raw_contract)
    commands: dict[str, CommandContract] = {}

    for function_promise in raw_contract["functionPromises"]:
        action = function_promise["action"]
        if action in commands:
            raise RuntimeError(f"CLI contract declares duplicate action '{action}'.")
        commands[action] = CommandContract(
            action=action,
            summary=function_promise["summary"],
            steps=_extract_steps(function_promise),
            invocation_field_names=_collect_invocation_field_names(function_promise),
            exclusive_groups=_collect_exclusive_groups(function_promise, invocation_fields),
        )

    return CliContract(invocation_fields=invocation_fields, commands=commands)


def build_parser(contract: CliContract | None = None) -> argparse.ArgumentParser:
    if contract is None:
        contract = load_cli_contract()
    parser = argparse.ArgumentParser(prog="promise", description="Promise DSL CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in contract.commands.values():
        command_parser = subparsers.add_parser(command.action, help=command.summary)
        grouped_fields = {field_name for group in command.exclusive_groups for field_name in group}

        for field_name in command.invocation_field_names:
            if field_name in grouped_fields:
                continue
            _add_cli_argument(command_parser, field_name, contract.invocation_fields[field_name])

        for field_group in command.exclusive_groups:
            mutex_group = command_parser.add_mutually_exclusive_group()
            for field_name in command.invocation_field_names:
                if field_name in field_group:
                    _add_cli_argument(mutex_group, field_name, contract.invocation_fields[field_name])

    return parser


def main(argv: list[str] | None = None) -> int:
    contract = load_cli_contract()
    parser = build_parser(contract)

    args = parser.parse_args(argv)
    command = contract.commands[args.command]
    return _run_command_steps(args, command)


STEP_HANDLERS: dict[str, StepFn] = {}


def _run_command_steps(args: argparse.Namespace, command: CommandContract) -> int:
    state: dict[str, Any] = {
        "args": args,
        "command": command,
        "path": getattr(args, "path", None),
        "html_path": getattr(args, "html", None),
        "mode": getattr(args, "mode", None),
        "target": getattr(args, "target", None),
        "out_path": getattr(args, "out", None),
        "type_map_path": getattr(args, "typeMap", None),
        "intent": getattr(args, "intent", None),
        "profile": getattr(args, "profile", "full"),
        "json_requested": getattr(args, "json", False),
        "raw_source": None,
        "formatted_source": None,
        "compiled_files": None,
        "compile_error": None,
        "graph_html": None,
        "graph_model": None,
        "graph_node_count": 0,
        "graph_edge_count": 0,
        "graph_view_mode": None,
        "graph_composition": None,
        "impact_report": None,
        "spec": None,
        "issues": [],
        "parse_error": None,
        "tooling_checks": [],
    }

    for step in command.steps:
        handler = _get_step_handler(step)
        result = handler(state)
        if result is not None:
            return result

    raise RuntimeError(f"Command '{command.action}' completed without a terminal step.")


def _get_step_handler(step_name: str) -> StepFn:
    handler = STEP_HANDLERS.get(step_name)
    if handler is None:
        raise RuntimeError(f"Unknown CLI step '{step_name}'.")
    return handler


def _print_issues(issues: list) -> None:
    for issue in issues:
        print(f"{issue.severity.upper()} [{issue.code}] {issue.message}", file=sys.stderr)


def _split_issues(issues: list[LintIssue]) -> tuple[list[LintIssue], list[LintIssue]]:
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity != "error"]
    return errors, warnings


def _build_report(
    *,
    path: str,
    profile: str,
    issues: list,
    error: dict | None,
    include_spec: bool,
    spec: dict | None,
) -> dict:
    errors, warnings = _split_issues(issues)
    return {
        "ok": error is None and len(errors) == 0,
        "path": path,
        "profile": profile,
        "issueCount": len(issues),
        "errorCount": len(errors),
        "warningCount": len(warnings),
        "issues": [asdict(issue) for issue in issues],
        "spec": spec if include_spec else None,
        "error": error,
    }


def _build_tooling_report(*, mode: str, issues: list[LintIssue], checks: list[dict[str, Any]]) -> dict[str, Any]:
    errors, warnings = _split_issues(issues)
    return {
        "ok": len(errors) == 0,
        "mode": mode,
        "issueCount": len(issues),
        "errorCount": len(errors),
        "warningCount": len(warnings),
        "issues": [asdict(issue) for issue in issues],
        "checks": checks,
    }


def _step(name: str) -> Callable[[StepFn], StepFn]:
    def decorator(func: StepFn) -> StepFn:
        STEP_HANDLERS[name] = func
        return func

    return decorator


@_step("load_source_text")
def _load_source_text_step(state: dict[str, Any]) -> int | None:
    path = Path(state["path"])
    state["raw_source"] = path.read_text(encoding="utf-8")
    return None


@_step("parse_source")
def _parse_source_step(state: dict[str, Any]) -> int | None:
    try:
        state["spec"] = parse_file(state["path"])
        state["parse_error"] = None
    except PromiseParseError as exc:
        state["spec"] = None
        state["parse_error"] = {
            "type": "parse_error",
            "message": str(exc),
        }
    return None


@_step("lint_spec")
def _lint_spec_step(state: dict[str, Any]) -> int | None:
    if state["spec"] is None:
        state["issues"] = []
        return None
    state["issues"] = lint_spec(state["spec"], profile=state["profile"])
    return None


@_step("format_spec")
def _format_spec_step(state: dict[str, Any]) -> int | None:
    if state["spec"] is None:
        state["formatted_source"] = None
        return None
    state["formatted_source"] = format_spec(state["spec"])
    return None


@_step("emit_spec_json")
def _emit_spec_json_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        print(f"Parse error: {state['parse_error']['message']}", file=sys.stderr)
        return 1
    print(to_json(state["spec"]))
    return 0


@_step("emit_formatted_result")
def _emit_formatted_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        print(f"Parse error: {state['parse_error']['message']}", file=sys.stderr)
        return 1

    args = state["args"]
    path = Path(state["path"])
    formatted = state["formatted_source"]
    raw = state["raw_source"]

    if getattr(args, "check", False):
        if raw == formatted:
            print(f"OK: {state['path']} is already formatted.")
            return 0
        print(f"FAILED: {state['path']} is not formatted.", file=sys.stderr)
        return 1

    if getattr(args, "write", False):
        path.write_text(formatted, encoding="utf-8")
        print(f"Formatted {state['path']}.")
        return 0

    print(formatted, end="")
    return 0


@_step("emit_lint_result")
def _emit_lint_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        return _emit_parse_error_result(
            path=state["path"],
            profile=state["profile"],
            error=state["parse_error"],
            json_requested=state["json_requested"],
            include_spec=False,
        )

    if state["json_requested"]:
        report = _build_report(
            path=state["path"],
            profile=state["profile"],
            issues=state["issues"],
            error=None,
            include_spec=False,
            spec=None,
        )
        print(to_json(report))
        return 0 if report["ok"] else 1

    errors, warnings = _split_issues(state["issues"])
    if state["issues"]:
        _print_issues(state["issues"])
    if errors:
        return 1
    if warnings:
        print(f"OK: {state['path']} passed lint with {len(warnings)} warning(s).")
        return 0

    print(f"OK: {state['path']} passed lint.")
    return 0


@_step("emit_check_result")
def _emit_check_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        return _emit_parse_error_result(
            path=state["path"],
            profile=state["profile"],
            error=state["parse_error"],
            json_requested=state["json_requested"],
            include_spec=True,
        )

    report = _build_report(
        path=state["path"],
        profile=state["profile"],
        issues=state["issues"],
        error=None,
        include_spec=True,
        spec=state["spec"],
    )
    if state["json_requested"]:
        print(to_json(report))
        return 0 if report["ok"] else 1

    errors, warnings = _split_issues(state["issues"])
    if state["issues"]:
        _print_issues(state["issues"])
    if errors:
        print(
            f"FAILED: {state['path']} has {len(errors)} error(s).",
            file=sys.stderr,
        )
        return 1
    if warnings:
        print(f"OK: {state['path']} passed check with {len(warnings)} warning(s).")
        return 0

    print(f"OK: {state['path']} passed parse and lint.")
    return 0


@_step("compile_go_contract")
def _compile_go_contract_step(state: dict[str, Any]) -> int | None:
    if state["spec"] is None:
        state["compiled_files"] = None
        return None

    errors, _warnings = _split_issues(state["issues"])
    if errors:
        state["compiled_files"] = None
        return None

    if state["target"] != "go":
        raise RuntimeError(f"Unknown compile target '{state['target']}'.")

    try:
        type_mappings = _load_type_mapping_plugin(state["type_map_path"], state["target"])
    except ValueError as exc:
        state["compile_error"] = str(exc)
        state["compiled_files"] = None
        return None

    state["compiled_files"] = _compile_go_contract_files(state["spec"], type_mappings)
    return None


@_step("emit_compile_result")
def _emit_compile_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        print(f"Parse error: {state['parse_error']['message']}", file=sys.stderr)
        return 1

    errors, warnings = _split_issues(state["issues"])
    if state["issues"]:
        _print_issues(state["issues"])
    if errors:
        print(
            f"FAILED: {state['path']} has {len(errors)} error(s); compile did not emit artifacts.",
            file=sys.stderr,
        )
        return 1
    if state["compile_error"] is not None:
        print(
            f"FAILED: {state['compile_error']}",
            file=sys.stderr,
        )
        return 1

    output_path = state["out_path"]
    if not output_path:
        print("FAILED: compile requires an explicit --out directory.", file=sys.stderr)
        return 1

    compiled_files = state["compiled_files"] or {}
    destination = Path(output_path)
    destination.mkdir(parents=True, exist_ok=True)
    for relative_path, content in compiled_files.items():
        file_path = destination / relative_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
    if state["target"] == "go":
        _remove_obsolete_go_generated_files(destination, compiled_files)

    message = f"Compiled {state['target']} Promise artifacts to {destination} ({len(compiled_files)} file(s))."
    if warnings:
        message += f" {len(warnings)} warning(s)."
    print(message)
    return 0


@_step("render_graph_html")
def _render_graph_html_step(state: dict[str, Any]) -> int | None:
    if state["spec"] is None:
        state["graph_model"] = None
        state["graph_html"] = None
        return None

    state["graph_model"] = _build_graph_model(state["spec"], state["path"])
    state["graph_html"] = _render_graph_html_document(state["graph_model"])
    state["graph_node_count"] = state["graph_model"]["nodeCount"]
    state["graph_edge_count"] = state["graph_model"]["edgeCount"]
    state["graph_view_mode"] = state["graph_model"]["viewMode"]
    state["graph_composition"] = state["graph_model"]["composition"]
    return None


@_step("emit_graph_result")
def _emit_graph_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        print(f"Parse error: {state['parse_error']['message']}", file=sys.stderr)
        return 1

    graph_html = state["graph_html"]
    output_path = state["html_path"]
    if output_path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(graph_html, encoding="utf-8")
        print(f"Wrote Promise graph HTML to {destination}.")
        return 0

    print(graph_html, end="")
    return 0


@_step("compute_intent_impact")
def _compute_intent_impact_step(state: dict[str, Any]) -> int | None:
    if state["spec"] is None:
        state["impact_report"] = None
        return None

    state["impact_report"] = _build_intent_impact_report(state["spec"], state["intent"])
    return None


@_step("emit_impact_result")
def _emit_impact_result_step(state: dict[str, Any]) -> int | None:
    if state["parse_error"] is not None:
        if state["json_requested"]:
            report = {
                "ok": False,
                "path": state["path"],
                "selectedIntent": state["intent"],
                "error": state["parse_error"],
            }
            print(to_json(report))
            return 1
        print(f"Parse error: {state['parse_error']['message']}", file=sys.stderr)
        return 1

    report = state["impact_report"]
    if state["json_requested"]:
        print(to_json(report))
        return 0 if report["ok"] else 1

    if not report["ok"]:
        print(f"FAILED: {report['error']['message']}", file=sys.stderr)
        return 1

    _print_intent_impact_report(report)
    return 0


@_step("collect_tooling_verification")
def _collect_tooling_verification_step(state: dict[str, Any]) -> int | None:
    if state["mode"] != "verify":
        raise RuntimeError(f"Unknown tooling mode '{state['mode']}'.")

    issues: list[LintIssue] = []
    checks: list[dict[str, Any]] = []
    validator_path = _quick_validate_path()

    if _is_repo_root(ROOT):
        for name, source_path, mirror_path in _repo_bundle_file_pairs(ROOT):
            _check_file_mirror(name, source_path, mirror_path, issues, checks)

        repo_skill_dir = _repo_skill_dir(ROOT)
        installed_skill_dir = _installed_skill_dir()
        _check_skill_directory_sync(repo_skill_dir, installed_skill_dir, issues, checks)
        _check_skill_validation("repo skill quick validate", repo_skill_dir, validator_path, issues, checks)
        _check_skill_validation(
            "installed skill quick validate",
            installed_skill_dir,
            validator_path,
            issues,
            checks,
            optional=True,
        )
    elif _is_skill_root(ROOT):
        _check_skill_bundle_presence(ROOT, issues, checks)
        installed_skill_dir = _installed_skill_dir()
        if ROOT.resolve() == installed_skill_dir.resolve():
            checks.append(
                {
                    "name": "current skill bundle is the installed skill",
                    "ok": True,
                    "details": "Running tooling verify from the installed Promise skill.",
                }
            )
        else:
            _check_skill_directory_sync(ROOT, installed_skill_dir, issues, checks)
        _check_skill_validation("current skill quick validate", ROOT, validator_path, issues, checks)
        _check_skill_validation(
            "installed skill quick validate",
            installed_skill_dir,
            validator_path,
            issues,
            checks,
            optional=True,
        )
    else:
        issues.append(
            LintIssue(
                "tooling-unknown-root",
                f"tooling verify does not recognize Promise workspace layout under {ROOT}.",
            )
        )
        checks.append(
            {
                "name": "recognized Promise tooling layout",
                "ok": False,
                "details": f"Unrecognized Promise tooling layout under {ROOT}.",
            }
        )

    state["issues"] = issues
    state["tooling_checks"] = checks
    return None


@_step("emit_tooling_verify_result")
def _emit_tooling_verify_result_step(state: dict[str, Any]) -> int | None:
    report = _build_tooling_report(
        mode=state["mode"],
        issues=state["issues"],
        checks=state["tooling_checks"],
    )
    if state["json_requested"]:
        print(to_json(report))
        return 0 if report["ok"] else 1

    errors, warnings = _split_issues(state["issues"])
    if state["issues"]:
        _print_issues(state["issues"])
    if errors:
        print(
            f"FAILED: tooling {state['mode']} found {len(errors)} error(s).",
            file=sys.stderr,
        )
        return 1
    if warnings:
        print(f"OK: tooling {state['mode']} passed with {len(warnings)} warning(s).")
        return 0

    skipped = sum(1 for check in state["tooling_checks"] if check.get("status") == "skipped")
    passed = sum(1 for check in state["tooling_checks"] if check.get("ok"))
    message = f"OK: tooling {state['mode']} passed {passed} check(s)."
    if skipped:
        message += f" Skipped {skipped} optional check(s)."
    print(message)
    return 0


def _emit_parse_error_result(
    *,
    path: str,
    profile: str,
    error: dict[str, str],
    json_requested: bool,
    include_spec: bool,
) -> int:
    if json_requested:
        report = _build_report(
            path=path,
            profile=profile,
            issues=[],
            error=error,
            include_spec=include_spec,
            spec=None,
        )
        print(to_json(report))
        return 1
    print(f"Parse error: {error['message']}", file=sys.stderr)
    return 1


def _build_intent_impact_report(spec: dict[str, Any], selected_intent: str | None) -> dict[str, Any]:
    intent_promises = spec.get("intentPromises", [])
    intent_index = {intent["name"]: intent for intent in intent_promises}
    root_intents = [intent["name"] for intent in intent_promises if intent.get("root")]
    root_intent = root_intents[0] if root_intents else None
    intent_tree = _build_intent_tree(intent_promises)

    base_report: dict[str, Any] = {
        "ok": True,
        "intentCount": len(intent_promises),
        "rootIntent": root_intent,
        "selectedIntent": selected_intent,
        "intentTree": intent_tree,
        "intentChain": None,
        "directItems": [],
        "downstreamItems": [],
        "relatedIntents": [],
        "error": None,
    }

    if not selected_intent:
        return base_report

    intent_promise = intent_index.get(selected_intent)
    if intent_promise is None:
        base_report["ok"] = False
        base_report["error"] = {
            "type": "unknown_intent",
            "message": f"Unknown intent '{selected_intent}'.",
        }
        return base_report

    item_index = _build_promise_item_index(spec)
    downstream_index = _build_impact_downstream_index(spec)
    direct_targets = [intent_map["target"] for intent_map in intent_promise.get("maps", [])]
    downstream_items = _collect_downstream_items(direct_targets, downstream_index, item_index)
    downstream_targets = [item["target"] for item in downstream_items]

    base_report["intentChain"] = _intent_chain_report(intent_promise, intent_promises)
    base_report["directItems"] = [
        _impact_item_report(
            intent_map["target"],
            item_index,
            relation=intent_map.get("relation"),
            note=intent_map.get("note"),
        )
        for intent_map in intent_promise.get("maps", [])
    ]
    base_report["downstreamItems"] = downstream_items
    base_report["relatedIntents"] = _related_intent_reports(
        selected_intent,
        intent_promises,
        set(direct_targets) | set(downstream_targets),
    )
    return base_report


def _build_intent_tree(intent_promises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    intent_index = {intent["name"]: intent for intent in intent_promises}
    children_by_parent: dict[str, list[str]] = {}
    child_names: set[str] = set()
    for intent in intent_promises:
        for parent in intent.get("parents", []):
            parent_name = parent["target"]
            children_by_parent.setdefault(parent_name, []).append(intent["name"])
            child_names.add(intent["name"])

    root_names = [intent["name"] for intent in intent_promises if intent.get("root")]
    if not root_names:
        root_names = [intent["name"] for intent in intent_promises if intent["name"] not in child_names]
    if not root_names:
        root_names = [intent["name"] for intent in intent_promises]

    def build_node(intent_name: str, seen: set[str]) -> dict[str, Any]:
        intent = intent_index[intent_name]
        node = _intent_summary(intent)
        if intent_name in seen:
            node["children"] = []
            return node
        next_seen = set(seen)
        next_seen.add(intent_name)
        node["children"] = [
            build_node(child_name, next_seen)
            for child_name in sorted(children_by_parent.get(intent_name, []))
            if child_name in intent_index
        ]
        return node

    return [build_node(root_name, set()) for root_name in root_names if root_name in intent_index]


def _intent_summary(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": intent["name"],
        "priority": intent.get("priority"),
        "status": intent.get("status"),
        "root": bool(intent.get("root")),
        "statement": intent.get("statement") or "",
    }


def _intent_chain_report(intent: dict[str, Any], intent_promises: list[dict[str, Any]]) -> dict[str, Any]:
    intent_index = {item["name"]: item for item in intent_promises}
    parent_by_name = {
        item["name"]: item.get("parents", [])[0]["target"]
        for item in intent_promises
        if len(item.get("parents", [])) == 1
    }
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in intent_promises:
        for parent in item.get("parents", []):
            children_by_parent.setdefault(parent["target"], []).append(item)

    ancestors: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = intent["name"]
    while current in parent_by_name:
        parent_name = parent_by_name[current]
        if parent_name in seen or parent_name not in intent_index:
            break
        seen.add(parent_name)
        ancestors.append(_intent_summary(intent_index[parent_name]))
        current = parent_name
    ancestors.reverse()

    return {
        "ancestors": ancestors,
        "self": _intent_summary(intent),
        "children": [
            _intent_summary(child)
            for child in sorted(children_by_parent.get(intent["name"], []), key=lambda item: item["name"])
        ],
        "subtree": [
            _intent_summary(descendant)
            for descendant in _intent_descendants(intent["name"], children_by_parent)
        ],
    }


def _intent_descendants(
    intent_name: str,
    children_by_parent: dict[str, list[dict[str, Any]]],
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    if seen is None:
        seen = set()
    if intent_name in seen:
        return []
    seen.add(intent_name)
    descendants: list[dict[str, Any]] = []
    for child in sorted(children_by_parent.get(intent_name, []), key=lambda item: item["name"]):
        descendants.append(child)
        descendants.extend(_intent_descendants(child["name"], children_by_parent, set(seen)))
    return descendants


def _build_promise_item_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}

    for intent in spec.get("intentPromises", []):
        _add_promise_item(items, intent["name"], "intent", intent["name"], intent.get("statement") or "")

    for type_promise in spec.get("typePromises", []):
        _add_promise_item(items, type_promise["name"], "type", type_promise["name"], type_promise.get("summary") or "")

    for field_promise in spec.get("fieldPromises", []):
        object_name = field_promise["object"]
        _add_promise_item(items, field_promise["name"], "field-promise", field_promise["name"], field_promise.get("summary") or "")
        _add_promise_item(items, object_name, "object", object_name, field_promise.get("summary") or "")
        for field in field_promise.get("fields", []):
            ref = f"{object_name}.{field['name']}"
            _add_promise_item(items, ref, "field", ref, field.get("semantic") or "", owner=field_promise["name"])
        for state in field_promise.get("states", []):
            ref = f"{object_name}.{state['value']}"
            _add_promise_item(items, ref, "state", ref, state.get("meaning") or "", owner=field_promise["name"])
        for clause in _field_clauses(field_promise):
            _add_promise_item(
                items,
                clause["id"],
                clause["kind"],
                clause["id"],
                clause.get("statement") or "",
                owner=field_promise["name"],
            )

    for function_promise in spec.get("functionPromises", []):
        _add_promise_item(
            items,
            function_promise["name"],
            "function",
            function_promise["name"],
            function_promise.get("summary") or "",
        )
        for clause in _function_clauses(function_promise):
            _add_promise_item(
                items,
                clause["id"],
                clause["kind"],
                clause["id"],
                clause.get("statement") or "",
                owner=function_promise["name"],
            )

    for verification_promise in spec.get("verificationPromises", []):
        _add_promise_item(
            items,
            verification_promise["name"],
            "verification",
            verification_promise["name"],
            verification_promise.get("claim") or "",
        )

    return items


def _add_promise_item(
    items: dict[str, dict[str, Any]],
    target: str,
    kind: str,
    label: str,
    summary: str,
    *,
    owner: str | None = None,
) -> None:
    if target in items:
        return
    item = {
        "target": target,
        "kind": kind,
        "label": label,
        "summary": summary,
    }
    if owner:
        item["owner"] = owner
    items[target] = item


def _field_clauses(field_promise: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for key, kind in (
        ("invariants", "field-invariant"),
        ("globalConstraints", "field-constraint"),
        ("forbiddenImplicitState", "field-forbid"),
    ):
        for clause in field_promise.get(key, []):
            enriched = dict(clause)
            enriched["kind"] = kind
            clauses.append(enriched)
    return clauses


def _function_clauses(function_promise: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for key, kind in (
        ("preconditions", "precondition"),
        ("successResults", "ensure"),
        ("failureConditions", "reject"),
        ("sideEffects", "sideeffect"),
        ("forbidden", "function-forbid"),
    ):
        for clause in function_promise.get(key, []):
            enriched = dict(clause)
            enriched["kind"] = kind
            clauses.append(enriched)
    return clauses


def _build_impact_downstream_index(spec: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    edges: dict[str, list[dict[str, str]]] = {}

    for field_promise in spec.get("fieldPromises", []):
        object_name = field_promise["object"]
        field_promise_name = field_promise["name"]
        _add_impact_edge(edges, field_promise_name, object_name, "defines object")

        state_field = _select_state_field(field_promise)
        for field in field_promise.get("fields", []):
            field_ref = f"{object_name}.{field['name']}"
            _add_impact_edge(edges, field_promise_name, field_ref, "defines field")
            _add_impact_edge(edges, object_name, field_ref, "has field")
            _add_impact_edge(edges, field["type"], field_ref, "types field")
        for state in field_promise.get("states", []):
            state_ref = f"{object_name}.{state['value']}"
            _add_impact_edge(edges, field_promise_name, state_ref, "defines state")
            _add_impact_edge(edges, object_name, state_ref, "has state")
            if state_field is not None:
                _add_impact_edge(edges, state_ref, f"{object_name}.{state_field['name']}", "state value of")
        for clause in _field_clauses(field_promise):
            _add_impact_edge(edges, field_promise_name, clause["id"], f"declares {clause['kind']}")
            for ref in clause.get("refs", []):
                _add_impact_edge(edges, ref, clause["id"], f"referenced by {clause['kind']}")

    for function_promise in spec.get("functionPromises", []):
        function_name = function_promise["name"]
        for dependency in function_promise.get("dependsOn", []):
            _add_impact_edge(edges, dependency, function_name, "required by function")
        for ref in function_promise.get("reads", []):
            _add_impact_edge(edges, ref, function_name, "read by function")
        for ref in function_promise.get("writes", []):
            _add_impact_edge(edges, ref, function_name, "written by function")
        for clause in _function_clauses(function_promise):
            _add_impact_edge(edges, function_name, clause["id"], f"declares {clause['kind']}")
            for ref in clause.get("refs", []):
                _add_impact_edge(edges, ref, function_name, f"referenced by {clause['kind']}")
                _add_impact_edge(edges, ref, clause["id"], f"referenced by {clause['kind']}")

    for verification_promise in spec.get("verificationPromises", []):
        verification_name = verification_promise["name"]
        for ref in verification_promise.get("verifies", []):
            _add_impact_edge(edges, ref, verification_name, "verified by")
        for scenario in verification_promise.get("scenarios", []):
            for ref in scenario.get("covers", []):
                _add_impact_edge(edges, ref, verification_name, "covered by scenario")

    return edges


def _add_impact_edge(
    edges: dict[str, list[dict[str, str]]],
    source: str,
    target: str,
    relation: str,
) -> None:
    if not source or not target or source == "-" or target == "-" or source == target:
        return
    edge = {"target": target, "relation": relation}
    bucket = edges.setdefault(source, [])
    if edge not in bucket:
        bucket.append(edge)


def _collect_downstream_items(
    direct_targets: list[str],
    downstream_index: dict[str, list[dict[str, str]]],
    item_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    visited = set(direct_targets)
    queue = list(direct_targets)
    downstream: list[dict[str, Any]] = []

    while queue:
        source = queue.pop(0)
        for edge in sorted(downstream_index.get(source, []), key=lambda item: (item["target"], item["relation"])):
            target = edge["target"]
            if target in visited:
                continue
            visited.add(target)
            downstream.append(
                _impact_item_report(
                    target,
                    item_index,
                    relation=edge["relation"],
                    source=source,
                )
            )
            queue.append(target)

    return downstream


def _impact_item_report(
    target: str,
    item_index: dict[str, dict[str, Any]],
    *,
    relation: str | None = None,
    note: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    item = item_index.get(target, {})
    report = {
        "target": target,
        "kind": item.get("kind", "unknown"),
        "label": item.get("label", target),
        "summary": item.get("summary", ""),
    }
    if item.get("owner"):
        report["owner"] = item["owner"]
    if relation:
        report["relation"] = relation
    if note:
        report["note"] = note
    if source:
        report["source"] = source
    return report


def _related_intent_reports(
    selected_intent: str,
    intent_promises: list[dict[str, Any]],
    affected_targets: set[str],
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for intent in intent_promises:
        if intent["name"] == selected_intent:
            continue
        shared_maps = [
            intent_map
            for intent_map in intent.get("maps", [])
            if intent_map.get("target") in affected_targets
        ]
        if not shared_maps:
            continue
        reports.append(
            {
                **_intent_summary(intent),
                "sharedTargets": [
                    {
                        "target": intent_map["target"],
                        "relation": intent_map.get("relation"),
                    }
                    for intent_map in shared_maps
                ],
            }
        )
    return sorted(reports, key=lambda item: item["name"])


def _print_intent_impact_report(report: dict[str, Any]) -> None:
    selected_intent = report.get("selectedIntent")
    if not selected_intent:
        print(f"Intent tree: {report['intentCount']} intent(s).")
        if not report["intentTree"]:
            print("No intents declared.")
            return
        for node in report["intentTree"]:
            _print_intent_tree_node(node, 0)
        return

    chain = report["intentChain"] or {}
    ancestors = chain.get("ancestors", [])
    chain_names = [item["name"] for item in ancestors] + [selected_intent]
    print(f"Intent: {selected_intent}")
    print(f"Root: {report.get('rootIntent') or '-'}")
    print(f"Chain: {' -> '.join(chain_names)}")

    children = chain.get("children", [])
    if children:
        print("Children:")
        for child in children:
            print(f"  - {child['name']}: {child['statement']}")

    print("Direct maps:")
    for item in report["directItems"]:
        note = f" ({item['note']})" if item.get("note") else ""
        print(f"  - {item['target']} [{item['kind']}] via {item.get('relation', '-')}{note}")

    print("Downstream impact:")
    if report["downstreamItems"]:
        for item in report["downstreamItems"]:
            print(
                f"  - {item['target']} [{item['kind']}] from {item.get('source', '-')} via {item.get('relation', '-')}"
            )
    else:
        print("  - none")

    print("Related intents:")
    if report["relatedIntents"]:
        for intent in report["relatedIntents"]:
            shared = ", ".join(item["target"] for item in intent.get("sharedTargets", []))
            print(f"  - {intent['name']} shares {shared}")
    else:
        print("  - none")


def _print_intent_tree_node(node: dict[str, Any], depth: int) -> None:
    prefix = "  " * depth
    marker = "root" if node.get("root") else node.get("priority", "intent")
    print(f"{prefix}- {node['name']} [{marker}]: {node.get('statement', '')}")
    for child in node.get("children", []):
        _print_intent_tree_node(child, depth + 1)


def _compile_go_contract_files(
    spec: dict[str, Any],
    type_mappings: dict[str, tuple[str, str | None]],
) -> dict[str, str]:
    package_name = _go_package_name(spec)
    return {
        "types.go": _render_go_types_file(spec, package_name, type_mappings),
        "constraints.go": _render_go_constraints_file(spec, package_name, type_mappings),
        "transitions.go": _render_go_transitions_file(spec, package_name),
    }


def _remove_obsolete_go_generated_files(destination: Path, compiled_files: dict[str, str]) -> None:
    for relative_path in GO_OBSOLETE_GENERATED_FILES:
        if relative_path in compiled_files:
            continue
        file_path = destination / relative_path
        if not file_path.exists() or not file_path.is_file():
            continue
        existing = file_path.read_text(encoding="utf-8")
        if existing.startswith("// Code generated by promise-go. DO NOT EDIT."):
            file_path.unlink()


def _render_go_types_file(
    spec: dict[str, Any],
    package_name: str,
    type_mappings: dict[str, tuple[str, str | None]],
) -> str:
    type_promises = _type_promises_by_name(spec)
    type_declarations: list[str] = []
    enum_declarations: list[str] = []
    struct_declarations: list[str] = []
    imports: set[str] = set()

    for type_promise in spec.get("typePromises", []):
        declaration, declaration_imports = _render_go_declared_type(type_promise, type_mappings)
        if declaration is not None:
            type_declarations.append(declaration)
        imports.update(declaration_imports)

    for field_promise in spec.get("fieldPromises", []):
        object_name = field_promise["object"]
        state_field = _select_state_field(field_promise)

        for field in field_promise.get("fields", []):
            enum_values = _go_enum_values_for_field(field_promise, field, state_field)
            if enum_values:
                enum_declarations.append(_render_go_enum_values(object_name, field, enum_values))

        struct_lines = [f"type {_go_exported_identifier(object_name)} struct {{"]
        for field in field_promise.get("fields", []):
            field_type, field_imports = _go_field_type(object_name, field, state_field, type_promises, type_mappings)
            imports.update(field_imports)
            json_tag = field["name"]
            if field.get("nullable"):
                json_tag += ",omitempty"
            struct_lines.append(f"\t{_go_exported_identifier(field['name'])} {field_type} `json:\"{json_tag}\"`")
        struct_lines.append("}")
        struct_declarations.append("\n".join(struct_lines))

    sections = [_go_generated_header(package_name)]
    if imports:
        sections.append(_render_go_imports(imports))
    sections.extend(type_declarations)
    sections.extend(enum_declarations)
    sections.extend(struct_declarations)
    return _join_go_sections(sections)


def _render_go_declared_type(
    type_promise: dict[str, Any],
    type_mappings: dict[str, tuple[str, str | None]],
) -> tuple[str | None, set[str]]:
    if type_promise["name"] in type_mappings:
        return None, set()
    base_type, imports = _go_primitive_type(type_promise["base"], type_mappings)
    declaration = f"type {_go_declared_type_name(type_promise['name'])} {base_type}"
    return declaration, imports


def _render_go_enum(object_name: str, field: dict[str, Any], states: list[dict[str, Any]]) -> str:
    values = [state["value"] for state in states] or _enum_choices(field["type"]) or []
    return _render_go_enum_values(object_name, field, values)


def _render_go_enum_values(object_name: str, field: dict[str, Any], values: list[str]) -> str:
    type_name = _go_enum_type_name(object_name, field["name"])
    lines = [f"type {type_name} string", "", "const ("]
    for value in values:
        lines.append(f"\t{_go_enum_const_name(type_name, value)} {type_name} = {_go_string(value)}")
    lines.append(")")
    return "\n".join(lines)


def _go_enum_values_for_field(
    field_promise: dict[str, Any],
    field: dict[str, Any],
    state_field: dict[str, Any] | None,
) -> list[str]:
    enum_values = _enum_choices(field["type"]) or []
    if state_field is not None and field["name"] == state_field["name"]:
        return [state["value"] for state in field_promise.get("states", [])] or enum_values
    return enum_values


def _render_go_constraints_file(
    spec: dict[str, Any],
    package_name: str,
    type_mappings: dict[str, tuple[str, str | None]],
) -> str:
    sections = [_go_generated_header(package_name), 'import "errors"']
    type_promises = _type_promises_by_name(spec)

    for field_promise in spec.get("fieldPromises", []):
        object_name = field_promise["object"]
        object_type = _go_exported_identifier(object_name)
        state_field = _select_state_field(field_promise)
        field_lookup = {field["name"]: field for field in field_promise.get("fields", [])}
        err_name = f"Err{object_type}InvariantViolation"

        lines = [
            f"var {err_name} = errors.New({_go_string(_go_error_message(object_name, 'invariant violation'))})",
            "",
            f"func Validate{object_type}Promise(value {object_type}) error {{",
        ]
        if state_field is not None:
            lines.extend(_render_go_state_value_validation(object_name, state_field, field_promise.get("states", [])))

        for clause in field_promise.get("invariants", []):
            compiled = _compile_go_invariant_clause(
                object_name,
                clause,
                field_lookup,
                state_field,
                field_promise.get("states", []),
                type_promises,
                type_mappings,
                err_name,
            )
            lines.extend(compiled)

        lines.append("\treturn nil")
        lines.append("}")
        sections.append("\n".join(lines))

    return _join_go_sections(sections)


def _render_go_state_value_validation(object_name: str, field: dict[str, Any], states: list[dict[str, Any]]) -> list[str]:
    if not states:
        return []
    field_name = _go_exported_identifier(field["name"])
    enum_type = _go_enum_type_name(object_name, field["name"])
    declared_states = ", ".join(_go_enum_const_name(enum_type, state["value"]) for state in states)
    accessor = f"value.{field_name}"
    lines: list[str] = []
    if field.get("nullable"):
        lines.append(f"\tif {accessor} == nil {{")
        lines.append("\t\treturn Err" + _go_exported_identifier(object_name) + "InvariantViolation")
        lines.append("\t}")
        accessor = f"*{accessor}"
    lines.append(f"\tswitch {accessor} {{")
    lines.append(f"\tcase {declared_states}:")
    lines.append("\t\t// declared state")
    lines.append("\tdefault:")
    lines.append("\t\treturn Err" + _go_exported_identifier(object_name) + "InvariantViolation")
    lines.append("\t}")
    return lines


def _compile_go_invariant_clause(
    object_name: str,
    clause: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
    err_name: str,
) -> list[str]:
    lines = [f"\t// {clause['id']}: {clause['statement']}"]
    when = clause.get("when")
    must = clause.get("must")
    condition = _go_condition_expression(object_name, when, field_lookup, state_field, states, type_promises, type_mappings)
    obligation = _go_obligation_violation_expression(
        object_name, must, field_lookup, state_field, states, type_promises, type_mappings
    )
    if condition is None or obligation is None:
        lines.append("\t// Not yet enforced by the Go target; keep this Promise claim covered by tests or handwritten guards.")
        return lines
    lines.append(f"\tif {condition} && {obligation} {{")
    lines.append(f"\t\treturn {err_name}")
    lines.append("\t}")
    return lines


def _render_go_transitions_file(spec: dict[str, Any], package_name: str) -> str:
    sections = [_go_generated_header(package_name)]
    transition_sections: list[str] = []

    for field_promise in spec.get("fieldPromises", []):
        state_field = _select_state_field(field_promise)
        states = field_promise.get("states", [])
        if state_field is None or not states:
            continue

        object_name = field_promise["object"]
        object_type = _go_exported_identifier(object_name)
        enum_type = _go_enum_type_name(object_name, state_field["name"])
        err_name = f"ErrInvalid{object_type}{_go_exported_identifier(state_field['name'])}Transition"
        can_name = f"CanTransition{object_type}{_go_exported_identifier(state_field['name'])}"
        validate_name = f"Validate{object_type}{_go_exported_identifier(state_field['name'])}Transition"

        lines = [
            f"var {err_name} = errors.New({_go_string(_go_error_message(object_name, state_field['name'] + ' transition'))})",
            "",
            f"func {can_name}(from {enum_type}, to {enum_type}) bool {{",
            "\tswitch from {",
        ]
        for state in states:
            lines.append(f"\tcase {_go_enum_const_name(enum_type, state['value'])}:")
            transitions = state.get("transitions", [])
            if transitions:
                allowed = ", ".join(_go_enum_const_name(enum_type, target) for target in transitions)
                lines.append(f"\t\tswitch to {{")
                lines.append(f"\t\tcase {allowed}:")
                lines.append("\t\t\treturn true")
                lines.append("\t\t}")
            lines.append("\t\treturn false")
        lines.append("\tdefault:")
        lines.append("\t\treturn false")
        lines.append("\t}")
        lines.append("}")
        lines.append("")
        lines.append(f"func {validate_name}(from {enum_type}, to {enum_type}) error {{")
        lines.append(f"\tif {can_name}(from, to) {{")
        lines.append("\t\treturn nil")
        lines.append("\t}")
        lines.append(f"\treturn {err_name}")
        lines.append("}")
        transition_sections.append("\n".join(lines))

    if transition_sections:
        sections.append('import "errors"')
        sections.extend(transition_sections)
    else:
        sections.append("// No state transitions are declared in this Promise.")

    return _join_go_sections(sections)


def _go_generated_header(package_name: str) -> str:
    return f"// Code generated by promise-go. DO NOT EDIT.\n\npackage {package_name}"


def _join_go_sections(sections: list[str]) -> str:
    return "\n\n".join(section for section in sections if section.strip()) + "\n"


def _go_package_name(spec: dict[str, Any]) -> str:
    domain = str(spec.get("meta", {}).get("domain") or "promisegen").lower()
    package_name = re.sub(r"[^a-z0-9_]", "_", domain)
    package_name = re.sub(r"_+", "_", package_name).strip("_") or "promisegen"
    if package_name[0].isdigit() or package_name in GO_KEYWORDS:
        package_name = f"promise_{package_name}"
    return package_name


def _go_field_type(
    object_name: str,
    field: dict[str, Any],
    state_field: dict[str, Any] | None,
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> tuple[str, set[str]]:
    field_type = field["type"]
    if state_field is not None and field["name"] == state_field["name"]:
        base_type = _go_enum_type_name(object_name, field["name"])
        imports: set[str] = set()
    elif _enum_choices(field_type) is not None:
        base_type = _go_enum_type_name(object_name, field["name"])
        imports = set()
    elif field_type in type_promises:
        base_type, imports = _go_declared_type(field_type, type_mappings)
    else:
        base_type, imports = _go_primitive_type(field_type, type_mappings)
    if field.get("nullable"):
        return f"*{base_type}", imports
    return base_type, imports


def _select_state_field(field_promise: dict[str, Any]) -> dict[str, Any] | None:
    states = field_promise.get("states", [])
    if not states:
        return None
    for field in field_promise.get("fields", []):
        enum_values = _enum_choices(field["type"])
        if enum_values and {state["value"] for state in states}.issubset(set(enum_values)):
            return field
    for field in field_promise.get("fields", []):
        if field["name"].lower() in {"status", "state"}:
            return field
    return None


def _go_condition_expression(
    object_name: str,
    expression: str | None,
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if not expression:
        return "true"
    parsed = _parse_simple_go_expression(object_name, expression, field_lookup, state_field)
    if parsed is None:
        return None
    field, operator, value = parsed
    accessor = f"value.{_go_exported_identifier(field['name'])}"
    rendered_value = _render_go_comparison_value(
        object_name, field, value, state_field, states, type_promises, type_mappings
    )
    if rendered_value is None:
        return None
    if value == "null":
        if operator == "=":
            return f"{accessor} == nil"
        if operator == "!=":
            return f"{accessor} != nil"
        return None
    comparable_accessor = f"*{accessor}" if field.get("nullable") else accessor
    if operator == "=":
        if field.get("nullable"):
            return f"{accessor} != nil && {comparable_accessor} == {rendered_value}"
        return f"{comparable_accessor} == {rendered_value}"
    if operator == "!=":
        if field.get("nullable"):
            return f"{accessor} == nil || {comparable_accessor} != {rendered_value}"
        return f"{comparable_accessor} != {rendered_value}"
    return None


def _go_obligation_violation_expression(
    object_name: str,
    expression: str | None,
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    parsed = _parse_simple_go_expression(object_name, expression, field_lookup, state_field)
    if parsed is None:
        return None
    field, operator, value = parsed
    accessor = f"value.{_go_exported_identifier(field['name'])}"
    rendered_value = _render_go_comparison_value(
        object_name, field, value, state_field, states, type_promises, type_mappings
    )
    if rendered_value is None:
        return None
    if value == "null":
        if operator == "=":
            return f"{accessor} != nil"
        if operator == "!=":
            return f"{accessor} == nil"
        return None
    comparable_accessor = f"*{accessor}" if field.get("nullable") else accessor
    if operator == "=":
        if field.get("nullable"):
            return f"{accessor} == nil || {comparable_accessor} != {rendered_value}"
        return f"{comparable_accessor} != {rendered_value}"
    if operator == "!=":
        if field.get("nullable"):
            return f"{accessor} != nil && {comparable_accessor} == {rendered_value}"
        return f"{comparable_accessor} == {rendered_value}"
    return None


def _parse_simple_go_expression(
    object_name: str,
    expression: str | None,
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
) -> tuple[dict[str, Any], str, str] | None:
    if expression is None:
        return None
    match = re.fullmatch(rf"{re.escape(object_name)}\.([A-Za-z0-9_]+)\s*(=|!=)\s*([A-Za-z0-9_.:/-]+)", expression.strip())
    if match is None:
        return None
    field_name, operator, value = match.groups()
    field = field_lookup.get(field_name)
    if field is None:
        return None
    if value == "null" and not field.get("nullable"):
        return None
    return field, operator, value


def _render_go_comparison_value(
    object_name: str,
    field: dict[str, Any],
    value: str,
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if value == "null":
        return "nil"
    enum_values = _enum_choices(field["type"])
    if state_field is not None and field["name"] == state_field["name"]:
        declared_values = [state["value"] for state in states] or enum_values or []
        if value not in declared_values:
            return None
        return _go_enum_const_name(_go_enum_type_name(object_name, field["name"]), value)
    if enum_values is not None:
        if value not in enum_values:
            return None
        return _go_enum_const_name(_go_enum_type_name(object_name, field["name"]), value)
    type_promise = type_promises.get(field["type"])
    if type_promise is not None:
        if field["type"] in type_mappings:
            return None
        rendered = _render_go_base_comparison_value(type_promise["base"], value)
        if rendered is None:
            return None
        return f"{_go_declared_type_name(type_promise['name'])}({rendered})"
    if field["type"] in type_mappings:
        return None
    if field["type"] in {"string", "text", "path"}:
        return _go_string(value)
    if field["type"] == "boolean" and value in {"true", "false"}:
        return value
    if field["type"] in {"integer", "number"} and re.fullmatch(r"-?\d+(\.\d+)?", value):
        return value
    return None


def _render_go_base_comparison_value(base_type: str, value: str) -> str | None:
    if base_type in {"string", "text", "path"}:
        return _go_string(value)
    if base_type == "boolean" and value in {"true", "false"}:
        return value
    if base_type in {"integer", "number"} and re.fullmatch(r"-?\d+(\.\d+)?", value):
        return value
    return None


def _type_promises_by_name(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        type_promise["name"]: type_promise
        for type_promise in spec.get("typePromises", [])
    }


def _load_type_mapping_plugin(path: str | None, target: str) -> dict[str, tuple[str, str | None]]:
    if not path:
        return {}

    plugin_path = Path(path)
    try:
        raw = json_lib.loads(plugin_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"type mapping plugin '{path}' could not be read: {exc}") from exc
    except json_lib.JSONDecodeError as exc:
        raise ValueError(f"type mapping plugin '{path}' is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"type mapping plugin '{path}' must contain a JSON object.")

    plugin = _select_type_mapping_target(raw, target)
    plugin_target = plugin.get("target")
    if plugin_target is not None and plugin_target != target:
        raise ValueError(
            f"type mapping plugin '{path}' targets '{plugin_target}', but compile target is '{target}'."
        )

    mappings: dict[str, tuple[str, str | None]] = {}
    for section_name in ("primitives", "types"):
        section = plugin.get(section_name, {})
        if not isinstance(section, dict):
            raise ValueError(f"type mapping plugin '{path}' section '{section_name}' must be an object.")
        for promise_type, mapping in section.items():
            mappings[promise_type] = _normalize_type_mapping_entry(path, promise_type, mapping)
    return mappings


def _select_type_mapping_target(raw: dict[str, Any], target: str) -> dict[str, Any]:
    targets = raw.get("targets")
    if isinstance(targets, dict) and target in targets:
        selected = targets[target]
        if isinstance(selected, dict):
            return selected
    if target in raw and isinstance(raw[target], dict):
        return raw[target]
    return raw


def _normalize_type_mapping_entry(path: str, promise_type: str, mapping: Any) -> tuple[str, str | None]:
    if isinstance(mapping, str):
        return mapping, None
    if not isinstance(mapping, dict):
        raise ValueError(f"type mapping plugin '{path}' entry '{promise_type}' must be a string or object.")
    language_type = mapping.get("type")
    if not isinstance(language_type, str) or not language_type:
        raise ValueError(f"type mapping plugin '{path}' entry '{promise_type}' is missing string key 'type'.")
    import_path = mapping.get("import")
    if import_path is not None and not isinstance(import_path, str):
        raise ValueError(f"type mapping plugin '{path}' entry '{promise_type}' key 'import' must be a string.")
    return language_type, import_path


def _go_declared_type(
    type_name: str,
    type_mappings: dict[str, tuple[str, str | None]],
) -> tuple[str, set[str]]:
    mapped = type_mappings.get(type_name)
    if mapped is None:
        return _go_declared_type_name(type_name), set()
    language_type, import_path = mapped
    return language_type, {import_path} if import_path else set()


def _go_primitive_type(
    field_type: str,
    type_mappings: dict[str, tuple[str, str | None]],
) -> tuple[str, set[str]]:
    mapped = type_mappings.get(field_type)
    if mapped is not None:
        language_type, import_path = mapped
        return language_type, {import_path} if import_path else set()
    language_type, import_path = GO_PRIMITIVE_FIELD_TYPES.get(field_type, ("string", None))
    return language_type, {import_path} if import_path else set()


def _render_go_imports(imports: set[str]) -> str:
    sorted_imports = sorted(import_path for import_path in imports if import_path)
    if len(sorted_imports) == 1:
        return f"import {_go_string(sorted_imports[0])}"
    lines = ["import ("]
    for import_path in sorted_imports:
        lines.append(f"\t{_go_string(import_path)}")
    lines.append(")")
    return "\n".join(lines)


def _go_declared_type_name(type_name: str) -> str:
    return _go_exported_identifier(type_name)


def _go_enum_type_name(object_name: str, field_name: str) -> str:
    return f"{_go_exported_identifier(object_name)}{_go_exported_identifier(field_name)}"


def _go_enum_const_name(type_name: str, value: str) -> str:
    return f"{type_name}{_go_exported_identifier(value)}"


def _go_exported_identifier(value: str) -> str:
    parts = _go_identifier_parts(value)
    if not parts:
        return "Value"
    identifier = "".join(GO_INITIALISMS.get(part.lower(), part[:1].upper() + part[1:]) for part in parts)
    if identifier in GO_KEYWORDS:
        identifier += "Value"
    if identifier[0].isdigit():
        identifier = "Value" + identifier
    return identifier


def _go_identifier_parts(value: str) -> list[str]:
    rough_parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    parts: list[str] = []
    for rough_part in rough_parts:
        parts.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|\b)|[A-Z]?[a-z]+|[0-9]+", rough_part))
    return parts


def _go_string(value: str) -> str:
    return json_lib.dumps(value, ensure_ascii=True)


def _go_error_message(object_name: str, detail: str) -> str:
    return f"{object_name}: {detail}"


def _build_graph_model(spec: dict[str, Any], source_path: str) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edge_labels: dict[tuple[str, str], set[str]] = {}
    promise_targets: dict[str, str] = {}
    object_targets: dict[str, str] = {}
    field_promise_objects: dict[str, str] = {}
    function_primary_anchors: dict[str, str] = {}

    system_id = "system::root"
    meta = spec["meta"]
    system_details = [
        f"domain {meta.get('domain', '-')}",
        f"version {meta.get('version', '-')}",
        f"status {meta.get('status', '-')}",
    ]
    if meta.get("owner"):
        system_details.append(f"owners {', '.join(meta['owner'])}")
    nodes.append(
        {
            "id": system_id,
            "lane": "system",
            "kind": "system",
            "anchor": "System",
            "label": meta.get("title") or meta.get("domain") or "System Promise",
            "summary": meta.get("summary") or "",
            "details": system_details,
        }
    )

    for intent_promise in spec.get("intentPromises", []):
        node_id = f"intent::{intent_promise['name']}"
        promise_targets[intent_promise["name"]] = node_id
        nodes.append(
            {
                "id": node_id,
                "lane": "intent",
                "kind": "intent",
                "anchor": "Intent",
                "label": intent_promise["name"],
                "summary": intent_promise.get("statement") or "",
                "details": [
                    f"priority {intent_promise.get('priority') or '-'}",
                    f"status {intent_promise.get('status') or '-'}",
                    "root true" if intent_promise.get("root") else "root false",
                    f"{len(intent_promise.get('parents', []))} parents",
                    f"{len(intent_promise.get('maps', []))} maps",
                ],
            }
        )
        if intent_promise.get("root") or not intent_promise.get("parents"):
            _add_graph_edge(edge_labels, system_id, node_id, "intent")

    type_targets: dict[str, str] = {}
    for type_promise in spec.get("typePromises", []):
        node_id = f"type::{type_promise['name']}"
        promise_targets[type_promise["name"]] = node_id
        type_targets[type_promise["name"]] = node_id
        nodes.append(
            {
                "id": node_id,
                "lane": "field",
                "kind": "type",
                "anchor": "Types",
                "label": type_promise["name"],
                "summary": type_promise.get("summary") or "",
                "details": [
                    f"kind {type_promise['kind']}",
                    f"base {type_promise['base']}",
                    f"format {type_promise.get('format') or '-'}",
                ],
            }
        )
        _add_graph_edge(edge_labels, system_id, node_id, "type")

    for field_promise in spec.get("fieldPromises", []):
        node_id = f"field::{field_promise['name']}"
        promise_targets[field_promise["name"]] = node_id
        object_targets[field_promise["object"]] = node_id
        field_promise_objects[field_promise["name"]] = field_promise["object"]
        nodes.append(
            {
                "id": node_id,
                "lane": "field",
                "kind": "field",
                "anchor": field_promise["object"],
                "label": field_promise["name"],
                "summary": field_promise.get("summary") or "",
                "details": [
                    f"object {field_promise['object']}",
                    f"{len(field_promise.get('fields', []))} fields",
                    f"{len(field_promise.get('states', []))} states",
                    f"{len(field_promise.get('invariants', []))} invariants",
                    f"{len(field_promise.get('forbiddenImplicitState', []))} forbids",
                ],
            }
        )
        _add_graph_edge(edge_labels, system_id, node_id, "field")
        for clause in _field_clauses(field_promise):
            promise_targets[clause["id"]] = node_id
        declared_types = [
            field["type"]
            for field in field_promise.get("fields", [])
            if field.get("type") in type_targets
        ]
        _add_graph_relations(node_id, declared_types, "uses type", promise_targets, object_targets, edge_labels)

    for function_promise in spec.get("functionPromises", []):
        primary_anchor = _select_primary_anchor(
            _resolve_object_anchors(
                function_promise.get("dependsOn", [])
                + function_promise.get("reads", [])
                + function_promise.get("writes", []),
                field_promise_objects,
                set(object_targets),
                function_primary_anchors,
            )
        )
        node_id = f"function::{function_promise['name']}"
        promise_targets[function_promise["name"]] = node_id
        function_primary_anchors[function_promise["name"]] = primary_anchor
        nodes.append(
            {
                "id": node_id,
                "lane": "function",
                "kind": "function",
                "anchor": primary_anchor,
                "label": function_promise["name"],
                "summary": function_promise.get("summary") or "",
                "details": [
                    f"action {function_promise['action']}",
                    f"focus {primary_anchor}",
                    f"{len(function_promise.get('dependsOn', []))} depends",
                    f"{len(function_promise.get('reads', []))} reads",
                    f"{len(function_promise.get('writes', []))} writes",
                    f"{len(function_promise.get('successResults', []))} ensures",
                    f"{len(function_promise.get('failureConditions', []))} rejects",
                ],
            }
        )
        _add_graph_edge(edge_labels, system_id, node_id, "function")
        for clause in _function_clauses(function_promise):
            promise_targets[clause["id"]] = node_id

    for verification_promise in spec.get("verificationPromises", []):
        scenario_covers: list[str] = []
        for scenario in verification_promise.get("scenarios", []):
            scenario_covers.extend(scenario.get("covers", []))
        primary_anchor = _select_primary_anchor(
            _resolve_object_anchors(
                verification_promise.get("verifies", []) + scenario_covers,
                field_promise_objects,
                set(object_targets),
                function_primary_anchors,
            )
        )
        node_id = f"verify::{verification_promise['name']}"
        promise_targets[verification_promise["name"]] = node_id
        nodes.append(
            {
                "id": node_id,
                "lane": "verify",
                "kind": "verify",
                "anchor": primary_anchor,
                "label": verification_promise["name"],
                "summary": verification_promise.get("claim") or "",
                "details": [
                    f"kind {verification_promise['kind']}",
                    f"focus {primary_anchor}",
                    f"methods {', '.join(verification_promise.get('methods', [])) or '-'}",
                    f"{len(verification_promise.get('verifies', []))} verifies",
                    f"{len(verification_promise.get('scenarios', []))} scenarios",
                    f"{len(verification_promise.get('failureCriteria', []))} fail rules",
                ],
            }
        )
        _add_graph_edge(edge_labels, system_id, node_id, "verify")

    for intent_promise in spec.get("intentPromises", []):
        source_id = promise_targets[intent_promise["name"]]
        for parent in intent_promise.get("parents", []):
            parent_id = promise_targets.get(parent["target"])
            if parent_id is not None:
                _add_graph_edge(edge_labels, parent_id, source_id, parent.get("relation") or "parent")
        for intent_map in intent_promise.get("maps", []):
            _add_graph_relations(
                source_id,
                [intent_map["target"]],
                intent_map.get("relation") or "maps",
                promise_targets,
                object_targets,
                edge_labels,
            )

    for function_promise in spec.get("functionPromises", []):
        source_id = promise_targets[function_promise["name"]]
        _add_graph_relations(source_id, function_promise.get("dependsOn", []), "depends", promise_targets, object_targets, edge_labels)
        _add_graph_relations(source_id, function_promise.get("reads", []), "reads", promise_targets, object_targets, edge_labels)
        _add_graph_relations(source_id, function_promise.get("writes", []), "writes", promise_targets, object_targets, edge_labels)

    for verification_promise in spec.get("verificationPromises", []):
        source_id = promise_targets[verification_promise["name"]]
        _add_graph_relations(source_id, verification_promise.get("verifies", []), "verifies", promise_targets, object_targets, edge_labels)
        for scenario in verification_promise.get("scenarios", []):
            _add_graph_relations(source_id, scenario.get("covers", []), "covers", promise_targets, object_targets, edge_labels)

    edges = [
        {
            "source": source,
            "target": target,
            "label": " / ".join(sorted(labels)),
        }
        for (source, target), labels in sorted(edge_labels.items())
    ]

    nodes_by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        node["relations"] = []
        node["search"] = " ".join(
            [
                node["lane"],
                node["kind"],
                node.get("anchor", ""),
                node["label"],
                node.get("summary", ""),
                *node.get("details", []),
            ]
        ).lower()

    for edge in edges:
        source_node = nodes_by_id[edge["source"]]
        target_node = nodes_by_id[edge["target"]]
        source_node["relations"].append(
            {
                "direction": "out",
                "label": edge["label"],
                "target": target_node["label"],
                "targetKind": target_node["kind"],
            }
        )
        target_node["relations"].append(
            {
                "direction": "in",
                "label": edge["label"],
                "target": source_node["label"],
                "targetKind": source_node["kind"],
            }
        )

    clusters, node_to_cluster = _build_graph_clusters(nodes)
    cluster_edges = _build_cluster_edges(edges, node_to_cluster)
    lane_counts = {
        lane: sum(1 for node in nodes if node["lane"] == lane)
        for lane in GRAPH_LANE_ORDER
    }
    view_mode = _select_graph_view_mode(len(nodes), len(edges))
    composition = "single" if view_mode == "full" else "composite"
    preview_per_lane = max(1, OVERVIEW_CLUSTER_PREVIEW_LIMIT // len(GRAPH_LANE_ORDER))
    cluster_preview = {
        lane: _sorted_clusters([cluster for cluster in clusters if cluster["lane"] == lane])[:preview_per_lane]
        for lane in GRAPH_LANE_ORDER
    }
    relation_preview = _sorted_cluster_edges(cluster_edges)[:OVERVIEW_RELATION_PREVIEW_LIMIT]
    overview_graph = _build_overview_cluster_graph(clusters, cluster_preview, cluster_edges)

    return {
        "title": meta.get("title") or meta.get("domain") or "System Promise",
        "domain": meta.get("domain") or "",
        "summary": meta.get("summary") or "",
        "sourcePath": source_path,
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "laneCounts": lane_counts,
        "viewMode": view_mode,
        "composition": composition,
        "nodes": nodes,
        "edges": edges,
        "clusters": _sorted_clusters(clusters),
        "clusterPreview": cluster_preview,
        "clusterEdges": _sorted_cluster_edges(cluster_edges),
        "relationPreview": relation_preview,
        "overviewGraph": overview_graph,
    }


def _add_graph_relations(
    source_id: str,
    refs: list[str],
    label: str,
    promise_targets: dict[str, str],
    object_targets: dict[str, str],
    edge_labels: dict[tuple[str, str], set[str]],
) -> None:
    for target_id in _resolve_graph_targets(refs, promise_targets, object_targets):
        _add_graph_edge(edge_labels, source_id, target_id, label)


def _resolve_graph_targets(
    refs: list[str],
    promise_targets: dict[str, str],
    object_targets: dict[str, str],
) -> set[str]:
    targets: set[str] = set()
    for ref in refs:
        if not ref or ref == "-":
            continue
        if ref in promise_targets:
            targets.add(promise_targets[ref])
            continue
        head = ref.split(".", 1)[0]
        if head in promise_targets:
            targets.add(promise_targets[head])
            continue
        if head in object_targets:
            targets.add(object_targets[head])
    return targets


def _add_graph_edge(
    edge_labels: dict[tuple[str, str], set[str]],
    source: str,
    target: str,
    label: str,
) -> None:
    if source == target:
        return
    edge_labels.setdefault((source, target), set()).add(label)


def _resolve_object_anchors(
    refs: list[str],
    field_promise_objects: dict[str, str],
    object_names: set[str],
    function_primary_anchors: dict[str, str],
) -> list[str]:
    anchors: list[str] = []
    for ref in refs:
        if not ref or ref == "-":
            continue
        head = ref.split(".", 1)[0]
        candidates = [
            field_promise_objects.get(ref),
            field_promise_objects.get(head),
            function_primary_anchors.get(ref),
            function_primary_anchors.get(head),
            head if head in object_names else None,
        ]
        for candidate in candidates:
            if candidate and candidate not in {"Cross-cutting", "Multi-object"} and candidate not in anchors:
                anchors.append(candidate)
    return anchors


def _select_primary_anchor(anchors: list[str]) -> str:
    if not anchors:
        return "Cross-cutting"
    if len(set(anchors)) == 1:
        return anchors[0]
    return "Multi-object"


def _build_graph_clusters(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for node in nodes:
        cluster_key = (node["lane"], node.get("anchor") or "Cross-cutting")
        grouped.setdefault(cluster_key, []).append(node)

    clusters: list[dict[str, Any]] = []
    node_to_cluster: dict[str, str] = {}
    for (lane, anchor), cluster_nodes in grouped.items():
        cluster_id = f"cluster::{lane}::{anchor.lower().replace(' ', '-')}"
        sorted_nodes = sorted(cluster_nodes, key=lambda item: item["label"].lower())
        for node in sorted_nodes:
            node_to_cluster[node["id"]] = cluster_id
        clusters.append(
            {
                "id": cluster_id,
                "lane": lane,
                "label": anchor,
                "nodeCount": len(sorted_nodes),
                "nodeIds": [node["id"] for node in sorted_nodes],
                "sampleLabels": [node["label"] for node in sorted_nodes[:3]],
            }
        )

    return clusters, node_to_cluster


def _build_cluster_edges(
    edges: list[dict[str, Any]],
    node_to_cluster: dict[str, str],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges:
        source_cluster = node_to_cluster.get(edge["source"])
        target_cluster = node_to_cluster.get(edge["target"])
        if source_cluster is None or target_cluster is None or source_cluster == target_cluster:
            continue
        bucket = grouped.setdefault(
            (source_cluster, target_cluster),
            {
                "source": source_cluster,
                "target": target_cluster,
                "count": 0,
                "labels": set(),
            },
        )
        bucket["count"] += 1
        bucket["labels"].update(part.strip() for part in edge["label"].split("/") if part.strip())

    cluster_edges: list[dict[str, Any]] = []
    for bucket in grouped.values():
        cluster_edges.append(
            {
                "source": bucket["source"],
                "target": bucket["target"],
                "count": bucket["count"],
                "label": " / ".join(sorted(bucket["labels"])),
            }
        )
    return cluster_edges


def _sorted_clusters(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        clusters,
        key=lambda item: (
            GRAPH_LANE_ORDER.index(item["lane"]),
            -item["nodeCount"],
            item["label"].lower(),
        ),
    )


def _sorted_cluster_edges(cluster_edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        cluster_edges,
        key=lambda item: (-item["count"], item["source"], item["target"]),
    )


def _build_overview_cluster_graph(
    clusters: list[dict[str, Any]],
    cluster_preview: dict[str, list[dict[str, Any]]],
    cluster_edges: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    visual_nodes: list[dict[str, Any]] = []
    cluster_to_visual: dict[str, str] = {}

    for lane in GRAPH_LANE_ORDER:
        lane_clusters = _sorted_clusters([cluster for cluster in clusters if cluster["lane"] == lane])
        visible_clusters = cluster_preview.get(lane, [])
        visible_ids = {cluster["id"] for cluster in visible_clusters}

        for cluster in visible_clusters:
            visual_nodes.append(
                {
                    "id": cluster["id"],
                    "lane": lane,
                    "kind": "cluster",
                    "label": cluster["label"],
                    "nodeCount": cluster["nodeCount"],
                    "clusterCount": 1,
                    "sampleLabels": cluster.get("sampleLabels", []),
                    "summary": f"{cluster['nodeCount']} nodes represented directly in the overview graph.",
                    "explorerCluster": cluster["id"],
                }
            )
            cluster_to_visual[cluster["id"]] = cluster["id"]

        hidden_clusters = [cluster for cluster in lane_clusters if cluster["id"] not in visible_ids]
        if hidden_clusters:
            overflow_id = f"overview::{lane}::overflow"
            for cluster in hidden_clusters:
                cluster_to_visual[cluster["id"]] = overflow_id
            visual_nodes.append(
                {
                    "id": overflow_id,
                    "lane": lane,
                    "kind": "overflow",
                    "label": f"+{len(hidden_clusters)} more",
                    "nodeCount": sum(cluster["nodeCount"] for cluster in hidden_clusters),
                    "clusterCount": len(hidden_clusters),
                    "sampleLabels": [cluster["label"] for cluster in hidden_clusters[:3]],
                    "summary": "Additional clusters grouped to keep the overview graph readable on one screen.",
                    "explorerCluster": "all",
                }
            )

    grouped_edges: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in cluster_edges:
        source = cluster_to_visual.get(edge["source"])
        target = cluster_to_visual.get(edge["target"])
        if source is None or target is None or source == target:
            continue
        bucket = grouped_edges.setdefault(
            (source, target),
            {
                "source": source,
                "target": target,
                "count": 0,
                "labels": set(),
            },
        )
        bucket["count"] += edge["count"]
        bucket["labels"].update(part.strip() for part in edge["label"].split("/") if part.strip())

    visual_edges = _sorted_cluster_edges(
        [
            {
                "source": bucket["source"],
                "target": bucket["target"],
                "count": bucket["count"],
                "label": " / ".join(sorted(bucket["labels"])),
            }
            for bucket in grouped_edges.values()
        ]
    )

    return {
        "nodes": _sorted_overview_nodes(visual_nodes),
        "edges": visual_edges,
    }


def _sorted_overview_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        nodes,
        key=lambda item: (
            GRAPH_LANE_ORDER.index(item["lane"]),
            1 if item["kind"] == "overflow" else 0,
            -item["nodeCount"],
            item["label"].lower(),
        ),
    )


def _select_graph_view_mode(node_count: int, edge_count: int) -> str:
    if node_count <= FULL_GRAPH_NODE_LIMIT and edge_count <= FULL_GRAPH_EDGE_LIMIT:
        return "full"
    return "overview"


def _render_graph_html_document(graph: dict[str, Any]) -> str:
    nodes_by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in GRAPH_LANE_ORDER}
    for node in graph["nodes"]:
        nodes_by_lane.setdefault(node["lane"], []).append(node)

    graph_markup = (
        _render_full_graph_section(nodes_by_lane)
        if graph["viewMode"] == "full"
        else _render_overview_graph_section(graph)
    )
    graph_json = json_lib.dumps(graph, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_lib.escape(graph['title'])} Graph</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f1e8;
      --panel: rgba(255, 253, 249, 0.92);
      --panel-border: rgba(34, 32, 28, 0.12);
      --text: #1f1a14;
      --muted: #6d6254;
      --system: #8c4b2f;
      --intent: #6f4ab7;
      --field: #2f6f63;
      --function: #325ca8;
      --verify: #8c6a1f;
      --edge: rgba(52, 45, 37, 0.28);
      --shadow: 0 20px 50px rgba(53, 45, 34, 0.12);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(140, 75, 47, 0.14), transparent 26%),
        radial-gradient(circle at top right, rgba(47, 111, 99, 0.15), transparent 22%),
        linear-gradient(180deg, #f8f3eb 0%, var(--bg) 100%);
    }}
    .page {{
      max-width: 1440px;
      margin: 0 auto;
      padding: 28px 20px 44px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.25fr) minmax(320px, 0.9fr);
      gap: 20px;
      align-items: end;
      margin-bottom: 20px;
    }}
    .hero-copy {{
      max-width: 760px;
    }}
    .eyebrow {{
      margin: 0 0 8px;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(2rem, 3.8vw, 3.6rem);
      line-height: 0.95;
      letter-spacing: -0.04em;
    }}
    .hero p {{
      margin: 14px 0 0;
      font-size: 1rem;
      line-height: 1.65;
      color: var(--muted);
    }}
    .hero-source {{
      margin-top: 16px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255, 252, 246, 0.86);
      border: 1px solid rgba(34, 32, 28, 0.08);
      color: var(--muted);
      font-size: 0.82rem;
      letter-spacing: 0.02em;
      box-shadow: 0 10px 24px rgba(53, 45, 34, 0.08);
    }}
    .hero-source strong {{
      color: var(--text);
      font-weight: 600;
    }}
    .hero-source code {{
      font-family: ui-monospace, "SFMono-Regular", "SF Mono", Menlo, monospace;
      font-size: 0.78rem;
      overflow-wrap: anywhere;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(136px, 1fr));
      gap: 10px;
      width: 100%;
    }}
    .meta-card {{
      padding: 13px 14px;
      border-radius: 16px;
      background: var(--panel);
      border: 1px solid var(--panel-border);
      box-shadow: var(--shadow);
    }}
    .meta-label {{
      display: block;
      margin-bottom: 6px;
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .meta-value {{
      font-size: 1.05rem;
      font-weight: 600;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .scale-banner {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(255, 251, 245, 0.9);
      border: 1px solid rgba(34, 32, 28, 0.08);
      box-shadow: 0 16px 34px rgba(53, 45, 34, 0.08);
    }}
    .scale-banner p {{
      margin: 0;
      max-width: 760px;
      color: var(--muted);
      line-height: 1.55;
    }}
    .scale-tag {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(34, 32, 28, 0.05);
      color: var(--text);
      font-size: 0.82rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .graph-shell {{
      position: relative;
      border-radius: 26px;
      padding: 18px;
      background: rgba(255, 252, 246, 0.82);
      border: 1px solid rgba(34, 32, 28, 0.08);
      box-shadow: var(--shadow);
      overflow-x: auto;
      overflow-y: visible;
    }}
    .graph-board {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(220px, 0.78fr) repeat(4, minmax(0, 1fr));
      gap: 16px;
      min-width: 0;
      padding: 4px;
    }}
    .lane {{
      position: relative;
      align-self: start;
      z-index: 1;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
      padding: 10px 10px 14px;
      border-radius: 22px;
      background: rgba(255, 251, 245, 0.78);
      border: 1px solid rgba(34, 32, 28, 0.06);
    }}
    .lane-title {{
      margin: 0;
      padding: 0 4px 10px;
      font-size: 0.88rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      border-bottom: 1px solid rgba(34, 32, 28, 0.08);
    }}
    .node {{
      position: relative;
      min-width: 0;
      padding: 14px 14px 16px;
      border-radius: 20px;
      background: var(--panel);
      border: 1px solid var(--panel-border);
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    .node::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 5px;
      border-radius: 22px 0 0 22px;
      background: var(--accent);
    }}
    .node.system {{ --accent: var(--system); }}
    .node.intent {{ --accent: var(--intent); }}
    .node.field {{ --accent: var(--field); }}
    .node.function {{ --accent: var(--function); }}
    .node.verify {{ --accent: var(--verify); }}
    .node-kind {{
      display: inline-flex;
      align-items: center;
      margin-bottom: 8px;
      padding: 4px 9px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 10%, white);
      color: var(--accent);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .node h3 {{
      margin: 0;
      font-size: 1.02rem;
      line-height: 1.12;
      overflow-wrap: anywhere;
    }}
    .node p {{
      margin: 9px 0 0;
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.42;
      overflow-wrap: anywhere;
    }}
    .node ul {{
      margin: 12px 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 0;
      border-top: 1px solid rgba(34, 32, 28, 0.08);
    }}
    .node li {{
      padding: 8px 2px;
      font-size: 0.88rem;
      color: #2d261f;
      border-bottom: 1px solid rgba(34, 32, 28, 0.06);
      overflow-wrap: anywhere;
    }}
    .node li:last-child {{
      border-bottom: none;
      padding-bottom: 0;
    }}
    svg.graph-edges {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      z-index: 0;
      overflow: visible;
    }}
    .edge-path {{
      fill: none;
      stroke: var(--edge);
      stroke-width: 2;
      stroke-linecap: round;
    }}
    .edge-label {{
      fill: #5c5144;
      font-size: 11px;
      font-family: ui-monospace, "SFMono-Regular", "SF Mono", Menlo, monospace;
      letter-spacing: 0.04em;
    }}
    svg.cluster-graph-edges .edge-path {{
      stroke: rgba(52, 45, 37, 0.42);
      stroke-width: 2.4;
    }}
    svg.cluster-graph-edges .edge-label {{
      fill: #4f4539;
      font-size: 10px;
      font-weight: 600;
    }}
    .overview-shell {{
      display: grid;
      grid-template-columns: minmax(260px, 0.78fr) minmax(0, 1.22fr);
      gap: 16px;
      margin-bottom: 18px;
    }}
    .overview-panel {{
      padding: 18px;
      border-radius: 22px;
      background: rgba(255, 252, 246, 0.86);
      border: 1px solid rgba(34, 32, 28, 0.08);
      box-shadow: var(--shadow);
    }}
    .overview-panel h2,
    .browser-shell h2 {{
      margin: 0 0 12px;
      font-size: 1.05rem;
      letter-spacing: 0.03em;
    }}
    .overview-panel p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .lane-stat-list,
    .relation-list,
    .cluster-summary-list,
    .detail-list,
    .detail-relations {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }}
    .lane-stat-list li,
    .relation-list li,
    .cluster-summary-list li,
    .detail-list li,
    .detail-relations li {{
      padding: 9px 11px;
      border-radius: 12px;
      background: rgba(34, 32, 28, 0.04);
      font-size: 0.9rem;
      color: #2d261f;
    }}
    .cluster-graph-shell {{
      margin-bottom: 18px;
      padding: 18px;
      border-radius: 24px;
      background: rgba(255, 252, 246, 0.88);
      border: 1px solid rgba(34, 32, 28, 0.08);
      box-shadow: var(--shadow);
    }}
    .cluster-graph-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }}
    .cluster-graph-header h2 {{
      margin: 0;
      font-size: 1.05rem;
      letter-spacing: 0.03em;
    }}
    .cluster-graph-header p {{
      margin: 8px 0 0;
      max-width: 760px;
      color: var(--muted);
      line-height: 1.58;
    }}
    .cluster-graph-shell-inner {{
      position: relative;
      overflow-x: auto;
      overflow-y: visible;
      border-radius: 22px;
      padding: 16px;
      background: rgba(255, 251, 245, 0.78);
      border: 1px solid rgba(34, 32, 28, 0.06);
    }}
    .cluster-graph-board {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(220px, 0.78fr) repeat(4, minmax(0, 1fr));
      gap: 16px;
      min-width: 0;
      padding: 4px;
    }}
    .cluster-lane {{
      min-height: 160px;
    }}
    svg.cluster-graph-edges {{
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      z-index: 0;
      overflow: visible;
    }}
    .overview-node {{
      position: relative;
      z-index: 1;
      display: block;
      width: 100%;
      font: inherit;
      appearance: none;
      min-width: 0;
      padding: 14px 14px 16px;
      border-radius: 18px;
      background: var(--panel);
      border: 1px solid var(--panel-border);
      box-shadow: 0 12px 28px rgba(53, 45, 34, 0.08);
      text-align: left;
      color: var(--text);
      cursor: pointer;
    }}
    .overview-node::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 5px;
      border-radius: 18px 0 0 18px;
      background: var(--accent);
    }}
    .overview-node:hover {{
      transform: translateY(-1px);
      box-shadow: 0 16px 30px rgba(53, 45, 34, 0.1);
    }}
    .overview-node.active {{
      border-color: rgba(34, 32, 28, 0.18);
      background: rgba(255, 255, 255, 0.97);
    }}
    .overview-node.cluster {{ --accent: color-mix(in srgb, var(--field) 50%, var(--function) 50%); }}
    .overview-node.overflow {{ --accent: #7b6a53; }}
    .overview-node-meta {{
      display: inline-flex;
      margin-bottom: 8px;
      padding: 4px 9px;
      border-radius: 999px;
      background: color-mix(in srgb, var(--accent) 10%, white);
      color: var(--accent);
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .overview-node strong {{
      display: block;
      font-size: 1rem;
      line-height: 1.12;
      overflow-wrap: anywhere;
    }}
    .overview-node-badge {{
      display: inline-flex;
      margin-top: 9px;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(34, 32, 28, 0.05);
      font-size: 0.78rem;
      color: var(--muted);
    }}
    .overview-node p {{
      margin: 10px 0 0;
      font-size: 0.84rem;
      line-height: 1.5;
      color: var(--muted);
    }}
    .overview-node ul {{
      margin: 10px 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 6px;
    }}
    .overview-node li {{
      font-size: 0.82rem;
      color: var(--muted);
      overflow-wrap: anywhere;
    }}
    .browser-shell {{
      padding: 18px;
      border-radius: 24px;
      background: rgba(255, 252, 246, 0.88);
      border: 1px solid rgba(34, 32, 28, 0.08);
      box-shadow: var(--shadow);
    }}
    .browser-toolbar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto auto auto;
      gap: 10px;
      align-items: center;
      margin-bottom: 14px;
    }}
    .browser-toolbar input,
    .browser-toolbar select,
    .browser-toolbar button {{
      font: inherit;
    }}
    .browser-toolbar input,
    .browser-toolbar select {{
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(34, 32, 28, 0.12);
      background: rgba(255, 255, 255, 0.84);
      color: var(--text);
    }}
    .browser-toolbar button {{
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid rgba(34, 32, 28, 0.1);
      background: rgba(255, 255, 255, 0.82);
      cursor: pointer;
      color: var(--text);
    }}
    .browser-toolbar button:disabled {{
      opacity: 0.45;
      cursor: not-allowed;
    }}
    .lane-filters {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .lane-filter.active {{
      background: rgba(34, 32, 28, 0.12);
      border-color: rgba(34, 32, 28, 0.18);
    }}
    .browser-layout {{
      display: grid;
      grid-template-columns: minmax(280px, 0.86fr) minmax(320px, 1.14fr);
      gap: 14px;
      align-items: start;
    }}
    .browser-list,
    .browser-detail {{
      min-height: 420px;
      padding: 14px;
      border-radius: 18px;
      background: rgba(255, 251, 245, 0.72);
      border: 1px solid rgba(34, 32, 28, 0.06);
    }}
    .browser-list-items {{
      display: grid;
      gap: 8px;
    }}
    .browser-item {{
      padding: 12px;
      border-radius: 14px;
      border: 1px solid rgba(34, 32, 28, 0.08);
      background: rgba(255, 255, 255, 0.82);
      cursor: pointer;
      text-align: left;
    }}
    .browser-item.active {{
      border-color: rgba(34, 32, 28, 0.18);
      background: rgba(255, 255, 255, 0.96);
      box-shadow: 0 8px 24px rgba(53, 45, 34, 0.08);
    }}
    .browser-item strong {{
      display: block;
      font-size: 0.98rem;
      line-height: 1.15;
      overflow-wrap: anywhere;
    }}
    .browser-item span {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 0.84rem;
      line-height: 1.45;
    }}
    .detail-kicker {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(34, 32, 28, 0.05);
      font-size: 0.78rem;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .detail-empty {{
      color: var(--muted);
      line-height: 1.6;
    }}
    @media (max-width: 1180px) {{
      .page {{
        padding: 22px 14px 30px;
      }}
      .hero {{
        grid-template-columns: 1fr;
      }}
      .overview-shell,
      .browser-layout {{
        grid-template-columns: 1fr;
      }}
      .browser-toolbar {{
        grid-template-columns: 1fr;
      }}
      .graph-shell,
      .cluster-graph-shell-inner {{
        padding: 16px;
      }}
      .graph-board,
      .cluster-graph-board {{
        min-width: 980px;
      }}
    }}
    @media (max-width: 720px) {{
      .meta-grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Promise Graph</p>
        <h1>{html_lib.escape(graph['title'])}</h1>
        <p>{html_lib.escape(graph['summary'] or 'Self-contained visualization of the current System Promise graph.')}</p>
        <div class="hero-source">
          <strong>Source</strong>
          <code>{html_lib.escape(graph['sourcePath'])}</code>
        </div>
      </div>
      <div class="meta-grid">
        <article class="meta-card">
          <span class="meta-label">Domain</span>
          <span class="meta-value">{html_lib.escape(graph['domain'] or '-')}</span>
        </article>
        <article class="meta-card">
          <span class="meta-label">Nodes</span>
          <span class="meta-value">{graph['nodeCount']}</span>
        </article>
        <article class="meta-card">
          <span class="meta-label">Edges</span>
          <span class="meta-value">{graph['edgeCount']}</span>
        </article>
        <article class="meta-card">
          <span class="meta-label">View</span>
          <span class="meta-value">{html_lib.escape(f"{graph['viewMode']} · {graph['composition']}")}</span>
        </article>
      </div>
    </section>
    {graph_markup}
  </main>
  <script id="promise-graph-data" type="application/json">{graph_json}</script>
  <script>
    const graph = JSON.parse(document.getElementById("promise-graph-data").textContent);
    const laneOrder = {json_lib.dumps(list(GRAPH_LANE_ORDER))};

    function attachEdgeRenderer(boardSelector, svgSelector, edges, sourceAttribute, targetAttribute, labelFormatter = (edge) => edge.label) {{
      const board = document.querySelector(boardSelector);
      const svg = document.querySelector(svgSelector);
      if (!board || !svg) {{
        return;
      }}

      function drawEdges() {{
        const boardRect = board.getBoundingClientRect();
        const width = Math.ceil(board.scrollWidth);
        const height = Math.ceil(board.scrollHeight);
        svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
        svg.innerHTML = "";

        for (const edge of edges) {{
          const source = board.querySelector(`[${{sourceAttribute}}="${{edge.source}}"]`);
          const target = board.querySelector(`[${{targetAttribute}}="${{edge.target}}"]`);
          if (!source || !target) {{
            continue;
          }}

          const sourceRect = source.getBoundingClientRect();
          const targetRect = target.getBoundingClientRect();
          const sourceCenterX = sourceRect.left - boardRect.left + sourceRect.width / 2;
          const targetCenterX = targetRect.left - boardRect.left + targetRect.width / 2;
          const sourceCenterY = sourceRect.top - boardRect.top + sourceRect.height / 2;
          const targetCenterY = targetRect.top - boardRect.top + targetRect.height / 2;
          const direction = targetCenterX >= sourceCenterX ? 1 : -1;
          const sourceX = direction > 0
            ? sourceRect.right - boardRect.left
            : sourceRect.left - boardRect.left;
          const targetX = direction > 0
            ? targetRect.left - boardRect.left
            : targetRect.right - boardRect.left;
          const curvature = Math.max(48, Math.abs(targetX - sourceX) * 0.32);
          const controlX1 = sourceX + curvature * direction;
          const controlX2 = targetX - curvature * direction;
          const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
          path.setAttribute("class", "edge-path");
          path.setAttribute("d", `M ${{sourceX}} ${{sourceCenterY}} C ${{controlX1}} ${{sourceCenterY}}, ${{controlX2}} ${{targetCenterY}}, ${{targetX}} ${{targetCenterY}}`);
          svg.appendChild(path);

          const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
          label.setAttribute("class", "edge-label");
          label.setAttribute("x", String((sourceX + targetX) / 2));
          label.setAttribute("y", String((sourceCenterY + targetCenterY) / 2 - 6));
          label.setAttribute("text-anchor", "middle");
          label.textContent = labelFormatter(edge);
          svg.appendChild(label);
        }}
      }}

      window.addEventListener("load", drawEdges);
      window.addEventListener("resize", drawEdges);
      if (document.fonts?.ready) {{
        document.fonts.ready.then(drawEdges);
      }}
      drawEdges();
    }}

    function initFullGraph() {{
      attachEdgeRenderer(".graph-board", ".graph-edges", graph.edges, "data-node-id", "data-node-id");
    }}

    function initCompositeGraph() {{
      attachEdgeRenderer(
        ".cluster-graph-board",
        ".cluster-graph-edges",
        graph.overviewGraph.edges,
        "data-overview-node-id",
        "data-overview-node-id",
        (edge) => `${{edge.count}} · ${{edge.label}}`
      );
    }}

    function initCompositeExplorer() {{
      const searchInput = document.getElementById("graph-search");
      const clusterSelect = document.getElementById("graph-cluster");
      const listContainer = document.getElementById("graph-node-list");
      const countLabel = document.getElementById("graph-count");
      const pageLabel = document.getElementById("graph-page");
      const detailContainer = document.getElementById("graph-detail");
      const prevButton = document.getElementById("graph-prev");
      const nextButton = document.getElementById("graph-next");
      const laneButtons = Array.from(document.querySelectorAll(".lane-filter"));
      const overviewButtons = Array.from(document.querySelectorAll(".overview-node"));
      if (!searchInput || !clusterSelect || !listContainer || !countLabel || !pageLabel || !detailContainer || !prevButton || !nextButton) {{
        return;
      }}

      const state = {{
        query: "",
        lane: "all",
        cluster: "all",
        page: 0,
        selectedNodeId: graph.nodes[0]?.id ?? null,
      }};

      const pageSize = {EXPLORER_PAGE_SIZE};
      const clusterMap = new Map(graph.clusters.map((cluster) => [cluster.id, cluster]));
      const clusterNodeIds = new Map(graph.clusters.map((cluster) => [cluster.id, new Set(cluster.nodeIds)]));
      const nodeMap = new Map(graph.nodes.map((node) => [node.id, node]));

      function compareNodes(left, right) {{
        const laneDelta = laneOrder.indexOf(left.lane) - laneOrder.indexOf(right.lane);
        if (laneDelta !== 0) {{
          return laneDelta;
        }}
        return left.label.localeCompare(right.label);
      }}

      function filteredNodes() {{
        const selectedClusterNodes = state.cluster === "all" ? null : clusterNodeIds.get(state.cluster);
        return [...graph.nodes]
          .filter((node) => state.lane === "all" || node.lane === state.lane)
          .filter((node) => selectedClusterNodes === null || selectedClusterNodes.has(node.id))
          .filter((node) => state.query === "" || node.search.includes(state.query))
          .sort(compareNodes);
      }}

      function syncLaneButtons() {{
        laneButtons.forEach((button) => {{
          button.classList.toggle("active", (button.dataset.lane || "all") === state.lane);
        }});
      }}

      function syncOverviewButtons() {{
        overviewButtons.forEach((button) => {{
          const buttonLane = button.dataset.overviewLane || "all";
          const buttonCluster = button.dataset.overviewCluster || "all";
          const isActive = buttonLane === state.lane && buttonCluster === state.cluster;
          button.classList.toggle("active", isActive);
        }});
      }}

      function syncClusterOptions() {{
        const eligibleClusters = graph.clusters.filter((cluster) => state.lane === "all" || cluster.lane === state.lane);
        clusterSelect.innerHTML = "";
        const allOption = document.createElement("option");
        allOption.value = "all";
        allOption.textContent = "All clusters";
        clusterSelect.appendChild(allOption);
        for (const cluster of eligibleClusters) {{
          const option = document.createElement("option");
          option.value = cluster.id;
          option.textContent = `${{cluster.label}} (${{
            cluster.nodeCount
          }})`;
          clusterSelect.appendChild(option);
        }}
        if (state.cluster !== "all" && !eligibleClusters.some((cluster) => cluster.id === state.cluster)) {{
          state.cluster = "all";
        }}
        clusterSelect.value = state.cluster;
      }}

      function applyOverviewSelection(button) {{
        state.lane = button.dataset.overviewLane || "all";
        state.cluster = button.dataset.overviewCluster || "all";
        state.page = 0;
        syncLaneButtons();
        syncClusterOptions();
        syncOverviewButtons();
        renderList();
      }}

      function renderDetail(node) {{
        if (!node) {{
          detailContainer.innerHTML = '<p class="detail-empty">No Promise node matches the current filters.</p>';
          return;
        }}
        const relationItems = node.relations.slice(0, 12).map((relation) => {{
          const prefix = relation.direction === "out"
            ? `${{relation.label}} → ${{relation.target}}`
            : `${{relation.target}} → ${{relation.label}}`;
          return `<li>${{prefix}}</li>`;
        }}).join("");
        const detailItems = node.details.map((detail) => `<li>${{detail}}</li>`).join("");
        detailContainer.innerHTML = `
          <div class="detail-kicker">${{node.lane}} · ${{node.anchor}}</div>
          <h2>${{node.label}}</h2>
          <p>${{node.summary || "No summary provided."}}</p>
          <ul class="detail-list">${{detailItems}}</ul>
          <h2>Relations</h2>
          <ul class="detail-relations">${{relationItems || "<li>No aggregate relations in current graph.</li>"}}</ul>
        `;
      }}

      function renderList() {{
        const results = filteredNodes();
        const totalPages = Math.max(1, Math.ceil(results.length / pageSize));
        state.page = Math.min(state.page, totalPages - 1);
        const start = state.page * pageSize;
        const visible = results.slice(start, start + pageSize);

        if (!visible.some((node) => node.id === state.selectedNodeId)) {{
          state.selectedNodeId = visible[0]?.id ?? null;
        }}

        countLabel.textContent = `${{results.length}} visible of ${{graph.nodeCount}} nodes`;
        pageLabel.textContent = `Page ${{results.length === 0 ? 0 : state.page + 1}} / ${{totalPages}}`;
        prevButton.disabled = state.page === 0;
        nextButton.disabled = state.page >= totalPages - 1 || results.length === 0;

        listContainer.innerHTML = "";
        if (visible.length === 0) {{
          listContainer.innerHTML = '<p class="detail-empty">No nodes match the current search and lane filters.</p>';
          renderDetail(null);
          return;
        }}

        for (const node of visible) {{
          const button = document.createElement("button");
          button.type = "button";
          button.className = `browser-item${{node.id === state.selectedNodeId ? " active" : ""}}`;
          button.innerHTML = `<strong>${{node.label}}</strong><span>${{node.lane}} · ${{node.anchor}}</span><span>${{node.summary || "No summary provided."}}</span>`;
          button.addEventListener("click", () => {{
            state.selectedNodeId = node.id;
            renderList();
          }});
          listContainer.appendChild(button);
        }}

        syncOverviewButtons();
        renderDetail(nodeMap.get(state.selectedNodeId) ?? null);
      }}

      laneButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          state.lane = button.dataset.lane || "all";
          state.cluster = "all";
          state.page = 0;
          syncLaneButtons();
          syncClusterOptions();
          renderList();
        }});
      }});

      overviewButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          applyOverviewSelection(button);
        }});
      }});

      searchInput.addEventListener("input", () => {{
        state.query = searchInput.value.trim().toLowerCase();
        state.page = 0;
        renderList();
      }});

      clusterSelect.addEventListener("change", () => {{
        state.cluster = clusterSelect.value;
        state.page = 0;
        syncOverviewButtons();
        renderList();
      }});

      prevButton.addEventListener("click", () => {{
        if (state.page > 0) {{
          state.page -= 1;
          renderList();
        }}
      }});

      nextButton.addEventListener("click", () => {{
        state.page += 1;
        renderList();
      }});

      syncLaneButtons();
      syncClusterOptions();
      syncOverviewButtons();
      renderList();
    }}

    if (graph.viewMode === "full") {{
      initFullGraph();
    }} else {{
      initCompositeGraph();
      initCompositeExplorer();
    }}
  </script>
</body>
</html>
"""


def _render_full_graph_section(nodes_by_lane: dict[str, list[dict[str, Any]]]) -> str:
    lane_markup = "\n".join(
        _render_graph_lane(lane, GRAPH_LANE_TITLES[lane], nodes_by_lane.get(lane, []))
        for lane in GRAPH_LANE_ORDER
    )
    return f"""<section class="graph-shell">
  <svg class="graph-edges" aria-hidden="true"></svg>
  <div class="graph-board">
    {lane_markup}
  </div>
</section>"""


def _render_overview_graph_section(graph: dict[str, Any]) -> str:
    lane_stats = "\n".join(
        f"<li>{html_lib.escape(GRAPH_LANE_TITLES[lane])}: {graph['laneCounts'].get(lane, 0)} nodes</li>"
        for lane in GRAPH_LANE_ORDER
    )
    cluster_lookup = {cluster["id"]: cluster for cluster in graph["clusters"]}
    overview_graph = graph["overviewGraph"]
    relation_items = "\n".join(
        _render_relation_preview_item(edge, cluster_lookup)
        for edge in graph["relationPreview"]
    ) or "<li>No aggregate cross-cluster relations.</li>"
    lane_filter_buttons = "\n".join(
        f'<button type="button" class="lane-filter{" active" if lane == "all" else ""}" data-lane="{lane}">{html_lib.escape("All lanes" if lane == "all" else GRAPH_LANE_TITLES[lane])}</button>'
        for lane in ("all", *GRAPH_LANE_ORDER)
    )
    return f"""<section class="scale-banner">
  <p>Large Promise graphs switch into a composite viewer. The page keeps an aggregate graph on screen first, then opens node-level filtering and detail inspection below.</p>
  <span class="scale-tag">{graph['nodeCount']} nodes · {graph['edgeCount']} edges · {html_lib.escape(graph['composition'])}</span>
</section>
<section class="cluster-graph-shell">
  <div class="cluster-graph-header">
    <div>
      <h2>Composite Graph</h2>
      <p>The overview still keeps a real graph surface. Each visible card is a cluster or overflow bucket, and the edge layer shows aggregate dependency flow across lanes.</p>
    </div>
    <span class="scale-tag">{len(overview_graph['nodes'])} overview nodes · {len(overview_graph['edges'])} aggregate links</span>
  </div>
  {_render_composite_graph(overview_graph)}
</section>
<section class="overview-shell">
  <article class="overview-panel">
    <h2>Overview</h2>
    <p>The graph viewer now prioritizes structural comprehension over full-card expansion. Lane totals, an aggregate visual graph, and cross-cluster relations stay visible at a glance, while the explorer below handles node-level browsing.</p>
    <ul class="lane-stat-list">
      {lane_stats}
    </ul>
  </article>
  <article class="overview-panel">
    <h2>Aggregate Relations</h2>
    <ul class="relation-list">
      {relation_items}
    </ul>
  </article>
</section>
<section class="browser-shell">
  <h2>Node Explorer</h2>
  <div class="browser-toolbar">
    <input id="graph-search" type="search" placeholder="Search node name, focus, or detail">
    <select id="graph-cluster" aria-label="Cluster filter"></select>
    <button id="graph-prev" type="button">Previous</button>
    <button id="graph-next" type="button">Next</button>
  </div>
  <div class="lane-filters">
    {lane_filter_buttons}
  </div>
  <div class="browser-toolbar" style="grid-template-columns: 1fr auto;">
    <div id="graph-count" class="scale-tag">0 visible</div>
    <div id="graph-page" class="scale-tag">Page 0 / 0</div>
  </div>
  <div class="browser-layout">
    <section class="browser-list">
      <div id="graph-node-list" class="browser-list-items"></div>
    </section>
    <aside id="graph-detail" class="browser-detail"></aside>
  </div>
</section>"""


def _render_graph_lane(lane: str, title: str, nodes: list[dict[str, Any]]) -> str:
    cards = "\n".join(_render_graph_card(node) for node in nodes)
    return f"""<section class="lane lane-{lane}">
  <h2 class="lane-title">{html_lib.escape(title)}</h2>
  {cards}
</section>"""


def _render_composite_graph(overview_graph: dict[str, Any]) -> str:
    nodes_by_lane: dict[str, list[dict[str, Any]]] = {lane: [] for lane in GRAPH_LANE_ORDER}
    for node in overview_graph["nodes"]:
        nodes_by_lane.setdefault(node["lane"], []).append(node)

    lane_markup = "\n".join(
        _render_overview_lane(lane, GRAPH_LANE_TITLES[lane], nodes_by_lane.get(lane, []))
        for lane in GRAPH_LANE_ORDER
    )
    return f"""<div class="cluster-graph-shell-inner">
  <svg class="cluster-graph-edges" aria-hidden="true"></svg>
  <div class="cluster-graph-board">
    {lane_markup}
  </div>
</div>"""


def _render_overview_lane(lane: str, title: str, nodes: list[dict[str, Any]]) -> str:
    cards = "\n".join(_render_overview_node(node) for node in nodes) or '<p class="detail-empty">No clusters in this lane.</p>'
    return f"""<section class="lane lane-{lane} cluster-lane">
  <h2 class="lane-title">{html_lib.escape(title)}</h2>
  {cards}
</section>"""


def _render_overview_node(node: dict[str, Any]) -> str:
    sample_items = "\n".join(
        f"<li>{html_lib.escape(label)}</li>"
        for label in node.get("sampleLabels", [])
    ) or "<li>Use the explorer below for exact nodes.</li>"
    if node["kind"] == "overflow":
        badge = f"{node['clusterCount']} clusters · {node['nodeCount']} nodes"
        summary = node.get("summary") or "Additional clusters grouped for one-screen readability."
    else:
        badge = f"{node['nodeCount']} nodes"
        summary = node.get("summary") or "Visible cluster in the overview graph."
    return f"""<button type="button" class="overview-node {html_lib.escape(node['kind'])}" data-overview-node-id="{html_lib.escape(node['id'])}" data-overview-lane="{html_lib.escape(node['lane'])}" data-overview-cluster="{html_lib.escape(node['explorerCluster'])}">
  <span class="overview-node-meta">{html_lib.escape(node['kind'])}</span>
  <strong>{html_lib.escape(node['label'])}</strong>
  <span class="overview-node-badge">{html_lib.escape(badge)}</span>
  <p>{html_lib.escape(summary)}</p>
  <ul>
    {sample_items}
  </ul>
</button>"""


def _render_cluster_lane_section(
    lane: str,
    title: str,
    clusters: list[dict[str, Any]],
    total_clusters: int,
) -> str:
    cards = "\n".join(_render_cluster_card(cluster) for cluster in clusters) or "<p class=\"detail-empty\">No clusters.</p>"
    hidden_count = max(total_clusters - len(clusters), 0)
    meta = f"{total_clusters} clusters"
    if hidden_count:
        meta += f" · {hidden_count} more in explorer"
    return f"""<section class="cluster-section">
  <div class="cluster-header">
    <h3>{html_lib.escape(title)}</h3>
    <span class="cluster-meta">{html_lib.escape(meta)}</span>
  </div>
  <div class="cluster-grid">
    {cards}
  </div>
</section>"""


def _render_cluster_card(cluster: dict[str, Any]) -> str:
    sample_items = "\n".join(
        f"<li>{html_lib.escape(label)}</li>"
        for label in cluster.get("sampleLabels", [])
    )
    return f"""<article class="cluster-card">
  <strong>{html_lib.escape(cluster['label'])}</strong>
  <p>{cluster['nodeCount']} nodes in this cluster.</p>
  <ul>
    {sample_items}
  </ul>
</article>"""


def _render_relation_preview_item(edge: dict[str, Any], cluster_lookup: dict[str, dict[str, Any]]) -> str:
    source = cluster_lookup.get(edge["source"], {"label": edge["source"]})
    target = cluster_lookup.get(edge["target"], {"label": edge["target"]})
    source_label = f"{source['label']} ({source.get('lane', '-')})"
    target_label = f"{target['label']} ({target.get('lane', '-')})"
    return f"<li>{html_lib.escape(source_label)} → {html_lib.escape(target_label)} · {edge['count']} links · {html_lib.escape(edge['label'])}</li>"


def _render_graph_card(node: dict[str, Any]) -> str:
    details = "\n".join(
        f"<li>{html_lib.escape(detail)}</li>"
        for detail in node.get("details", [])
    )
    return f"""<article class="node {html_lib.escape(node['kind'])}" data-node-id="{html_lib.escape(node['id'])}">
  <span class="node-kind">{html_lib.escape(node['kind'])}</span>
  <h3>{html_lib.escape(node['label'])}</h3>
  <p>{html_lib.escape(node.get('summary') or 'No summary provided.')}</p>
  <ul>
    {details}
  </ul>
</article>"""


def _collect_invocation_fields(contract: dict) -> dict[str, dict]:
    field_promises = contract.get("fieldPromises", [])
    for field_promise in field_promises:
        if field_promise["object"] == CLI_INVOCATION_OBJECT:
            return {field["name"]: field for field in field_promise["fields"]}
    raise RuntimeError(f"CLI contract is missing field object '{CLI_INVOCATION_OBJECT}'.")


def _collect_invocation_field_names(function_promise: dict) -> list[str]:
    prefix = f"{CLI_INVOCATION_OBJECT}."
    field_names: list[str] = []
    for ref in function_promise.get("reads", []):
        if not ref.startswith(prefix):
            continue
        field_name = ref.split(".", 1)[1]
        if field_name not in field_names:
            field_names.append(field_name)
    return field_names


def _collect_exclusive_groups(function_promise: dict, invocation_fields: dict[str, dict]) -> list[set[str]]:
    prefix = f"{CLI_INVOCATION_OBJECT}."
    exclusive_groups: list[set[str]] = []

    for clause in function_promise.get("forbidden", []):
        field_names: list[str] = []
        for ref in clause.get("refs", []):
            if not ref.startswith(prefix):
                continue
            field_name = ref.split(".", 1)[1]
            field = invocation_fields.get(field_name)
            if field is None or field["type"] != "boolean":
                continue
            if field_name not in field_names:
                field_names.append(field_name)
        if len(field_names) > 1:
            exclusive_groups.append(set(field_names))

    return exclusive_groups


def _extract_steps(function_promise: dict) -> list[str]:
    steps: list[str] = []
    for clause in function_promise.get("successResults", []):
        must = clause.get("must")
        if not must:
            continue
        match = STEP_RE.match(must)
        if not match:
            continue
        steps.append(match.group(1))
    if not steps:
        raise RuntimeError(
            f"Function promise '{function_promise['name']}' is missing a step declaration."
        )
    return steps


def _add_cli_argument(target, field_name: str, field: dict) -> None:
    help_text = field["semantic"]
    field_type = field["type"]
    default = field.get("default")
    enum_choices = _enum_choices(field_type)

    if field.get("required") and default is None:
        kwargs: dict[str, Any] = {"help": help_text}
        if enum_choices is not None:
            kwargs["choices"] = enum_choices
        target.add_argument(field_name, **kwargs)
        return

    option_name = f"--{_to_kebab_case(field_name)}"

    if field_type == "boolean":
        if default is True:
            target.add_argument(option_name, action="store_false", dest=field_name, help=help_text)
            return
        target.add_argument(option_name, action="store_true", dest=field_name, help=help_text)
        return

    if enum_choices is not None:
        target.add_argument(
            option_name,
            choices=enum_choices,
            default=default,
            dest=field_name,
            help=help_text,
        )
        return

    target.add_argument(option_name, default=default, dest=field_name, help=help_text)


def _to_kebab_case(value: str) -> str:
    with_dashes = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", value)
    return with_dashes.replace("_", "-").lower()


def _enum_choices(field_type: str) -> list[str] | None:
    enum_match = ENUM_TYPE_RE.match(field_type)
    if enum_match is None:
        return None
    return [item.strip() for item in enum_match.group(1).split("|") if item.strip()]


def _repo_bundle_file_pairs(root: Path) -> list[tuple[str, Path, Path]]:
    repo_skill_dir = _repo_skill_dir(root)
    repo_skill_scripts = repo_skill_dir / "scripts" / "promise_cli"
    repo_skill_refs = repo_skill_dir / "references"
    return [
        (
            "repo skill mirrors src/promise_cli/__init__.py",
            root / "src" / "promise_cli" / "__init__.py",
            repo_skill_scripts / "__init__.py",
        ),
        (
            "repo skill mirrors src/promise_cli/__main__.py",
            root / "src" / "promise_cli" / "__main__.py",
            repo_skill_scripts / "__main__.py",
        ),
        (
            "repo skill mirrors src/promise_cli/cli.py",
            root / "src" / "promise_cli" / "cli.py",
            repo_skill_scripts / "cli.py",
        ),
        (
            "repo skill mirrors src/promise_cli/dsl.py",
            root / "src" / "promise_cli" / "dsl.py",
            repo_skill_scripts / "dsl.py",
        ),
        (
            "repo skill mirrors docs/promise-standard.md",
            root / "docs" / "promise-standard.md",
            repo_skill_refs / "promise-standard.md",
        ),
        (
            "repo skill mirrors docs/architecture.md",
            root / "docs" / "architecture.md",
            repo_skill_refs / "promise-architecture.md",
        ),
        (
            "repo skill mirrors docs/promise-core.md",
            root / "docs" / "promise-core.md",
            repo_skill_refs / "promise-core.md",
        ),
        (
            "repo skill mirrors docs/promise-language.md",
            root / "docs" / "promise-language.md",
            repo_skill_refs / "promise-language.md",
        ),
        (
            "repo skill mirrors tooling/promise-cli.promise",
            root / "tooling" / "promise-cli.promise",
            repo_skill_refs / "promise-cli.promise",
        ),
        (
            "repo skill mirrors tooling/README.md",
            root / "tooling" / "README.md",
            repo_skill_refs / "promise-tooling-readme.md",
        ),
    ]


def _repo_skill_dir(root: Path) -> Path:
    return root / "skills" / SKILL_NAME


def _is_repo_root(root: Path) -> bool:
    return (
        (root / "src" / "promise_cli" / "cli.py").exists()
        and (root / "tooling" / "promise-cli.promise").exists()
        and _repo_skill_dir(root).exists()
    )


def _is_skill_root(root: Path) -> bool:
    return (root / "scripts" / "promise_cli" / "cli.py").exists() and (root / "references").exists()


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _installed_skill_dir() -> Path:
    return _codex_home() / "skills" / SKILL_NAME


def _quick_validate_path() -> Path:
    return _codex_home() / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py"


def _check_file_mirror(
    name: str,
    source_path: Path,
    mirror_path: Path,
    issues: list[LintIssue],
    checks: list[dict[str, Any]],
) -> None:
    if not source_path.exists():
        issues.append(LintIssue("tooling-missing-source", f"{name}: missing source file {source_path}."))
        checks.append(
            {
                "name": name,
                "ok": False,
                "details": f"Missing source file {source_path}.",
            }
        )
        return

    if not mirror_path.exists():
        issues.append(LintIssue("tooling-missing-mirror", f"{name}: missing mirrored file {mirror_path}."))
        checks.append(
            {
                "name": name,
                "ok": False,
                "details": f"Missing mirrored file {mirror_path}.",
            }
        )
        return

    if source_path.read_bytes() != mirror_path.read_bytes():
        issues.append(
            LintIssue(
                "tooling-file-drift",
                f"{name}: {mirror_path} is out of sync with {source_path}.",
            )
        )
        checks.append(
            {
                "name": name,
                "ok": False,
                "details": f"{mirror_path} is out of sync with {source_path}.",
            }
        )
        return

    checks.append({"name": name, "ok": True, "details": "Files are synchronized."})


def _check_skill_directory_sync(
    repo_skill_dir: Path,
    installed_skill_dir: Path,
    issues: list[LintIssue],
    checks: list[dict[str, Any]],
) -> None:
    name = "installed skill matches repo skill bundle"
    if not repo_skill_dir.exists():
        issues.append(LintIssue("tooling-missing-repo-skill", f"Missing repo skill directory {repo_skill_dir}."))
        checks.append({"name": name, "ok": False, "details": f"Missing repo skill directory {repo_skill_dir}."})
        return

    if not installed_skill_dir.exists():
        checks.append(
            {
                "name": name,
                "ok": True,
                "status": "skipped",
                "details": f"Installed skill directory {installed_skill_dir} is not present.",
            }
        )
        return

    repo_files = _relative_files(repo_skill_dir)
    installed_files = _relative_files(installed_skill_dir)
    missing_files = sorted(repo_files - installed_files)
    extra_files = sorted(installed_files - repo_files)
    changed_files = sorted(
        relative_path
        for relative_path in repo_files & installed_files
        if (repo_skill_dir / relative_path).read_bytes() != (installed_skill_dir / relative_path).read_bytes()
    )

    if not missing_files and not extra_files and not changed_files:
        checks.append({"name": name, "ok": True, "details": "Installed skill matches repo bundle."})
        return

    details: list[str] = []
    if missing_files:
        details.append(f"missing {len(missing_files)} file(s)")
    if extra_files:
        details.append(f"extra {len(extra_files)} file(s)")
    if changed_files:
        details.append(f"changed {len(changed_files)} file(s)")
    summary = ", ".join(details)
    issues.append(
        LintIssue(
            "tooling-installed-skill-drift",
            f"{name}: {summary}.",
        )
    )
    checks.append(
        {
            "name": name,
            "ok": False,
            "details": summary,
            "missingFiles": missing_files,
            "extraFiles": extra_files,
            "changedFiles": changed_files,
        }
    )


def _check_skill_bundle_presence(
    skill_dir: Path,
    issues: list[LintIssue],
    checks: list[dict[str, Any]],
) -> None:
    required_paths = [
        "SKILL.md",
        "agents/openai.yaml",
        "scripts/promise",
        "scripts/promise_cli/__init__.py",
        "scripts/promise_cli/__main__.py",
        "scripts/promise_cli/cli.py",
        "scripts/promise_cli/dsl.py",
        "references/promise-standard.md",
        "references/promise-architecture.md",
        "references/promise-core.md",
        "references/promise-language.md",
        "references/promise-cli.promise",
        "references/promise-tooling-readme.md",
    ]
    missing_paths = sorted(path for path in required_paths if not (skill_dir / path).exists())
    name = "current skill bundle contains required files"
    if not missing_paths:
        checks.append({"name": name, "ok": True, "details": "All required skill files are present."})
        return

    issues.append(
        LintIssue(
            "tooling-skill-bundle-incomplete",
            f"{name}: missing {len(missing_paths)} required file(s).",
        )
    )
    checks.append(
        {
            "name": name,
            "ok": False,
            "details": f"Missing {len(missing_paths)} required file(s).",
            "missingFiles": missing_paths,
        }
    )


def _relative_files(root: Path) -> set[str]:
    files: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative_path = path.relative_to(root)
        if "__pycache__" in relative_path.parts or path.name == ".DS_Store":
            continue
        files.add(relative_path.as_posix())
    return files


def _check_skill_validation(
    name: str,
    skill_dir: Path,
    validator_path: Path,
    issues: list[LintIssue],
    checks: list[dict[str, Any]],
    *,
    optional: bool = False,
) -> None:
    if not skill_dir.exists():
        if optional:
            checks.append(
                {
                    "name": name,
                    "ok": True,
                    "status": "skipped",
                    "details": f"Skill directory {skill_dir} is not present.",
                }
            )
            return
        issues.append(LintIssue("tooling-missing-skill-dir", f"{name}: missing skill directory {skill_dir}."))
        checks.append({"name": name, "ok": False, "details": f"Missing skill directory {skill_dir}."})
        return

    if not validator_path.exists():
        checks.append(
            {
                "name": name,
                "ok": True,
                "status": "skipped",
                "details": f"Validator script {validator_path} is not present.",
            }
        )
        return

    result = subprocess.run(
        [sys.executable, str(validator_path), str(skill_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode != 0:
        issues.append(
            LintIssue(
                "tooling-skill-invalid",
                f"{name}: validator returned exit code {result.returncode}.",
            )
        )
        checks.append(
            {
                "name": name,
                "ok": False,
                "details": output or f"Validator returned exit code {result.returncode}.",
            }
        )
        return

    checks.append(
        {
            "name": name,
            "ok": True,
            "details": output or "Skill is valid.",
        }
    )
