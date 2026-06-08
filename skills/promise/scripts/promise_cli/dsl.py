from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import re
import shlex
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0.0"
BOOLEAN_VALUES = {"true": True, "false": False}
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9*._:/-]+$")
VERIFICATION_METHODS = {
    "unit",
    "integration",
    "e2e",
    "static-check",
    "review",
    "property-test",
}
VERIFICATION_KINDS = {"field", "function", "cross-cutting"}
INTENT_PRIORITIES = {"must", "should", "may"}
INTENT_STATUSES = {"active", "changed", "deprecated"}
INTENT_RELATIONS = {
    "motivates",
    "constrains",
    "explains",
    "verifies",
    "conflicts",
    "refines",
    "supports",
    "requires",
    "blocks",
}
INTENT_CONFLICT_SEVERITIES = {"blocking", "tension", "advisory"}
INTENT_NEGATIVE_RELATIONS = {"conflicts", "blocks"}
INTENT_POSITIVE_RELATIONS = INTENT_RELATIONS - INTENT_NEGATIVE_RELATIONS
INTENT_REQUIREMENT_KINDS = {"requires", "forbids", "prefers", "accepts"}
INTENT_REQUIREMENT_REQUIRED_KEYS = {"subject", "predicate", "object"}
INTENT_REQUIREMENT_OPERATION_KEYS = {"actor", "action", "resource"}
INTENT_REQUIREMENT_SEMANTIC_KEYS = {"scope", "effect", "constraint"}
INTENT_TERM_KINDS = {"action", "effect", "constraint", "scope"}
INTENT_CYCLE_KINDS = {"feedback"}
INTENT_GRAPH_RELATIONS = INTENT_RELATIONS | {
    "actor",
    "action",
    "constraint",
    "contains",
    "disjoint",
    "effect",
    "maps",
    "operates",
    "opposite",
    "over",
    "scope",
}
INTENT_GRAPH_SYMMETRIC_RELATIONS = {"conflicts", "disjoint", "opposite"}
INTENT_GRAPH_STRICT_RELATIONS = {
    "constrains",
    "contains",
    "explains",
    "maps",
    "motivates",
    "refines",
    "supports",
    "verifies",
}
INTENT_RESOURCE_KINDS = {
    "actor",
    "artifact",
    "capability",
    "concept",
    "data",
    "entity",
    "external",
    "system",
    "ui",
    "workflow",
}
PRIMITIVE_FIELD_TYPES = {
    "boolean",
    "datetime",
    "integer",
    "json",
    "number",
    "path",
    "string",
    "text",
}
ENUM_TYPE_RE = re.compile(r"^enum\(([^)]+)\)$")


class PromiseParseError(Exception):
    """Raised when the Promise DSL cannot be parsed."""


class PromiseExpressionError(Exception):
    """Raised when a Promise expression cannot be parsed or typed."""


@dataclass
class LintIssue:
    code: str
    message: str
    severity: str = "error"


def parse_file(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    return parse_text(file_path.read_text(encoding="utf-8"))


def parse_text(text: str) -> dict[str, Any]:
    parser = _PromiseParser(text)
    return parser.parse()


def lint_spec(spec: dict[str, Any], profile: str = "full") -> list[LintIssue]:
    issues: list[LintIssue] = []

    meta = spec.get("meta", {})
    intent_promises = spec.get("intentPromises", [])
    intent_resources = spec.get("intentResources", [])
    intent_terms = spec.get("intentTerms", [])
    intent_cycles = spec.get("intentCycles", [])
    type_promises = spec.get("typePromises", [])
    field_promises = spec.get("fieldPromises", [])
    function_promises = spec.get("functionPromises", [])
    verification_promises = spec.get("verificationPromises", [])

    for key in ("title", "domain", "version", "status", "summary"):
        if not meta.get(key):
            issues.append(
                LintIssue(
                    "meta-missing-key",
                    f"Meta block is missing required key '{key}'.",
                )
            )

    if not field_promises:
        issues.append(
            LintIssue(
                "structure-missing-field-layer",
                "System Promise must declare at least one field promise.",
            )
        )
    if not function_promises:
        issues.append(
            LintIssue(
                "structure-missing-function-layer",
                "System Promise must declare at least one function promise.",
            )
        )
    if not verification_promises:
        issues.append(
            LintIssue(
                "structure-missing-verification-layer",
                "System Promise must declare at least one verification promise.",
            )
        )

    intent_promise_names = _collect_unique_names(
        intent_promises, "name", "intent-promise-duplicate-name", issues
    )
    intent_resource_names = _collect_unique_names(
        intent_resources, "name", "intent-resource-duplicate-name", issues
    )
    intent_term_names = _collect_unique_names(
        intent_terms, "name", "intent-term-duplicate-name", issues
    )
    intent_cycle_names = _collect_unique_names(
        intent_cycles, "name", "intent-cycle-duplicate-name", issues
    )
    type_promise_names = _collect_unique_names(
        type_promises, "name", "type-promise-duplicate-name", issues
    )
    field_promise_names = _collect_unique_names(
        field_promises, "name", "field-promise-duplicate-name", issues
    )
    function_promise_names = _collect_unique_names(
        function_promises, "name", "function-promise-duplicate-name", issues
    )
    verification_promise_names = _collect_unique_names(
        verification_promises,
        "name",
        "verification-promise-duplicate-name",
        issues,
    )

    intent_requirement_ids: set[str] = set()
    intent_resource_map_targets: list[tuple[str, str]] = []
    intent_term_map_targets: list[tuple[str, str]] = []
    field_refs: set[str] = set()
    object_refs: set[str] = set()
    state_refs: set[str] = set()
    clause_ids: set[str] = set()
    declared_type_names = set(type_promise_names)
    type_lookup = {type_promise["name"]: type_promise for type_promise in type_promises}

    for intent_resource in intent_resources:
        resource_name = intent_resource["name"]
        if intent_resource.get("kind") not in INTENT_RESOURCE_KINDS:
            issues.append(
                LintIssue(
                    "intent-resource-invalid-kind",
                    f"Intent resource '{resource_name}' uses unknown kind '{intent_resource.get('kind')}'.",
                )
            )
        if not intent_resource.get("summary"):
            issues.append(
                LintIssue(
                    "intent-resource-missing-summary",
                    f"Intent resource '{resource_name}' is missing summary.",
                )
            )
        for alias in intent_resource.get("aliases", []):
            if not SAFE_TOKEN_RE.match(str(alias)):
                issues.append(
                    LintIssue(
                        "intent-resource-invalid-alias",
                        f"Intent resource '{resource_name}' uses non-canonical alias '{alias}'.",
                    )
                )
        for resource_map in intent_resource.get("maps", []):
            relation = resource_map.get("relation")
            if relation not in INTENT_RELATIONS:
                issues.append(
                    LintIssue(
                        "intent-resource-invalid-relation",
                        f"Intent resource '{resource_name}' uses unknown map relation '{relation}'.",
                    )
                )
            if resource_map.get("target"):
                intent_resource_map_targets.append((resource_name, resource_map["target"]))

    intent_term_index = {term["name"]: term for term in intent_terms}
    intent_term_names_by_kind = _intent_term_names_by_kind(intent_terms)
    for intent_term in intent_terms:
        term_name = intent_term["name"]
        term_kind = intent_term.get("kind")
        if term_kind not in INTENT_TERM_KINDS:
            issues.append(
                LintIssue(
                    "intent-term-invalid-kind",
                    f"Intent term '{term_name}' uses unknown kind '{term_kind}'.",
                )
            )
        if not intent_term.get("summary"):
            issues.append(
                LintIssue(
                    "intent-term-missing-summary",
                    f"Intent term '{term_name}' is missing summary.",
                )
            )
        for alias in intent_term.get("aliases", []):
            if not SAFE_TOKEN_RE.match(str(alias)):
                issues.append(
                    LintIssue(
                        "intent-term-invalid-alias",
                        f"Intent term '{term_name}' uses non-canonical alias '{alias}'.",
                    )
                )
        parent = intent_term.get("parent")
        if parent:
            parent_term = intent_term_index.get(parent)
            if parent == term_name:
                issues.append(
                    LintIssue(
                        "intent-term-self-parent",
                        f"Intent term '{term_name}' cannot be its own parent.",
                    )
                )
            elif parent_term is None:
                issues.append(
                    LintIssue(
                        "intent-term-unknown-parent",
                        f"Intent term '{term_name}' declares unknown parent term '{parent}'.",
                    )
                )
            elif parent_term.get("kind") != term_kind:
                issues.append(
                    LintIssue(
                        "intent-term-parent-kind-mismatch",
                        f"Intent term '{term_name}' parent '{parent}' must use the same kind.",
                    )
                )
        if intent_term.get("disjoint") and term_kind != "scope":
            issues.append(
                LintIssue(
                    "intent-term-disjoint-non-scope",
                    f"Intent term '{term_name}' can declare disjoint terms only when kind is scope.",
                )
            )
        for disjoint in intent_term.get("disjoint", []):
            target_term = intent_term_index.get(disjoint)
            if disjoint == term_name:
                issues.append(
                    LintIssue(
                        "intent-term-self-disjoint",
                        f"Intent scope term '{term_name}' cannot be disjoint with itself.",
                    )
                )
            elif target_term is None:
                issues.append(
                    LintIssue(
                        "intent-term-unknown-disjoint",
                        f"Intent scope term '{term_name}' declares unknown disjoint term '{disjoint}'.",
                    )
                )
            elif target_term.get("kind") != "scope":
                issues.append(
                    LintIssue(
                        "intent-term-disjoint-kind-mismatch",
                        f"Intent scope term '{term_name}' disjoint target '{disjoint}' must also be a scope.",
                    )
                )
        for opposite in intent_term.get("opposites", []):
            target_term = intent_term_index.get(opposite)
            if opposite == term_name:
                issues.append(
                    LintIssue(
                        "intent-term-self-opposite",
                        f"Intent term '{term_name}' cannot be opposite to itself.",
                    )
                )
            elif target_term is None:
                issues.append(
                    LintIssue(
                        "intent-term-unknown-opposite",
                        f"Intent term '{term_name}' declares unknown opposite term '{opposite}'.",
                    )
                )
            elif target_term.get("kind") != term_kind:
                issues.append(
                    LintIssue(
                        "intent-term-opposite-kind-mismatch",
                        f"Intent term '{term_name}' opposite '{opposite}' must use the same kind.",
                    )
                )
        for term_map in intent_term.get("maps", []):
            relation = term_map.get("relation")
            if relation not in INTENT_RELATIONS:
                issues.append(
                    LintIssue(
                        "intent-term-invalid-relation",
                        f"Intent term '{term_name}' uses unknown map relation '{relation}'.",
                    )
                )
            if term_map.get("target"):
                intent_term_map_targets.append((term_name, term_map["target"]))
    _lint_intent_term_parent_cycles(intent_terms, issues)

    declared_intent_conflict_pairs: set[tuple[str, str]] = set()
    for intent_promise in intent_promises:
        intent_name = intent_promise["name"]
        if not intent_promise.get("statement"):
            issues.append(
                LintIssue(
                    "intent-missing-statement",
                    f"Intent promise '{intent_name}' is missing statement.",
                )
            )
        if not intent_promise.get("rationale"):
            issues.append(
                LintIssue(
                    "intent-missing-rationale",
                    f"Intent promise '{intent_name}' is missing rationale.",
                )
            )
        if intent_promise.get("priority") not in INTENT_PRIORITIES:
            issues.append(
                LintIssue(
                    "intent-invalid-priority",
                    f"Intent promise '{intent_name}' uses unknown priority '{intent_promise.get('priority')}'.",
                )
            )
        if intent_promise.get("status") not in INTENT_STATUSES:
            issues.append(
                LintIssue(
                    "intent-invalid-status",
                    f"Intent promise '{intent_name}' uses unknown status '{intent_promise.get('status')}'.",
                )
            )
        if intent_promise.get("root") and intent_promise.get("parents"):
            issues.append(
                LintIssue(
                    "intent-root-has-parent",
                    f"Root intent promise '{intent_name}' must not declare a parent.",
                )
            )
        if not intent_promise.get("root") and intent_promises and not intent_promise.get("parents"):
            issues.append(
                LintIssue(
                    "intent-missing-parent",
                    f"Intent promise '{intent_name}' must declare a parent intent unless it is the root intent.",
                )
            )
        if len(intent_promise.get("parents", [])) > 1:
            issues.append(
                LintIssue(
                    "intent-multiple-parents",
                    f"Intent promise '{intent_name}' declares more than one parent; the intent hierarchy must remain a tree.",
                )
            )
        for parent in intent_promise.get("parents", []):
            relation = parent.get("relation")
            if relation not in INTENT_RELATIONS:
                issues.append(
                    LintIssue(
                        "intent-invalid-parent-relation",
                        f"Intent promise '{intent_name}' uses unknown parent relation '{relation}'.",
                    )
                )
            parent_target = parent.get("target")
            if parent_target == intent_name:
                issues.append(
                    LintIssue(
                        "intent-self-parent",
                        f"Intent promise '{intent_name}' cannot be its own parent.",
                    )
                )
            if parent_target not in intent_promise_names:
                issues.append(
                    LintIssue(
                        "intent-unknown-parent",
                        f"Intent promise '{intent_name}' declares unknown parent intent '{parent_target}'.",
                    )
                )
        if not intent_promise.get("maps") and not intent_promise.get("requirements"):
            issues.append(
                LintIssue(
                    "intent-missing-maps",
                    f"Intent promise '{intent_name}' must map to at least one Promise item or declare structured requirements.",
                )
            )
        for intent_map in intent_promise.get("maps", []):
            relation = intent_map.get("relation")
            if relation not in INTENT_RELATIONS:
                issues.append(
                    LintIssue(
                        "intent-invalid-relation",
                        f"Intent promise '{intent_name}' uses unknown relation '{relation}'.",
                    )
                )
        _lint_intent_requirements(
            intent_promise,
            intent_requirement_ids,
            intent_resource_names,
            intent_term_names_by_kind,
            issues,
        )
        for intent_conflict in intent_promise.get("conflicts", []):
            target = intent_conflict.get("target")
            severity = intent_conflict.get("severity")
            if severity not in INTENT_CONFLICT_SEVERITIES:
                issues.append(
                    LintIssue(
                        "intent-invalid-conflict-severity",
                        f"Intent promise '{intent_name}' declares conflict severity '{severity}', which is not supported.",
                    )
                )
            if target == intent_name:
                issues.append(
                    LintIssue(
                        "intent-self-conflict",
                        f"Intent promise '{intent_name}' cannot conflict with itself.",
                    )
                )
            if target not in intent_promise_names:
                issues.append(
                    LintIssue(
                        "intent-unknown-conflict-target",
                        f"Intent promise '{intent_name}' declares conflict with unknown intent '{target}'.",
                    )
                )
            if not intent_conflict.get("reason"):
                issues.append(
                    LintIssue(
                        "intent-conflict-missing-reason",
                        f"Intent promise '{intent_name}' declares conflict with '{target}' without a reason.",
                    )
                )
            if severity == "blocking" and not intent_conflict.get("resolution"):
                issues.append(
                    LintIssue(
                        "intent-unresolved-blocking-conflict",
                        f"Intent promise '{intent_name}' declares a blocking conflict with '{target}' without a resolution.",
                    )
                )
            if target:
                conflict_pair = tuple(sorted((intent_name, target)))
                if conflict_pair in declared_intent_conflict_pairs:
                    issues.append(
                        LintIssue(
                            "intent-duplicate-conflict",
                            f"Intent conflict between '{conflict_pair[0]}' and '{conflict_pair[1]}' is declared more than once.",
                        )
                    )
                declared_intent_conflict_pairs.add(conflict_pair)

    if intent_promises:
        root_intents = [intent_promise["name"] for intent_promise in intent_promises if intent_promise.get("root")]
        if not root_intents:
            issues.append(
                LintIssue(
                    "intent-missing-root",
                    "Intent tree must declare exactly one root intent.",
                )
            )
        if len(root_intents) > 1:
            issues.append(
                LintIssue(
                    "intent-multiple-roots",
                    f"Intent tree declares multiple root intents: {', '.join(root_intents)}.",
                )
            )
        _lint_intent_parent_cycles(intent_promises, issues)

    for type_promise in type_promises:
        type_name = type_promise["name"]
        if type_name in PRIMITIVE_FIELD_TYPES:
            issues.append(
                LintIssue(
                    "type-conflicts-with-primitive",
                    f"Type promise '{type_name}' conflicts with a built-in primitive type.",
                )
            )
        if not type_promise.get("summary"):
            issues.append(
                LintIssue(
                    "type-missing-summary",
                    f"Type promise '{type_name}' is missing summary.",
                )
            )
        base_type = type_promise.get("base")
        if base_type not in PRIMITIVE_FIELD_TYPES:
            issues.append(
                LintIssue(
                    "type-unknown-base",
                    f"Type promise '{type_name}' declares unknown base type '{base_type}'.",
                )
            )

    for field_promise in field_promises:
        object_name = field_promise["object"]
        object_refs.add(object_name)
        fields = field_promise.get("fields", [])
        states = field_promise.get("states", [])

        if not field_promise.get("summary"):
            issues.append(
                LintIssue(
                    "field-missing-summary",
                    f"Field promise '{field_promise['name']}' is missing summary.",
                )
            )
        if not fields:
            issues.append(
                LintIssue(
                    "field-missing-fields",
                    f"Field promise '{field_promise['name']}' has no fields.",
                )
            )
        if _field_requires_invariant_coverage(field_promise) and not field_promise.get("invariants"):
            issues.append(
                LintIssue(
                    "field-missing-invariant-coverage",
                    f"Field promise '{field_promise['name']}' has state or multi-field complexity but no invariant coverage. Add an invariant only if it captures object-specific truth.",
                    severity="warning",
                )
            )
        if _field_requires_forbid_coverage(field_promise) and not field_promise.get("forbiddenImplicitState"):
            issues.append(
                LintIssue(
                    "field-missing-forbid-coverage",
                    f"Field promise '{field_promise['name']}' has state or multi-field complexity but no explicit forbid coverage. Add a forbid only if it blocks a real drift path.",
                    severity="warning",
                )
            )

        seen_field_names: set[str] = set()
        for field in fields:
            field_name = field["name"]
            field_type = field["type"]
            if field_name in seen_field_names:
                issues.append(
                    LintIssue(
                        "field-duplicate-name",
                        f"Field promise '{field_promise['name']}' declares duplicate field '{field_name}'.",
                    )
                )
            seen_field_names.add(field_name)
            if not _is_known_field_type(field_type, declared_type_names):
                issues.append(
                    LintIssue(
                        "field-unknown-type",
                        f"Field '{object_name}.{field_name}' uses unknown type '{field_type}'.",
                    )
                )
            field_refs.add(f"{object_name}.{field_name}")

        initial_states = 0
        state_values: set[str] = set()
        for state in states:
            value = state["value"]
            state_refs.add(f"{object_name}.{value}")
            if value in state_values:
                issues.append(
                    LintIssue(
                        "state-duplicate-value",
                        f"Field promise '{field_promise['name']}' declares duplicate state '{value}'.",
                    )
                )
            state_values.add(value)
            if state.get("initial"):
                initial_states += 1

        if states and initial_states == 0:
            issues.append(
                LintIssue(
                    "state-missing-initial",
                    f"Field promise '{field_promise['name']}' defines states but none is marked initial.",
                )
            )
        if initial_states > 1:
            issues.append(
                LintIssue(
                    "state-multiple-initial",
                    f"Field promise '{field_promise['name']}' defines more than one initial state.",
                )
            )

        for state in states:
            for target in state.get("transitions", []):
                if target not in state_values:
                    issues.append(
                        LintIssue(
                            "state-unknown-transition",
                            f"State '{state['value']}' in '{field_promise['name']}' transitions to unknown state '{target}'.",
                        )
                    )

        field_lookup = {field["name"]: field for field in fields}
        state_field = _select_state_field_for_lint(field_promise)
        _lint_field_clause_expressions(
            field_promise,
            field_lookup,
            state_field,
            state_values,
            type_lookup,
            issues,
        )

        for clause_group in (
            field_promise.get("invariants", []),
            field_promise.get("globalConstraints", []),
            field_promise.get("forbiddenImplicitState", []),
        ):
            _collect_clause_ids(clause_group, clause_ids, issues, field_promise["name"])

    known_refs = (
        set(intent_promise_names)
        | set(intent_resource_names)
        | set(intent_term_names)
        | set(intent_cycle_names)
        | set(type_promise_names)
        | set(field_promise_names)
        | set(function_promise_names)
        | set(verification_promise_names)
        | object_refs
        | field_refs
        | state_refs
        | clause_ids
        | intent_requirement_ids
    )

    for function_promise in function_promises:
        if not function_promise.get("summary"):
            issues.append(
                LintIssue(
                    "function-missing-summary",
                    f"Function promise '{function_promise['name']}' is missing summary.",
                )
            )
        if not function_promise.get("triggers"):
            issues.append(
                LintIssue(
                    "function-missing-trigger",
                    f"Function promise '{function_promise['name']}' has no triggers.",
                )
            )
        if not function_promise.get("successResults"):
            issues.append(
                LintIssue(
                    "function-missing-ensure",
                    f"Function promise '{function_promise['name']}' has no success results.",
                )
            )
        if _function_requires_forbid_coverage(function_promise) and not function_promise.get("forbidden"):
            issues.append(
                LintIssue(
                    "function-missing-forbid-coverage",
                    f"Function promise '{function_promise['name']}' mutates or rejects state but has no explicit forbid coverage. Add a forbid only if it blocks a real behavioral drift path.",
                    severity="warning",
                )
            )

        for dependency in function_promise.get("dependsOn", []):
            if dependency not in field_promise_names:
                issues.append(
                    LintIssue(
                        "function-unknown-dependency",
                        f"Function promise '{function_promise['name']}' depends on unknown field promise '{dependency}'.",
                    )
                )

        for ref in function_promise.get("reads", []):
            if ref not in field_refs:
                issues.append(
                    LintIssue(
                        "function-unknown-read",
                        f"Function promise '{function_promise['name']}' reads unknown field reference '{ref}'.",
                    )
                )

        for ref in function_promise.get("writes", []):
            if ref not in field_refs:
                issues.append(
                    LintIssue(
                        "function-unknown-write",
                        f"Function promise '{function_promise['name']}' writes unknown field reference '{ref}'.",
                    )
                )

        for clause_group in (
            function_promise.get("preconditions", []),
            function_promise.get("successResults", []),
            function_promise.get("failureConditions", []),
            function_promise.get("sideEffects", []),
            function_promise.get("forbidden", []),
        ):
            _collect_clause_ids(clause_group, clause_ids, issues, function_promise["name"])
            for clause in clause_group:
                for ref in clause.get("refs", []):
                    if ref not in known_refs and ref not in field_refs:
                        issues.append(
                            LintIssue(
                                "function-unknown-ref",
                                f"Clause '{clause['id']}' in '{function_promise['name']}' references unknown target '{ref}'.",
                            )
                        )

    known_refs = known_refs | clause_ids

    for verification_promise in verification_promises:
        if not verification_promise.get("claim"):
            issues.append(
                LintIssue(
                    "verification-missing-claim",
                    f"Verification promise '{verification_promise['name']}' is missing claim.",
                )
            )
        if not verification_promise.get("verifies"):
            issues.append(
                LintIssue(
                    "verification-missing-verifies",
                    f"Verification promise '{verification_promise['name']}' has no verifies list.",
                )
            )
        if not verification_promise.get("methods"):
            issues.append(
                LintIssue(
                    "verification-missing-methods",
                    f"Verification promise '{verification_promise['name']}' has no methods.",
                )
            )
        if not verification_promise.get("scenarios"):
            issues.append(
                LintIssue(
                    "verification-missing-scenarios",
                    f"Verification promise '{verification_promise['name']}' has no scenarios.",
                )
            )
        if not verification_promise.get("failureCriteria"):
            issues.append(
                LintIssue(
                    "verification-missing-failure-criteria",
                    f"Verification promise '{verification_promise['name']}' has no failure criteria.",
                )
            )

        if verification_promise["kind"] not in VERIFICATION_KINDS:
            issues.append(
                LintIssue(
                    "verification-invalid-kind",
                    f"Verification promise '{verification_promise['name']}' uses unknown kind '{verification_promise['kind']}'.",
                )
            )

        for method in verification_promise.get("methods", []):
            if method not in VERIFICATION_METHODS:
                issues.append(
                    LintIssue(
                        "verification-invalid-method",
                        f"Verification promise '{verification_promise['name']}' uses unknown method '{method}'.",
                    )
                )

        for ref in verification_promise.get("verifies", []):
            if ref not in known_refs:
                issues.append(
                    LintIssue(
                        "verification-unknown-ref",
                        f"Verification promise '{verification_promise['name']}' verifies unknown target '{ref}'.",
                    )
                )

        for scenario in verification_promise.get("scenarios", []):
            if not scenario.get("covers"):
                issues.append(
                    LintIssue(
                        "scenario-missing-covers",
                        f"Scenario '{scenario['name']}' in '{verification_promise['name']}' has no covers list.",
                    )
                )
            if not scenario.get("when") or not scenario.get("then"):
                issues.append(
                    LintIssue(
                        "scenario-missing-when-then",
                        f"Scenario '{scenario['name']}' in '{verification_promise['name']}' must define when/then steps.",
                    )
                )
            for ref in scenario.get("covers", []):
                if ref not in known_refs:
                    issues.append(
                        LintIssue(
                            "scenario-unknown-cover",
                            f"Scenario '{scenario['name']}' in '{verification_promise['name']}' covers unknown target '{ref}'.",
                        )
                    )

    known_refs = known_refs | clause_ids

    for intent_promise in intent_promises:
        for intent_map in intent_promise.get("maps", []):
            target = intent_map.get("target")
            if target not in known_refs:
                issues.append(
                    LintIssue(
                        "intent-unknown-map-target",
                        f"Intent promise '{intent_promise['name']}' maps to unknown Promise item '{target}'.",
                    )
                )

    for resource_name, target in intent_resource_map_targets:
        if target not in known_refs:
            issues.append(
                LintIssue(
                    "intent-resource-unknown-map-target",
                    f"Intent resource '{resource_name}' maps to unknown Promise item '{target}'.",
                )
            )

    for term_name, target in intent_term_map_targets:
        if target not in known_refs:
            issues.append(
                LintIssue(
                    "intent-term-unknown-map-target",
                    f"Intent term '{term_name}' maps to unknown Promise item '{target}'.",
                )
            )

    _lint_intent_cycle_declarations(spec, known_refs, issues)
    intent_graph_analysis = analyze_intent_graph(spec)
    for cycle in intent_graph_analysis["unexpectedCycles"]:
        issues.append(
            LintIssue(
                "intent-graph-unexpected-cycle",
                (
                    f"Unexpected intent graph cycle detected over "
                    f"{', '.join(cycle['nodes'])}: {cycle['reason']}"
                ),
            )
        )
    for declared_cycle in intent_graph_analysis["declaredCycles"]:
        if not declared_cycle.get("matched"):
            issues.append(
                LintIssue(
                    "intent-graph-stale-cycle-declaration",
                    f"Intent cycle '{declared_cycle['name']}' is declared but no matching graph cycle exists.",
                    severity="warning",
                )
            )

    for conflict in analyze_intent_conflicts(spec)["detected"]:
        issues.append(
            LintIssue(
                "intent-auto-conflict-candidate",
                (
                    f"Intent conflict candidate detected between '{conflict['source']}' and "
                    f"'{conflict['target']}' ({conflict['severity']}): {conflict['reason']}"
                ),
                severity="warning",
            )
        )

    if profile == "core":
        issues.extend(_lint_core_subset(spec))

    return issues


def _lint_intent_requirements(
    intent_promise: dict[str, Any],
    seen_requirement_ids: set[str],
    resource_names: set[str],
    term_names_by_kind: dict[str, set[str]],
    issues: list[LintIssue],
) -> None:
    for requirement in intent_promise.get("requirements", []):
        requirement_id = requirement.get("id")
        kind = requirement.get("kind")
        if not requirement_id:
            issues.append(
                LintIssue(
                    "intent-requirement-missing-id",
                    f"Intent promise '{intent_promise['name']}' declares a structured requirement without an id.",
                )
            )
        elif requirement_id in seen_requirement_ids:
            issues.append(
                LintIssue(
                    "intent-requirement-duplicate-id",
                    f"Intent requirement '{requirement_id}' is declared more than once.",
                )
            )
        else:
            seen_requirement_ids.add(requirement_id)

        if kind not in INTENT_REQUIREMENT_KINDS:
            issues.append(
                LintIssue(
                    "intent-requirement-invalid-kind",
                    f"Intent requirement '{requirement_id or '-'}' uses unknown kind '{kind}'.",
                )
            )
        uses_operation_shape = any(key in requirement for key in INTENT_REQUIREMENT_OPERATION_KEYS)
        required_keys = (
            INTENT_REQUIREMENT_OPERATION_KEYS
            if uses_operation_shape
            else INTENT_REQUIREMENT_REQUIRED_KEYS
        )
        missing_keys = [key for key in sorted(required_keys) if not requirement.get(key)]
        if missing_keys:
            issues.append(
                LintIssue(
                    "intent-requirement-missing-field",
                    f"Intent requirement '{requirement_id or '-'}' is missing required field(s): {', '.join(missing_keys)}.",
                )
            )
        if kind == "prefers" and not requirement.get("over"):
            issues.append(
                LintIssue(
                    "intent-preference-missing-over",
                    f"Intent preference '{requirement_id or '-'}' must declare what it is preferred over.",
                )
            )
        requirement_priority = requirement.get("priority")
        if requirement_priority and requirement_priority not in INTENT_PRIORITIES:
            issues.append(
                LintIssue(
                    "intent-requirement-invalid-priority",
                    f"Intent requirement '{requirement_id or '-'}' uses unknown priority '{requirement_priority}'.",
                )
            )
        for atom_key in (
            "subject",
            "predicate",
            "object",
            "actor",
            "action",
            "resource",
            "over",
            "scope",
            "effect",
            "constraint",
        ):
            atom_value = requirement.get(atom_key)
            if atom_value and not SAFE_TOKEN_RE.match(str(atom_value)):
                issues.append(
                    LintIssue(
                        "intent-requirement-invalid-atom",
                        (
                            f"Intent requirement '{requirement_id or '-'}' uses non-canonical "
                            f"{atom_key} atom '{atom_value}'. Use a stable token instead of free text."
                        ),
                    )
                )
        if uses_operation_shape and resource_names:
            for resource_key in ("actor", "resource", "over"):
                resource_value = requirement.get(resource_key)
                if resource_value and resource_value not in resource_names:
                    issues.append(
                        LintIssue(
                            "intent-requirement-unknown-resource",
                            (
                                f"Intent requirement '{requirement_id or '-'}' references unknown "
                                f"{resource_key} resource '{resource_value}'."
                            ),
                        )
                    )
        term_checks = {
            "action": requirement.get("action"),
            "scope": requirement.get("scope"),
            "effect": requirement.get("effect"),
            "constraint": requirement.get("constraint"),
        }
        for term_kind, term_value in term_checks.items():
            known_terms = term_names_by_kind.get(term_kind, set())
            if known_terms and term_value and term_value not in known_terms:
                issues.append(
                    LintIssue(
                        "intent-requirement-unknown-term",
                        (
                            f"Intent requirement '{requirement_id or '-'}' references unknown "
                            f"{term_kind} term '{term_value}'."
                        ),
                    )
                )


def _intent_term_names_by_kind(intent_terms: list[dict[str, Any]]) -> dict[str, set[str]]:
    names_by_kind: dict[str, set[str]] = {}
    for intent_term in intent_terms:
        term_kind = intent_term.get("kind")
        if not term_kind:
            continue
        names_by_kind.setdefault(term_kind, set()).add(intent_term["name"])
        for alias in intent_term.get("aliases", []):
            names_by_kind[term_kind].add(alias)
    return names_by_kind


def _lint_field_clause_expressions(
    field_promise: dict[str, Any],
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    state_values: set[str],
    type_lookup: dict[str, dict[str, Any]],
    issues: list[LintIssue],
) -> None:
    object_name = field_promise["object"]
    for clause_group in (
        field_promise.get("invariants", []),
        field_promise.get("globalConstraints", []),
        field_promise.get("forbiddenImplicitState", []),
    ):
        for clause in clause_group:
            for expression_key in ("when", "must"):
                expression = clause.get(expression_key)
                if not expression:
                    continue
                try:
                    expression_ast = parse_promise_expression(expression)
                except PromiseExpressionError as exc:
                    issues.append(
                        LintIssue(
                            "expression-syntax-error",
                            f"Clause '{clause['id']}' in '{field_promise['name']}' has invalid {expression_key} expression: {exc}",
                        )
                    )
                    continue
                expression_issues = type_check_promise_expression(
                    expression_ast,
                    object_name,
                    field_lookup,
                    state_field,
                    state_values,
                    type_lookup,
                    clause["id"],
                    field_promise["name"],
                )
                issues.extend(expression_issues)


def parse_promise_expression(expression: str) -> dict[str, Any]:
    tokens = _tokenize_promise_expression(expression)
    parser = _PromiseExpressionParser(tokens, expression)
    return parser.parse()


def type_check_promise_expression(
    expression_ast: dict[str, Any],
    object_name: str,
    field_lookup: dict[str, dict[str, Any]],
    state_field: dict[str, Any] | None,
    state_values: set[str],
    type_lookup: dict[str, dict[str, Any]],
    clause_id: str,
    owner: str,
) -> list[LintIssue]:
    context = {
        "object": object_name,
        "field_lookup": field_lookup,
        "state_field": state_field,
        "state_values": state_values,
        "type_lookup": type_lookup,
        "clause_id": clause_id,
        "owner": owner,
    }
    issues: list[LintIssue] = []
    result = _resolve_expression_type(expression_ast, context, issues)
    if result.get("type") != "boolean" and not result.get("error"):
        issues.append(
            _expression_type_issue(
                context,
                "Expression must produce a boolean result.",
            )
        )
    return issues


def _tokenize_promise_expression(expression: str) -> list[dict[str, Any]]:
    tokens: list[dict[str, Any]] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace():
            index += 1
            continue
        if char in {"'", '"'}:
            value, index = _read_expression_string(expression, index)
            tokens.append({"kind": "string", "value": value})
            continue
        if char.isdigit() or (char == "-" and index + 1 < len(expression) and expression[index + 1].isdigit()):
            value, raw, index = _read_expression_number(expression, index)
            tokens.append({"kind": "number", "value": value, "raw": raw})
            continue
        if char.isalpha() or char == "_":
            raw, index = _read_expression_identifier(expression, index)
            lowered = raw.lower()
            if lowered in {"and", "or", "not", "in"}:
                tokens.append({"kind": "keyword", "value": lowered})
            elif lowered == "true":
                tokens.append({"kind": "boolean", "value": True, "raw": raw})
            elif lowered == "false":
                tokens.append({"kind": "boolean", "value": False, "raw": raw})
            elif lowered == "null":
                tokens.append({"kind": "null", "value": None, "raw": raw})
            else:
                tokens.append({"kind": "identifier", "value": raw})
            continue
        if char in {"(", ")", "[", "]", ","}:
            tokens.append({"kind": "punct", "value": char})
            index += 1
            continue
        two_char = expression[index : index + 2]
        if two_char in {"==", "!=", "<=", ">="}:
            tokens.append({"kind": "operator", "value": two_char})
            index += 2
            continue
        if char in {"=", "<", ">"}:
            tokens.append({"kind": "operator", "value": char})
            index += 1
            continue
        raise PromiseExpressionError(f"Unexpected character '{char}'.")
    tokens.append({"kind": "eof", "value": ""})
    return tokens


def _read_expression_string(expression: str, start: int) -> tuple[str, int]:
    quote = expression[start]
    index = start + 1
    chars: list[str] = []
    while index < len(expression):
        char = expression[index]
        if char == "\\" and index + 1 < len(expression):
            chars.append(expression[index + 1])
            index += 2
            continue
        if char == quote:
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    raise PromiseExpressionError("Unterminated string literal.")


def _read_expression_number(expression: str, start: int) -> tuple[int | float, str, int]:
    match = re.match(r"-?\d+(\.\d+)?", expression[start:])
    if match is None:
        raise PromiseExpressionError("Invalid number literal.")
    raw = match.group(0)
    value: int | float = float(raw) if "." in raw else int(raw)
    return value, raw, start + len(raw)


def _read_expression_identifier(expression: str, start: int) -> tuple[str, int]:
    match = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", expression[start:])
    if match is None:
        raise PromiseExpressionError("Invalid identifier.")
    raw = match.group(0)
    if raw.endswith(".") or ".." in raw:
        raise PromiseExpressionError(f"Invalid reference '{raw}'.")
    return raw, start + len(raw)


class _PromiseExpressionParser:
    def __init__(self, tokens: list[dict[str, Any]], source: str) -> None:
        self.tokens = tokens
        self.source = source
        self.index = 0

    def parse(self) -> dict[str, Any]:
        if self._peek()["kind"] == "eof":
            raise PromiseExpressionError("Expression is empty.")
        expression = self._parse_or()
        if self._peek()["kind"] != "eof":
            raise PromiseExpressionError(f"Unexpected token '{self._peek()['value']}'.")
        return expression

    def _parse_or(self) -> dict[str, Any]:
        expression = self._parse_and()
        while self._match_keyword("or"):
            right = self._parse_and()
            expression = {"kind": "binary", "operator": "or", "left": expression, "right": right}
        return expression

    def _parse_and(self) -> dict[str, Any]:
        expression = self._parse_not()
        while self._match_keyword("and"):
            right = self._parse_not()
            expression = {"kind": "binary", "operator": "and", "left": expression, "right": right}
        return expression

    def _parse_not(self) -> dict[str, Any]:
        if self._match_keyword("not"):
            return {"kind": "not", "operand": self._parse_not()}
        return self._parse_comparison()

    def _parse_comparison(self) -> dict[str, Any]:
        left = self._parse_primary()
        operator = self._match_comparison_operator()
        if operator is None:
            return left
        right = self._parse_primary()
        if operator == "=":
            operator = "=="
        return {"kind": "comparison", "operator": operator, "left": left, "right": right}

    def _parse_primary(self) -> dict[str, Any]:
        token = self._peek()
        if self._match_punct("("):
            expression = self._parse_or()
            self._expect_punct(")")
            return expression
        if self._match_punct("["):
            items: list[dict[str, Any]] = []
            if not self._match_punct("]"):
                while True:
                    items.append(self._parse_or())
                    if self._match_punct("]"):
                        break
                    self._expect_punct(",")
            return {"kind": "list", "items": items}
        if token["kind"] == "identifier":
            self.index += 1
            return {"kind": "reference", "name": token["value"], "parts": token["value"].split(".")}
        if token["kind"] in {"string", "number", "boolean", "null"}:
            self.index += 1
            literal = {"kind": "literal", "literalType": token["kind"], "value": token["value"]}
            if "raw" in token:
                literal["raw"] = token["raw"]
            return literal
        raise PromiseExpressionError(f"Expected expression value but got '{token['value']}'.")

    def _match_comparison_operator(self) -> str | None:
        token = self._peek()
        if token["kind"] == "operator" and token["value"] in {"=", "==", "!=", "<", "<=", ">", ">="}:
            self.index += 1
            return token["value"]
        if token["kind"] == "keyword" and token["value"] == "in":
            self.index += 1
            return "in"
        return None

    def _match_keyword(self, value: str) -> bool:
        token = self._peek()
        if token["kind"] == "keyword" and token["value"] == value:
            self.index += 1
            return True
        return False

    def _match_punct(self, value: str) -> bool:
        token = self._peek()
        if token["kind"] == "punct" and token["value"] == value:
            self.index += 1
            return True
        return False

    def _expect_punct(self, value: str) -> None:
        if not self._match_punct(value):
            raise PromiseExpressionError(f"Expected '{value}' but got '{self._peek()['value']}'.")

    def _peek(self) -> dict[str, Any]:
        return self.tokens[self.index]


def _resolve_expression_type(
    expression_ast: dict[str, Any],
    context: dict[str, Any],
    issues: list[LintIssue],
    expected_field: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = expression_ast["kind"]
    if kind == "binary":
        left = _resolve_expression_type(expression_ast["left"], context, issues)
        right = _resolve_expression_type(expression_ast["right"], context, issues)
        for side in (left, right):
            if side.get("error"):
                return _error_type()
            if side.get("type") != "boolean":
                issues.append(
                    _expression_type_issue(
                        context,
                        f"Operator '{expression_ast['operator']}' requires boolean operands.",
                    )
                )
                return _error_type()
        return _boolean_type()
    if kind == "not":
        operand = _resolve_expression_type(expression_ast["operand"], context, issues)
        if operand.get("error"):
            return _error_type()
        if operand.get("type") != "boolean":
            issues.append(_expression_type_issue(context, "Operator 'not' requires a boolean operand."))
            return _error_type()
        return _boolean_type()
    if kind == "comparison":
        return _resolve_comparison_type(expression_ast, context, issues)
    if kind == "reference":
        return _resolve_reference_type(expression_ast, context, issues, expected_field)
    if kind == "literal":
        return _literal_type(expression_ast)
    if kind == "list":
        item_types = [
            _resolve_expression_type(item, context, issues, expected_field)
            for item in expression_ast["items"]
        ]
        if any(item_type.get("error") for item_type in item_types):
            return _error_type()
        return {"type": "list", "items": item_types, "nullable": False}
    return _error_type()


def _resolve_comparison_type(
    expression_ast: dict[str, Any],
    context: dict[str, Any],
    issues: list[LintIssue],
) -> dict[str, Any]:
    operator = expression_ast["operator"]
    left = _resolve_expression_type(expression_ast["left"], context, issues)
    expected_field = left.get("field") if not left.get("error") else None
    right = _resolve_expression_type(expression_ast["right"], context, issues, expected_field)
    if left.get("error") or right.get("error"):
        return _error_type()

    if operator == "in":
        if right.get("type") != "list":
            issues.append(_expression_type_issue(context, "Operator 'in' requires a list on the right side."))
            return _error_type()
        for item in right.get("items", []):
            _check_comparable_types(left, item, "==", context, issues)
        return _boolean_type()

    if operator in {"<", "<=", ">", ">="}:
        if not _is_numeric_type(left) or not _is_numeric_type(right):
            issues.append(
                _expression_type_issue(
                    context,
                    f"Operator '{operator}' requires numeric operands.",
                )
            )
            return _error_type()
        return _boolean_type()

    if operator in {"==", "!="}:
        _check_comparable_types(left, right, operator, context, issues)
        return _boolean_type()

    issues.append(_expression_type_issue(context, f"Unknown comparison operator '{operator}'."))
    return _error_type()


def _resolve_reference_type(
    expression_ast: dict[str, Any],
    context: dict[str, Any],
    issues: list[LintIssue],
    expected_field: dict[str, Any] | None,
) -> dict[str, Any]:
    parts = expression_ast["parts"]
    if len(parts) == 2 and parts[0] == context["object"]:
        field = context["field_lookup"].get(parts[1])
        if field is not None:
            return _field_type(field, context)
    if expected_field is not None:
        enum_result = _resolve_enum_literal_type(expression_ast, expected_field, context, issues)
        if enum_result is not None:
            return enum_result
    issues.append(
        LintIssue(
            "expression-unknown-reference",
            f"Clause '{context['clause_id']}' in '{context['owner']}' references unknown expression value '{expression_ast['name']}'.",
        )
    )
    return _error_type()


def _resolve_enum_literal_type(
    expression_ast: dict[str, Any],
    expected_field: dict[str, Any],
    context: dict[str, Any],
    issues: list[LintIssue],
) -> dict[str, Any] | None:
    enum_values = _field_enum_values(expected_field, context)
    if enum_values is None:
        return None
    parts = expression_ast["parts"]
    literal = parts[-1]
    if literal not in enum_values:
        issues.append(
            LintIssue(
                "field-unknown-enum-literal",
                f"Clause '{context['clause_id']}' in '{context['owner']}' compares '{context['object']}.{expected_field['name']}' to unknown enum literal '{literal}'.",
            )
        )
        return _error_type()
    if len(parts) == 1 or _is_expression_enum_namespace(parts[:-1], expected_field, context):
        return {
            "type": "enum",
            "enumKey": f"{context['object']}.{expected_field['name']}",
            "value": literal,
            "nullable": False,
        }
    return None


def _is_expression_enum_namespace(
    namespace_parts: list[str],
    field: dict[str, Any],
    context: dict[str, Any],
) -> bool:
    namespace = ".".join(namespace_parts)
    pascal_name = _expression_pascal_identifier(f"{context['object']}_{field['name']}")
    return namespace in {
        field["name"],
        f"{context['object']}.{field['name']}",
        pascal_name,
    }


def _field_type(field: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    enum_values = _field_enum_values(field, context)
    if enum_values is not None:
        return {
            "type": "enum",
            "enumKey": f"{context['object']}.{field['name']}",
            "enumValues": enum_values,
            "field": field,
            "nullable": bool(field.get("nullable")),
        }
    field_type = field["type"]
    type_promise = context["type_lookup"].get(field_type)
    if type_promise is not None:
        field_type = type_promise["base"]
    return {
        "type": field_type,
        "field": field,
        "nullable": bool(field.get("nullable")),
    }


def _field_enum_values(field: dict[str, Any], context: dict[str, Any]) -> set[str] | None:
    state_field = context["state_field"]
    if state_field is not None and field["name"] == state_field["name"]:
        enum_values = _enum_choices(field["type"]) or []
        return set(context["state_values"]) or set(enum_values)
    enum_values = _enum_choices(field["type"])
    if enum_values is None:
        return None
    return set(enum_values)


def _literal_type(expression_ast: dict[str, Any]) -> dict[str, Any]:
    literal_type = expression_ast["literalType"]
    if literal_type == "null":
        return {"type": "null", "nullable": True}
    if literal_type == "boolean":
        return {"type": "boolean", "nullable": False}
    if literal_type == "string":
        return {"type": "string", "nullable": False}
    if literal_type == "number":
        value = expression_ast["value"]
        return {"type": "integer" if isinstance(value, int) else "number", "nullable": False}
    return _error_type()


def _check_comparable_types(
    left: dict[str, Any],
    right: dict[str, Any],
    operator: str,
    context: dict[str, Any],
    issues: list[LintIssue],
) -> None:
    if left.get("type") == "null" or right.get("type") == "null":
        other = right if left.get("type") == "null" else left
        if operator == "==" and other.get("field") is not None and not other.get("nullable"):
            issues.append(
                _expression_type_issue(
                    context,
                    f"Non-nullable field '{context['object']}.{other['field']['name']}' cannot be required to equal null.",
                )
            )
        return
    if left.get("type") == "enum" or right.get("type") == "enum":
        if left.get("type") != "enum" or right.get("type") != "enum" or left.get("enumKey") != right.get("enumKey"):
            issues.append(_expression_type_issue(context, "Enum comparisons must use literals from the same field enum."))
        return
    if _is_numeric_type(left) and _is_numeric_type(right):
        return
    if left.get("type") in {"string", "text", "path"} and right.get("type") in {"string", "text", "path"}:
        return
    if left.get("type") == right.get("type"):
        return
    issues.append(
        _expression_type_issue(
            context,
            f"Cannot compare {left.get('type')} with {right.get('type')}.",
        )
    )


def _is_numeric_type(value_type: dict[str, Any]) -> bool:
    return value_type.get("type") in {"integer", "number"}


def _boolean_type() -> dict[str, Any]:
    return {"type": "boolean", "nullable": False}


def _error_type() -> dict[str, Any]:
    return {"type": "error", "error": True, "nullable": False}


def _expression_type_issue(context: dict[str, Any], message: str) -> LintIssue:
    return LintIssue(
        "expression-type-error",
        f"Clause '{context['clause_id']}' in '{context['owner']}' has invalid expression types: {message}",
    )


def _expression_pascal_identifier(value: str) -> str:
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return "".join(part[:1].upper() + part[1:] for part in parts)


def _select_state_field_for_lint(field_promise: dict[str, Any]) -> dict[str, Any] | None:
    states = field_promise.get("states", [])
    if not states:
        return None
    state_values = {state["value"] for state in states}
    for field in field_promise.get("fields", []):
        enum_values = _enum_choices(field["type"])
        if enum_values and state_values.issubset(set(enum_values)):
            return field
    for field in field_promise.get("fields", []):
        if field["name"].lower() in {"status", "state"}:
            return field
    return None


def analyze_intent_graph(spec: dict[str, Any]) -> dict[str, Any]:
    """Return typed intent graph cycle analysis."""
    nodes = _build_intent_graph_nodes(spec)
    edges = _build_intent_graph_edges(spec, nodes)
    declared_cycles = _declared_intent_cycle_reports(spec, nodes, edges)
    unexpected_cycles = _detect_unexpected_intent_graph_cycles(edges, declared_cycles)
    return {
        "nodes": sorted(nodes.values(), key=lambda node: node["id"]),
        "edges": sorted(edges, key=lambda edge: (edge["source"], edge["target"], edge["relation"], edge["kind"])),
        "declaredCycles": declared_cycles,
        "unexpectedCycles": unexpected_cycles,
    }


def _lint_intent_cycle_declarations(
    spec: dict[str, Any],
    known_refs: set[str],
    issues: list[LintIssue],
) -> None:
    intent_graph_ref_index = _build_intent_graph_ref_index(spec, known_refs)
    for cycle in spec.get("intentCycles", []):
        cycle_name = cycle["name"]
        if cycle.get("kind") not in INTENT_CYCLE_KINDS:
            issues.append(
                LintIssue(
                    "intent-cycle-invalid-kind",
                    f"Intent cycle '{cycle_name}' uses unknown kind '{cycle.get('kind')}'.",
                )
            )
        if not cycle.get("summary"):
            issues.append(
                LintIssue(
                    "intent-cycle-missing-summary",
                    f"Intent cycle '{cycle_name}' is missing summary.",
                )
            )
        if not cycle.get("rationale"):
            issues.append(
                LintIssue(
                    "intent-cycle-missing-rationale",
                    f"Intent cycle '{cycle_name}' is missing rationale.",
                )
            )
        if len(cycle.get("edges", [])) < 2:
            issues.append(
                LintIssue(
                    "intent-cycle-missing-edges",
                    f"Intent cycle '{cycle_name}' must declare at least two directed edges.",
                )
            )
        for edge in cycle.get("edges", []):
            relation = edge.get("relation")
            source_ref = edge.get("source", "")
            target_ref = edge.get("target", "")
            source_id = intent_graph_ref_index.get(source_ref)
            target_id = intent_graph_ref_index.get(target_ref)
            if relation not in INTENT_GRAPH_RELATIONS:
                issues.append(
                    LintIssue(
                        "intent-cycle-invalid-edge-relation",
                        f"Intent cycle '{cycle_name}' uses unknown edge relation '{relation}'.",
                    )
                )
            if source_id is None:
                issues.append(
                    LintIssue(
                        "intent-cycle-unknown-edge-source",
                        f"Intent cycle '{cycle_name}' edge source '{source_ref}' is unknown.",
                    )
                )
            if target_id is None:
                issues.append(
                    LintIssue(
                        "intent-cycle-unknown-edge-target",
                        f"Intent cycle '{cycle_name}' edge target '{target_ref}' is unknown.",
                    )
                )


def _build_intent_graph_nodes(spec: dict[str, Any]) -> dict[str, dict[str, str]]:
    nodes: dict[str, dict[str, str]] = {}
    for intent in spec.get("intentPromises", []):
        node_id = _intent_graph_intent_id(intent["name"])
        nodes[node_id] = {"id": node_id, "ref": intent["name"], "kind": "intent", "label": intent["name"]}
    for resource in spec.get("intentResources", []):
        node_id = _intent_graph_resource_id(resource["name"])
        nodes[node_id] = {"id": node_id, "ref": resource["name"], "kind": "resource", "label": resource["name"]}
    for term in spec.get("intentTerms", []):
        node_id = _intent_graph_term_id(term.get("kind", "term"), term["name"])
        nodes[node_id] = {
            "id": node_id,
            "ref": term["name"],
            "kind": "term",
            "termKind": term.get("kind", ""),
            "label": term["name"],
        }
    for cycle in spec.get("intentCycles", []):
        node_id = _intent_graph_cycle_id(cycle["name"])
        nodes[node_id] = {"id": node_id, "ref": cycle["name"], "kind": "cycle", "label": cycle["name"]}
    for promise_ref in _collect_promise_item_refs(spec):
        node_id = _intent_graph_promise_id(promise_ref)
        nodes.setdefault(node_id, {"id": node_id, "ref": promise_ref, "kind": "promise", "label": promise_ref})
    return nodes


def _build_intent_graph_ref_index(spec: dict[str, Any], known_refs: set[str] | None = None) -> dict[str, str]:
    refs: dict[str, str] = {}
    for intent in spec.get("intentPromises", []):
        node_id = _intent_graph_intent_id(intent["name"])
        refs[intent["name"]] = node_id
        refs[node_id] = node_id
    for resource in spec.get("intentResources", []):
        node_id = _intent_graph_resource_id(resource["name"])
        refs[resource["name"]] = node_id
        refs[node_id] = node_id
    for term in spec.get("intentTerms", []):
        node_id = _intent_graph_term_id(term.get("kind", "term"), term["name"])
        refs[term["name"]] = node_id
        refs[node_id] = node_id
    for cycle in spec.get("intentCycles", []):
        node_id = _intent_graph_cycle_id(cycle["name"])
        refs[cycle["name"]] = node_id
        refs[node_id] = node_id
    for promise_ref in sorted((known_refs or set()) | _collect_promise_item_refs(spec)):
        if promise_ref not in refs:
            node_id = _intent_graph_promise_id(promise_ref)
            refs[promise_ref] = node_id
            refs[node_id] = node_id
    return refs


def _collect_promise_item_refs(spec: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for type_promise in spec.get("typePromises", []):
        refs.add(type_promise["name"])
    for field_promise in spec.get("fieldPromises", []):
        refs.add(field_promise["name"])
        refs.add(field_promise["object"])
        for field in field_promise.get("fields", []):
            refs.add(f"{field_promise['object']}.{field['name']}")
        for state in field_promise.get("states", []):
            refs.add(f"{field_promise['object']}.{state['value']}")
        for clause in _field_clauses_for_intent_graph(field_promise):
            refs.add(clause["id"])
    for function_promise in spec.get("functionPromises", []):
        refs.add(function_promise["name"])
        for clause in _function_clauses_for_intent_graph(function_promise):
            refs.add(clause["id"])
    for verification_promise in spec.get("verificationPromises", []):
        refs.add(verification_promise["name"])
        for scenario in verification_promise.get("scenarios", []):
            refs.update(scenario.get("covers", []))
    for intent in spec.get("intentPromises", []):
        for requirement in intent.get("requirements", []):
            if requirement.get("id"):
                refs.add(requirement["id"])
    return refs


def _intent_cycle_node_refs(cycle: dict[str, Any]) -> list[str]:
    node_refs: list[str] = []
    for edge in cycle.get("edges", []):
        for endpoint in (edge.get("source"), edge.get("target")):
            if endpoint and endpoint not in node_refs:
                node_refs.append(endpoint)
    return node_refs


def _field_clauses_for_intent_graph(field_promise: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    clauses.extend(field_promise.get("invariants", []))
    clauses.extend(field_promise.get("globalConstraints", []))
    clauses.extend(field_promise.get("forbiddenImplicitState", []))
    return clauses


def _function_clauses_for_intent_graph(function_promise: dict[str, Any]) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    clauses.extend(function_promise.get("preconditions", []))
    clauses.extend(function_promise.get("successResults", []))
    clauses.extend(function_promise.get("failureConditions", []))
    clauses.extend(function_promise.get("sideEffects", []))
    clauses.extend(function_promise.get("forbidden", []))
    return clauses


def _build_intent_graph_edges(
    spec: dict[str, Any],
    nodes: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    ref_index = _build_intent_graph_ref_index(spec)
    edges: list[dict[str, Any]] = []

    def add_edge(
        source_ref: str,
        target_ref: str,
        relation: str,
        kind: str,
        note: str = "",
        cycle_relevant: bool = True,
    ) -> None:
        source_id = ref_index.get(source_ref)
        target_id = ref_index.get(target_ref)
        if source_id is None or target_id is None:
            return
        if source_id not in nodes or target_id not in nodes:
            return
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "sourceRef": source_ref,
                "targetRef": target_ref,
                "relation": relation,
                "kind": kind,
                "note": note,
                "cycleRelevant": cycle_relevant and relation not in INTENT_GRAPH_SYMMETRIC_RELATIONS,
                "strict": relation in INTENT_GRAPH_STRICT_RELATIONS or kind in {"intent-parent", "term-parent"},
            }
        )

    for intent in spec.get("intentPromises", []):
        intent_name = intent["name"]
        for parent in intent.get("parents", []):
            add_edge(
                parent.get("target", ""),
                intent_name,
                parent.get("relation") or "refines",
                "intent-parent",
                parent.get("note", ""),
            )
        for intent_map in intent.get("maps", []):
            add_edge(
                intent_name,
                intent_map.get("target", ""),
                intent_map.get("relation") or "maps",
                "intent-map",
                intent_map.get("note", ""),
            )
        for conflict in intent.get("conflicts", []):
            add_edge(
                intent_name,
                conflict.get("target", ""),
                "conflicts",
                "intent-conflict",
                conflict.get("reason", ""),
                cycle_relevant=False,
            )
        for requirement in intent.get("requirements", []):
            if requirement.get("resource"):
                add_edge(
                    intent_name,
                    requirement["resource"],
                    "operates",
                    "intent-requirement-resource",
                    requirement.get("id", ""),
                )
            if requirement.get("over"):
                add_edge(
                    intent_name,
                    requirement["over"],
                    "over",
                    "intent-requirement-over",
                    requirement.get("id", ""),
                )
            for semantic_key in ("action", "scope", "effect", "constraint"):
                if requirement.get(semantic_key):
                    add_edge(
                        intent_name,
                        requirement[semantic_key],
                        semantic_key,
                        "intent-requirement-term",
                        requirement.get("id", ""),
                    )

    for term in spec.get("intentTerms", []):
        term_name = term["name"]
        if term.get("parent"):
            add_edge(term["parent"], term_name, "contains", "term-parent")
        for disjoint in term.get("disjoint", []):
            add_edge(term_name, disjoint, "disjoint", "term-disjoint", cycle_relevant=False)
        for opposite in term.get("opposites", []):
            add_edge(term_name, opposite, "opposite", "term-opposite", cycle_relevant=False)
        for term_map in term.get("maps", []):
            add_edge(
                term_name,
                term_map.get("target", ""),
                term_map.get("relation") or "maps",
                "term-map",
                term_map.get("note", ""),
            )

    for resource in spec.get("intentResources", []):
        for resource_map in resource.get("maps", []):
            add_edge(
                resource["name"],
                resource_map.get("target", ""),
                resource_map.get("relation") or "maps",
                "resource-map",
                resource_map.get("note", ""),
            )

    return edges


def _declared_intent_cycle_reports(
    spec: dict[str, Any],
    nodes: dict[str, dict[str, str]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ref_index = _build_intent_graph_ref_index(spec)
    actual_edge_keys = {_intent_graph_edge_key(edge) for edge in edges}
    reports: list[dict[str, Any]] = []
    for cycle in spec.get("intentCycles", []):
        node_ids = sorted({ref_index[node] for node in _intent_cycle_node_refs(cycle) if node in ref_index})
        declared_edges = []
        for edge in cycle.get("edges", []):
            source_id = ref_index.get(edge.get("source", ""))
            target_id = ref_index.get(edge.get("target", ""))
            relation = edge.get("relation")
            if source_id is None or target_id is None or relation is None:
                continue
            declared_edges.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "sourceRef": edge.get("source", ""),
                    "targetRef": edge.get("target", ""),
                    "relation": relation,
                    "exists": (source_id, target_id, relation) in actual_edge_keys,
                }
            )
        reports.append(
            {
                "name": cycle["name"],
                "kind": cycle.get("kind"),
                "summary": cycle.get("summary", ""),
                "rationale": cycle.get("rationale", ""),
                "nodeIds": node_ids,
                "nodes": [nodes[node_id]["label"] for node_id in node_ids if node_id in nodes],
                "edges": declared_edges,
                "matched": False,
            }
        )
    return sorted(reports, key=lambda item: item["name"])


def _detect_unexpected_intent_graph_cycles(
    edges: list[dict[str, Any]],
    declared_cycles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cycle_edges = [
        edge
        for edge in edges
        if edge.get("cycleRelevant")
        and edge.get("source") != edge.get("target")
        and edge.get("relation") not in INTENT_GRAPH_SYMMETRIC_RELATIONS
    ]
    components = _strongly_connected_components(cycle_edges)
    declared_node_sets = {
        frozenset(cycle.get("nodeIds", [])): cycle
        for cycle in declared_cycles
        if cycle.get("nodeIds")
    }
    unexpected: list[dict[str, Any]] = []
    for component in components:
        if len(component) < 2:
            continue
        component_key = frozenset(component)
        component_edges = [
            edge
            for edge in cycle_edges
            if edge["source"] in component_key and edge["target"] in component_key
        ]
        has_strict_edge = any(edge.get("strict") for edge in component_edges)
        declared_cycle = declared_node_sets.get(component_key)
        declared_edges_exist = (
            declared_cycle is not None
            and declared_cycle.get("edges")
            and all(edge.get("exists") for edge in declared_cycle.get("edges", []))
        )
        if declared_cycle is not None:
            declared_cycle["matched"] = True
        if declared_cycle is not None and declared_edges_exist and not has_strict_edge:
            continue
        unexpected.append(
            {
                "kind": _intent_graph_unexpected_cycle_kind(component, component_edges),
                "nodeIds": sorted(component),
                "nodes": sorted(_intent_graph_label_from_id(node_id) for node_id in component),
                "edges": [_intent_graph_edge_report(edge) for edge in component_edges],
                "declaredCycle": declared_cycle.get("name") if declared_cycle else "",
                "reason": _intent_graph_cycle_reason(component_edges, declared_cycle, has_strict_edge),
            }
        )
    self_loops = [edge for edge in edges if edge.get("cycleRelevant") and edge.get("source") == edge.get("target")]
    for edge in self_loops:
        unexpected.append(
            {
                "kind": "self-loop",
                "nodeIds": [edge["source"]],
                "nodes": [_intent_graph_label_from_id(edge["source"])],
                "edges": [_intent_graph_edge_report(edge)],
                "declaredCycle": "",
                "reason": f"self-loop via {edge['relation']} is not a declared feedback cycle.",
            }
        )
    return sorted(unexpected, key=lambda item: (",".join(item["nodes"]), item["reason"]))


def _intent_graph_unexpected_cycle_kind(
    component: set[str],
    component_edges: list[dict[str, Any]],
) -> str:
    if len(component) == 1:
        return "self-loop"
    if len(component) == 2:
        return "reciprocal"
    return "scc"


def _strongly_connected_components(edges: list[dict[str, Any]]) -> list[set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set())

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in adjacency.get(node, set()):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] != indices[node]:
            return
        component: set[str] = set()
        while stack:
            current = stack.pop()
            on_stack.remove(current)
            component.add(current)
            if current == node:
                break
        components.append(component)

    for node in sorted(adjacency):
        if node not in indices:
            visit(node)
    return components


def _intent_graph_edge_key(edge: dict[str, Any]) -> tuple[str, str, str]:
    return (edge["source"], edge["target"], edge["relation"])


def _intent_graph_edge_report(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": _intent_graph_label_from_id(edge["source"]),
        "target": _intent_graph_label_from_id(edge["target"]),
        "sourceId": edge["source"],
        "targetId": edge["target"],
        "relation": edge["relation"],
        "kind": edge["kind"],
        "strict": bool(edge.get("strict")),
        "note": edge.get("note", ""),
    }


def _intent_graph_cycle_reason(
    component_edges: list[dict[str, Any]],
    declared_cycle: dict[str, Any] | None,
    has_strict_edge: bool,
) -> str:
    relations = ", ".join(sorted({edge["relation"] for edge in component_edges}))
    if has_strict_edge:
        return f"strict relation cycle contains {relations}; structural/lowering edges cannot be cycle-covered."
    if declared_cycle is None:
        return f"cycle uses {relations} but no matching cycle declaration covers it."
    missing = [
        f"{edge['sourceRef']} -> {edge['targetRef']} relation {edge['relation']}"
        for edge in declared_cycle.get("edges", [])
        if not edge.get("exists")
    ]
    if missing:
        return f"cycle declaration is missing actual edge(s): {', '.join(missing)}."
    return f"cycle uses {relations} but is not fully covered by a matching declaration."


def _intent_graph_label_from_id(node_id: str) -> str:
    return node_id.split("::")[-1]


def _intent_graph_intent_id(name: str) -> str:
    return f"intent::{name}"


def _intent_graph_resource_id(name: str) -> str:
    return f"resource::{name}"


def _intent_graph_term_id(kind: str, name: str) -> str:
    return f"term::{kind}::{name}"


def _intent_graph_cycle_id(name: str) -> str:
    return f"cycle::{name}"


def _intent_graph_promise_id(name: str) -> str:
    return f"promise::{name}"


def analyze_intent_conflicts(spec: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return declared and automatically detected intent conflict records."""
    intent_promises = spec.get("intentPromises", [])
    declared = _declared_intent_conflict_reports(intent_promises)
    declared_pairs = {
        _intent_conflict_pair_key(conflict["source"], conflict["target"])
        for conflict in declared
        if conflict.get("source") and conflict.get("target")
    }
    detected = _detect_intent_conflict_candidates(spec, declared_pairs)
    return {
        "declared": declared,
        "detected": detected,
        "all": declared + detected,
    }


def _declared_intent_conflict_reports(intent_promises: list[dict[str, Any]]) -> list[dict[str, Any]]:
    intent_names = {intent["name"] for intent in intent_promises}
    reports: list[dict[str, Any]] = []
    for intent in intent_promises:
        for conflict in intent.get("conflicts", []):
            reports.append(
                {
                    "source": intent["name"],
                    "target": conflict.get("target"),
                    "targetKnown": conflict.get("target") in intent_names,
                    "severity": conflict.get("severity"),
                    "reason": conflict.get("reason", ""),
                    "resolution": conflict.get("resolution", ""),
                    "note": conflict.get("note", ""),
                    "sourceType": "declared",
                    "detector": "declared",
                    "evidence": [],
                }
            )
    return sorted(reports, key=lambda item: (item["source"], item["target"] or ""))


def _detect_intent_conflict_candidates(
    spec: dict[str, Any],
    declared_pairs: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    intent_promises = spec.get("intentPromises", [])
    intent_index = {intent["name"]: intent for intent in intent_promises}
    intent_term_index = {term["name"]: term for term in spec.get("intentTerms", [])}
    seen_pairs = set(declared_pairs)
    candidates: list[dict[str, Any]] = []

    _detect_opposed_intent_map_conflicts(intent_promises, intent_index, seen_pairs, candidates)
    _detect_opposed_intent_assertion_conflicts(spec, intent_index, seen_pairs, candidates)
    _detect_opposed_intent_requirement_conflicts(
        intent_promises,
        intent_index,
        intent_term_index,
        seen_pairs,
        candidates,
    )

    return sorted(candidates, key=lambda item: (item["source"], item["target"], item["detector"]))


def _detect_opposed_intent_map_conflicts(
    intent_promises: list[dict[str, Any]],
    intent_index: dict[str, dict[str, Any]],
    seen_pairs: set[tuple[str, str]],
    candidates: list[dict[str, Any]],
) -> None:
    maps_by_target: dict[str, list[dict[str, str]]] = {}
    for intent in intent_promises:
        for intent_map in intent.get("maps", []):
            relation = intent_map.get("relation")
            target = intent_map.get("target")
            if not relation or not target or relation not in INTENT_RELATIONS:
                continue
            maps_by_target.setdefault(target, []).append(
                {
                    "intent": intent["name"],
                    "target": target,
                    "relation": relation,
                    "note": intent_map.get("note", ""),
                }
            )

    for target, intent_maps in maps_by_target.items():
        for left_index, left in enumerate(intent_maps):
            for right in intent_maps[left_index + 1 :]:
                if left["intent"] == right["intent"]:
                    continue
                if not _intent_map_relations_conflict(left["relation"], right["relation"]):
                    continue
                negative = left if left["relation"] in INTENT_NEGATIVE_RELATIONS else right
                positive = right if negative is left else left
                if _skip_auto_intent_conflict_pair(
                    intent_index[negative["intent"]],
                    intent_index[positive["intent"]],
                ):
                    continue
                pair_key = _intent_conflict_pair_key(negative["intent"], positive["intent"])
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                severity = _intent_conflict_candidate_severity(
                    intent_index[negative["intent"]],
                    intent_index[positive["intent"]],
                )
                candidates.append(
                    {
                        "source": negative["intent"],
                        "target": positive["intent"],
                        "targetKnown": True,
                        "severity": severity,
                        "reason": (
                            f"{negative['intent']} maps {target} as conflicts while "
                            f"{positive['intent']} maps the same item as {positive['relation']}."
                        ),
                        "resolution": "",
                        "note": "",
                        "sourceType": "detected",
                        "detector": "opposed-map-relation",
                        "evidence": [
                            {
                                "type": "shared-map-target",
                                "target": target,
                                "sourceRelation": negative["relation"],
                                "targetRelation": positive["relation"],
                            }
                        ],
                    }
                )


def _intent_map_relations_conflict(left_relation: str, right_relation: str) -> bool:
    return (
        left_relation in INTENT_NEGATIVE_RELATIONS
        and right_relation in INTENT_POSITIVE_RELATIONS
    ) or (
        right_relation in INTENT_NEGATIVE_RELATIONS
        and left_relation in INTENT_POSITIVE_RELATIONS
    )


def _detect_opposed_intent_assertion_conflicts(
    spec: dict[str, Any],
    intent_index: dict[str, dict[str, Any]],
    seen_pairs: set[tuple[str, str]],
    candidates: list[dict[str, Any]],
) -> None:
    assertions_by_clause = _field_clause_assertions_by_id(spec)
    if not assertions_by_clause:
        return

    mapped_assertions: list[dict[str, Any]] = []
    for intent in spec.get("intentPromises", []):
        for intent_map in intent.get("maps", []):
            for assertion in assertions_by_clause.get(intent_map.get("target"), []):
                mapped_assertions.append(
                    {
                        "intent": intent["name"],
                        "relation": intent_map.get("relation"),
                        **assertion,
                    }
                )

    for left_index, left in enumerate(mapped_assertions):
        for right in mapped_assertions[left_index + 1 :]:
            if left["intent"] == right["intent"]:
                continue
            if not _field_assertions_conflict(left, right):
                continue
            if _skip_auto_intent_conflict_pair(
                intent_index[left["intent"]],
                intent_index[right["intent"]],
            ):
                continue
            pair_key = _intent_conflict_pair_key(left["intent"], right["intent"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            severity = _intent_conflict_candidate_severity(
                intent_index[left["intent"]],
                intent_index[right["intent"]],
            )
            candidates.append(
                {
                    "source": left["intent"],
                    "target": right["intent"],
                    "targetKnown": True,
                    "severity": severity,
                    "reason": (
                        f"{left['intent']} and {right['intent']} map clauses with incompatible "
                        f"requirements for {left['subject']}."
                    ),
                    "resolution": "",
                    "note": "",
                    "sourceType": "detected",
                    "detector": "opposed-field-assertion",
                    "evidence": [
                        {
                            "type": "field-assertion",
                            "sourceClause": left["clause"],
                            "targetClause": right["clause"],
                            "subject": left["subject"],
                            "sourceOperator": left["operator"],
                            "sourceValue": left["value"],
                            "targetOperator": right["operator"],
                            "targetValue": right["value"],
                        }
                    ],
                }
            )


def _detect_opposed_intent_requirement_conflicts(
    intent_promises: list[dict[str, Any]],
    intent_index: dict[str, dict[str, Any]],
    intent_term_index: dict[str, dict[str, Any]],
    seen_pairs: set[tuple[str, str]],
    candidates: list[dict[str, Any]],
) -> None:
    requirements: list[dict[str, Any]] = []
    for intent in intent_promises:
        for requirement in intent.get("requirements", []):
            atom = _intent_requirement_atom(requirement)
            if atom is None:
                continue
            requirements.append(
                {
                    "intent": intent["name"],
                    "requirement": requirement,
                    **atom,
                }
            )

    for left_index, left in enumerate(requirements):
        for right in requirements[left_index + 1 :]:
            if left["intent"] == right["intent"]:
                continue
            conflict = _intent_requirements_conflict(left, right, intent_term_index)
            if conflict is None:
                continue
            source = conflict["source"]
            target = conflict["target"]
            if _skip_auto_intent_conflict_pair(
                intent_index[source["intent"]],
                intent_index[target["intent"]],
            ):
                continue
            pair_key = _intent_conflict_pair_key(source["intent"], target["intent"])
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            severity = _intent_conflict_candidate_severity(
                intent_index[source["intent"]],
                intent_index[target["intent"]],
            )
            candidates.append(
                {
                    "source": source["intent"],
                    "target": target["intent"],
                    "targetKnown": True,
                    "severity": severity,
                    "reason": conflict["reason"],
                    "resolution": "",
                    "note": "",
                    "sourceType": "detected",
                    "detector": "opposed-intent-requirement",
                    "evidence": [
                        {
                            "type": "intent-requirement",
                            "sourceRequirement": source["requirement"].get("id"),
                            "targetRequirement": target["requirement"].get("id"),
                            "sourceKind": source["kind"],
                            "targetKind": target["kind"],
                            "subject": source["subject"],
                            "predicate": source["predicate"],
                            "sourceObject": source["object"],
                            "targetObject": target["object"],
                            "sourceActor": source.get("actor", ""),
                            "targetActor": target.get("actor", ""),
                            "sourceAction": source.get("action", ""),
                            "targetAction": target.get("action", ""),
                            "sourceResource": source.get("resource", ""),
                            "targetResource": target.get("resource", ""),
                            "sourceOver": source.get("over", ""),
                            "targetOver": target.get("over", ""),
                            "sourceScope": source.get("scope", ""),
                            "targetScope": target.get("scope", ""),
                            "sourceEffect": source.get("effect", ""),
                            "targetEffect": target.get("effect", ""),
                            "sourceConstraint": source.get("constraint", ""),
                            "targetConstraint": target.get("constraint", ""),
                            "sourcePriority": source.get("priority", ""),
                            "targetPriority": target.get("priority", ""),
                        }
                    ],
                }
            )


def _intent_requirement_atom(requirement: dict[str, Any]) -> dict[str, str] | None:
    kind = requirement.get("kind")
    subject = requirement.get("subject")
    predicate = requirement.get("predicate")
    object_value = requirement.get("object")
    if kind not in {"requires", "forbids", "prefers"}:
        return None
    if not subject or not predicate or not object_value:
        return None
    atom = {
        "kind": kind,
        "subject": str(subject).strip(),
        "predicate": str(predicate).strip(),
        "object": str(object_value).strip(),
    }
    for resource_key in ("actor", "action", "resource"):
        if requirement.get(resource_key):
            atom[resource_key] = str(requirement[resource_key]).strip()
    if requirement.get("over"):
        atom["over"] = str(requirement["over"]).strip()
    for semantic_key in ("scope", "effect", "constraint", "priority"):
        if requirement.get(semantic_key):
            atom[semantic_key] = str(requirement[semantic_key]).strip()
    return atom


def _intent_requirements_conflict(
    left: dict[str, Any],
    right: dict[str, Any],
    intent_term_index: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if left["subject"] != right["subject"] or left["predicate"] != right["predicate"]:
        return None
    if not _intent_requirement_scopes_overlap(left, right, intent_term_index):
        return None
    if left["object"] == right["object"] and {left["kind"], right["kind"]} == {"requires", "forbids"}:
        source = left if left["kind"] == "forbids" else right
        target = right if source is left else left
        return {
            "source": source,
            "target": target,
            "reason": (
                f"{source['intent']} forbids {source['subject']} {source['predicate']} "
                f"{source['object']} while {target['intent']} requires the same requirement atom."
            ),
        }
    if (
        left["kind"] == "prefers"
        and right["kind"] == "prefers"
        and left.get("over")
        and right.get("over")
        and left["object"] == right["over"]
        and right["object"] == left["over"]
    ):
        return {
            "source": left,
            "target": right,
            "reason": (
                f"{left['intent']} prefers {left['subject']} {left['predicate']} {left['object']} "
                f"over {left['over']} while {right['intent']} prefers the reverse."
            ),
        }
    return None


def _intent_requirement_scopes_overlap(
    left: dict[str, Any],
    right: dict[str, Any],
    intent_term_index: dict[str, dict[str, Any]],
) -> bool:
    left_scope = left.get("scope")
    right_scope = right.get("scope")
    if not left_scope or not right_scope:
        return True
    if left_scope == right_scope:
        return True
    if _intent_scope_terms_disjoint(left_scope, right_scope, intent_term_index):
        return False
    return _intent_scope_is_ancestor(left_scope, right_scope, intent_term_index) or _intent_scope_is_ancestor(
        right_scope,
        left_scope,
        intent_term_index,
    )


def _intent_scope_terms_disjoint(
    left_scope: str,
    right_scope: str,
    intent_term_index: dict[str, dict[str, Any]],
) -> bool:
    for left_candidate in _intent_scope_ancestors_with_self(left_scope, intent_term_index):
        left_term = intent_term_index.get(left_candidate, {})
        for disjoint_scope in left_term.get("disjoint", []):
            if _intent_scope_same_or_descendant(right_scope, disjoint_scope, intent_term_index):
                return True
    for right_candidate in _intent_scope_ancestors_with_self(right_scope, intent_term_index):
        right_term = intent_term_index.get(right_candidate, {})
        for disjoint_scope in right_term.get("disjoint", []):
            if _intent_scope_same_or_descendant(left_scope, disjoint_scope, intent_term_index):
                return True
    return False


def _intent_scope_is_ancestor(
    ancestor_scope: str,
    descendant_scope: str,
    intent_term_index: dict[str, dict[str, Any]],
) -> bool:
    return ancestor_scope in _intent_scope_ancestors_with_self(descendant_scope, intent_term_index)


def _intent_scope_same_or_descendant(
    candidate_scope: str,
    parent_scope: str,
    intent_term_index: dict[str, dict[str, Any]],
) -> bool:
    return candidate_scope == parent_scope or _intent_scope_is_ancestor(
        parent_scope,
        candidate_scope,
        intent_term_index,
    )


def _intent_scope_ancestors_with_self(
    scope_name: str,
    intent_term_index: dict[str, dict[str, Any]],
) -> set[str]:
    ancestors = {scope_name}
    current = scope_name
    while current in intent_term_index:
        parent = intent_term_index[current].get("parent")
        if not parent or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


def _field_clause_assertions_by_id(spec: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    assertions_by_clause: dict[str, list[dict[str, str]]] = {}
    for field_promise in spec.get("fieldPromises", []):
        for clause in field_promise.get("invariants", []) + field_promise.get("globalConstraints", []):
            expression = clause.get("must")
            if not expression:
                continue
            try:
                expression_ast = parse_promise_expression(expression)
            except PromiseExpressionError:
                continue
            assertion = _extract_field_assertion(expression_ast)
            if assertion is None:
                continue
            assertions_by_clause.setdefault(clause["id"], []).append(
                {
                    "clause": clause["id"],
                    **assertion,
                }
            )
    return assertions_by_clause


def _extract_field_assertion(expression_ast: dict[str, Any]) -> dict[str, str] | None:
    if expression_ast.get("kind") != "comparison":
        return None
    operator = expression_ast.get("operator")
    if operator not in {"==", "!="}:
        return None
    left = _expression_reference_name(expression_ast.get("left", {}))
    right = _expression_value_name(expression_ast.get("right", {}))
    if left and _is_field_reference_name(left) and right:
        return {
            "subject": left,
            "operator": operator,
            "value": right,
        }
    right_ref = _expression_reference_name(expression_ast.get("right", {}))
    left_value = _expression_value_name(expression_ast.get("left", {}))
    if right_ref and _is_field_reference_name(right_ref) and left_value:
        return {
            "subject": right_ref,
            "operator": operator,
            "value": left_value,
        }
    return None


def _expression_reference_name(expression_ast: dict[str, Any]) -> str | None:
    if expression_ast.get("kind") != "reference":
        return None
    return expression_ast.get("name")


def _expression_value_name(expression_ast: dict[str, Any]) -> str | None:
    if expression_ast.get("kind") == "reference":
        return expression_ast.get("name")
    if expression_ast.get("kind") != "literal":
        return None
    literal_type = expression_ast.get("literalType")
    if literal_type == "null":
        return "null"
    if literal_type == "boolean":
        return "true" if expression_ast.get("value") else "false"
    if literal_type == "number":
        return str(expression_ast.get("value"))
    if literal_type == "string":
        return json.dumps(expression_ast.get("value"), ensure_ascii=True)
    return None


def _is_field_reference_name(value: str) -> bool:
    return len(value.split(".")) == 2


def _field_assertions_conflict(left: dict[str, str], right: dict[str, str]) -> bool:
    if left["subject"] != right["subject"]:
        return False
    if left["operator"] == "==" and right["operator"] == "==":
        return left["value"] != right["value"]
    if left["operator"] == "==" and right["operator"] == "!=":
        return left["value"] == right["value"]
    if left["operator"] == "!=" and right["operator"] == "==":
        return left["value"] == right["value"]
    return False


def _intent_conflict_candidate_severity(
    left_intent: dict[str, Any],
    right_intent: dict[str, Any],
) -> str:
    priorities = {left_intent.get("priority"), right_intent.get("priority")}
    if priorities == {"must"}:
        return "blocking"
    if "must" in priorities or "should" in priorities:
        return "tension"
    return "advisory"


def _skip_auto_intent_conflict_pair(
    left_intent: dict[str, Any],
    right_intent: dict[str, Any],
) -> bool:
    return bool(left_intent.get("root") or right_intent.get("root"))


def _intent_conflict_pair_key(source: str, target: str) -> tuple[str, str]:
    return tuple(sorted((source, target)))


def _lint_core_subset(spec: dict[str, Any]) -> list[LintIssue]:
    issues: list[LintIssue] = []

    for intent_promise in spec.get("intentPromises", []):
        issues.append(
            LintIssue(
                "core-non-minimal-intent",
                f"Intent promise '{intent_promise['name']}' uses non-core 'intent' declarations.",
            )
        )

    for intent_resource in spec.get("intentResources", []):
        issues.append(
            LintIssue(
                "core-non-minimal-resource",
                f"Intent resource '{intent_resource['name']}' uses non-core 'resource' declarations.",
            )
        )

    for intent_term in spec.get("intentTerms", []):
        issues.append(
            LintIssue(
                "core-non-minimal-term",
                f"Intent term '{intent_term['name']}' uses non-core 'term' declarations.",
            )
        )

    for intent_cycle in spec.get("intentCycles", []):
        issues.append(
            LintIssue(
                "core-non-minimal-cycle",
                f"Intent cycle '{intent_cycle['name']}' uses non-core 'cycle' declarations.",
            )
        )

    for type_promise in spec.get("typePromises", []):
        issues.append(
            LintIssue(
                "core-non-minimal-type",
                f"Type promise '{type_promise['name']}' uses non-core 'type' declarations.",
            )
        )

    for field_promise in spec.get("fieldPromises", []):
        if field_promise.get("states"):
            issues.append(
                LintIssue(
                    "core-non-minimal-state",
                    f"Field promise '{field_promise['name']}' uses non-core 'state' definitions.",
                )
            )
        if field_promise.get("globalConstraints"):
            issues.append(
                LintIssue(
                    "core-non-minimal-constraint",
                    f"Field promise '{field_promise['name']}' uses non-core 'constraint' clauses.",
                )
            )
        for field in field_promise.get("fields", []):
            field_ref = f"{field_promise['object']}.{field['name']}"
            if field.get("readers"):
                issues.append(
                    LintIssue(
                        "core-non-minimal-readers",
                        f"Field '{field_ref}' uses non-core 'readers' metadata.",
                    )
                )
            if field.get("writers"):
                issues.append(
                    LintIssue(
                        "core-non-minimal-writers",
                        f"Field '{field_ref}' uses non-core 'writers' metadata.",
                    )
                )
            if field.get("derivedFrom"):
                issues.append(
                    LintIssue(
                        "core-non-minimal-derived",
                        f"Field '{field_ref}' uses non-core 'derived' metadata.",
                    )
                )

    for function_promise in spec.get("functionPromises", []):
        if function_promise.get("dependsOn"):
            issues.append(
                LintIssue(
                    "core-non-minimal-depends",
                    f"Function promise '{function_promise['name']}' uses non-core 'depends' clauses.",
                )
            )
        if function_promise.get("preconditions"):
            issues.append(
                LintIssue(
                    "core-non-minimal-precondition",
                    f"Function promise '{function_promise['name']}' uses non-core 'precondition' clauses.",
                )
            )
        if function_promise.get("failureConditions"):
            issues.append(
                LintIssue(
                    "core-non-minimal-reject",
                    f"Function promise '{function_promise['name']}' uses non-core 'reject' clauses.",
                )
            )
        if function_promise.get("sideEffects"):
            issues.append(
                LintIssue(
                    "core-non-minimal-sideeffect",
                    f"Function promise '{function_promise['name']}' uses non-core 'sideeffect' clauses.",
                )
            )
        if function_promise.get("idempotency"):
            issues.append(
                LintIssue(
                    "core-non-minimal-idempotency",
                    f"Function promise '{function_promise['name']}' uses non-core 'idempotency' metadata.",
                )
            )

    for verification_promise in spec.get("verificationPromises", []):
        if verification_promise.get("evidenceRequired"):
            issues.append(
                LintIssue(
                    "core-non-minimal-evidence",
                    f"Verification promise '{verification_promise['name']}' uses non-core 'evidence' clauses.",
                )
            )
        for scenario in verification_promise.get("scenarios", []):
            if scenario.get("given"):
                issues.append(
                    LintIssue(
                        "core-non-minimal-given",
                        f"Scenario '{scenario['name']}' in '{verification_promise['name']}' uses non-core 'given' steps.",
                    )
                )
            if scenario.get("regressionGuards"):
                issues.append(
                    LintIssue(
                        "core-non-minimal-guard",
                        f"Scenario '{scenario['name']}' in '{verification_promise['name']}' uses non-core 'guard' steps.",
                    )
                )

    return issues


def _field_requires_invariant_coverage(field_promise: dict[str, Any]) -> bool:
    fields = field_promise.get("fields", [])
    states = field_promise.get("states", [])
    return bool(states) or len(fields) > 1 or any(field.get("derivedFrom") for field in fields)


def _field_requires_forbid_coverage(field_promise: dict[str, Any]) -> bool:
    fields = field_promise.get("fields", [])
    states = field_promise.get("states", [])
    return bool(states) or len(fields) > 1 or any(field.get("derivedFrom") for field in fields)


def _function_requires_forbid_coverage(function_promise: dict[str, Any]) -> bool:
    return bool(
        function_promise.get("writes")
        or function_promise.get("sideEffects")
        or function_promise.get("failureConditions")
    )


def _collect_unique_names(
    items: list[dict[str, Any]], key: str, code: str, issues: list[LintIssue]
) -> set[str]:
    seen: set[str] = set()
    for item in items:
        value = item[key]
        if value in seen:
            issues.append(LintIssue(code, f"Duplicate name '{value}' found."))
        seen.add(value)
    return seen


def _collect_clause_ids(
    clauses: list[dict[str, Any]],
    clause_ids: set[str],
    issues: list[LintIssue],
    owner: str,
) -> None:
    for clause in clauses:
        clause_id = clause["id"]
        if clause_id in clause_ids:
            issues.append(
                LintIssue(
                    "clause-duplicate-id",
                    f"Clause id '{clause_id}' is duplicated and reused in '{owner}'.",
                )
            )
        clause_ids.add(clause_id)


def _lint_intent_parent_cycles(intent_promises: list[dict[str, Any]], issues: list[LintIssue]) -> None:
    parent_by_name: dict[str, str] = {}
    for intent_promise in intent_promises:
        parents = intent_promise.get("parents", [])
        if len(parents) == 1:
            parent_by_name[intent_promise["name"]] = parents[0]["target"]

    intent_names = {intent_promise["name"] for intent_promise in intent_promises}
    for intent_name in intent_names:
        seen: set[str] = set()
        current = intent_name
        while current in parent_by_name:
            if current in seen:
                issues.append(
                    LintIssue(
                        "intent-parent-cycle",
                        f"Intent tree contains a parent cycle involving '{current}'.",
                    )
                )
                break
            seen.add(current)
            parent = parent_by_name[current]
            if parent not in intent_names:
                break
            current = parent


def _lint_intent_term_parent_cycles(intent_terms: list[dict[str, Any]], issues: list[LintIssue]) -> None:
    parent_by_name = {
        intent_term["name"]: intent_term["parent"]
        for intent_term in intent_terms
        if intent_term.get("parent")
    }
    term_names = {intent_term["name"] for intent_term in intent_terms}
    for term_name in term_names:
        seen: set[str] = set()
        current = term_name
        while current in parent_by_name:
            if current in seen:
                issues.append(
                    LintIssue(
                        "intent-term-parent-cycle",
                        f"Intent term hierarchy contains a parent cycle involving '{current}'.",
                    )
                )
                break
            seen.add(current)
            parent = parent_by_name[current]
            if parent not in term_names:
                break
            current = parent


def _is_known_field_type(field_type: str, declared_type_names: set[str]) -> bool:
    if field_type in PRIMITIVE_FIELD_TYPES:
        return True
    if field_type in declared_type_names:
        return True
    return _enum_choices(field_type) is not None


def _enum_choices(field_type: str) -> list[str] | None:
    enum_match = ENUM_TYPE_RE.match(field_type)
    if enum_match is None:
        return None
    choices = [item.strip() for item in enum_match.group(1).split("|") if item.strip()]
    return choices or None


class _PromiseParser:
    def __init__(self, text: str) -> None:
        self.records = self._prepare_records(text)
        self.spec = {
            "schemaVersion": SCHEMA_VERSION,
            "meta": {
                "owners": [],
                "sourceDocuments": [],
            },
            "intentPromises": [],
            "intentResources": [],
            "intentTerms": [],
            "intentCycles": [],
            "typePromises": [],
            "fieldPromises": [],
            "functionPromises": [],
            "verificationPromises": [],
        }
        self.current_top: dict[str, Any] | None = None
        self.current_kind: str | None = None
        self.current_scenario: dict[str, Any] | None = None

    def parse(self) -> dict[str, Any]:
        for line_no, indent, content in self.records:
            if indent == 0:
                self._parse_top_level(line_no, content)
                continue

            if indent == 2:
                self._parse_second_level(line_no, content)
                continue

            if indent == 4:
                self._parse_third_level(line_no, content)
                continue

            self._error(line_no, "Indentation must use only 0, 2, or 4 spaces.")

        self._finalize()
        return self.spec

    def _prepare_records(self, text: str) -> list[tuple[int, int, str]]:
        records: list[tuple[int, int, str]] = []
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent % 2 != 0:
                self._error(line_no, "Indentation must be a multiple of two spaces.")
            records.append((line_no, indent, raw_line.strip()))
        return records

    def _parse_top_level(self, line_no: int, content: str) -> None:
        self.current_scenario = None

        if content == "meta:":
            self.current_kind = "meta"
            self.current_top = self.spec["meta"]
            return

        if content.startswith("intent ") and content.endswith(":"):
            self.current_kind = "intent"
            self.current_top = self._new_intent_promise(line_no, content)
            self.spec["intentPromises"].append(self.current_top)
            return

        if content.startswith("resource ") and content.endswith(":"):
            self.current_kind = "resource"
            self.current_top = self._new_intent_resource(line_no, content)
            self.spec["intentResources"].append(self.current_top)
            return

        if content.startswith("term ") and content.endswith(":"):
            self.current_kind = "term"
            self.current_top = self._new_intent_term(line_no, content)
            self.spec["intentTerms"].append(self.current_top)
            return

        if content.startswith("cycle ") and content.endswith(":"):
            self.current_kind = "cycle"
            self.current_top = self._new_intent_cycle(line_no, content)
            self.spec["intentCycles"].append(self.current_top)
            return

        if content.startswith("type ") and content.endswith(":"):
            self.current_kind = "type"
            self.current_top = self._new_type_promise(line_no, content)
            self.spec["typePromises"].append(self.current_top)
            return

        if content.startswith("field ") and content.endswith(":"):
            self.current_kind = "field"
            self.current_top = self._new_field_promise(line_no, content)
            self.spec["fieldPromises"].append(self.current_top)
            return

        if content.startswith("function ") and content.endswith(":"):
            self.current_kind = "function"
            self.current_top = self._new_function_promise(line_no, content)
            self.spec["functionPromises"].append(self.current_top)
            return

        if content.startswith("verify ") and content.endswith(":"):
            self.current_kind = "verify"
            self.current_top = self._new_verification_promise(line_no, content)
            self.spec["verificationPromises"].append(self.current_top)
            return

        self._error(line_no, f"Unknown top-level declaration '{content}'.")

    def _parse_second_level(self, line_no: int, content: str) -> None:
        self.current_scenario = None

        if self.current_kind == "meta":
            self._parse_meta_line(line_no, content)
            return
        if self.current_kind == "intent":
            self._parse_intent_line(line_no, content)
            return
        if self.current_kind == "resource":
            self._parse_intent_resource_line(line_no, content)
            return
        if self.current_kind == "term":
            self._parse_intent_term_line(line_no, content)
            return
        if self.current_kind == "cycle":
            self._parse_intent_cycle_line(line_no, content)
            return
        if self.current_kind == "type":
            self._parse_type_line(line_no, content)
            return
        if self.current_kind == "field":
            self._parse_field_line(line_no, content)
            return
        if self.current_kind == "function":
            self._parse_function_line(line_no, content)
            return
        if self.current_kind == "verify":
            if content.startswith("scenario ") and content.endswith(":"):
                self.current_scenario = self._new_scenario(line_no, content)
                self.current_top["scenarios"].append(self.current_scenario)
                return
            self._parse_verification_line(line_no, content)
            return

        self._error(line_no, "Nested content appears before a valid top-level block.")

    def _parse_third_level(self, line_no: int, content: str) -> None:
        if self.current_kind != "verify" or self.current_scenario is None:
            self._error(line_no, "Only verification scenarios may contain 4-space nested content.")
        self._parse_scenario_line(line_no, content)

    def _parse_meta_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        key = tokens[0]
        value = self._single_or_joined_text(tokens, 1, line_no)

        if key in {"title", "domain", "version", "status", "summary"}:
            self.current_top[key] = value
            return
        if key == "owner":
            self.current_top["owners"].append(value)
            return
        if key == "source":
            self.current_top["sourceDocuments"].append(value)
            return
        self._error(line_no, f"Unknown meta property '{key}'.")

    def _parse_intent_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "statement":
            self.current_top["statement"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "rationale":
            self.current_top["rationale"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "status":
            self.current_top["status"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "root":
            self.current_top["root"] = self._parse_bool(
                line_no, self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        if keyword == "parent":
            self.current_top["parents"].append(self._parse_intent_parent(line_no, tokens))
            return
        if keyword == "conflicts":
            self.current_top["conflicts"].append(self._parse_intent_conflict(line_no, tokens))
            return
        if keyword == "source":
            self.current_top["sources"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword in INTENT_REQUIREMENT_KINDS:
            self.current_top["requirements"].append(
                self._parse_intent_requirement(line_no, keyword, tokens)
            )
            return
        if keyword == "maps":
            self.current_top["maps"].append(self._parse_intent_map(line_no, tokens))
            return
        self._error(line_no, f"Unknown intent block property '{keyword}'.")

    def _parse_intent_resource_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "alias":
            self.current_top["aliases"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "maps":
            self.current_top["maps"].append(self._parse_intent_map(line_no, tokens))
            return
        self._error(line_no, f"Unknown resource block property '{keyword}'.")

    def _parse_intent_term_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "alias":
            self.current_top["aliases"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "parent":
            self.current_top["parent"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "disjoint":
            self.current_top["disjoint"].extend(
                self._parse_csv(self._single_or_joined_text(tokens, 1, line_no))
            )
            return
        if keyword == "opposite":
            self.current_top["opposites"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "maps":
            self.current_top["maps"].append(self._parse_intent_map(line_no, tokens))
            return
        self._error(line_no, f"Unknown term block property '{keyword}'.")

    def _parse_intent_cycle_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "rationale":
            self.current_top["rationale"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "edge":
            self.current_top["edges"].append(self._parse_intent_cycle_edge(line_no, tokens))
            return
        self._error(line_no, f"Unknown cycle block property '{keyword}'.")

    def _parse_type_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "format":
            self.current_top["format"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "generated":
            self.current_top["generated"] = self._parse_bool(
                line_no, self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        self._error(line_no, f"Unknown type block property '{keyword}'.")

    def _parse_field_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "field":
            self.current_top["fields"].append(self._parse_field_definition(line_no, tokens))
            return
        if keyword == "state":
            self.current_top["states"].append(self._parse_state_definition(line_no, tokens))
            return
        if keyword == "invariant":
            self.current_top["invariants"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "constraint":
            self.current_top["globalConstraints"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "forbid":
            self.current_top["forbiddenImplicitState"].append(
                self._parse_clause(line_no, tokens, 1)
            )
            return
        self._error(line_no, f"Unknown field block property '{keyword}'.")

    def _parse_function_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "summary":
            self.current_top["summary"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "depends":
            self.current_top["dependsOn"] = self._parse_csv(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "trigger":
            self.current_top["triggers"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "precondition":
            self.current_top["preconditions"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "reads":
            self.current_top["reads"] = self._parse_csv(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "writes":
            self.current_top["writes"] = self._parse_csv(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "ensure":
            self.current_top["successResults"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "reject":
            self.current_top["failureConditions"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "sideeffect":
            self.current_top["sideEffects"].append(self._parse_clause(line_no, tokens, 1))
            return
        if keyword == "idempotency":
            self.current_top["idempotency"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "forbid":
            self.current_top["forbidden"].append(self._parse_clause(line_no, tokens, 1))
            return
        self._error(line_no, f"Unknown function block property '{keyword}'.")

    def _parse_verification_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "claim":
            self.current_top["claim"] = self._single_or_joined_text(tokens, 1, line_no)
            return
        if keyword == "verifies":
            self.current_top["verifies"] = self._parse_csv(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        if keyword == "methods":
            self.current_top["methods"] = self._parse_csv(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        if keyword == "evidence":
            self.current_top["evidenceRequired"].append(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        if keyword == "fail":
            self.current_top["failureCriteria"].append(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        self._error(line_no, f"Unknown verification block property '{keyword}'.")

    def _parse_scenario_line(self, line_no: int, content: str) -> None:
        tokens = self._tokenize(line_no, content)
        keyword = tokens[0]

        if keyword == "covers":
            self.current_scenario["covers"] = self._parse_csv(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        if keyword == "given":
            self.current_scenario["given"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "when":
            self.current_scenario["when"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "then":
            self.current_scenario["then"].append(self._single_or_joined_text(tokens, 1, line_no))
            return
        if keyword == "guard":
            self.current_scenario["regressionGuards"].append(
                self._single_or_joined_text(tokens, 1, line_no)
            )
            return
        self._error(line_no, f"Unknown scenario property '{keyword}'.")

    def _new_field_promise(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "field" or tokens[2] != "for":
            self._error(line_no, "Field blocks must use 'field <Name> for <Object>:' syntax.")
        return {
            "name": tokens[1],
            "object": tokens[3],
            "summary": "",
            "fields": [],
            "states": [],
            "invariants": [],
            "globalConstraints": [],
            "forbiddenImplicitState": [],
        }

    def _new_intent_promise(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "intent" or tokens[2] != "priority":
            self._error(line_no, "Intent blocks must use 'intent <Name> priority <Priority>:' syntax.")
        return {
            "name": tokens[1],
            "priority": tokens[3],
            "status": "active",
            "root": False,
            "statement": "",
            "rationale": "",
            "sources": [],
            "parents": [],
            "conflicts": [],
            "requirements": [],
            "maps": [],
        }

    def _new_intent_resource(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "resource" or tokens[2] != "kind":
            self._error(line_no, "Resource blocks must use 'resource <Name> kind <Kind>:' syntax.")
        return {
            "name": tokens[1],
            "kind": tokens[3],
            "summary": "",
            "aliases": [],
            "maps": [],
        }

    def _new_intent_term(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "term" or tokens[2] != "kind":
            self._error(line_no, "Term blocks must use 'term <Name> kind <Kind>:' syntax.")
        return {
            "name": tokens[1],
            "kind": tokens[3],
            "summary": "",
            "aliases": [],
            "parent": "",
            "disjoint": [],
            "opposites": [],
            "maps": [],
        }

    def _new_intent_cycle(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "cycle" or tokens[2] != "kind":
            self._error(line_no, "Cycle blocks must use 'cycle <Name> kind <Kind>:' syntax.")
        return {
            "name": tokens[1],
            "kind": tokens[3],
            "summary": "",
            "rationale": "",
            "nodes": [],
            "edges": [],
        }

    def _new_type_promise(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 6 or tokens[0] != "type" or tokens[2] != "kind" or tokens[4] != "base":
            self._error(line_no, "Type blocks must use 'type <Name> kind <Kind> base <Base>:' syntax.")
        return {
            "name": tokens[1],
            "kind": tokens[3],
            "base": tokens[5],
            "summary": "",
        }

    def _new_function_promise(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "function" or tokens[2] != "action":
            self._error(
                line_no, "Function blocks must use 'function <Name> action <Action>:' syntax."
            )
        return {
            "name": tokens[1],
            "action": tokens[3],
            "summary": "",
            "dependsOn": [],
            "triggers": [],
            "preconditions": [],
            "reads": [],
            "writes": [],
            "successResults": [],
            "failureConditions": [],
            "sideEffects": [],
            "forbidden": [],
        }

    def _new_verification_promise(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) != 4 or tokens[0] != "verify" or tokens[2] != "kind":
            self._error(line_no, "Verification blocks must use 'verify <Name> kind <Kind>:' syntax.")
        return {
            "name": tokens[1],
            "kind": tokens[3],
            "claim": "",
            "verifies": [],
            "methods": [],
            "scenarios": [],
            "evidenceRequired": [],
            "failureCriteria": [],
        }

    def _new_scenario(self, line_no: int, content: str) -> dict[str, Any]:
        tokens = self._tokenize(line_no, content[:-1])
        if len(tokens) < 2 or tokens[0] != "scenario":
            self._error(line_no, "Scenario blocks must use 'scenario <Name>:' syntax.")
        return {
            "name": " ".join(tokens[1:]),
            "covers": [],
            "given": [],
            "when": [],
            "then": [],
            "regressionGuards": [],
        }

    def _parse_field_definition(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 3:
            self._error(line_no, "Field definitions require at least a name and one attribute.")
        name = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        required_keys = {"type", "required", "nullable", "default", "semantic"}
        self._missing_keys(line_no, attrs, required_keys, "field definition")
        field = {
            "name": name,
            "type": attrs["type"],
            "required": self._parse_bool(line_no, attrs["required"]),
            "nullable": self._parse_bool(line_no, attrs["nullable"]),
            "default": self._parse_scalar(attrs["default"]),
            "semantic": attrs["semantic"],
        }
        if "mutable" in attrs:
            field["mutable"] = self._parse_bool(line_no, attrs["mutable"])
        if "system" in attrs:
            field["systemManaged"] = self._parse_bool(line_no, attrs["system"])
        if "readers" in attrs:
            field["readers"] = self._parse_csv(attrs["readers"])
        if "writers" in attrs:
            field["writers"] = self._parse_csv(attrs["writers"])
        if "derived" in attrs:
            field["derivedFrom"] = self._parse_csv(attrs["derived"])
        return field

    def _parse_state_definition(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 3:
            self._error(line_no, "State definitions require a value and attributes.")
        value = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        required_keys = {"meaning", "terminal", "transitions"}
        self._missing_keys(line_no, attrs, required_keys, "state definition")
        state = {
            "value": value,
            "meaning": attrs["meaning"],
            "terminal": self._parse_bool(line_no, attrs["terminal"]),
            "transitions": self._parse_csv(attrs["transitions"]),
        }
        if "initial" in attrs:
            state["initial"] = self._parse_bool(line_no, attrs["initial"])
        return state

    def _parse_intent_parent(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 4:
            self._error(line_no, "Intent parents require 'parent <IntentName> relation <Relation>' syntax.")
        target = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        if "relation" not in attrs:
            self._error(line_no, f"Intent parent '{target}' is missing required attribute 'relation'.")
        parent = {
            "target": target,
            "relation": attrs["relation"],
        }
        if "note" in attrs:
            parent["note"] = attrs["note"]
        return parent

    def _parse_intent_conflict(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 6:
            self._error(
                line_no,
                "Intent conflicts require 'conflicts <IntentName> severity <Severity> reason <Text>' syntax.",
            )
        target = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        required_keys = {"severity", "reason"}
        self._missing_keys(line_no, attrs, required_keys, f"intent conflict '{target}'")
        intent_conflict = {
            "target": target,
            "severity": attrs["severity"],
            "reason": attrs["reason"],
        }
        if "resolution" in attrs:
            intent_conflict["resolution"] = attrs["resolution"]
        if "note" in attrs:
            intent_conflict["note"] = attrs["note"]
        return intent_conflict

    def _parse_intent_requirement(self, line_no: int, kind: str, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 8:
            self._error(
                line_no,
                (
                    "Intent requirements require "
                    "'<requires|forbids|prefers|accepts> <RequirementId> "
                    "subject <Subject> predicate <Predicate> object <Object>' or "
                    "'<requires|forbids|prefers|accepts> <RequirementId> "
                    "actor <Resource> action <Action> resource <Resource>' syntax, "
                    "with optional scope/effect/constraint/priority atoms."
                ),
            )
        requirement_id = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        uses_operation_shape = any(key in attrs for key in INTENT_REQUIREMENT_OPERATION_KEYS)
        required_keys = (
            INTENT_REQUIREMENT_OPERATION_KEYS
            if uses_operation_shape
            else INTENT_REQUIREMENT_REQUIRED_KEYS
        )
        self._missing_keys(
            line_no,
            attrs,
            required_keys,
            f"intent requirement '{requirement_id}'",
        )
        if kind == "prefers" and "over" not in attrs:
            self._error(line_no, f"Intent preference '{requirement_id}' is missing required attribute 'over'.")
        requirement = {
            "id": requirement_id,
            "kind": kind,
        }
        if uses_operation_shape:
            requirement["actor"] = attrs["actor"]
            requirement["action"] = attrs["action"]
            requirement["resource"] = attrs["resource"]
            requirement["subject"] = attrs["actor"]
            requirement["predicate"] = attrs["action"]
            requirement["object"] = attrs["resource"]
        else:
            requirement["subject"] = attrs["subject"]
            requirement["predicate"] = attrs["predicate"]
            requirement["object"] = attrs["object"]
        if "over" in attrs:
            requirement["over"] = attrs["over"]
        for semantic_key in ("scope", "effect", "constraint"):
            if semantic_key in attrs:
                requirement[semantic_key] = attrs[semantic_key]
        requirement["priority"] = attrs.get("priority") or self.current_top.get("priority")
        if "when" in attrs:
            requirement["when"] = attrs["when"]
        if "because" in attrs:
            requirement["because"] = attrs["because"]
        if "note" in attrs:
            requirement["note"] = attrs["note"]
        return requirement

    def _parse_intent_map(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 4:
            self._error(line_no, "Intent maps require 'maps <Target> relation <Relation>' syntax.")
        target = tokens[1]
        attrs = self._parse_pairs(line_no, tokens[2:])
        if "relation" not in attrs:
            self._error(line_no, f"Intent map '{target}' is missing required attribute 'relation'.")
        intent_map = {
            "target": target,
            "relation": attrs["relation"],
        }
        if "note" in attrs:
            intent_map["note"] = attrs["note"]
        return intent_map

    def _parse_intent_cycle_edge(self, line_no: int, tokens: list[str]) -> dict[str, Any]:
        if len(tokens) < 5 or tokens[2] != "->":
            self._error(line_no, "Cycle edges require 'edge <Source> -> <Target> relation <Relation>' syntax.")
        source = tokens[1]
        target = tokens[3]
        attrs = self._parse_pairs(line_no, tokens[4:])
        if "relation" not in attrs:
            self._error(line_no, f"Cycle edge '{source} -> {target}' is missing required attribute 'relation'.")
        edge = {
            "source": source,
            "target": target,
            "relation": attrs["relation"],
        }
        if "note" in attrs:
            edge["note"] = attrs["note"]
        return edge

    def _parse_clause(self, line_no: int, tokens: list[str], start_index: int) -> dict[str, Any]:
        if len(tokens) <= start_index:
            self._error(line_no, "Clause declarations require an id.")
        clause_id = tokens[start_index]
        attrs = self._parse_pairs(line_no, tokens[start_index + 1 :])
        if "statement" not in attrs:
            self._error(line_no, f"Clause '{clause_id}' is missing required attribute 'statement'.")
        clause = {
            "id": clause_id,
            "statement": attrs["statement"],
        }
        if "refs" in attrs:
            clause["refs"] = self._parse_csv(attrs["refs"])
        if "when" in attrs:
            clause["when"] = attrs["when"]
        if "must" in attrs:
            clause["must"] = attrs["must"]
        if "severity" in attrs:
            clause["severity"] = attrs["severity"]
        return clause

    def _parse_pairs(self, line_no: int, tokens: list[str]) -> dict[str, str]:
        if len(tokens) % 2 != 0:
            self._error(line_no, f"Expected key/value pairs but got '{' '.join(tokens)}'.")
        attrs: dict[str, str] = {}
        for index in range(0, len(tokens), 2):
            key = tokens[index]
            value = tokens[index + 1]
            attrs[key] = value
        return attrs

    def _tokenize(self, line_no: int, content: str) -> list[str]:
        try:
            return shlex.split(content)
        except ValueError as exc:
            self._error(line_no, f"Invalid syntax: {exc}.")
        raise AssertionError("unreachable")

    def _parse_csv(self, value: str) -> list[str]:
        stripped = value.strip()
        if stripped in {"-", ""}:
            return []
        return [item.strip() for item in stripped.split(",") if item.strip()]

    def _single_or_joined_text(self, tokens: list[str], start: int, line_no: int) -> str:
        if len(tokens) <= start:
            self._error(line_no, "Missing value.")
        return " ".join(tokens[start:])

    def _parse_bool(self, line_no: int, value: str) -> bool:
        lowered = value.lower()
        if lowered not in BOOLEAN_VALUES:
            self._error(line_no, f"Expected boolean true/false but got '{value}'.")
        return BOOLEAN_VALUES[lowered]

    def _parse_scalar(self, value: str) -> Any:
        lowered = value.lower()
        if lowered == "null":
            return None
        if lowered in BOOLEAN_VALUES:
            return BOOLEAN_VALUES[lowered]
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"-?\d+\.\d+", value):
            return float(value)
        return value

    def _missing_keys(
        self, line_no: int, attrs: dict[str, str], required: set[str], context: str
    ) -> None:
        missing = sorted(required - set(attrs))
        if missing:
            self._error(line_no, f"Missing {', '.join(missing)} in {context}.")

    def _finalize(self) -> None:
        for intent_cycle in self.spec.get("intentCycles", []):
            intent_cycle["nodes"] = _intent_cycle_node_refs(intent_cycle)
        return None

    def _error(self, line_no: int, message: str) -> None:
        raise PromiseParseError(f"Line {line_no}: {message}")


def to_json(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True)


def clone_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(spec)


def format_spec(spec: dict[str, Any]) -> str:
    lines: list[str] = []

    lines.extend(_format_meta(spec["meta"]))

    for intent_resource in spec.get("intentResources", []):
        lines.append("")
        lines.extend(_format_intent_resource(intent_resource))

    for intent_term in spec.get("intentTerms", []):
        lines.append("")
        lines.extend(_format_intent_term(intent_term))

    for intent_cycle in spec.get("intentCycles", []):
        lines.append("")
        lines.extend(_format_intent_cycle(intent_cycle))

    for intent_promise in spec.get("intentPromises", []):
        lines.append("")
        lines.extend(_format_intent_promise(intent_promise))

    for type_promise in spec.get("typePromises", []):
        lines.append("")
        lines.extend(_format_type_promise(type_promise))

    for field_promise in spec.get("fieldPromises", []):
        lines.append("")
        lines.extend(_format_field_promise(field_promise))

    for function_promise in spec.get("functionPromises", []):
        lines.append("")
        lines.extend(_format_function_promise(function_promise))

    for verification_promise in spec.get("verificationPromises", []):
        lines.append("")
        lines.extend(_format_verification_promise(verification_promise))

    return "\n".join(lines) + "\n"


def _format_meta(meta: dict[str, Any]) -> list[str]:
    lines = ["meta:"]
    ordered_keys = ("title", "domain", "version", "status")
    for key in ordered_keys:
        lines.append(f"  {key} {_format_atom(meta[key])}")
    for owner in meta.get("owners", []):
        lines.append(f"  owner {_format_atom(owner)}")
    lines.append(f"  summary {_format_atom(meta['summary'])}")
    for source in meta.get("sourceDocuments", []):
        lines.append(f"  source {_format_atom(source)}")
    return lines


def _format_intent_resource(intent_resource: dict[str, Any]) -> list[str]:
    lines = [f"resource {intent_resource['name']} kind {_format_atom(intent_resource['kind'])}:"]
    lines.append(f"  summary {_format_atom(intent_resource['summary'])}")
    for alias in intent_resource.get("aliases", []):
        lines.append(f"  alias {_format_atom(alias)}")
    for resource_map in intent_resource.get("maps", []):
        parts = [
            "maps",
            resource_map["target"],
            f"relation {_format_atom(resource_map['relation'])}",
        ]
        if resource_map.get("note"):
            parts.append(f"note {_format_atom(resource_map['note'])}")
        lines.append("  " + " ".join(parts))
    return lines


def _format_intent_term(intent_term: dict[str, Any]) -> list[str]:
    lines = [f"term {intent_term['name']} kind {_format_atom(intent_term['kind'])}:"]
    lines.append(f"  summary {_format_atom(intent_term['summary'])}")
    for alias in intent_term.get("aliases", []):
        lines.append(f"  alias {_format_atom(alias)}")
    if intent_term.get("parent"):
        lines.append(f"  parent {_format_atom(intent_term['parent'])}")
    if intent_term.get("disjoint"):
        lines.append(f"  disjoint {_format_csv(intent_term['disjoint'])}")
    for opposite in intent_term.get("opposites", []):
        lines.append(f"  opposite {_format_atom(opposite)}")
    for term_map in intent_term.get("maps", []):
        parts = [
            "maps",
            term_map["target"],
            f"relation {_format_atom(term_map['relation'])}",
        ]
        if term_map.get("note"):
            parts.append(f"note {_format_atom(term_map['note'])}")
        lines.append("  " + " ".join(parts))
    return lines


def _format_intent_cycle(intent_cycle: dict[str, Any]) -> list[str]:
    lines = [f"cycle {intent_cycle['name']} kind {_format_atom(intent_cycle['kind'])}:"]
    lines.append(f"  summary {_format_atom(intent_cycle['summary'])}")
    if intent_cycle.get("rationale"):
        lines.append(f"  rationale {_format_atom(intent_cycle['rationale'])}")
    for edge in intent_cycle.get("edges", []):
        parts = [
            "edge",
            edge["source"],
            "->",
            edge["target"],
            f"relation {_format_atom(edge['relation'])}",
        ]
        if edge.get("note"):
            parts.append(f"note {_format_atom(edge['note'])}")
        lines.append("  " + " ".join(parts))
    return lines


def _format_intent_promise(intent_promise: dict[str, Any]) -> list[str]:
    lines = [f"intent {intent_promise['name']} priority {intent_promise['priority']}:"]
    lines.append(f"  statement {_format_atom(intent_promise['statement'])}")
    if intent_promise.get("rationale"):
        lines.append(f"  rationale {_format_atom(intent_promise['rationale'])}")
    lines.append(f"  status {_format_atom(intent_promise.get('status', 'active'))}")
    if intent_promise.get("root"):
        lines.append("  root true")
    for source in intent_promise.get("sources", []):
        lines.append(f"  source {_format_atom(source)}")
    for parent in intent_promise.get("parents", []):
        parts = [
            "parent",
            parent["target"],
            f"relation {_format_atom(parent['relation'])}",
        ]
        if parent.get("note"):
            parts.append(f"note {_format_atom(parent['note'])}")
        lines.append("  " + " ".join(parts))
    for requirement in intent_promise.get("requirements", []):
        parts = [requirement.get("kind", "requires"), requirement["id"]]
        if all(requirement.get(key) for key in ("actor", "action", "resource")):
            parts.extend(
                [
                    f"actor {_format_atom(requirement['actor'])}",
                    f"action {_format_atom(requirement['action'])}",
                    f"resource {_format_atom(requirement['resource'])}",
                ]
            )
        else:
            parts.extend(
                [
                    f"subject {_format_atom(requirement['subject'])}",
                    f"predicate {_format_atom(requirement['predicate'])}",
                    f"object {_format_atom(requirement['object'])}",
                ]
            )
        if requirement.get("over"):
            parts.append(f"over {_format_atom(requirement['over'])}")
        for semantic_key in ("scope", "effect", "constraint"):
            if requirement.get(semantic_key):
                parts.append(f"{semantic_key} {_format_atom(requirement[semantic_key])}")
        effective_priority = requirement.get("priority") or intent_promise.get("priority")
        if effective_priority:
            parts.append(f"priority {_format_atom(effective_priority)}")
        if requirement.get("when"):
            parts.append(f"when {_format_atom(requirement['when'])}")
        if requirement.get("because"):
            parts.append(f"because {_format_atom(requirement['because'])}")
        if requirement.get("note"):
            parts.append(f"note {_format_atom(requirement['note'])}")
        lines.append("  " + " ".join(parts))
    for intent_conflict in intent_promise.get("conflicts", []):
        parts = [
            "conflicts",
            intent_conflict["target"],
            f"severity {_format_atom(intent_conflict['severity'])}",
            f"reason {_format_atom(intent_conflict['reason'])}",
        ]
        if intent_conflict.get("resolution"):
            parts.append(f"resolution {_format_atom(intent_conflict['resolution'])}")
        if intent_conflict.get("note"):
            parts.append(f"note {_format_atom(intent_conflict['note'])}")
        lines.append("  " + " ".join(parts))
    for intent_map in intent_promise.get("maps", []):
        parts = [
            "maps",
            intent_map["target"],
            f"relation {_format_atom(intent_map['relation'])}",
        ]
        if intent_map.get("note"):
            parts.append(f"note {_format_atom(intent_map['note'])}")
        lines.append("  " + " ".join(parts))
    return lines


def _format_type_promise(type_promise: dict[str, Any]) -> list[str]:
    lines = [
        f"type {type_promise['name']} kind {type_promise['kind']} base {_format_atom(type_promise['base'])}:"
    ]
    lines.append(f"  summary {_format_atom(type_promise['summary'])}")
    if type_promise.get("format"):
        lines.append(f"  format {_format_atom(type_promise['format'])}")
    if "generated" in type_promise:
        lines.append(f"  generated {_format_bool(type_promise['generated'])}")
    return lines


def _format_field_promise(field_promise: dict[str, Any]) -> list[str]:
    lines = [f"field {field_promise['name']} for {field_promise['object']}:"]
    lines.append(f"  summary {_format_atom(field_promise['summary'])}")

    for field in field_promise.get("fields", []):
        parts = [
            f"field {field['name']}",
            f"type {_format_atom(field['type'])}",
            f"required {_format_bool(field['required'])}",
            f"nullable {_format_bool(field['nullable'])}",
            f"default {_format_scalar(field.get('default'))}",
            f"semantic {_format_atom(field['semantic'])}",
        ]
        if "mutable" in field:
            parts.append(f"mutable {_format_bool(field['mutable'])}")
        if "systemManaged" in field:
            parts.append(f"system {_format_bool(field['systemManaged'])}")
        if field.get("readers") is not None:
            parts.append(f"readers {_format_csv(field.get('readers', []))}")
        if field.get("writers") is not None:
            parts.append(f"writers {_format_csv(field.get('writers', []))}")
        if field.get("derivedFrom"):
            parts.append(f"derived {_format_csv(field['derivedFrom'])}")
        lines.append("  " + " ".join(parts))

    for state in field_promise.get("states", []):
        parts = [
            f"state {state['value']}",
            f"meaning {_format_atom(state['meaning'])}",
            f"terminal {_format_bool(state['terminal'])}",
            f"initial {_format_bool(state.get('initial', False))}",
            f"transitions {_format_csv(state.get('transitions', []))}",
        ]
        lines.append("  " + " ".join(parts))

    for clause in field_promise.get("invariants", []):
        lines.append("  " + _format_clause("invariant", clause))
    for clause in field_promise.get("globalConstraints", []):
        lines.append("  " + _format_clause("constraint", clause))
    for clause in field_promise.get("forbiddenImplicitState", []):
        lines.append("  " + _format_clause("forbid", clause))

    return lines


def _format_function_promise(function_promise: dict[str, Any]) -> list[str]:
    lines = [f"function {function_promise['name']} action {function_promise['action']}:"]
    lines.append(f"  summary {_format_atom(function_promise['summary'])}")

    if function_promise.get("dependsOn"):
        lines.append(f"  depends {_format_csv(function_promise['dependsOn'])}")
    for trigger in function_promise.get("triggers", []):
        lines.append(f"  trigger {_format_atom(trigger)}")
    for clause in function_promise.get("preconditions", []):
        lines.append("  " + _format_clause("precondition", clause))
    lines.append(f"  reads {_format_csv(function_promise.get('reads', []))}")
    lines.append(f"  writes {_format_csv(function_promise.get('writes', []))}")
    for clause in function_promise.get("successResults", []):
        lines.append("  " + _format_clause("ensure", clause))
    for clause in function_promise.get("failureConditions", []):
        lines.append("  " + _format_clause("reject", clause))
    for clause in function_promise.get("sideEffects", []):
        lines.append("  " + _format_clause("sideeffect", clause))
    if function_promise.get("idempotency"):
        lines.append(f"  idempotency {_format_atom(function_promise['idempotency'])}")
    for clause in function_promise.get("forbidden", []):
        lines.append("  " + _format_clause("forbid", clause))

    return lines


def _format_verification_promise(verification_promise: dict[str, Any]) -> list[str]:
    lines = [
        f"verify {verification_promise['name']} kind {verification_promise['kind']}:"
    ]
    lines.append(f"  claim {_format_atom(verification_promise['claim'])}")
    lines.append(f"  verifies {_format_csv(verification_promise.get('verifies', []))}")
    lines.append(f"  methods {_format_csv(verification_promise.get('methods', []))}")

    for scenario in verification_promise.get("scenarios", []):
        lines.append(f"  scenario {_format_atom(scenario['name'])}:")
        lines.append(f"    covers {_format_csv(scenario.get('covers', []))}")
        for given in scenario.get("given", []):
            lines.append(f"    given {_format_atom(given)}")
        for when in scenario.get("when", []):
            lines.append(f"    when {_format_atom(when)}")
        for then in scenario.get("then", []):
            lines.append(f"    then {_format_atom(then)}")
        for guard in scenario.get("regressionGuards", []):
            lines.append(f"    guard {_format_atom(guard)}")

    for evidence in verification_promise.get("evidenceRequired", []):
        lines.append(f"  evidence {_format_atom(evidence)}")
    for failure in verification_promise.get("failureCriteria", []):
        lines.append(f"  fail {_format_atom(failure)}")

    return lines


def _format_clause(keyword: str, clause: dict[str, Any]) -> str:
    parts = [keyword, clause["id"], f"statement {_format_atom(clause['statement'])}"]
    if clause.get("refs") is not None:
        refs = clause.get("refs", [])
        if refs:
            parts.append(f"refs {_format_csv(refs)}")
    if clause.get("when"):
        parts.append(f"when {_format_atom(clause['when'])}")
    if clause.get("must"):
        parts.append(f"must {_format_atom(clause['must'])}")
    if clause.get("severity"):
        parts.append(f"severity {_format_atom(clause['severity'])}")
    return " ".join(parts)


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return _format_bool(value)
    if isinstance(value, (int, float)):
        return str(value)
    return _format_atom(str(value))


def _format_atom(value: str) -> str:
    if SAFE_TOKEN_RE.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=True)


def _format_csv(values: list[str]) -> str:
    if not values:
        return "-"
    return ",".join(values)
