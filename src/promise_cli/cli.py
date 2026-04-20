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
SKILL_NAME = "promise-authoring"
GRAPH_LANE_ORDER = ("system", "field", "function", "verify")
GRAPH_LANE_TITLES = {
    "system": "System",
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
        "profile": getattr(args, "profile", "full"),
        "json_requested": getattr(args, "json", False),
        "raw_source": None,
        "formatted_source": None,
        "graph_html": None,
        "graph_model": None,
        "graph_node_count": 0,
        "graph_edge_count": 0,
        "graph_view_mode": None,
        "graph_composition": None,
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
                    "details": "Running tooling verify from the installed promise-authoring skill.",
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
      grid-template-columns: minmax(220px, 0.78fr) repeat(3, minmax(0, 1fr));
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
      grid-template-columns: minmax(220px, 0.78fr) repeat(3, minmax(0, 1fr));
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
