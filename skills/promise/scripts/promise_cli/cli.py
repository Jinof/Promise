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

from promise_cli.dsl import (
    LintIssue,
    PromiseExpressionError,
    PromiseParseError,
    analyze_intent_conflicts,
    analyze_intent_graph,
    format_spec,
    lint_spec,
    parse_file,
    parse_promise_expression,
    to_json,
)


ROOT = Path(__file__).resolve().parents[2]
CLI_INVOCATION_OBJECT = "PromiseCliInvocation"
ENUM_TYPE_RE = re.compile(r"^enum\(([^)]+)\)$")
STEP_RE = re.compile(r"^step\s*=\s*([A-Za-z0-9_-]+)$")
SKILL_NAME = "promise"
GRAPH_LANE_ORDER = ("intent", "system", "field", "function", "verify")
GRAPH_LANE_TITLES = {
    "system": "System",
    "intent": "Intent Layer",
    "field": "Field Layer",
    "function": "Function Layer",
    "verify": "Verify Layer",
}
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
    intent_conflict_analysis = analyze_intent_conflicts(spec)
    intent_graph_analysis = analyze_intent_graph(spec)
    resource_index = _build_intent_resource_index(spec)
    term_index = _build_intent_term_index(spec)

    base_report: dict[str, Any] = {
        "ok": True,
        "intentCount": len(intent_promises),
        "resourceCount": len(resource_index),
        "termCount": len(term_index),
        "rootIntent": root_intent,
        "selectedIntent": selected_intent,
        "intentResources": [_intent_resource_summary(resource) for resource in resource_index.values()],
        "intentTerms": [_intent_term_summary(term) for term in term_index.values()],
        "intentTree": intent_tree,
        "intentChain": None,
        "intentGraph": _intent_graph_analysis_summary(intent_graph_analysis),
        "declaredIntentConflicts": intent_conflict_analysis["declared"],
        "detectedIntentConflicts": intent_conflict_analysis["detected"],
        "intentConflicts": intent_conflict_analysis["all"],
        "graphIssues": [],
        "conflicts": [],
        "requirements": [],
        "resources": [],
        "terms": [],
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
    requirement_resources = _intent_requirement_resource_reports(intent_promise, resource_index)
    requirement_terms = _intent_requirement_term_reports(intent_promise, term_index)
    requirement_resource_targets = [resource["name"] for resource in requirement_resources]
    direct_targets = [intent_map["target"] for intent_map in intent_promise.get("maps", [])] + requirement_resource_targets
    downstream_items = _collect_downstream_items(direct_targets, downstream_index, item_index)
    downstream_targets = [item["target"] for item in downstream_items]

    base_report["intentChain"] = _intent_chain_report(intent_promise, intent_promises)
    base_report["requirements"] = [
        _intent_requirement_report(requirement, intent_promise)
        for requirement in intent_promise.get("requirements", [])
    ]
    base_report["resources"] = requirement_resources
    base_report["terms"] = requirement_terms
    base_report["conflicts"] = _intent_conflict_reports(
        selected_intent,
        intent_promises,
        intent_conflict_analysis["all"],
    )
    base_report["graphIssues"] = _intent_graph_issue_reports(selected_intent, intent_graph_analysis)
    base_report["directItems"] = [
        _impact_item_report(
            intent_map["target"],
            item_index,
            relation=intent_map.get("relation"),
            note=intent_map.get("note"),
        )
        for intent_map in intent_promise.get("maps", [])
    ] + [
        _impact_item_report(
            resource["name"],
            item_index,
            relation=resource.get("operationRelation"),
            note=resource.get("operationNote"),
        )
        for resource in requirement_resources
    ]
    base_report["downstreamItems"] = downstream_items
    base_report["relatedIntents"] = _related_intent_reports(
        selected_intent,
        intent_promises,
        set(direct_targets) | set(downstream_targets),
    )
    return base_report


def _intent_requirement_report(requirement: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    report = dict(requirement)
    report.setdefault("priority", intent.get("priority") or "")
    for semantic_key in ("scope", "effect", "constraint"):
        report.setdefault(semantic_key, "")
    return report


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


def _build_intent_resource_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        resource["name"]: resource
        for resource in spec.get("intentResources", [])
    }


def _build_intent_term_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        term["name"]: term
        for term in spec.get("intentTerms", [])
    }


def _intent_resource_summary(resource: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": resource["name"],
        "kind": resource.get("kind"),
        "summary": resource.get("summary") or "",
        "aliases": list(resource.get("aliases", [])),
        "mapCount": len(resource.get("maps", [])),
    }


def _intent_term_summary(term: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": term["name"],
        "kind": term.get("kind"),
        "summary": term.get("summary") or "",
        "aliases": list(term.get("aliases", [])),
        "parent": term.get("parent") or "",
        "disjoint": list(term.get("disjoint", [])),
        "opposites": list(term.get("opposites", [])),
        "mapCount": len(term.get("maps", [])),
    }


def _intent_summary(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": intent["name"],
        "priority": intent.get("priority"),
        "status": intent.get("status"),
        "root": bool(intent.get("root")),
        "conflictCount": len(intent.get("conflicts", [])),
        "requirementCount": len(intent.get("requirements", [])),
        "statement": intent.get("statement") or "",
    }


def _intent_requirement_resource_reports(
    intent: dict[str, Any],
    resource_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    reports_by_name: dict[str, dict[str, Any]] = {}
    for requirement in intent.get("requirements", []):
        operation_relation = _intent_requirement_operation_relation(requirement)
        for resource_key in ("actor", "resource", "over"):
            resource_name = requirement.get(resource_key)
            if not resource_name or resource_name not in resource_index:
                continue
            report = {
                **_intent_resource_summary(resource_index[resource_name]),
                "role": resource_key,
                "requirement": requirement.get("id"),
                "operationRelation": operation_relation,
                "scope": requirement.get("scope") or "",
                "effect": requirement.get("effect") or "",
                "constraint": requirement.get("constraint") or "",
                "priority": requirement.get("priority") or intent.get("priority") or "",
            }
            if requirement.get("because"):
                report["operationNote"] = requirement["because"]
            reports_by_name.setdefault(resource_name, report)
    return sorted(reports_by_name.values(), key=lambda item: item["name"])


def _intent_requirement_term_reports(
    intent: dict[str, Any],
    term_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    reports_by_role_name: dict[tuple[str, str], dict[str, Any]] = {}
    for requirement in intent.get("requirements", []):
        for term_kind, term_name in (
            ("action", requirement.get("action")),
            ("scope", requirement.get("scope")),
            ("effect", requirement.get("effect")),
            ("constraint", requirement.get("constraint")),
        ):
            if not term_name or term_name not in term_index:
                continue
            report = {
                **_intent_term_summary(term_index[term_name]),
                "role": term_kind,
                "requirement": requirement.get("id"),
                "operationRelation": _intent_requirement_operation_relation(requirement),
                "priority": requirement.get("priority") or intent.get("priority") or "",
            }
            reports_by_role_name.setdefault((term_kind, term_name), report)
    return sorted(reports_by_role_name.values(), key=lambda item: (item["role"], item["name"]))


def _intent_requirement_operation_relation(requirement: dict[str, Any]) -> str:
    relation = f"{requirement.get('kind', 'requires')} {requirement.get('predicate') or requirement.get('action') or '-'}"
    if requirement.get("effect"):
        relation += f" -> {requirement['effect']}"
    if requirement.get("scope"):
        relation += f" @{requirement['scope']}"
    return relation


def _intent_requirement_graph_details(intent: dict[str, Any]) -> list[str]:
    details: list[str] = []
    for requirement in intent.get("requirements", [])[:3]:
        semantic_parts = [
            f"{requirement.get('kind', 'requires')} {requirement.get('predicate') or requirement.get('action') or '-'}",
            f"target {requirement.get('object') or requirement.get('resource') or '-'}",
        ]
        for semantic_key in ("scope", "effect", "constraint", "priority"):
            semantic_value = requirement.get(semantic_key)
            if semantic_value:
                semantic_parts.append(f"{semantic_key} {semantic_value}")
        details.append(f"req {requirement.get('id', '-')}: {'; '.join(semantic_parts)}")
    if len(intent.get("requirements", [])) > 3:
        details.append(f"{len(intent.get('requirements', [])) - 3} more requirements")
    return details


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

    for resource in spec.get("intentResources", []):
        _add_promise_item(
            items,
            resource["name"],
            "intent-resource",
            resource["name"],
            resource.get("summary") or "",
        )

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

    for resource in spec.get("intentResources", []):
        for resource_map in resource.get("maps", []):
            _add_impact_edge(
                edges,
                resource["name"],
                resource_map["target"],
                resource_map.get("relation") or "maps",
            )

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


def _intent_conflict_reports(
    selected_intent: str,
    intent_promises: list[dict[str, Any]],
    intent_conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    intent_index = {intent["name"]: intent for intent in intent_promises}
    for conflict in intent_conflicts:
        target = conflict.get("target")
        source = conflict.get("source")
        if source != selected_intent and target != selected_intent:
            continue
        peer_name = target if source == selected_intent else source
        peer = intent_index.get(peer_name)
        report = {
            **conflict,
            "direction": "out" if source == selected_intent else "in",
        }
        if peer is not None:
            report["peer"] = _intent_summary(peer)
        reports.append(report)
    return sorted(reports, key=lambda item: (item["direction"], item["source"], item["target"] or ""))


def _intent_graph_analysis_summary(intent_graph_analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodeCount": len(intent_graph_analysis.get("nodes", [])),
        "edgeCount": len(intent_graph_analysis.get("edges", [])),
        "declaredCycleCount": len(intent_graph_analysis.get("declaredCycles", [])),
        "unexpectedCycleCount": len(intent_graph_analysis.get("unexpectedCycles", [])),
        "declaredCycles": intent_graph_analysis.get("declaredCycles", []),
        "unexpectedCycles": intent_graph_analysis.get("unexpectedCycles", []),
    }


def _intent_graph_issue_reports(
    selected_intent: str,
    intent_graph_analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    selected_id = f"intent::{selected_intent}"
    reports: list[dict[str, Any]] = []
    for cycle in intent_graph_analysis.get("unexpectedCycles", []):
        if selected_id not in cycle.get("nodeIds", []):
            continue
        reports.append(
            {
                "type": "unexpectedCycle",
                "severity": "error",
                **cycle,
            }
        )
    return sorted(reports, key=lambda item: (item["type"], ",".join(item.get("nodes", [])), item.get("reason", "")))


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

    print("Structured requirements:")
    if report["requirements"]:
        for requirement in report["requirements"]:
            over = f" over {requirement['over']}" if requirement.get("over") else ""
            actor = f" actor {requirement['actor']}" if requirement.get("actor") else ""
            resource = f" resource {requirement['resource']}" if requirement.get("resource") else ""
            scope = f" scope {requirement['scope']}" if requirement.get("scope") else ""
            effect = f" effect {requirement['effect']}" if requirement.get("effect") else ""
            constraint = f" constraint {requirement['constraint']}" if requirement.get("constraint") else ""
            priority = f" priority {requirement['priority']}" if requirement.get("priority") else ""
            print(
                f"  - {requirement['kind']} {requirement['id']}: "
                f"{requirement['subject']} {requirement['predicate']} {requirement['object']}"
                f"{over}{actor}{resource}{scope}{effect}{constraint}{priority}"
            )
    else:
        print("  - none")

    print("Resources:")
    if report["resources"]:
        for resource in report["resources"]:
            print(f"  - {resource['name']} [{resource['kind']}] as {resource.get('role', '-')}")
    else:
        print("  - none")

    print("Terms:")
    if report["terms"]:
        for term in report["terms"]:
            parent = f" parent {term['parent']}" if term.get("parent") else ""
            print(f"  - {term['name']} [{term['kind']}] as {term.get('role', '-')}{parent}")
    else:
        print("  - none")

    print("Intent conflicts:")
    if report["conflicts"]:
        for conflict in report["conflicts"]:
            direction = "to" if conflict.get("direction") == "out" else "from"
            peer = conflict.get("target") if direction == "to" else conflict.get("source")
            resolution = f"; resolution: {conflict['resolution']}" if conflict.get("resolution") else ""
            source_type = conflict.get("sourceType", "declared")
            print(
                f"  - {direction} {peer} [{conflict.get('severity', '-')} {source_type}] "
                f"{conflict.get('reason', '-')}{resolution}"
            )
    else:
        print("  - none")

    print("Intent graph issues:")
    if report["graphIssues"]:
        for issue in report["graphIssues"]:
            nodes = ", ".join(issue.get("nodes", [])) or f"{issue.get('source', '-')} <-> {issue.get('target', '-')}"
            print(f"  - {issue['type']} [{issue.get('severity', '-')}] {nodes}: {issue.get('reason', '-')}")
    else:
        print("  - none")

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
    try:
        expression_ast = parse_promise_expression(expression)
    except PromiseExpressionError:
        return None
    return _render_go_expression(
        object_name,
        expression_ast,
        field_lookup,
        state_field,
        states,
        type_promises,
        type_mappings,
    )


def _go_obligation_violation_expression(
    object_name: str,
    expression: str | None,
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if not expression:
        return None
    try:
        expression_ast = parse_promise_expression(expression)
    except PromiseExpressionError:
        return None
    return _render_go_expression_violation(
        object_name,
        expression_ast,
        field_lookup,
        state_field,
        states,
        type_promises,
        type_mappings,
    )


def _render_go_expression(
    object_name: str,
    expression_ast: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    kind = expression_ast["kind"]
    if kind == "binary":
        left = _render_go_expression(object_name, expression_ast["left"], field_lookup, state_field, states, type_promises, type_mappings)
        right = _render_go_expression(object_name, expression_ast["right"], field_lookup, state_field, states, type_promises, type_mappings)
        if left is None or right is None:
            return None
        operator = "&&" if expression_ast["operator"] == "and" else "||"
        return f"({left} {operator} {right})"
    if kind == "not":
        operand = _render_go_expression(object_name, expression_ast["operand"], field_lookup, state_field, states, type_promises, type_mappings)
        if operand is None:
            return None
        return f"!({operand})"
    if kind == "comparison":
        return _render_go_expression_comparison(
            object_name,
            expression_ast,
            field_lookup,
            state_field,
            states,
            type_promises,
            type_mappings,
        )
    if kind == "reference":
        field = _go_expression_field_reference(object_name, expression_ast, field_lookup)
        if field is None:
            return None
        return _render_go_boolean_field(field, type_promises)
    if kind == "literal" and expression_ast["literalType"] == "boolean":
        return "true" if expression_ast["value"] else "false"
    return None


def _render_go_expression_violation(
    object_name: str,
    expression_ast: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if expression_ast["kind"] == "comparison":
        return _render_go_expression_comparison(
            object_name,
            expression_ast,
            field_lookup,
            state_field,
            states,
            type_promises,
            type_mappings,
            invert=True,
        )
    rendered = _render_go_expression(
        object_name,
        expression_ast,
        field_lookup,
        state_field,
        states,
        type_promises,
        type_mappings,
    )
    if rendered is None:
        return None
    return f"!({rendered})"


def _render_go_expression_comparison(
    object_name: str,
    expression_ast: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
    *,
    invert: bool = False,
) -> str | None:
    operator = expression_ast["operator"]
    if invert:
        operator = _invert_go_expression_operator(operator)
    left = expression_ast["left"]
    right = expression_ast["right"]
    left_field = _go_expression_field_reference(object_name, left, field_lookup)
    right_field = _go_expression_field_reference(object_name, right, field_lookup)
    if left_field is not None:
        return _render_go_field_comparison(
            object_name,
            left_field,
            operator,
            right,
            state_field,
            states,
            type_promises,
            type_mappings,
        )
    if right_field is not None and operator != "in":
        reversed_operator = _reverse_go_expression_operator(operator)
        if reversed_operator is None:
            return None
        return _render_go_field_comparison(
            object_name,
            right_field,
            reversed_operator,
            left,
            state_field,
            states,
            type_promises,
            type_mappings,
        )
    return None


def _render_go_field_comparison(
    object_name: str,
    field: dict[str, Any],
    operator: str,
    value_ast: dict[str, Any],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if operator in {"in", "not in"}:
        if value_ast["kind"] != "list":
            return None
        item_operator = "==" if operator == "in" else "!="
        comparisons = [
            _render_go_field_comparison(
                object_name,
                field,
                item_operator,
                item,
                state_field,
                states,
                type_promises,
                type_mappings,
            )
            for item in value_ast.get("items", [])
        ]
        if any(comparison is None for comparison in comparisons):
            return None
        if not comparisons:
            return "false" if operator == "in" else "true"
        joiner = " || " if operator == "in" else " && "
        return "(" + joiner.join(comparisons) + ")"

    if _go_expression_is_null(value_ast):
        if operator == "==":
            return _render_go_null_comparison(field, "==")
        if operator == "!=":
            return _render_go_null_comparison(field, "!=")
        return None

    rendered_value = _render_go_expression_value(
        object_name,
        field,
        value_ast,
        state_field,
        states,
        type_promises,
        type_mappings,
    )
    if rendered_value is None:
        return None

    accessor = f"value.{_go_exported_identifier(field['name'])}"
    comparable_accessor = f"*{accessor}" if field.get("nullable") else accessor
    if field.get("nullable"):
        if operator == "==":
            return f"{accessor} != nil && {comparable_accessor} == {rendered_value}"
        if operator == "!=":
            return f"{accessor} == nil || {comparable_accessor} != {rendered_value}"
        if operator in {"<", "<=", ">", ">="}:
            return f"{accessor} != nil && {comparable_accessor} {operator} {rendered_value}"
        return None
    if operator in {"==", "!=", "<", "<=", ">", ">="}:
        return f"{comparable_accessor} {operator} {rendered_value}"
    return None


def _render_go_expression_value(
    object_name: str,
    expected_field: dict[str, Any],
    value_ast: dict[str, Any],
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    if value_ast["kind"] == "literal":
        return _render_go_expression_literal(expected_field, value_ast, type_promises, type_mappings)
    if value_ast["kind"] == "reference":
        field_ref = _go_expression_field_reference(object_name, value_ast, {})
        if field_ref is not None:
            return f"value.{_go_exported_identifier(field_ref['name'])}"
        enum_literal = _go_expression_enum_literal(expected_field, value_ast, object_name, state_field, states)
        if enum_literal is not None:
            return _go_enum_const_name(_go_enum_type_name(object_name, expected_field["name"]), enum_literal)
        if expected_field["type"] in type_mappings:
            return None
        type_promise = type_promises.get(expected_field["type"])
        if type_promise is not None:
            if expected_field["type"] in type_mappings:
                return None
            if len(value_ast["parts"]) == 1:
                rendered = _render_go_base_comparison_value(type_promise["base"], value_ast["parts"][0])
                if rendered is None:
                    return None
                return f"{_go_declared_type_name(type_promise['name'])}({rendered})"
        if len(value_ast["parts"]) == 1:
            return _render_go_comparison_value(
                object_name,
                expected_field,
                value_ast["parts"][0],
                state_field,
                states,
                type_promises,
                type_mappings,
            )
    return None


def _render_go_expression_literal(
    expected_field: dict[str, Any],
    value_ast: dict[str, Any],
    type_promises: dict[str, dict[str, Any]],
    type_mappings: dict[str, tuple[str, str | None]],
) -> str | None:
    literal_type = value_ast["literalType"]
    if literal_type == "string":
        rendered = _go_string(value_ast["value"])
    elif literal_type == "boolean":
        rendered = "true" if value_ast["value"] else "false"
    elif literal_type == "number":
        rendered = str(value_ast.get("raw", value_ast["value"]))
    elif literal_type == "null":
        rendered = "nil"
    else:
        return None

    type_promise = type_promises.get(expected_field["type"])
    if type_promise is not None:
        if expected_field["type"] in type_mappings:
            return None
        base_rendered = _render_go_base_literal_value(type_promise["base"], value_ast)
        if base_rendered is None:
            return None
        return f"{_go_declared_type_name(type_promise['name'])}({base_rendered})"
    if expected_field["type"] in type_mappings:
        return None
    return rendered


def _render_go_base_literal_value(base_type: str, value_ast: dict[str, Any]) -> str | None:
    literal_type = value_ast["literalType"]
    if base_type in {"string", "text", "path"} and literal_type == "string":
        return _go_string(value_ast["value"])
    if base_type == "boolean" and literal_type == "boolean":
        return "true" if value_ast["value"] else "false"
    if base_type in {"integer", "number"} and literal_type == "number":
        return str(value_ast.get("raw", value_ast["value"]))
    return None


def _render_go_boolean_field(field: dict[str, Any], type_promises: dict[str, dict[str, Any]]) -> str | None:
    field_type = field["type"]
    type_promise = type_promises.get(field_type)
    if type_promise is not None:
        field_type = type_promise["base"]
    if field_type != "boolean":
        return None
    accessor = f"value.{_go_exported_identifier(field['name'])}"
    if field.get("nullable"):
        return f"{accessor} != nil && *{accessor}"
    return accessor


def _render_go_null_comparison(field: dict[str, Any], operator: str) -> str:
    accessor = f"value.{_go_exported_identifier(field['name'])}"
    if field.get("nullable"):
        return f"{accessor} {operator} nil"
    return "false" if operator == "==" else "true"


def _go_expression_field_reference(
    object_name: str,
    value_ast: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if value_ast["kind"] != "reference" or len(value_ast["parts"]) != 2:
        return None
    if value_ast["parts"][0] != object_name:
        return None
    return field_lookup.get(value_ast["parts"][1])


def _go_expression_enum_literal(
    expected_field: dict[str, Any],
    value_ast: dict[str, Any],
    object_name: str,
    state_field: dict[str, Any] | None,
    states: list[dict[str, Any]],
) -> str | None:
    enum_values = _enum_choices(expected_field["type"]) or []
    if state_field is not None and expected_field["name"] == state_field["name"]:
        enum_values = [state["value"] for state in states] or enum_values
    if not enum_values:
        return None
    literal = value_ast["parts"][-1]
    if literal not in enum_values:
        return None
    if len(value_ast["parts"]) == 1:
        return literal
    namespace = ".".join(value_ast["parts"][:-1])
    allowed_namespaces = {
        expected_field["name"],
        f"{object_name}.{expected_field['name']}",
        _go_enum_type_name(object_name, expected_field["name"]),
    }
    if namespace in allowed_namespaces:
        return literal
    return None


def _go_expression_is_null(value_ast: dict[str, Any]) -> bool:
    return value_ast["kind"] == "literal" and value_ast["literalType"] == "null"


def _invert_go_expression_operator(operator: str) -> str:
    return {
        "==": "!=",
        "!=": "==",
        "<": ">=",
        "<=": ">",
        ">": "<=",
        ">=": "<",
        "in": "not in",
    }.get(operator, operator)


def _reverse_go_expression_operator(operator: str) -> str | None:
    return {
        "==": "==",
        "!=": "!=",
        "<": ">",
        "<=": ">=",
        ">": "<",
        ">=": "<=",
    }.get(operator)


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
    term_targets: dict[tuple[str, str], str] = {}
    field_promise_objects: dict[str, str] = {}
    function_primary_anchors: dict[str, str] = {}

    system_id = "system::root"
    meta = spec["meta"]
    intent_conflicts = analyze_intent_conflicts(spec)["all"]
    intent_graph_analysis = analyze_intent_graph(spec)
    intent_graph_issue_edge_pairs = _intent_graph_issue_edge_pairs(intent_graph_analysis)
    intent_graph_issue_node_counts = _intent_graph_issue_node_counts(intent_graph_analysis)
    intent_conflict_counts = {
        intent_promise["name"]: 0
        for intent_promise in spec.get("intentPromises", [])
    }
    seen_intent_conflict_pairs: set[tuple[str, str]] = set()
    for intent_conflict in intent_conflicts:
        source_name = intent_conflict.get("source")
        target_name = intent_conflict.get("target")
        if not source_name or not target_name:
            continue
        pair_key = tuple(sorted((source_name, target_name)))
        if pair_key in seen_intent_conflict_pairs:
            continue
        seen_intent_conflict_pairs.add(pair_key)
        if source_name in intent_conflict_counts:
            intent_conflict_counts[source_name] += 1
        if target_name in intent_conflict_counts:
            intent_conflict_counts[target_name] += 1
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
            "anchor": "System Promise",
            "label": "System Promise",
            "summary": meta.get("summary") or "",
            "details": [f"title {meta.get('title') or meta.get('domain') or 'System Promise'}", *system_details],
        }
    )

    for intent_resource in spec.get("intentResources", []):
        node_id = f"resource::{intent_resource['name']}"
        promise_targets[intent_resource["name"]] = node_id
        nodes.append(
            {
                "id": node_id,
                "lane": "intent",
                "kind": "resource",
                "anchor": "Resource",
                "label": intent_resource["name"],
                "summary": intent_resource.get("summary") or "",
                "details": [
                    f"kind {intent_resource.get('kind') or '-'}",
                    f"{len(intent_resource.get('aliases', []))} aliases",
                    f"{len(intent_resource.get('maps', []))} maps",
                ],
            }
        )

    for intent_term in spec.get("intentTerms", []):
        node_id = f"term::{intent_term.get('kind', 'term')}::{intent_term['name']}"
        term_targets[(intent_term.get("kind", "term"), intent_term["name"])] = node_id
        nodes.append(
            {
                "id": node_id,
                "lane": "intent",
                "kind": "term",
                "anchor": f"Term:{intent_term.get('kind') or '-'}",
                "label": intent_term["name"],
                "summary": intent_term.get("summary") or "",
                "details": [
                    f"kind {intent_term.get('kind') or '-'}",
                    f"parent {intent_term.get('parent') or '-'}",
                    f"{len(intent_term.get('aliases', []))} aliases",
                    f"{len(intent_term.get('disjoint', []))} disjoint",
                    f"{len(intent_term.get('opposites', []))} opposites",
                    f"{len(intent_term.get('maps', []))} maps",
                ],
            }
        )

    for intent_cycle in spec.get("intentCycles", []):
        node_id = f"cycle::{intent_cycle['name']}"
        promise_targets[intent_cycle["name"]] = node_id
        cycle_node_refs = _intent_cycle_node_refs_for_graph(intent_cycle)
        nodes.append(
            {
                "id": node_id,
                "lane": "intent",
                "kind": "cycle",
                "anchor": f"Cycle:{intent_cycle.get('kind') or '-'}",
                "label": intent_cycle["name"],
                "summary": intent_cycle.get("summary") or "",
                "details": [
                    f"kind {intent_cycle.get('kind') or '-'}",
                    f"{len(cycle_node_refs)} nodes",
                    f"{len(intent_cycle.get('edges', []))} edges",
                    f"rationale {intent_cycle.get('rationale') or '-'}",
                ],
            }
        )

    for intent_promise in spec.get("intentPromises", []):
        node_id = f"intent::{intent_promise['name']}"
        promise_targets[intent_promise["name"]] = node_id
        requirement_details = _intent_requirement_graph_details(intent_promise)
        nodes.append(
            {
                "id": node_id,
                "lane": "intent",
                "kind": "intent",
                "anchor": "Intent",
                "label": intent_promise["name"],
                "summary": intent_promise.get("statement") or "",
                "root": bool(intent_promise.get("root")),
                "details": [
                    f"priority {intent_promise.get('priority') or '-'}",
                    f"status {intent_promise.get('status') or '-'}",
                    "root true" if intent_promise.get("root") else "root false",
                    f"{len(intent_promise.get('parents', []))} parents",
                    f"{intent_conflict_counts.get(intent_promise['name'], 0)} conflicts",
                    f"{intent_graph_issue_node_counts.get(node_id, 0)} graph issues",
                    f"{len(intent_promise.get('requirements', []))} requirements",
                    f"{len(intent_promise.get('maps', []))} maps",
                ]
                + requirement_details,
                "conflictCount": intent_conflict_counts.get(intent_promise["name"], 0),
            }
        )
        if intent_promise.get("root") or not intent_promise.get("parents"):
            _add_graph_edge(edge_labels, node_id, system_id, "defines System Promise")

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
        for requirement in intent_promise.get("requirements", []):
            if requirement.get("actor") and requirement["actor"] in promise_targets:
                _add_graph_edge(edge_labels, promise_targets[requirement["actor"]], source_id, "actor")
            if requirement.get("resource") and requirement["resource"] in promise_targets:
                _add_graph_edge(
                    edge_labels,
                    source_id,
                    promise_targets[requirement["resource"]],
                    _intent_requirement_operation_relation(requirement),
                )
            if requirement.get("over") and requirement["over"] in promise_targets:
                _add_graph_edge(edge_labels, source_id, promise_targets[requirement["over"]], "over")
            for term_kind, term_value in (
                ("action", requirement.get("action")),
                ("scope", requirement.get("scope")),
                ("effect", requirement.get("effect")),
                ("constraint", requirement.get("constraint")),
            ):
                term_id = term_targets.get((term_kind, term_value or ""))
                if term_id is not None:
                    _add_graph_edge(edge_labels, source_id, term_id, term_kind)
        for intent_map in intent_promise.get("maps", []):
            _add_graph_relations(
                source_id,
                [intent_map["target"]],
                intent_map.get("relation") or "maps",
                promise_targets,
                object_targets,
                edge_labels,
            )

    for intent_term in spec.get("intentTerms", []):
        source_id = term_targets.get((intent_term.get("kind", "term"), intent_term["name"]))
        if source_id is None:
            continue
        parent_id = term_targets.get((intent_term.get("kind", "term"), intent_term.get("parent") or ""))
        if parent_id is not None:
            _add_graph_edge(edge_labels, parent_id, source_id, f"{intent_term.get('kind')} contains")
        for disjoint in intent_term.get("disjoint", []):
            disjoint_id = term_targets.get((intent_term.get("kind", "term"), disjoint))
            if disjoint_id is not None:
                _add_graph_edge(edge_labels, source_id, disjoint_id, "disjoint")
        for opposite in intent_term.get("opposites", []):
            opposite_id = term_targets.get((intent_term.get("kind", "term"), opposite))
            if opposite_id is not None:
                _add_graph_edge(edge_labels, source_id, opposite_id, "opposite")
        for term_map in intent_term.get("maps", []):
            _add_graph_relations(
                source_id,
                [term_map["target"]],
                term_map.get("relation") or "maps",
                promise_targets,
                object_targets,
                edge_labels,
            )

    for intent_resource in spec.get("intentResources", []):
        source_id = promise_targets[intent_resource["name"]]
        for resource_map in intent_resource.get("maps", []):
            _add_graph_relations(
                source_id,
                [resource_map["target"]],
                resource_map.get("relation") or "maps",
                promise_targets,
                object_targets,
                edge_labels,
            )

    for intent_cycle in spec.get("intentCycles", []):
        source_id = promise_targets.get(intent_cycle["name"])
        if source_id is None:
            continue
        for node_ref in _intent_cycle_node_refs_for_graph(intent_cycle):
            target_id = _resolve_intent_cycle_graph_target(node_ref, promise_targets, term_targets)
            if target_id is not None:
                _add_graph_edge(edge_labels, source_id, target_id, "declares cycle")

    for intent_conflict in intent_conflicts:
        source_id = promise_targets.get(intent_conflict.get("source"))
        target_id = promise_targets.get(intent_conflict.get("target"))
        if source_id is None or target_id is None:
            continue
        severity = intent_conflict.get("severity") or "conflict"
        label_prefix = "auto conflict" if intent_conflict.get("sourceType") == "detected" else "conflicts"
        _add_graph_edge(edge_labels, source_id, target_id, f"{label_prefix} {severity}")

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

    edges = []
    for (source, target), labels in sorted(edge_labels.items()):
        sorted_labels = sorted(labels)
        graph_issue_type = intent_graph_issue_edge_pairs.get((source, target))
        edge = {
            "source": source,
            "target": target,
            "label": " / ".join(sorted_labels),
            "kind": "graph-issue" if graph_issue_type else _graph_edge_kind(sorted_labels),
        }
        if graph_issue_type:
            edge["analysisIssue"] = graph_issue_type
        conflict_severity = _graph_edge_conflict_severity(sorted_labels)
        if conflict_severity:
            edge["severity"] = conflict_severity
        edges.append(edge)

    for node in nodes:
        node["graphIssueCount"] = intent_graph_issue_node_counts.get(node["id"], 0)

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

    lane_counts = {
        lane: sum(1 for node in nodes if node["lane"] == lane)
        for lane in GRAPH_LANE_ORDER
    }
    view_mode = _select_graph_view_mode(len(nodes), len(edges))
    composition = "single"
    intent_nodes = [node for node in nodes if node["lane"] == "intent"]
    root_intent = next((node for node in intent_nodes if node.get("root")), intent_nodes[0] if intent_nodes else None)

    return {
        "title": meta.get("title") or meta.get("domain") or "System Promise",
        "domain": meta.get("domain") or "",
        "summary": meta.get("summary") or "",
        "rootIntentLabel": root_intent["label"] if root_intent else "",
        "rootIntentSummary": root_intent.get("summary", "") if root_intent else "",
        "sourcePath": source_path,
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "laneCounts": lane_counts,
        "viewMode": view_mode,
        "composition": composition,
        "intentGraphAnalysis": intent_graph_analysis,
        "nodes": nodes,
        "edges": edges,
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


def _intent_graph_issue_edge_pairs(intent_graph_analysis: dict[str, Any]) -> dict[tuple[str, str], str]:
    issue_pairs: dict[tuple[str, str], str] = {}
    for cycle in intent_graph_analysis.get("unexpectedCycles", []):
        for edge in cycle.get("edges", []):
            issue_pairs[(edge["sourceId"], edge["targetId"])] = "cycle"
    return issue_pairs


def _intent_graph_issue_node_counts(intent_graph_analysis: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cycle in intent_graph_analysis.get("unexpectedCycles", []):
        for node_id in cycle.get("nodeIds", []):
            counts[node_id] = counts.get(node_id, 0) + 1
    return counts


def _graph_edge_kind(labels: list[str]) -> str:
    if any(label.startswith("conflicts") or label.startswith("auto conflict") for label in labels):
        return "conflict"
    return "relation"


def _graph_edge_conflict_severity(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith("auto conflict"):
            parts = label.split(maxsplit=2)
            if len(parts) == 3:
                return parts[2]
            return "conflict"
        if label.startswith("conflicts"):
            parts = label.split(maxsplit=1)
            if len(parts) == 2:
                return parts[1]
            return "conflict"
    return None


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


def _resolve_intent_cycle_graph_target(
    ref: str,
    promise_targets: dict[str, str],
    term_targets: dict[tuple[str, str], str],
) -> str | None:
    if ref in promise_targets:
        return promise_targets[ref]
    if ref.startswith("term::"):
        parts = ref.split("::", 2)
        if len(parts) == 3:
            return term_targets.get((parts[1], parts[2]))
    matches = [node_id for (_kind, name), node_id in term_targets.items() if name == ref]
    if len(matches) == 1:
        return matches[0]
    return None


def _intent_cycle_node_refs_for_graph(intent_cycle: dict[str, Any]) -> list[str]:
    node_refs: list[str] = []
    for edge in intent_cycle.get("edges", []):
        for endpoint in (edge.get("source"), edge.get("target")):
            if endpoint and endpoint not in node_refs:
                node_refs.append(endpoint)
    return node_refs


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


def _select_graph_view_mode(node_count: int, edge_count: int) -> str:
    return "full"


def _render_graph_html_document(graph: dict[str, Any]) -> str:
    graph_markup = _render_full_graph_section(graph)
    graph_json = json_lib.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    hero_title = graph.get("rootIntentLabel") or graph["title"]
    hero_summary = graph.get("rootIntentSummary") or graph["summary"] or "Self-contained visualization of the current System Promise graph."

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_lib.escape(hero_title)} Graph</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #dfe4e3;
      --panel: rgba(241, 244, 242, 0.96);
      --panel-border: rgba(42, 54, 55, 0.18);
      --text: #182326;
      --muted: #586769;
      --system: #9a512f;
      --intent: #6450b8;
      --field: #267465;
      --function: #285ea7;
      --verify: #896e1e;
      --conflict: #b0443f;
      --edge: rgba(42, 54, 55, 0.34);
      --cad-line: rgba(45, 65, 68, 0.16);
      --cad-line-strong: rgba(45, 65, 68, 0.28);
      --shadow: 0 18px 36px rgba(26, 38, 40, 0.12);
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        linear-gradient(var(--cad-line) 1px, transparent 1px),
        linear-gradient(90deg, var(--cad-line) 1px, transparent 1px),
        linear-gradient(var(--cad-line-strong) 1px, transparent 1px),
        linear-gradient(90deg, var(--cad-line-strong) 1px, transparent 1px),
        linear-gradient(180deg, #eef2f1 0%, var(--bg) 100%);
      background-size: 24px 24px, 24px 24px, 120px 120px, 120px 120px, auto;
    }}
    .page {{
      max-width: none;
      min-height: 100vh;
      margin: 0 auto;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(380px, 0.8fr);
      gap: 12px;
      align-items: center;
      margin-bottom: 0;
      padding: 9px 10px;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      background: rgba(238, 242, 241, 0.92);
      box-shadow: var(--shadow);
    }}
    .hero-copy {{
      max-width: none;
      min-width: 0;
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
      font-size: 1.45rem;
      line-height: 1.05;
      letter-spacing: 0;
      overflow-wrap: anywhere;
    }}
    .hero p {{
      margin: 6px 0 0;
      font-size: 0.82rem;
      line-height: 1.35;
      color: var(--muted);
      max-width: 980px;
    }}
    .hero-source {{
      margin-top: 8px;
      margin-right: 6px;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 5px 9px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.64);
      border: 1px solid rgba(42, 54, 55, 0.12);
      color: var(--muted);
      font-size: 0.75rem;
      letter-spacing: 0.02em;
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
      grid-template-columns: repeat(4, minmax(88px, 1fr));
      gap: 6px;
      width: 100%;
    }}
    .meta-card {{
      padding: 8px 9px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.68);
      border: 1px solid var(--panel-border);
    }}
    .meta-label {{
      display: block;
      margin-bottom: 4px;
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .meta-value {{
      font-size: 0.9rem;
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
    .network-graph-shell {{
      flex: 1;
      min-height: 0;
      margin-bottom: 0;
      padding: 0;
      border-radius: 8px;
      background: rgba(228, 234, 233, 0.9);
      border: 1px solid rgba(42, 54, 55, 0.2);
      box-shadow: var(--shadow);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .network-graph-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 0;
      padding: 9px 10px;
      border-bottom: 1px solid rgba(42, 54, 55, 0.18);
      background: rgba(241, 244, 242, 0.94);
      flex-wrap: wrap;
    }}
    .network-graph-header h2 {{
      margin: 0;
      font-size: 0.88rem;
      letter-spacing: 0.03em;
      text-transform: uppercase;
    }}
    .network-graph-header p {{
      margin: 3px 0 0;
      max-width: 980px;
      color: var(--muted);
      font-size: 0.76rem;
      line-height: 1.36;
    }}
    .network-graph-board {{
      position: relative;
      flex: none;
      overflow: hidden;
      border-radius: 0;
      padding: 0;
      min-height: 620px;
      height: clamp(620px, calc(100vh - 180px), 920px);
      background:
        linear-gradient(rgba(35, 58, 62, 0.08) 1px, transparent 1px),
        linear-gradient(90deg, rgba(35, 58, 62, 0.08) 1px, transparent 1px),
        linear-gradient(rgba(35, 58, 62, 0.18) 1px, transparent 1px),
        linear-gradient(90deg, rgba(35, 58, 62, 0.18) 1px, transparent 1px),
        #eef1ef;
      background-size: 20px 20px, 20px 20px, 100px 100px, 100px 100px, auto;
      border: 0;
      cursor: grab;
      overscroll-behavior: contain;
      touch-action: none;
      user-select: none;
    }}
    .network-graph-board.panning {{
      cursor: grabbing;
    }}
    .graph-toolbar {{
      position: absolute;
      top: 14px;
      right: 14px;
      z-index: 3;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px;
      border-radius: 8px;
      border: 1px solid rgba(34, 32, 28, 0.1);
      background: rgba(255, 252, 246, 0.9);
      box-shadow: 0 12px 28px rgba(53, 45, 34, 0.1);
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .graph-toolbar button {{
      width: 34px;
      height: 34px;
      border: 0;
      border-radius: 7px;
      background: rgba(31, 26, 20, 0.08);
      color: var(--text);
      font: inherit;
      font-size: 15px;
      font-weight: 800;
      cursor: pointer;
    }}
    .graph-toolbar button:hover {{
      background: rgba(31, 26, 20, 0.14);
    }}
    .graph-toolbar button[data-graph-zoom="fit"] {{
      width: auto;
      padding: 0 10px;
      font-size: 12px;
      letter-spacing: 0;
    }}
    .graph-zoom-value {{
      min-width: 48px;
      text-align: center;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .graph-minimap {{
      position: absolute;
      right: 14px;
      bottom: 38px;
      z-index: 3;
      width: 210px;
      height: 132px;
      border: 1px solid rgba(42, 54, 55, 0.22);
      border-radius: 8px;
      background: rgba(238, 242, 241, 0.92);
      box-shadow: 0 12px 28px rgba(26, 38, 40, 0.12);
      overflow: hidden;
    }}
    .graph-minimap svg {{
      display: block;
      width: 100%;
      height: 100%;
    }}
    .cad-status-bar {{
      position: absolute;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 3;
      display: flex;
      align-items: center;
      gap: 14px;
      height: 28px;
      padding: 0 10px;
      border-top: 1px solid rgba(42, 54, 55, 0.2);
      background: rgba(226, 232, 231, 0.95);
      color: #354547;
      font-family: ui-monospace, "SFMono-Regular", "SF Mono", Menlo, monospace;
      font-size: 11px;
      letter-spacing: 0;
      text-transform: uppercase;
    }}
    .cad-status-bar strong {{
      color: var(--text);
      font-weight: 800;
    }}
    .layer-row-guide {{
      pointer-events: none;
      stroke: rgba(42, 54, 55, 0.12);
      stroke-width: 1;
      stroke-dasharray: 4 8;
    }}
    .network-graph {{
      display: block;
      width: 100%;
      min-width: 0;
      height: 100%;
      min-height: 720px;
      overflow: hidden;
      touch-action: none;
    }}
    .full-graph-network {{
      min-width: 0;
      height: 100%;
    }}
    .network-edge {{
      fill: none;
      stroke: rgba(52, 45, 37, 0.46);
      stroke-width: 2.5;
      stroke-linecap: round;
      stroke-linejoin: round;
      marker-end: url(#promise-graph-arrow);
    }}
    .network-edge.connected {{
      stroke: rgba(31, 26, 20, 0.78);
      stroke-width: 3;
    }}
    .network-edge.conflict-edge {{
      stroke: var(--conflict);
      stroke-dasharray: 10 8;
      stroke-width: 3;
    }}
    .network-edge.conflict-edge.connected {{
      stroke: #832b28;
      stroke-width: 3.6;
    }}
    .network-edge.graph-issue-edge {{
      stroke: #b35f00;
      stroke-dasharray: 4 6;
      stroke-width: 3.2;
    }}
    .network-edge.graph-issue-edge.connected {{
      stroke: #7f3f00;
      stroke-width: 3.8;
    }}
    .network-edge-label {{
      fill: #51483c;
      paint-order: stroke;
      stroke: rgba(255, 252, 246, 0.92);
      stroke-width: 4;
      stroke-linejoin: round;
      font-size: 12px;
      font-family: ui-monospace, "SFMono-Regular", "SF Mono", Menlo, monospace;
    }}
    .network-node {{
      cursor: pointer;
      outline: none;
    }}
    .network-node circle {{
      stroke: rgba(255, 255, 255, 0.92);
      stroke-width: 3;
      filter: drop-shadow(0 12px 20px rgba(45, 38, 31, 0.18));
    }}
    .network-node text {{
      pointer-events: none;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    .network-node-token {{
      fill: #fff;
      font-size: 12px;
      font-weight: 800;
    }}
    .network-node-label {{
      fill: var(--text);
      paint-order: stroke;
      stroke: rgba(255, 252, 246, 0.95);
      stroke-width: 5;
      stroke-linejoin: round;
      font-size: 13px;
      font-weight: 700;
    }}
    .network-node-meta {{
      fill: var(--muted);
      paint-order: stroke;
      stroke: rgba(255, 252, 246, 0.95);
      stroke-width: 4;
      stroke-linejoin: round;
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
    }}
    .network-node.active circle {{
      stroke: rgba(31, 26, 20, 0.86);
      stroke-width: 4;
    }}
    .network-node.root-intent-node circle {{
      stroke: rgba(111, 74, 183, 0.42);
      stroke-width: 4;
    }}
    .network-node.root-intent-node .network-node-token {{
      font-size: 11px;
    }}
    .network-node.conflicted-intent-node circle {{
      stroke: var(--conflict);
      stroke-width: 4;
    }}
    .network-node.graph-issue-node circle {{
      stroke: #b35f00;
      stroke-width: 4;
    }}
    .network-node.faded {{
      opacity: 0.36;
    }}
    .graph-fallback {{
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      white-space: nowrap;
    }}
    @media (max-width: 1180px) {{
      .page {{
        padding: 22px 14px 30px;
      }}
      .hero {{
        grid-template-columns: 1fr;
      }}
      .network-graph-shell {{
        padding: 16px;
      }}
    }}
    @media (max-width: 720px) {{
      .network-graph-board {{
        min-height: 520px;
        height: min(72vh, 640px);
      }}
      .meta-grid {{
        grid-template-columns: 1fr;
      }}
      h1 {{
        font-size: 2rem;
        line-height: 1.08;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Promise Graph · Human Intent → System Promise</p>
        <h1>{html_lib.escape(hero_title)}</h1>
        <p>{html_lib.escape(hero_summary)}</p>
        <div class="hero-source">
          <strong>System Promise</strong>
          <code>{html_lib.escape(graph['title'])}</code>
        </div>
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

    function svgElement(name) {{
      return document.createElementNS("http://www.w3.org/2000/svg", name);
    }}

    function laneColor(lane) {{
      const value = getComputedStyle(document.documentElement).getPropertyValue(`--${{lane}}`).trim();
      return value || "#475569";
    }}

    function graphRegion(node) {{
      if (node.lane === "intent") {{
        return "intent";
      }}
      return "promise";
    }}

    function shortText(value, limit) {{
      const text = String(value || "");
      if (text.length <= limit) {{
        return text;
      }}
      return text.slice(0, Math.max(0, limit - 1)) + "…";
    }}

    function splitLabel(value, limit = 18) {{
      const words = String(value || "").split(/(?=[A-Z][a-z])|\\s+|_/).filter(Boolean);
      const lines = [];
      let current = "";
      for (const word of words) {{
        const next = current ? current + " " + word : word;
        if (next.length > limit && current) {{
          lines.push(current);
          current = word;
        }} else {{
          current = next;
        }}
        if (lines.length === 1 && current.length > limit) {{
          break;
        }}
      }}
      if (current) {{
        lines.push(current);
      }}
      return lines.slice(0, 2).map((line) => shortText(line, limit));
    }}

    function nodeToken(node) {{
      if (node.lane === "intent" && node.root) {{
        return "ROOT";
      }}
      if (node.lane === "intent" && Number(node.graphIssueCount || 0) > 0) {{
        return "CYC";
      }}
      if (node.lane === "intent" && Number(node.conflictCount || 0) > 0) {{
        return "CNF";
      }}
      if (node.kind === "system") {{
        return "SYS";
      }}
      return String(node.kind || "?").slice(0, 3).toUpperCase();
    }}

    function nodeRadius(node) {{
      if (node.lane === "intent" && node.root) {{
        return 54;
      }}
      if (node.kind === "system") {{
        return 38;
      }}
      if (node.lane === "intent") {{
        return 44;
      }}
      return 42;
    }}

    function clamp(value, min, max) {{
      return Math.min(max, Math.max(min, value));
    }}

    function createGraphViewportController(board, svg, worldWidth, worldHeight, contentBounds = null) {{
      const zoomValues = board.querySelectorAll("[data-graph-zoom-value]");
      const coordinateValue = board.querySelector("[data-graph-coordinates]");
      const minimap = board.querySelector(".graph-minimap-svg");
      const toolbarButtons = board.querySelectorAll("[data-graph-zoom]");
      const content = {{
        left: Number.isFinite(contentBounds?.left) ? contentBounds.left : 0,
        top: Number.isFinite(contentBounds?.top) ? contentBounds.top : 0,
        right: Number.isFinite(contentBounds?.right) ? contentBounds.right : worldWidth,
        bottom: Number.isFinite(contentBounds?.bottom) ? contentBounds.bottom : worldHeight,
        bottomVisualTop: Number.isFinite(contentBounds?.bottomVisualTop) ? contentBounds.bottomVisualTop : null,
      }};
      const viewport = {{
        x: 0,
        y: 0,
        width: worldWidth,
        height: worldHeight,
      }};

      function boardSize() {{
        return {{
          width: Math.max(1, board.clientWidth || 1120),
          height: Math.max(1, board.clientHeight || 720),
        }};
      }}

      function normalizeViewport(next) {{
        const size = boardSize();
        const aspect = size.width / size.height;
        const minWidth = Math.max(420, worldWidth * 0.18);
        const maxWidth = worldWidth * 1.45;
        let nextWidth = clamp(next.width, minWidth, maxWidth);
        let nextHeight = nextWidth / aspect;
        const maxHeight = worldHeight * 1.45;
        if (nextHeight > maxHeight) {{
          nextHeight = maxHeight;
          nextWidth = nextHeight * aspect;
        }}
        const chromeBottom = Math.max(180, nextHeight * 0.22, nextHeight * (198 / size.height));
        const chromeRight = Math.max(160, nextWidth * 0.16, nextWidth * (238 / size.width));
        const paddingX = Math.min(360, Math.max(140, nextWidth * 0.2));
        const paddingTop = Math.min(320, Math.max(130, nextHeight * 0.18));
        const minX = Math.min(0, content.left) - paddingX;
        const maxX = content.right - nextWidth + chromeRight;
        const minY = Math.min(0, content.top) - paddingTop;
        const bottomBlankMaxY = content.bottom - nextHeight + chromeBottom;
        const bottomRevealMaxY = Number.isFinite(content.bottomVisualTop)
          ? content.bottomVisualTop - 8
          : bottomBlankMaxY;
        const maxY = Math.min(bottomBlankMaxY, bottomRevealMaxY);
        return {{
          x: minX <= maxX ? clamp(next.x, minX, maxX) : (minX + maxX) / 2,
          y: minY <= maxY ? clamp(next.y, minY, maxY) : (minY + maxY) / 2,
          width: nextWidth,
          height: nextHeight,
        }};
      }}

      function applyViewport(next = viewport) {{
        const normalized = normalizeViewport(next);
        viewport.x = normalized.x;
        viewport.y = normalized.y;
        viewport.width = normalized.width;
        viewport.height = normalized.height;
        svg.setAttribute("viewBox", viewport.x.toFixed(1) + " " + viewport.y.toFixed(1) + " " + viewport.width.toFixed(1) + " " + viewport.height.toFixed(1));
        const size = boardSize();
        const zoomText = Math.round((size.width / viewport.width) * 100) + "%";
        zoomValues.forEach((item) => {{
          item.textContent = zoomText;
        }});
        if (coordinateValue) {{
          coordinateValue.textContent = "XY " + Math.round(viewport.x) + "," + Math.round(viewport.y);
        }}
        if (minimap) {{
          const minimapWidth = 210;
          const minimapHeight = 132;
          const scale = Math.min(minimapWidth / worldWidth, minimapHeight / worldHeight);
          const offsetX = (minimapWidth - worldWidth * scale) / 2;
          const offsetY = (minimapHeight - worldHeight * scale) / 2;
          const viewX = offsetX + viewport.x * scale;
          const viewY = offsetY + viewport.y * scale;
          const viewWidth = viewport.width * scale;
          const viewHeight = viewport.height * scale;
          minimap.setAttribute("viewBox", "0 0 " + minimapWidth + " " + minimapHeight);
          minimap.innerHTML = "";
          const base = svgElement("rect");
          base.setAttribute("x", String(offsetX));
          base.setAttribute("y", String(offsetY));
          base.setAttribute("width", String(worldWidth * scale));
          base.setAttribute("height", String(worldHeight * scale));
          base.setAttribute("fill", "rgba(24, 35, 38, 0.05)");
          base.setAttribute("stroke", "rgba(24, 35, 38, 0.18)");
          minimap.appendChild(base);
          svg.querySelectorAll(".network-node").forEach((node) => {{
            const transform = node.getAttribute("transform") || "";
            const match = transform.match(/translate\\(([-0-9.]+)\\s+([-0-9.]+)\\)/);
            if (!match) {{
              return;
            }}
            const dot = svgElement("circle");
            dot.setAttribute("cx", String(offsetX + Number(match[1]) * scale));
            dot.setAttribute("cy", String(offsetY + Number(match[2]) * scale));
            dot.setAttribute("r", "2");
            dot.setAttribute("fill", laneColor(node.getAttribute("data-graph-lane") || "intent"));
            minimap.appendChild(dot);
          }});
          const viewportRect = svgElement("rect");
          viewportRect.setAttribute("x", String(viewX));
          viewportRect.setAttribute("y", String(viewY));
          viewportRect.setAttribute("width", String(viewWidth));
          viewportRect.setAttribute("height", String(viewHeight));
          viewportRect.setAttribute("fill", "rgba(80, 105, 110, 0.12)");
          viewportRect.setAttribute("stroke", "rgba(24, 35, 38, 0.62)");
          viewportRect.setAttribute("stroke-width", "1.4");
          minimap.appendChild(viewportRect);
        }}
      }}

      function fitViewport() {{
        const size = boardSize();
        const aspect = size.width / size.height;
        const paddedWidth = worldWidth + 180;
        const paddedHeight = worldHeight + 180;
        let viewWidth = paddedWidth;
        let viewHeight = viewWidth / aspect;
        if (viewHeight < paddedHeight) {{
          viewHeight = paddedHeight;
          viewWidth = viewHeight * aspect;
        }}
        applyViewport({{
          x: (worldWidth - viewWidth) / 2,
          y: (worldHeight - viewHeight) / 2,
          width: viewWidth,
          height: viewHeight,
        }});
      }}

      function transformCenter(selector) {{
        const element = svg.querySelector(selector);
        const transform = element?.getAttribute("transform") || "";
        const match = transform.match(/translate\\(([-0-9.]+)\\s+([-0-9.]+)\\)/);
        if (!match) {{
          return null;
        }}
        return {{
          x: Number(match[1]),
          y: Number(match[2]),
        }};
      }}

      function focusInitialViewport() {{
        const size = boardSize();
        const rootCenter = transformCenter('[data-graph-root="true"]');
        const systemCenter = transformCenter('[data-network-node-id="system::root"]');
        const centerX = rootCenter && systemCenter ? (rootCenter.x + systemCenter.x) / 2 : worldWidth * 0.45;
        const centerY = rootCenter && systemCenter ? (rootCenter.y + systemCenter.y) / 2 : worldHeight * 0.48;
        applyViewport({{
          x: centerX - size.width * 0.5,
          y: centerY - size.height * 0.5,
          width: size.width,
          height: size.height,
        }});
      }}

      function zoomAt(factor, clientX, clientY) {{
        const rect = board.getBoundingClientRect();
        const localX = clamp(clientX - rect.left, 0, Math.max(1, rect.width));
        const localY = clamp(clientY - rect.top, 0, Math.max(1, rect.height));
        const worldX = viewport.x + (localX / Math.max(1, rect.width)) * viewport.width;
        const worldY = viewport.y + (localY / Math.max(1, rect.height)) * viewport.height;
        const nextWidth = viewport.width / factor;
        const nextHeight = viewport.height / factor;
        applyViewport({{
          x: worldX - (localX / Math.max(1, rect.width)) * nextWidth,
          y: worldY - (localY / Math.max(1, rect.height)) * nextHeight,
          width: nextWidth,
          height: nextHeight,
        }});
      }}

      function normalizeWheelZoomFactor(event) {{
        const modeScale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
          ? 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
            ? boardSize().height
            : 1;
        const primaryDelta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
        const scaledDelta = primaryDelta * modeScale;
        if (!Number.isFinite(scaledDelta) || scaledDelta === 0) {{
          return 1;
        }}
        return clamp(Math.exp(-scaledDelta / 420), 0.72, 1.36);
      }}

      const wheelGestureIdleMs = 180;
      let wheelGesture = {{
        kind: "",
        lastAt: 0,
      }};

      function isGraphControlTarget(target) {{
        return Boolean(target?.closest?.(".graph-toolbar, .graph-minimap, .cad-status-bar"));
      }}

      function isModifierWheelZoom(event) {{
        return event.metaKey || event.altKey;
      }}

      function isTrackpadPinchWheel(event) {{
        return event.ctrlKey && event.deltaMode === WheelEvent.DOM_DELTA_PIXEL;
      }}

      function isDiscreteMouseWheel(event) {{
        if (event.deltaMode !== WheelEvent.DOM_DELTA_PIXEL || Math.abs(event.deltaX) > 0) {{
          return false;
        }}
        const delta = Math.abs(event.deltaY);
        return Number.isInteger(delta) && (delta === 100 || delta === 120 || delta === 240 || delta === 360 || (delta > 0 && delta % 120 === 0));
      }}

      function classifyWheelEvent(event) {{
        if (isModifierWheelZoom(event)) {{
          return "modifier-wheel-zoom";
        }}
        if (isTrackpadPinchWheel(event)) {{
          return "trackpad-pinch-zoom";
        }}
        if (event.deltaMode === WheelEvent.DOM_DELTA_LINE || event.deltaMode === WheelEvent.DOM_DELTA_PAGE || isDiscreteMouseWheel(event)) {{
          return "mouse-wheel-zoom";
        }}
        return "trackpad-scroll-pan";
      }}

      function resolveWheelGestureKind(event) {{
        const now = performance.now();
        const proposedKind = classifyWheelEvent(event);
        if (!wheelGesture.kind || now - wheelGesture.lastAt > wheelGestureIdleMs) {{
          wheelGesture.kind = proposedKind;
        }}
        wheelGesture.lastAt = now;
        board.dataset.graphLastWheelGesture = wheelGesture.kind;
        return wheelGesture.kind;
      }}

      function isZoomWheelGesture(kind) {{
        return kind === "modifier-wheel-zoom" || kind === "trackpad-pinch-zoom" || kind === "mouse-wheel-zoom";
      }}

      function panByWheel(event) {{
        const modeScale = event.deltaMode === WheelEvent.DOM_DELTA_LINE
          ? 16
          : event.deltaMode === WheelEvent.DOM_DELTA_PAGE
            ? boardSize().height
            : 1;
        const size = boardSize();
        const deltaX = event.deltaX * modeScale * (viewport.width / size.width);
        const deltaY = event.deltaY * modeScale * (viewport.height / size.height);
        applyViewport({{
          x: viewport.x + deltaX,
          y: viewport.y + deltaY,
          width: viewport.width,
          height: viewport.height,
        }});
      }}

      let safariGestureState = null;

      function beginSafariGestureZoom(event) {{
        event.preventDefault();
        safariGestureState = {{
          scale: event.scale || 1,
        }};
        board.dataset.graphLastWheelGesture = "trackpad-pinch-zoom";
      }}

      function updateSafariGestureZoom(event) {{
        if (!safariGestureState) {{
          beginSafariGestureZoom(event);
          return;
        }}
        event.preventDefault();
        const nextScale = event.scale || safariGestureState.scale || 1;
        const factor = nextScale / Math.max(0.01, safariGestureState.scale || 1);
        safariGestureState.scale = nextScale;
        if (Number.isFinite(factor) && factor > 0) {{
          zoomAt(clamp(factor, 0.72, 1.36), event.clientX, event.clientY);
        }}
      }}

      function endSafariGestureZoom(event) {{
        event.preventDefault();
        safariGestureState = null;
      }}

      toolbarButtons.forEach((button) => {{
        button.addEventListener("click", (event) => {{
          event.preventDefault();
          const action = button.getAttribute("data-graph-zoom");
          const rect = board.getBoundingClientRect();
          if (action === "in") {{
            zoomAt(1.24, rect.left + rect.width / 2, rect.top + rect.height / 2);
          }} else if (action === "out") {{
            zoomAt(0.82, rect.left + rect.width / 2, rect.top + rect.height / 2);
          }} else if (action === "fit") {{
            fitViewport();
          }}
        }});
      }});

      board.addEventListener("wheel", (event) => {{
        event.preventDefault();
        const wheelKind = resolveWheelGestureKind(event);
        if (isZoomWheelGesture(wheelKind)) {{
          zoomAt(normalizeWheelZoomFactor(event), event.clientX, event.clientY);
        }} else {{
          panByWheel(event);
        }}
      }}, {{ passive: false }});

      board.addEventListener("gesturestart", beginSafariGestureZoom, {{ passive: false }});
      board.addEventListener("gesturechange", updateSafariGestureZoom, {{ passive: false }});
      board.addEventListener("gestureend", endSafariGestureZoom, {{ passive: false }});

      let dragState = null;
      function beginDrag(event, pointerId, capturePointer = false) {{
        if (dragState || event.button !== 0 || isGraphControlTarget(event.target)) {{
          return;
        }}
        event.preventDefault();
        dragState = {{
          pointerId,
          clientX: event.clientX,
          clientY: event.clientY,
          x: viewport.x,
          y: viewport.y,
        }};
        board.classList.add("panning");
        if (capturePointer) {{
          board.setPointerCapture?.(pointerId);
        }}
      }}

      function moveDrag(event, pointerId) {{
        if (!dragState || dragState.pointerId !== pointerId) {{
          return;
        }}
        event.preventDefault();
        const size = boardSize();
        const dx = (event.clientX - dragState.clientX) * (viewport.width / size.width);
        const dy = (event.clientY - dragState.clientY) * (viewport.height / size.height);
        applyViewport({{
          x: dragState.x - dx,
          y: dragState.y - dy,
          width: viewport.width,
          height: viewport.height,
        }});
      }}

      function endDrag(event, pointerId, releasePointer = false) {{
        if (dragState && dragState.pointerId === pointerId) {{
          dragState = null;
          board.classList.remove("panning");
          if (releasePointer) {{
            board.releasePointerCapture?.(pointerId);
          }}
        }}
      }}

      board.addEventListener("pointerdown", (event) => beginDrag(event, event.pointerId, true));
      board.addEventListener("pointermove", (event) => moveDrag(event, event.pointerId));
      board.addEventListener("pointerup", (event) => endDrag(event, event.pointerId, true));
      board.addEventListener("pointercancel", (event) => endDrag(event, event.pointerId, true));
      board.addEventListener("mousedown", (event) => beginDrag(event, "mouse"));
      window.addEventListener("mousemove", (event) => moveDrag(event, "mouse"));
      window.addEventListener("mouseup", (event) => endDrag(event, "mouse"));
      window.addEventListener("resize", () => applyViewport(viewport));
      focusInitialViewport();
      return {{
        applyViewport,
        fitViewport,
        zoomAt,
        panByWheel,
        classifyWheelEvent,
        resolveWheelGestureKind,
      }};
    }}

    function renderNetworkGraph(config) {{
      const board = document.querySelector(config.boardSelector);
      const svg = document.querySelector(config.svgSelector);
      if (!board || !svg) {{
        return;
      }}

      const nodes = config.nodes || [];
      const sourceEdges = config.edges || [];
      const layout = {{
        left: 220,
        right: 220,
        top: 230,
        bottom: 150,
        layerGap: 320,
        rowGap: 174,
        rowGuideWidth: 180,
      }};
      const graphWorkspacePadding = {{
        left: 220,
        right: 260,
        top: 260,
        bottom: 240,
      }};

      const modelNodes = nodes.map((node, index) => {{
        return Object.assign({{}}, node, {{
          index: index,
          graphLayer: 0,
          layerRow: 0,
          anchorX: layout.left,
          anchorY: layout.top,
          x: layout.left,
          y: layout.top,
          vx: 0,
          vy: 0,
        }});
      }});

      const nodeMap = new Map(modelNodes.map((node) => [node.id, node]));
      const edges = sourceEdges.filter((edge) => nodeMap.has(edge.source) && nodeMap.has(edge.target));
      const outgoingById = new Map(modelNodes.map((node) => [node.id, []]));
      const incomingById = new Map(modelNodes.map((node) => [node.id, []]));
      for (const edge of edges) {{
        outgoingById.get(edge.source)?.push(edge.target);
        incomingById.get(edge.target)?.push(edge.source);
      }}
      const laneRank = (node) => {{
        const index = laneOrder.indexOf(node.lane || "system");
        return index >= 0 ? index : laneOrder.length;
      }};
      const compareNodes = (left, right) => {{
        const rootDelta = Number(Boolean(right.root)) - Number(Boolean(left.root));
        if (rootDelta !== 0) {{
          return rootDelta;
        }}
        const laneDelta = laneRank(left) - laneRank(right);
        if (laneDelta !== 0) {{
          return laneDelta;
        }}
        return String(left.label).localeCompare(String(right.label));
      }};
      const sortedNodes = modelNodes.slice().sort(compareNodes);
      const sortedNodeIds = (ids) => {{
        return Array.from(new Set(ids))
          .map((id) => nodeMap.get(id))
          .filter(Boolean)
          .sort(compareNodes)
          .map((node) => node.id);
      }};
      const unresolvedIncomingCount = (node, remainingIds) => {{
        let count = 0;
        for (const sourceId of incomingById.get(node.id) || []) {{
          if (remainingIds.has(sourceId)) {{
            count += 1;
          }}
        }}
        return count;
      }};

      function layoutBreadthFirstLayers() {{
        const layerById = new Map();
        const assignReachable = (seedNodes, startLayer) => {{
          const queue = [];
          seedNodes.sort(compareNodes).forEach((seed) => {{
            if (!layerById.has(seed.id)) {{
              layerById.set(seed.id, startLayer);
              queue.push(seed.id);
            }}
          }});
          for (let cursor = 0; cursor < queue.length; cursor += 1) {{
            const sourceId = queue[cursor];
            const sourceLayer = layerById.get(sourceId) || startLayer;
            for (const targetId of sortedNodeIds(outgoingById.get(sourceId) || [])) {{
              if (!layerById.has(targetId)) {{
                layerById.set(targetId, sourceLayer + 1);
                queue.push(targetId);
              }}
            }}
          }}
        }};

        const rootNodes = sortedNodes.filter((node) => (incomingById.get(node.id) || []).length === 0);
        assignReachable(rootNodes.length ? rootNodes : sortedNodes.filter((node) => node.root).slice(0, 1), 0);
        if (layerById.size === 0 && sortedNodes.length > 0) {{
          assignReachable([sortedNodes[0]], 0);
        }}

        while (layerById.size < modelNodes.length) {{
          const remaining = sortedNodes.filter((node) => !layerById.has(node.id));
          const remainingIds = new Set(remaining.map((node) => node.id));
          let seeds = remaining.filter((node) => unresolvedIncomingCount(node, remainingIds) === 0);
          if (seeds.length === 0) {{
            const minIncoming = Math.min(...remaining.map((node) => unresolvedIncomingCount(node, remainingIds)));
            seeds = remaining.filter((node) => unresolvedIncomingCount(node, remainingIds) === minIncoming).slice(0, 1);
          }}
          const nextLayer = Math.max(-1, ...Array.from(layerById.values())) + 1;
          assignReachable(seeds, nextLayer);
        }}

        const layerBuckets = new Map();
        for (const node of modelNodes) {{
          const layer = layerById.get(node.id) || 0;
          node.graphLayer = layer;
          if (!layerBuckets.has(layer)) {{
            layerBuckets.set(layer, []);
          }}
          layerBuckets.get(layer).push(node);
        }}
        const layerIndexes = Array.from(layerBuckets.keys()).sort((left, right) => left - right);
        const assignLayerRows = () => {{
          for (const layerIndex of layerIndexes) {{
            const bucket = layerBuckets.get(layerIndex) || [];
            bucket.forEach((node, rowIndex) => {{
              node.layerRow = rowIndex;
              node.anchorX = layout.left + layerIndex * layout.layerGap;
              node.anchorY = layout.top + rowIndex * layout.rowGap;
              node.x = node.anchorX;
              node.y = node.anchorY;
            }});
          }}
        }};
        for (const layerIndex of layerIndexes) {{
          (layerBuckets.get(layerIndex) || []).sort(compareNodes);
        }}
        assignLayerRows();
        for (const layerIndex of layerIndexes.filter((layer) => layer > 0)) {{
          const bucket = layerBuckets.get(layerIndex) || [];
          bucket.sort((left, right) => {{
            const parentAverage = (node) => {{
              const parents = (incomingById.get(node.id) || [])
                .map((sourceId) => nodeMap.get(sourceId))
                .filter((parent) => parent && Number.isFinite(parent.y));
              if (!parents.length) {{
                return Number.POSITIVE_INFINITY;
              }}
              return parents.reduce((sum, parent) => sum + parent.y, 0) / parents.length;
            }};
            const leftAverage = parentAverage(left);
            const rightAverage = parentAverage(right);
            if (leftAverage !== rightAverage) {{
              return leftAverage - rightAverage;
            }}
            return compareNodes(left, right);
          }});
          assignLayerRows();
        }}
        return {{
          layerBuckets,
          layerIndexes,
          layerCount: Math.max(1, Math.max(0, ...layerIndexes) + 1),
          maxLayerSize: Math.max(1, ...Array.from(layerBuckets.values()).map((bucket) => bucket.length)),
        }};
      }}

      const layeredLayout = layoutBreadthFirstLayers();
      const width = Math.max(config.minWidth || 1880, layout.left + layout.right + Math.max(0, layeredLayout.layerCount - 1) * layout.layerGap + 260);
      let height = Math.max(config.minHeight || 1040, layout.top + layout.bottom + Math.max(1, layeredLayout.maxLayerSize - 1) * layout.rowGap + 260);
      const edgeDirections = new Set(edges.map((edge) => edge.source + "->" + edge.target));
      const rowGuideCount = Math.max(1, layeredLayout.maxLayerSize);
      const graphContentBounds = (() => {{
        const bounds = {{
          left: layout.left - layout.rowGuideWidth,
          top: layout.top - 104,
          right: layout.left + Math.max(0, layeredLayout.layerCount - 1) * layout.layerGap + layout.rowGuideWidth,
          bottom: layout.top + Math.max(0, rowGuideCount - 1) * layout.rowGap + 92,
          bottomVisualTop: layout.top + Math.max(0, rowGuideCount - 1) * layout.rowGap - 72,
        }};
        let deepestNodeBottom = bounds.bottom;
        for (const node of modelNodes) {{
          const radius = nodeRadius(node);
          const labelLineCount = splitLabel(node.label, 20).length;
          const nodeWidth = Math.max(radius + 14, 132);
          const nodeTop = node.y - radius - 16;
          const nodeBottom = node.y + radius + 38 + labelLineCount * 15 + 18;
          bounds.left = Math.min(bounds.left, node.x - nodeWidth);
          bounds.top = Math.min(bounds.top, nodeTop);
          bounds.right = Math.max(bounds.right, node.x + nodeWidth);
          bounds.bottom = Math.max(bounds.bottom, nodeBottom);
          if (nodeBottom >= deepestNodeBottom) {{
            deepestNodeBottom = nodeBottom;
            bounds.bottomVisualTop = nodeTop;
          }}
        }}
        return bounds;
      }})();
      const graphPanBounds = {{
        left: graphContentBounds.left - graphWorkspacePadding.left,
        top: graphContentBounds.top - graphWorkspacePadding.top,
        right: graphContentBounds.right + graphWorkspacePadding.right,
        bottom: graphContentBounds.bottom + graphWorkspacePadding.bottom,
        bottomVisualTop: graphContentBounds.bottomVisualTop,
      }};
      height = Math.max(height, graphPanBounds.bottom + 48);
      svg.setAttribute("viewBox", "0 0 " + width + " " + height);
      svg.dataset.graphWorldWidth = String(width);
      svg.dataset.graphWorldHeight = String(height);
      svg.setAttribute("data-graph-content-bounds", JSON.stringify(graphContentBounds));
      svg.setAttribute("data-graph-pan-bounds", JSON.stringify(graphPanBounds));
      svg.setAttribute("data-graph-workspace-padding", JSON.stringify(graphWorkspacePadding));
      svg.setAttribute("data-graph-layout", "breadth-first-layers");
      svg.setAttribute("data-graph-layer-count", String(layeredLayout.layerCount));

      svg.replaceChildren();
      const defs = svgElement("defs");
      const marker = svgElement("marker");
      marker.setAttribute("id", "promise-graph-arrow");
      marker.setAttribute("viewBox", "0 0 10 10");
      marker.setAttribute("refX", "8.5");
      marker.setAttribute("refY", "5");
      marker.setAttribute("markerWidth", "8");
      marker.setAttribute("markerHeight", "8");
      marker.setAttribute("orient", "auto-start-reverse");
      const arrowPath = svgElement("path");
      arrowPath.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
      arrowPath.setAttribute("fill", "#4a4035");
      marker.appendChild(arrowPath);
      defs.appendChild(marker);
      svg.appendChild(defs);

      const layoutLayer = svgElement("g");
      layoutLayer.setAttribute("class", "layout-layer");
      const edgeLayer = svgElement("g");
      edgeLayer.setAttribute("class", "network-edge-layer");
      const nodeLayer = svgElement("g");
      nodeLayer.setAttribute("class", "network-node-layer");
      svg.appendChild(layoutLayer);
      svg.appendChild(edgeLayer);
      svg.appendChild(nodeLayer);

      for (let rowIndex = 0; rowIndex < rowGuideCount; rowIndex += 1) {{
        const guide = svgElement("line");
        const y = layout.top + rowIndex * layout.rowGap;
        guide.setAttribute("class", "layer-row-guide");
        guide.setAttribute("x1", String(layout.left - layout.rowGuideWidth));
        guide.setAttribute("x2", String(layout.left + Math.max(0, layeredLayout.layerCount - 1) * layout.layerGap + layout.rowGuideWidth));
        guide.setAttribute("y1", String(y));
        guide.setAttribute("y2", String(y));
        layoutLayer.appendChild(guide);
      }}

      const renderedEdges = [];
      edges.forEach((edge, index) => {{
        const source = nodeMap.get(edge.source);
        const target = nodeMap.get(edge.target);
        const sourceRadius = nodeRadius(source);
        const targetRadius = nodeRadius(target);
        const dx = target.x - source.x;
        const dy = target.y - source.y;
        const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
        const nx = dx / distance;
        const ny = dy / distance;
        const startX = source.x + nx * (sourceRadius + 2);
        const startY = source.y + ny * (sourceRadius + 2);
        const endX = target.x - nx * (targetRadius + 10);
        const endY = target.y - ny * (targetRadius + 10);
        const sourceRegion = graphRegion(source);
        const targetRegion = graphRegion(target);
        let pathData = "";
        let labelX = (startX + endX) / 2;
        let labelY = (startY + endY) / 2;
        if (source.graphLayer !== target.graphLayer) {{
          const direction = target.x >= source.x ? 1 : -1;
          const sourceRailX = source.x + direction * (sourceRadius + 38);
          const targetRailX = target.x - direction * (targetRadius + 38);
          const railOffset = ((index % 5) - 2) * 12;
          const midY = (startY + endY) / 2 + railOffset;
          pathData = "M " + startX.toFixed(1) + " " + startY.toFixed(1)
            + " L " + sourceRailX.toFixed(1) + " " + startY.toFixed(1)
            + " L " + sourceRailX.toFixed(1) + " " + midY.toFixed(1)
            + " L " + targetRailX.toFixed(1) + " " + midY.toFixed(1)
            + " L " + targetRailX.toFixed(1) + " " + endY.toFixed(1)
            + " L " + endX.toFixed(1) + " " + endY.toFixed(1);
          labelX = (sourceRailX + targetRailX) / 2;
          labelY = midY;
        }} else {{
          const normalX = -ny;
          const normalY = nx;
          const reciprocal = edgeDirections.has(edge.target + "->" + edge.source);
          const pairSign = edge.source < edge.target ? 1 : -1;
          const curve = pairSign * (reciprocal ? 78 : 40) + ((index % 3) - 1) * 10;
          const controlX = (startX + endX) / 2 + normalX * curve;
          const controlY = (startY + endY) / 2 + normalY * curve;
          pathData = "M " + startX.toFixed(1) + " " + startY.toFixed(1) + " Q " + controlX.toFixed(1) + " " + controlY.toFixed(1) + " " + endX.toFixed(1) + " " + endY.toFixed(1);
          labelX = controlX;
          labelY = controlY;
        }}

        const path = svgElement("path");
        const edgeClasses = ["network-edge"];
        if (edge.kind === "conflict") {{
          edgeClasses.push("conflict-edge");
        }}
        if (edge.kind === "graph-issue") {{
          edgeClasses.push("graph-issue-edge");
        }}
        path.setAttribute("class", edgeClasses.join(" "));
        path.setAttribute("data-source", edge.source);
        path.setAttribute("data-target", edge.target);
        path.setAttribute("data-edge-kind", edge.kind || "relation");
        if (edge.analysisIssue) {{
          path.setAttribute("data-analysis-issue", edge.analysisIssue);
        }}
        if (edge.severity) {{
          path.setAttribute("data-conflict-severity", edge.severity);
        }}
        path.setAttribute("data-layer-route", source.graphLayer !== target.graphLayer ? "rail" : "arc");
        path.setAttribute("marker-end", "url(#promise-graph-arrow)");
        path.setAttribute("d", pathData);
        edgeLayer.appendChild(path);
        renderedEdges.push(path);

        if (edges.length <= 42) {{
          const label = svgElement("text");
          label.setAttribute("class", "network-edge-label");
          label.setAttribute("x", String(labelX));
          label.setAttribute("y", String(labelY - 7));
          label.setAttribute("text-anchor", "middle");
          label.textContent = shortText(config.edgeLabelFormatter ? config.edgeLabelFormatter(edge) : edge.label, 34);
          edgeLayer.appendChild(label);
        }}
      }});

      const renderedNodes = [];
      for (const node of modelNodes) {{
        const radius = nodeRadius(node);
        const group = svgElement("g");
        group.setAttribute("class", "network-node");
        group.setAttribute("tabindex", "0");
        group.setAttribute("role", "button");
        group.setAttribute("data-network-node-id", node.id);
        group.setAttribute("data-graph-lane", node.lane || "system");
        group.setAttribute("data-graph-region", graphRegion(node));
        group.setAttribute("data-graph-root", node.root ? "true" : "false");
        group.setAttribute("data-graph-layer", String(node.graphLayer));
        group.setAttribute("data-layer-row", String(node.layerRow));
        group.setAttribute("transform", "translate(" + node.x.toFixed(1) + " " + node.y.toFixed(1) + ")");
        if (node.lane === "intent" && node.root) {{
          group.classList.add("root-intent-node");
        }}
        if (node.lane === "intent" && Number(node.conflictCount || 0) > 0) {{
          group.classList.add("conflicted-intent-node");
        }}
        if (node.lane === "intent" && Number(node.graphIssueCount || 0) > 0) {{
          group.classList.add("graph-issue-node");
        }}

        const title = svgElement("title");
        title.textContent = node.label + " · " + (node.summary || "No summary provided.");
        group.appendChild(title);

        const circle = svgElement("circle");
        circle.setAttribute("r", String(radius));
        circle.setAttribute("fill", laneColor(node.lane || "system"));
        group.appendChild(circle);

        const token = svgElement("text");
        token.setAttribute("class", "network-node-token");
        token.setAttribute("text-anchor", "middle");
        token.setAttribute("dominant-baseline", "middle");
        token.textContent = nodeToken(node);
        group.appendChild(token);

        const labelLines = splitLabel(node.label, 20);
        labelLines.forEach((line, lineIndex) => {{
          const label = svgElement("text");
          label.setAttribute("class", "network-node-label");
          label.setAttribute("text-anchor", "middle");
          label.setAttribute("x", "0");
          label.setAttribute("y", String(radius + 22 + lineIndex * 15));
          label.textContent = line;
          group.appendChild(label);
        }});

        const meta = svgElement("text");
        meta.setAttribute("class", "network-node-meta");
        meta.setAttribute("text-anchor", "middle");
        meta.setAttribute("x", "0");
        meta.setAttribute("y", String(radius + 24 + labelLines.length * 15));
        meta.textContent = (node.kind || node.lane || "node") + " · " + (node.anchor || node.nodeCount || "");
        group.appendChild(meta);

        function activate() {{
          const connectedIds = new Set([node.id]);
          for (const edge of edges) {{
            if (edge.source === node.id || edge.target === node.id) {{
              connectedIds.add(edge.source);
              connectedIds.add(edge.target);
            }}
          }}
          renderedNodes.forEach((entry) => {{
            entry.group.classList.toggle("active", entry.node.id === node.id);
            entry.group.classList.toggle("faded", !connectedIds.has(entry.node.id));
          }});
          renderedEdges.forEach((path) => {{
            const connected = path.dataset.source === node.id || path.dataset.target === node.id;
            path.classList.toggle("connected", connected);
          }});
        }}

        function clear() {{
          renderedNodes.forEach((entry) => {{
            entry.group.classList.remove("active", "faded");
          }});
          renderedEdges.forEach((path) => path.classList.remove("connected"));
        }}

        group.addEventListener("mouseenter", activate);
        group.addEventListener("focus", activate);
        group.addEventListener("mouseleave", clear);
        group.addEventListener("blur", clear);
        group.addEventListener("click", () => {{
          activate();
          if (config.onNodeClick) {{
            config.onNodeClick(node);
          }}
        }});
        group.addEventListener("keydown", (event) => {{
          if (event.key === "Enter" || event.key === " ") {{
            event.preventDefault();
            group.dispatchEvent(new MouseEvent("click"));
          }}
        }});

        nodeLayer.appendChild(group);
        renderedNodes.push({{ node: node, group: group }});
      }}
      board.graphViewportController = createGraphViewportController(board, svg, width, height, graphPanBounds);
    }}

    function initFullGraph() {{
      renderNetworkGraph({{
        boardSelector: ".full-graph-board",
        svgSelector: ".full-graph-network",
        nodes: graph.nodes,
        edges: graph.edges,
        minWidth: 1720,
        minHeight: 1040,
        edgeLabelFormatter: (edge) => edge.label,
      }});
    }}

    initFullGraph();
  </script>
</body>
</html>
"""


def _render_full_graph_section(graph: dict[str, Any]) -> str:
    fallback_items = "\n".join(
        f'<li data-node-id="{html_lib.escape(node["id"])}">{html_lib.escape(node["label"])} · {html_lib.escape(node["kind"])} · {html_lib.escape(node.get("anchor") or node["lane"])}</li>'
        for node in graph["nodes"]
    )
    return f"""<section class="network-graph-shell">
  <div class="network-graph-header">
    <div>
      <h2>Layered Directed Graph</h2>
      <p>Nodes with no incoming parent edge form the first layer, then outgoing edges are expanded breadth-first into the next layers. Node color and metadata preserve intent, system, field, function, and verify type, while cross-layer edges use rail routing. The graph keeps intent conflict edges explicit. Cycles and reciprocal edges remain curved directed paths.</p>
    </div>
    <span class="scale-tag">{graph['nodeCount']} nodes · {graph['edgeCount']} directed edges</span>
  </div>
  <div class="network-graph-board full-graph-board" data-graph-trackpad-pan="true" data-graph-pinch-zoom="true" data-graph-mouse-wheel-zoom="true" data-graph-modifier-wheel-zoom="true">
    <div class="graph-toolbar" aria-label="Graph zoom controls">
      <button type="button" data-graph-zoom="out" aria-label="Zoom out">−</button>
      <button type="button" data-graph-zoom="in" aria-label="Zoom in">+</button>
      <button type="button" data-graph-zoom="fit" aria-label="Fit graph">Fit</button>
      <span class="graph-zoom-value" data-graph-zoom-value>100%</span>
    </div>
    <div class="graph-minimap" aria-label="Graph minimap">
      <svg class="graph-minimap-svg" role="img" aria-label="Promise graph minimap"></svg>
    </div>
    <div class="cad-status-bar" aria-label="Graph workspace status">
      <span><strong>MODEL</strong> SPACE</span>
      <span>BFS LAYERED</span>
      <span>{graph['nodeCount']}N / {graph['edgeCount']}E</span>
      <span data-graph-coordinates>XY 0,0</span>
      <span data-graph-zoom-value>100%</span>
    </div>
    <svg class="network-graph full-graph-network" role="img" aria-label="Directed Promise graph"></svg>
    <ul class="graph-fallback" aria-label="Promise graph node index">
      {fallback_items}
    </ul>
  </div>
</section>"""


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
