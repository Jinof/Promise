from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from promise_cli.cli import build_parser, load_cli_contract, main
from promise_cli.dsl import (
    PromiseExpressionError,
    analyze_intent_conflicts,
    analyze_intent_graph,
    clone_spec,
    format_spec,
    lint_spec,
    parse_file,
    parse_promise_expression,
    parse_text,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "task" / "task.promise"
CORE_TASK = ROOT / "examples" / "core" / "task-core.promise"
CORE_TOOLING = ROOT / "examples" / "core" / "promise-tooling-core.promise"
TOOLING_PROMISE = ROOT / "tooling" / "promise-cli.promise"
WARNING_PROMISE_TEXT = """meta:
  title "Warning Promise"
  domain warning
  version v1
  status active
  summary "Promise with advisory coverage gaps."

field WarningFieldPromise for Warning:
  summary "Defines the Warning object."
  field id type string required true nullable false default null semantic "Unique identifier." mutable false system true
  field status type string required true nullable false default draft semantic "Workflow status." mutable true system false
  state draft meaning "Work has not completed." terminal false initial true transitions done
  state done meaning "Work has completed." terminal true initial false transitions -

function CompleteWarningPromise action CompleteWarning:
  summary "Completes the warning object."
  trigger "The complete action is executed."
  reads Warning.status
  writes Warning.status
  ensure CompleteWarningPromise.status_updated statement "The action writes the completed status." refs Warning.status

verify WarningVerification kind function:
  claim "The warning example still verifies behavior."
  verifies WarningFieldPromise,CompleteWarningPromise
  methods unit
  scenario "complete warning":
    covers CompleteWarningPromise.status_updated
    when "The action is executed."
    then "The completion write is recorded."
  fail "The action cannot be considered complete when the ensure clause does not hold."
"""
GO_EDGE_PROMISE_TEXT = """meta:
  title "Go Edge Promise"
  domain ticket
  version v1
  status active
  summary "Promise that exercises Go enum and nullable generation."

field TicketFieldPromise for Ticket:
  summary "Defines a ticket with nullable state and non-state enum."
  field id type string required true nullable false default null semantic "Ticket identifier." mutable false system true
  field status type "enum(todo|done)" required false nullable true default null semantic "Nullable workflow state." mutable true system false
  field priority type "enum(low|high)" required true nullable false default low semantic "Non-state priority enum." mutable true system false
  state todo meaning "Ticket is open." terminal false initial true transitions done
  state done meaning "Ticket is closed." terminal true initial false transitions -
  invariant Ticket.done_requires_high_priority statement "Done tickets must be high priority." refs Ticket.status,Ticket.priority when "Ticket.status == Ticket.status.done and Ticket.priority in [Ticket.priority.low,Ticket.priority.high]" must "Ticket.priority == Ticket.priority.high"
  forbid Ticket.no_hidden_state statement "Do not introduce hidden ticket state." refs Ticket.status

function UpdateTicketFunctionPromise action UpdateTicket:
  summary "Updates ticket state and priority."
  trigger "A user updates a ticket."
  reads Ticket.id,Ticket.status,Ticket.priority
  writes Ticket.status,Ticket.priority
  ensure UpdateTicketFunctionPromise.updates_declared_fields statement "The update only writes declared fields." refs Ticket.status,Ticket.priority
  forbid UpdateTicketFunctionPromise.no_undeclared_writes statement "UpdateTicket must not write undeclared fields." refs Ticket.status,Ticket.priority

verify TicketVerification kind function:
  claim "Ticket generated contracts build for enum and nullable fields."
  verifies TicketFieldPromise,UpdateTicketFunctionPromise
  methods unit
  scenario "generated contract builds":
    covers Ticket.done_requires_high_priority,UpdateTicketFunctionPromise.updates_declared_fields
    when "The Promise is compiled to Go."
    then "The generated package builds."
  fail "Generated Go references undeclared enum constants or invalid nullable comparisons."
"""


def _expr_ref(value: str) -> dict[str, object]:
    return {"kind": "reference", "name": value, "parts": value.split(".")}


def _expr_literal(literal_type: str, value: object, raw: str | None = None) -> dict[str, object]:
    literal = {"kind": "literal", "literalType": literal_type, "value": value}
    if raw is not None:
        literal["raw"] = raw
    return literal


def _expr_comparison(operator: str, left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    return {"kind": "comparison", "operator": operator, "left": left, "right": right}


def _expr_binary(operator: str, left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    return {"kind": "binary", "operator": operator, "left": left, "right": right}


def _expr_not(operand: dict[str, object]) -> dict[str, object]:
    return {"kind": "not", "operand": operand}


class PromiseCliTests(unittest.TestCase):
    def test_parse_example(self) -> None:
        spec = parse_file(EXAMPLE)

        self.assertEqual(spec["schemaVersion"], "1.0.0")
        self.assertEqual(spec["meta"]["domain"], "task")
        self.assertEqual(len(spec["intentPromises"]), 3)
        self.assertEqual(spec["intentPromises"][0]["name"], "TaskSystemIntent")
        self.assertTrue(spec["intentPromises"][0]["root"])
        self.assertEqual(
            [{"target": "TaskSystemIntent", "relation": "refines", "note": "Lifecycle truth is one branch of the system-level task intent."}],
            spec["intentPromises"][1]["parents"],
        )
        self.assertEqual(len(spec["typePromises"]), 1)
        self.assertEqual(spec["typePromises"][0]["name"], "TaskID")
        self.assertEqual(spec["fieldPromises"][0]["fields"][0]["type"], "TaskID")
        self.assertEqual(len(spec["fieldPromises"]), 1)
        self.assertEqual(len(spec["functionPromises"]), 2)
        self.assertEqual(len(spec["verificationPromises"]), 2)

    def test_parse_core_examples(self) -> None:
        task_core = parse_file(CORE_TASK)
        tooling_core = parse_file(CORE_TOOLING)
        tooling_promise = parse_file(TOOLING_PROMISE)

        self.assertEqual("task", task_core["meta"]["domain"])
        self.assertEqual("promise_tooling", tooling_core["meta"]["domain"])
        self.assertEqual("promise_cli", tooling_promise["meta"]["domain"])
        self.assertEqual(7, len(tooling_promise["intentPromises"]))
        self.assertGreaterEqual(len(tooling_promise["intentResources"]), 5)
        self.assertEqual("PromiseToolingSystemIntent", tooling_promise["intentPromises"][0]["name"])
        self.assertTrue(tooling_promise["intentPromises"][0]["root"])
        self.assertEqual(0, tooling_core["fieldPromises"][0]["fields"][2]["default"])
        tooling_document_fields = {
            field["name"]: field["default"]
            for field in tooling_promise["fieldPromises"][1]["fields"]
        }
        self.assertEqual(0, tooling_document_fields["issueCount"])
        self.assertEqual(1, len(task_core["fieldPromises"]))
        self.assertEqual(3, len(tooling_core["functionPromises"]))
        self.assertEqual(2, len(tooling_promise["fieldPromises"]))
        self.assertEqual(8, len(tooling_promise["functionPromises"]))

    def test_parse_promise_expression_comparison_forms(self) -> None:
        cases = [
            (
                "Task.status = done",
                _expr_comparison("==", _expr_ref("Task.status"), _expr_ref("done")),
            ),
            (
                "Task.completedAt != null",
                _expr_comparison(
                    "!=",
                    _expr_ref("Task.completedAt"),
                    _expr_literal("null", None, "null"),
                ),
            ),
            (
                "Task.retryCount <= 3",
                _expr_comparison(
                    "<=",
                    _expr_ref("Task.retryCount"),
                    _expr_literal("number", 3, "3"),
                ),
            ),
            (
                'Task.title != ""',
                _expr_comparison(
                    "!=",
                    _expr_ref("Task.title"),
                    _expr_literal("string", ""),
                ),
            ),
            (
                "Task.enabled == true",
                _expr_comparison(
                    "==",
                    _expr_ref("Task.enabled"),
                    _expr_literal("boolean", True, "true"),
                ),
            ),
        ]

        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(expected, parse_promise_expression(expression))

    def test_parse_promise_expression_boolean_precedence_and_lists(self) -> None:
        expression = (
            "not Task.archived or "
            "Task.status == Task.status.done and "
            "Task.priority in [Task.priority.low,Task.priority.high]"
        )

        expected = _expr_binary(
            "or",
            _expr_not(_expr_ref("Task.archived")),
            _expr_binary(
                "and",
                _expr_comparison(
                    "==",
                    _expr_ref("Task.status"),
                    _expr_ref("Task.status.done"),
                ),
                _expr_comparison(
                    "in",
                    _expr_ref("Task.priority"),
                    {
                        "kind": "list",
                        "items": [
                            _expr_ref("Task.priority.low"),
                            _expr_ref("Task.priority.high"),
                        ],
                    },
                ),
            ),
        )

        self.assertEqual(expected, parse_promise_expression(expression))

    def test_parse_promise_expression_parentheses_override_precedence(self) -> None:
        expression = "(Task.status == Task.status.done or Task.status == Task.status.todo) and not Task.archived"

        expected = _expr_binary(
            "and",
            _expr_binary(
                "or",
                _expr_comparison(
                    "==",
                    _expr_ref("Task.status"),
                    _expr_ref("Task.status.done"),
                ),
                _expr_comparison(
                    "==",
                    _expr_ref("Task.status"),
                    _expr_ref("Task.status.todo"),
                ),
            ),
            _expr_not(_expr_ref("Task.archived")),
        )

        self.assertEqual(expected, parse_promise_expression(expression))

    def test_parse_promise_expression_rejects_invalid_syntax(self) -> None:
        invalid_expressions = [
            "",
            "Task.status ==",
            "Task..status == done",
            "Task.status == done)",
            "[Task.status,]",
            "\"unterminated",
        ]

        for expression in invalid_expressions:
            with self.subTest(expression=expression):
                with self.assertRaises(PromiseExpressionError):
                    parse_promise_expression(expression)

    def test_lint_example(self) -> None:
        spec = parse_file(EXAMPLE)
        issues = lint_spec(spec)
        self.assertEqual([], issues)

    def test_lint_core_examples(self) -> None:
        self.assertEqual([], lint_spec(parse_file(CORE_TASK)))
        self.assertEqual([], lint_spec(parse_file(CORE_TOOLING)))

    def test_lint_core_profile_examples(self) -> None:
        self.assertEqual([], lint_spec(parse_file(CORE_TASK), profile="core"))
        self.assertEqual([], lint_spec(parse_file(CORE_TOOLING), profile="core"))

    def test_lint_core_profile_rejects_enhanced_promise(self) -> None:
        issues = lint_spec(parse_file(EXAMPLE), profile="core")

        self.assertTrue(
            any(issue.code.startswith("core-non-minimal-") for issue in issues),
            "expected core profile to reject enhanced Promise features",
        )

    def test_lint_core_profile_rejects_self_bootstrap_intents(self) -> None:
        issues = lint_spec(parse_file(TOOLING_PROMISE), profile="core")

        self.assertTrue(
            any(issue.code == "core-non-minimal-intent" for issue in issues),
            "expected core profile to reject self-bootstrap intent declarations",
        )

    def test_lint_unknown_write(self) -> None:
        spec = parse_file(EXAMPLE)
        broken = clone_spec(spec)
        broken["functionPromises"][0]["writes"].append("Task.unknownField")

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "function-unknown-write" for issue in issues),
            "expected an unknown write lint issue",
        )

    def test_lint_unknown_field_type(self) -> None:
        spec = parse_file(EXAMPLE)
        broken = clone_spec(spec)
        broken["fieldPromises"][0]["fields"][0]["type"] = "MissingType"

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "field-unknown-type" for issue in issues),
            "expected an unknown field type lint issue",
        )

    def test_lint_unknown_intent_map_target(self) -> None:
        spec = parse_file(EXAMPLE)
        broken = clone_spec(spec)
        broken["intentPromises"][0]["maps"].append(
            {"target": "Task.unknownIntentTarget", "relation": "constrains"}
        )

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "intent-unknown-map-target" for issue in issues),
            "expected an unknown intent map target lint issue",
        )

    def test_lint_rejects_intent_without_rationale(self) -> None:
        spec = parse_file(EXAMPLE)
        broken = clone_spec(spec)
        broken["intentPromises"][0]["rationale"] = ""

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "intent-missing-rationale" for issue in issues),
            "expected a missing intent rationale lint issue",
        )

    def test_lint_rejects_unknown_enum_invariant_literal(self) -> None:
        broken = parse_text(
            GO_EDGE_PROMISE_TEXT.replace("Ticket.status.done", "Ticket.status.archived", 1)
        )

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "field-unknown-enum-literal" for issue in issues),
            "expected an unknown enum literal lint issue",
        )

    def test_lint_rejects_expression_syntax_error(self) -> None:
        broken = parse_text(
            GO_EDGE_PROMISE_TEXT.replace("Ticket.priority == Ticket.priority.high", "Ticket.priority ==", 1)
        )

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "expression-syntax-error" for issue in issues),
            "expected an expression syntax lint issue",
        )

    def test_lint_rejects_unknown_expression_reference(self) -> None:
        broken = parse_text(
            GO_EDGE_PROMISE_TEXT.replace("Ticket.priority == Ticket.priority.high", "Ticket.missing == true", 1)
        )

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "expression-unknown-reference" for issue in issues),
            "expected an unknown expression reference lint issue",
        )

    def test_lint_rejects_expression_type_mismatch(self) -> None:
        broken = parse_text(
            GO_EDGE_PROMISE_TEXT.replace("Ticket.priority == Ticket.priority.high", "Ticket.priority == 1", 1)
        )

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "expression-type-error" for issue in issues),
            "expected an expression type mismatch lint issue",
        )

    def test_lint_rejects_intent_parent_cycle(self) -> None:
        spec = parse_file(EXAMPLE)
        broken = clone_spec(spec)
        broken["intentPromises"][0]["root"] = False
        broken["intentPromises"][0]["parents"] = [
            {"target": "PreserveTaskLifecycleTruth", "relation": "refines"}
        ]
        broken["intentPromises"][1]["parents"] = [
            {"target": "TaskSystemIntent", "relation": "refines"}
        ]

        issues = lint_spec(broken)

        self.assertTrue(
            any(issue.code == "intent-missing-root" for issue in issues),
            "expected a missing root lint issue",
        )
        self.assertTrue(
            any(issue.code == "intent-parent-cycle" for issue in issues),
            "expected an intent parent cycle lint issue",
        )

    def test_parse_and_format_intent_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["conflicts"].append(
            {
                "target": "KeepTaskCreationSimple",
                "severity": "tension",
                "reason": "Strict lifecycle truth can add work to the simple creation path.",
                "resolution": "Creation remains simple while lifecycle state stays system-derived.",
                "note": "Detected at the abstract intent layer.",
            }
        )

        formatted = format_spec(spec)
        parsed = parse_text(formatted)

        self.assertIn(
            'conflicts KeepTaskCreationSimple severity tension reason "Strict lifecycle truth can add work to the simple creation path." resolution "Creation remains simple while lifecycle state stays system-derived." note "Detected at the abstract intent layer."',
            formatted,
        )
        self.assertEqual(
            [
                {
                    "target": "KeepTaskCreationSimple",
                    "severity": "tension",
                    "reason": "Strict lifecycle truth can add work to the simple creation path.",
                    "resolution": "Creation remains simple while lifecycle state stays system-derived.",
                    "note": "Detected at the abstract intent layer.",
                }
            ],
            parsed["intentPromises"][1]["conflicts"],
        )

    def test_parse_and_format_intent_requirements(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["requirements"] = [
            {
                "id": "LifecycleTruth",
                "kind": "requires",
                "subject": "Task.lifecycle",
                "predicate": "exposes",
                "object": "explicit_state",
                "scope": "task_runtime",
                "effect": "state_visible",
                "constraint": "single_source",
                "priority": "must",
                "because": "Lifecycle truth is a human-level requirement before it is mapped to fields.",
                "note": "Structured from ordinary requirement language.",
            },
            {
                "id": "LifecycleLayoutPreference",
                "kind": "prefers",
                "subject": "Task.lifecycle",
                "predicate": "uses",
                "object": "derived_state",
                "over": "hidden_state",
                "priority": "must",
            },
        ]

        formatted = format_spec(spec)
        parsed = parse_text(formatted)

        self.assertIn(
            'requires LifecycleTruth subject Task.lifecycle predicate exposes object explicit_state scope task_runtime effect state_visible constraint single_source priority must because "Lifecycle truth is a human-level requirement before it is mapped to fields." note "Structured from ordinary requirement language."',
            formatted,
        )
        self.assertIn(
            "prefers LifecycleLayoutPreference subject Task.lifecycle predicate uses object derived_state over hidden_state priority must",
            formatted,
        )
        self.assertEqual(spec["intentPromises"][1]["requirements"], parsed["intentPromises"][1]["requirements"])

    def test_parse_and_format_intent_resources_and_resource_operations(self) -> None:
        promise_text = """meta:
  title "Intent Resource Promise"
  domain intent_resource
  version v1
  status active
  summary "Promise with intent resources."

resource User kind actor:
  summary "Human user operating the system."
  alias end_user

resource Task kind entity:
  summary "Task resource operated by user intent."
  maps TaskFieldPromise relation constrains

intent ResourceSystemIntent priority must:
  statement "The task system must express intent as operations on resources."
  rationale "Resources make human requirements precise before field and function promises exist."
  status active
  root true
  requires UserExportsTask actor User action export resource Task scope user_workspace effect export_file constraint authorized_user priority must because "A user requirement is an operation on Task."

field TaskFieldPromise for TaskRecord:
  summary "Defines task record state."
  field id type string required true nullable false default null semantic "Task id." mutable false system true

function ExportTaskPromise action ExportTask:
  summary "Exports task data."
  trigger "A user exports a task."
  reads TaskRecord.id
  writes TaskRecord.id
  ensure ExportTaskPromise.records_export statement "Export is recorded." refs TaskRecord.id

verify IntentResourceVerification kind function:
  claim "Intent resources parse and format."
  verifies ResourceSystemIntent,Task
  methods unit
  scenario "resource operation is visible":
    covers ExportTaskPromise.records_export
    when "The Promise is parsed."
    then "The intent requirement preserves actor, action, and resource fields."
  fail "Intent resources are dropped."
"""
        spec = parse_text(promise_text)
        formatted = format_spec(spec)
        parsed = parse_text(formatted)

        self.assertEqual("User", spec["intentResources"][0]["name"])
        self.assertEqual("actor", spec["intentResources"][0]["kind"])
        self.assertEqual(["end_user"], spec["intentResources"][0]["aliases"])
        requirement = spec["intentPromises"][0]["requirements"][0]
        self.assertEqual("User", requirement["actor"])
        self.assertEqual("export", requirement["action"])
        self.assertEqual("Task", requirement["resource"])
        self.assertEqual("User", requirement["subject"])
        self.assertEqual("export", requirement["predicate"])
        self.assertEqual("Task", requirement["object"])
        self.assertEqual("user_workspace", requirement["scope"])
        self.assertEqual("export_file", requirement["effect"])
        self.assertEqual("authorized_user", requirement["constraint"])
        self.assertEqual("must", requirement["priority"])
        self.assertIn("resource User kind actor:", formatted)
        self.assertIn(
            'requires UserExportsTask actor User action export resource Task scope user_workspace effect export_file constraint authorized_user priority must because "A user requirement is an operation on Task."',
            formatted,
        )
        self.assertEqual(spec["intentResources"], parsed["intentResources"])

    def test_parse_and_format_intent_terms_and_scope_hierarchy(self) -> None:
        promise_text = """meta:
  title "Intent Term Promise"
  domain intent_term
  version v1
  status active
  summary "Promise with controlled intent vocabulary."

resource User kind actor:
  summary "Human user operating the system."

resource Task kind entity:
  summary "Task resource operated by user intent."
  maps TaskFieldPromise relation constrains

term tenant kind scope:
  summary "Tenant-wide human requirement scope."

term user_workspace kind scope:
  summary "Workspace visible to one user."
  alias workspace
  parent tenant
  disjoint admin_console

term admin_console kind scope:
  summary "Administrative console scope."
  parent tenant
  disjoint user_workspace

term export kind action:
  summary "Make a resource available outside the system."
  opposite import

term import kind action:
  summary "Bring an external resource into the system."
  opposite export

term export_file kind effect:
  summary "A downloadable file is produced."

term authorized_user kind constraint:
  summary "Only an authorized user can operate the resource."
  maps TaskFieldPromise relation constrains

intent ResourceSystemIntent priority must:
  statement "The task system must express intent as controlled operations on resources."
  rationale "Controlled vocabulary makes intent atoms comparable before field and function promises exist."
  status active
  root true
  requires UserExportsTask actor User action export resource Task scope user_workspace effect export_file constraint authorized_user because "A user requirement is an operation on Task."

field TaskFieldPromise for TaskRecord:
  summary "Defines task record state."
  field id type string required true nullable false default null semantic "Task id." mutable false system true

function ExportTaskPromise action ExportTask:
  summary "Exports task data."
  trigger "A user exports a task."
  reads TaskRecord.id
  writes TaskRecord.id
  ensure ExportTaskPromise.records_export statement "Export is recorded." refs TaskRecord.id

verify IntentTermVerification kind function:
  claim "Intent terms parse and format."
  verifies ResourceSystemIntent,Task
  methods unit
  scenario "controlled term is visible":
    covers ExportTaskPromise.records_export
    when "The Promise is parsed."
    then "The intent requirement preserves controlled vocabulary fields."
  fail "Intent terms are dropped."
"""
        spec = parse_text(promise_text)
        formatted = format_spec(spec)
        parsed = parse_text(formatted)

        self.assertEqual("tenant", spec["intentTerms"][0]["name"])
        self.assertEqual("scope", spec["intentTerms"][0]["kind"])
        self.assertEqual("tenant", spec["intentTerms"][1]["parent"])
        self.assertEqual(["admin_console"], spec["intentTerms"][1]["disjoint"])
        self.assertEqual(["import"], spec["intentTerms"][3]["opposites"])
        requirement = spec["intentPromises"][0]["requirements"][0]
        self.assertEqual("export", requirement["action"])
        self.assertEqual("user_workspace", requirement["scope"])
        self.assertEqual("export_file", requirement["effect"])
        self.assertEqual("authorized_user", requirement["constraint"])
        self.assertIn("term user_workspace kind scope:", formatted)
        self.assertIn("  parent tenant", formatted)
        self.assertIn("  disjoint admin_console", formatted)
        self.assertIn("  opposite import", formatted)
        self.assertIn(
            'requires UserExportsTask actor User action export resource Task scope user_workspace effect export_file constraint authorized_user priority must because "A user requirement is an operation on Task."',
            formatted,
        )
        self.assertEqual(spec["intentTerms"], parsed["intentTerms"])

    def test_parse_and_format_intent_cycles(self) -> None:
        promise_text = """meta:
  title "Intent Cycle Promise"
  domain cycle
  version v1
  status active
  summary "Promise with declared intent graph cycle."

cycle ReviewFeedbackLoop kind feedback:
  summary "Review and revision intentionally feed each other."
  rationale "The loop models an intentional negotiation path rather than structural lowering."
  edge ReviewIntent -> ReviseIntent relation requires
  edge ReviseIntent -> ReviewIntent relation blocks note "Revision blocks completion until review passes."

intent ReviewIntent priority must:
  statement "Review requires revision intent when changes are requested."
  rationale "The review branch initiates revision."
  status active
  root true
  maps ReviseIntent relation requires

intent ReviseIntent priority must:
  statement "Revision blocks review completion until it is done."
  rationale "The revision branch feeds back into review."
  status active
  parent ReviewIntent relation supports
  maps ReviewIntent relation blocks

field CycleFieldPromise for CycleRecord:
  summary "Defines the cycle record."
  field id type string required true nullable false default null semantic "Cycle id." mutable false system true

function CycleFunctionPromise action CycleAction:
  summary "Touches the cycle record."
  trigger "The cycle action is executed."
  reads CycleRecord.id
  writes CycleRecord.id
  ensure CycleFunctionPromise.touched statement "The cycle record is touched." refs CycleRecord.id

verify CycleVerification kind function:
  claim "Intent cycles parse and format."
  verifies CycleFunctionPromise
  methods unit
  scenario "declared cycle":
    covers CycleFunctionPromise.touched
    when "The Promise is parsed."
    then "The cycle declaration survives formatting."
  fail "The cycle declaration is dropped."
"""
        spec = parse_text(promise_text)
        formatted = format_spec(spec)
        parsed = parse_text(formatted)

        self.assertEqual("ReviewFeedbackLoop", spec["intentCycles"][0]["name"])
        self.assertEqual("feedback", spec["intentCycles"][0]["kind"])
        self.assertEqual(["ReviewIntent", "ReviseIntent"], spec["intentCycles"][0]["nodes"])
        self.assertEqual(
            {"source": "ReviseIntent", "target": "ReviewIntent", "relation": "blocks", "note": "Revision blocks completion until review passes."},
            spec["intentCycles"][0]["edges"][1],
        )
        self.assertIn("cycle ReviewFeedbackLoop kind feedback:", formatted)
        self.assertNotIn("  node ReviewIntent", formatted)
        self.assertIn("  edge ReviewIntent -> ReviseIntent relation requires", formatted)
        self.assertIn(
            '  edge ReviseIntent -> ReviewIntent relation blocks note "Revision blocks completion until review passes."',
            formatted,
        )
        self.assertEqual(spec["intentCycles"], parsed["intentCycles"])

    def test_lint_validates_intent_requirements(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["requirements"] = [
            {
                "id": "BadRequirement",
                "kind": "requires",
                "subject": "Task lifecycle",
                "predicate": "exposes",
                "object": "explicit_state",
                "scope": "task runtime",
                "priority": "urgent",
            },
            {
                "id": "BadRequirement",
                "kind": "prefers",
                "subject": "Task.lifecycle",
                "predicate": "uses",
                "object": "derived_state",
            },
        ]

        issues = lint_spec(spec)

        self.assertTrue(any(issue.code == "intent-requirement-duplicate-id" for issue in issues))
        self.assertTrue(any(issue.code == "intent-requirement-invalid-atom" for issue in issues))
        self.assertTrue(any(issue.code == "intent-requirement-invalid-priority" for issue in issues))
        self.assertTrue(any(issue.code == "intent-preference-missing-over" for issue in issues))

    def test_lint_validates_intent_resource_references(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentResources"] = [
            {
                "name": "User",
                "kind": "actor",
                "summary": "Human user.",
                "aliases": [],
                "maps": [],
            }
        ]
        spec["intentPromises"][1]["requirements"] = [
            {
                "id": "UnknownResourceOperation",
                "kind": "requires",
                "actor": "User",
                "action": "export",
                "resource": "MissingTask",
                "subject": "User",
                "predicate": "export",
                "object": "MissingTask",
            }
        ]

        issues = lint_spec(spec)

        self.assertTrue(any(issue.code == "intent-requirement-unknown-resource" for issue in issues))

    def test_lint_validates_intent_terms_and_requirement_references(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentTerms"] = [
            {
                "name": "tenant",
                "kind": "scope",
                "summary": "Tenant scope.",
                "aliases": ["tenant scope"],
                "parent": "missing_parent",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "user_workspace",
                "kind": "scope",
                "summary": "User workspace scope.",
                "aliases": [],
                "parent": "tenant",
                "disjoint": ["missing_scope"],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "export",
                "kind": "action",
                "summary": "Export action.",
                "aliases": [],
                "parent": "",
                "disjoint": ["import"],
                "opposites": ["missing_action"],
                "maps": [{"target": "MissingPromiseItem", "relation": "constrains"}],
            },
            {
                "name": "export_file",
                "kind": "effect",
                "summary": "A file is produced.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "authorized_user",
                "kind": "constraint",
                "summary": "Only an authorized user can operate.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [{"target": "TaskFieldPromise", "relation": "constrains"}],
            },
        ]
        spec["intentPromises"][1]["requirements"] = [
            {
                "id": "UnknownTermRequirement",
                "kind": "requires",
                "actor": "User",
                "action": "delete",
                "resource": "Task",
                "subject": "User",
                "predicate": "delete",
                "object": "Task",
                "scope": "admin_console",
                "effect": "missing_effect",
                "constraint": "missing_constraint",
                "priority": "must",
            }
        ]

        issues = lint_spec(spec)

        self.assertTrue(any(issue.code == "intent-term-invalid-alias" for issue in issues))
        self.assertTrue(any(issue.code == "intent-term-unknown-parent" for issue in issues))
        self.assertTrue(any(issue.code == "intent-term-unknown-disjoint" for issue in issues))
        self.assertTrue(any(issue.code == "intent-term-disjoint-non-scope" for issue in issues))
        self.assertTrue(any(issue.code == "intent-term-unknown-opposite" for issue in issues))
        self.assertTrue(any(issue.code == "intent-term-unknown-map-target" for issue in issues))
        self.assertTrue(any(issue.code == "intent-requirement-unknown-term" for issue in issues))

    def test_lint_detects_unexpected_reciprocal_intent_graph_cycle(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["maps"].append(
            {
                "target": "TaskSystemIntent",
                "relation": "supports",
                "note": "This creates an unexpected edge back to the parent intent.",
            }
        )

        analysis = analyze_intent_graph(spec)
        issues = lint_spec(spec)

        self.assertTrue(analysis["unexpectedCycles"])
        self.assertEqual("reciprocal", analysis["unexpectedCycles"][0]["kind"])
        self.assertTrue(any(issue.code == "intent-graph-unexpected-cycle" for issue in issues))

    def test_lint_allows_declared_feedback_intent_graph_cycle(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentCycles"] = [
            {
                "name": "CreationLifecycleFeedback",
                "kind": "feedback",
                "summary": "Creation and lifecycle checks intentionally feed each other.",
                "rationale": "This example models a declared negotiation loop.",
                "edges": [
                    {
                        "source": "KeepTaskCreationSimple",
                        "target": "PreserveTaskLifecycleTruth",
                        "relation": "requires",
                    },
                    {
                        "source": "PreserveTaskLifecycleTruth",
                        "target": "KeepTaskCreationSimple",
                        "relation": "blocks",
                    },
                ],
            }
        ]
        spec["intentPromises"][2]["maps"].append(
            {
                "target": "PreserveTaskLifecycleTruth",
                "relation": "requires",
            }
        )
        spec["intentPromises"][1]["maps"].append(
            {
                "target": "KeepTaskCreationSimple",
                "relation": "blocks",
            }
        )

        analysis = analyze_intent_graph(spec)
        issues = lint_spec(spec)

        self.assertEqual(1, len(analysis["declaredCycles"]))
        self.assertTrue(analysis["declaredCycles"][0]["matched"])
        self.assertFalse(analysis["unexpectedCycles"])
        self.assertFalse(any(issue.code == "intent-graph-unexpected-cycle" for issue in issues))

    def test_lint_warns_stale_intent_graph_cycle_declaration(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentCycles"] = [
            {
                "name": "StaleFeedback",
                "kind": "feedback",
                "summary": "A stale declared loop.",
                "rationale": "The declaration no longer matches actual intent graph edges.",
                "edges": [
                    {
                        "source": "KeepTaskCreationSimple",
                        "target": "PreserveTaskLifecycleTruth",
                        "relation": "requires",
                    },
                    {
                        "source": "PreserveTaskLifecycleTruth",
                        "target": "KeepTaskCreationSimple",
                        "relation": "blocks",
                    },
                ],
            }
        ]

        issues = lint_spec(spec)

        self.assertTrue(
            any(issue.code == "intent-graph-stale-cycle-declaration" and issue.severity == "warning" for issue in issues)
        )

    def test_lint_core_profile_rejects_intent_resources(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentResources"] = [
            {
                "name": "Task",
                "kind": "entity",
                "summary": "Task resource.",
                "aliases": [],
                "maps": [],
            }
        ]

        issues = lint_spec(spec, profile="core")

        self.assertTrue(any(issue.code == "core-non-minimal-resource" for issue in issues))

    def test_lint_core_profile_rejects_intent_terms(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentTerms"] = [
            {
                "name": "tenant",
                "kind": "scope",
                "summary": "Tenant scope.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            }
        ]

        issues = lint_spec(spec, profile="core")

        self.assertTrue(any(issue.code == "core-non-minimal-term" for issue in issues))

    def test_lint_core_profile_rejects_intent_cycles(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentCycles"] = [
            {
                "name": "FeedbackLoop",
                "kind": "feedback",
                "summary": "Declared feedback loop.",
                "rationale": "Cycles are non-core intent graph declarations.",
                "edges": [
                    {
                        "source": "KeepTaskCreationSimple",
                        "target": "PreserveTaskLifecycleTruth",
                        "relation": "requires",
                    },
                    {
                        "source": "PreserveTaskLifecycleTruth",
                        "target": "KeepTaskCreationSimple",
                        "relation": "blocks",
                    },
                ],
            }
        ]

        issues = lint_spec(spec, profile="core")

        self.assertTrue(any(issue.code == "core-non-minimal-cycle" for issue in issues))

    def test_lint_detects_scope_hierarchy_requirement_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentTerms"] = [
            {
                "name": "tenant",
                "kind": "scope",
                "summary": "Tenant scope.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "user_workspace",
                "kind": "scope",
                "summary": "User workspace scope.",
                "aliases": [],
                "parent": "tenant",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
        ]
        spec["intentPromises"].extend(
            [
                {
                    "name": "AllowTenantExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must be able to export task data at tenant scope.",
                    "rationale": "A broad scope requirement should apply to narrower workspace requirements.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "AllowTenantTaskExport",
                            "kind": "requires",
                            "subject": "Task",
                            "predicate": "export",
                            "object": "data",
                            "scope": "tenant",
                            "priority": "must",
                        }
                    ],
                    "maps": [],
                },
                {
                    "name": "ForbidWorkspaceExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must not export task data from a workspace.",
                    "rationale": "A narrower scope can conflict with its parent scope requirement.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "ForbidWorkspaceTaskExport",
                            "kind": "forbids",
                            "subject": "Task",
                            "predicate": "export",
                            "object": "data",
                            "scope": "user_workspace",
                            "priority": "must",
                        }
                    ],
                    "maps": [],
                },
            ]
        )

        conflicts = analyze_intent_conflicts(spec)["detected"]

        self.assertTrue(
            any(
                conflict["source"] == "ForbidWorkspaceExport"
                and conflict["target"] == "AllowTenantExport"
                and conflict["detector"] == "opposed-intent-requirement"
                for conflict in conflicts
            )
        )

    def test_lint_ignores_disjoint_scope_requirement_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentTerms"] = [
            {
                "name": "tenant",
                "kind": "scope",
                "summary": "Tenant scope.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "user_workspace",
                "kind": "scope",
                "summary": "User workspace scope.",
                "aliases": [],
                "parent": "tenant",
                "disjoint": ["admin_console"],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "admin_console",
                "kind": "scope",
                "summary": "Admin console scope.",
                "aliases": [],
                "parent": "tenant",
                "disjoint": ["user_workspace"],
                "opposites": [],
                "maps": [],
            },
        ]
        spec["intentPromises"].extend(
            [
                {
                    "name": "AllowWorkspaceExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must be able to export task data from workspace.",
                    "rationale": "Workspace export is separate from admin export.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "AllowWorkspaceTaskExport",
                            "kind": "requires",
                            "subject": "Task",
                            "predicate": "export",
                            "object": "data",
                            "scope": "user_workspace",
                            "priority": "must",
                        }
                    ],
                    "maps": [],
                },
                {
                    "name": "ForbidAdminExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Admins must not export task data from admin console.",
                    "rationale": "Admin console restrictions should not conflict with workspace requirements.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "ForbidAdminTaskExport",
                            "kind": "forbids",
                            "subject": "Task",
                            "predicate": "export",
                            "object": "data",
                            "scope": "admin_console",
                            "priority": "must",
                        }
                    ],
                    "maps": [],
                },
            ]
        )

        conflicts = analyze_intent_conflicts(spec)["detected"]

        self.assertFalse(
            any(
                {conflict["source"], conflict["target"]} == {"AllowWorkspaceExport", "ForbidAdminExport"}
                and conflict["detector"] == "opposed-intent-requirement"
                for conflict in conflicts
            )
        )


    def test_lint_allows_abstract_intent_with_structured_requirements(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].append(
            {
                "name": "CaptureAbstractRequirement",
                "priority": "should",
                "status": "active",
                "root": False,
                "statement": "The system should keep a human-level requirement before it maps to implementation promises.",
                "rationale": "Intent must not be forced to bind to System Promise Items too early.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [],
                "requirements": [
                    {
                        "id": "AbstractRequirementAtom",
                        "kind": "requires",
                        "subject": "IntentLayer",
                        "predicate": "captures",
                        "object": "human_requirement_atom",
                    }
                ],
                "maps": [],
            }
        )

        issues = lint_spec(spec)

        self.assertFalse(
            any(
                issue.code == "intent-missing-maps"
                and "CaptureAbstractRequirement" in issue.message
                for issue in issues
            )
        )

    def test_lint_detects_invalid_intent_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["conflicts"] = [
            {
                "target": "KeepTaskCreationSimple",
                "severity": "blocking",
                "reason": "Lifecycle strictness blocks simple creation without a design decision.",
            },
            {
                "target": "MissingIntent",
                "severity": "tension",
                "reason": "Unknown targets cannot be evaluated.",
            },
            {
                "target": "PreserveTaskLifecycleTruth",
                "severity": "advisory",
                "reason": "Self conflicts are not meaningful.",
            },
        ]

        issues = lint_spec(spec)

        self.assertTrue(
            any(issue.code == "intent-unresolved-blocking-conflict" for issue in issues),
            "expected an unresolved blocking conflict lint issue",
        )
        self.assertTrue(
            any(issue.code == "intent-unknown-conflict-target" for issue in issues),
            "expected an unknown conflict target lint issue",
        )
        self.assertTrue(
            any(issue.code == "intent-self-conflict" for issue in issues),
            "expected a self conflict lint issue",
        )

    def test_lint_warns_auto_intent_conflict_from_opposed_maps(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].append(
            {
                "name": "AvoidCompletionBehavior",
                "priority": "must",
                "status": "active",
                "root": False,
                "statement": "The task system must avoid exposing completion behavior.",
                "rationale": "This intent intentionally opposes the completion function for conflict detection.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [],
                "maps": [
                    {
                        "target": "CompleteTaskFunctionPromise",
                        "relation": "conflicts",
                        "note": "Completion behavior is intentionally opposed.",
                    }
                ],
            }
        )

        issues = lint_spec(spec)
        analysis = analyze_intent_conflicts(spec)

        self.assertTrue(
            any(issue.code == "intent-auto-conflict-candidate" and issue.severity == "warning" for issue in issues),
            "expected an automatic intent conflict warning",
        )
        self.assertEqual(1, len(analysis["detected"]))
        self.assertEqual("opposed-map-relation", analysis["detected"][0]["detector"])
        self.assertEqual("blocking", analysis["detected"][0]["severity"])

    def test_lint_warns_auto_intent_conflict_from_opposed_requirements(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].extend(
            [
                {
                    "name": "RequireExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must be able to export a task.",
                    "rationale": "Export is a human requirement before implementation details are chosen.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "UserExportTask",
                            "kind": "requires",
                            "subject": "User",
                            "predicate": "can",
                            "object": "export_task",
                        }
                    ],
                    "maps": [],
                },
                {
                    "name": "ForbidExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must not be able to export a task.",
                    "rationale": "This intentionally opposes the export requirement.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "NoUserExportTask",
                            "kind": "forbids",
                            "subject": "User",
                            "predicate": "can",
                            "object": "export_task",
                        }
                    ],
                    "maps": [],
                },
            ]
        )

        issues = lint_spec(spec)
        analysis = analyze_intent_conflicts(spec)

        self.assertTrue(
            any(issue.code == "intent-auto-conflict-candidate" and issue.severity == "warning" for issue in issues),
            "expected an automatic requirement conflict warning",
        )
        self.assertEqual(1, len(analysis["detected"]))
        self.assertEqual("opposed-intent-requirement", analysis["detected"][0]["detector"])
        self.assertEqual("blocking", analysis["detected"][0]["severity"])
        self.assertEqual("NoUserExportTask", analysis["detected"][0]["evidence"][0]["sourceRequirement"])

    def test_lint_warns_auto_intent_conflict_from_opposed_resource_operations(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentResources"] = [
            {
                "name": "User",
                "kind": "actor",
                "summary": "Human user.",
                "aliases": [],
                "maps": [],
            },
            {
                "name": "Task",
                "kind": "entity",
                "summary": "Task resource.",
                "aliases": [],
                "maps": [],
            },
        ]
        spec["intentPromises"].extend(
            [
                {
                    "name": "RequireTaskExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must export tasks.",
                    "rationale": "This requirement is an operation on the Task resource.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "UserExportsTask",
                            "kind": "requires",
                            "actor": "User",
                            "action": "export",
                            "resource": "Task",
                            "subject": "User",
                            "predicate": "export",
                            "object": "Task",
                        }
                    ],
                    "maps": [],
                },
                {
                    "name": "ForbidTaskExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must not export tasks.",
                    "rationale": "This intentionally opposes the Task export operation.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "NoUserExportsTask",
                            "kind": "forbids",
                            "actor": "User",
                            "action": "export",
                            "resource": "Task",
                            "subject": "User",
                            "predicate": "export",
                            "object": "Task",
                        }
                    ],
                    "maps": [],
                },
            ]
        )

        issues = lint_spec(spec)
        analysis = analyze_intent_conflicts(spec)

        self.assertTrue(any(issue.code == "intent-auto-conflict-candidate" for issue in issues))
        self.assertEqual(1, len(analysis["detected"]))
        self.assertEqual("opposed-intent-requirement", analysis["detected"][0]["detector"])
        self.assertEqual("User", analysis["detected"][0]["evidence"][0]["sourceActor"])
        self.assertEqual("export", analysis["detected"][0]["evidence"][0]["sourceAction"])
        self.assertEqual("Task", analysis["detected"][0]["evidence"][0]["sourceResource"])

    def test_lint_does_not_warn_auto_conflict_when_declared(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].append(
            {
                "name": "AvoidCompletionBehavior",
                "priority": "must",
                "status": "active",
                "root": False,
                "statement": "The task system must avoid exposing completion behavior.",
                "rationale": "This intent intentionally opposes the completion function for conflict detection.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [
                    {
                        "target": "PreserveTaskLifecycleTruth",
                        "severity": "blocking",
                        "reason": "One intent requires completion behavior while the other opposes it.",
                        "resolution": "Keep completion behavior and reject the opposing intent.",
                    }
                ],
                "maps": [
                    {
                        "target": "CompleteTaskFunctionPromise",
                        "relation": "conflicts",
                        "note": "Completion behavior is intentionally opposed.",
                    }
                ],
            }
        )

        issues = lint_spec(spec)
        analysis = analyze_intent_conflicts(spec)

        self.assertFalse(any(issue.code == "intent-auto-conflict-candidate" for issue in issues))
        self.assertEqual(1, len(analysis["declared"]))
        self.assertEqual([], analysis["detected"])

    def test_detects_auto_intent_conflict_from_opposed_field_assertions(self) -> None:
        promise_text = """meta:
  title "Intent Assertion Conflict"
  domain conflict
  version v1
  status active
  summary "Promise with automatically detectable opposed field assertions."

intent ConflictSystemIntent priority must:
  statement "The system must expose an explicit mode decision."
  rationale "The root intent anchors the conflict example."
  status active
  root true
  maps ModeFieldPromise relation constrains

intent KeepModeAutomatic priority must:
  statement "The mode must stay automatic."
  rationale "This intent maps to a hard automatic-mode invariant."
  status active
  parent ConflictSystemIntent relation refines
  maps Mode.must_be_auto relation constrains

intent KeepModeManual priority must:
  statement "The mode must stay manual."
  rationale "This intent maps to a hard manual-mode invariant."
  status active
  parent ConflictSystemIntent relation refines
  maps Mode.must_be_manual relation constrains

field ModeFieldPromise for Mode:
  summary "Defines mode state."
  field value type "enum(auto|manual)" required true nullable false default auto semantic "Mode value." mutable true system false
  invariant Mode.must_be_auto statement "Mode must be auto." refs Mode.value must "Mode.value == Mode.value.auto"
  invariant Mode.must_be_manual statement "Mode must be manual." refs Mode.value must "Mode.value == Mode.value.manual"

function ModeFunctionPromise action SetMode:
  summary "Updates mode."
  trigger "Mode is set."
  reads Mode.value
  writes Mode.value
  ensure ModeFunctionPromise.records_mode statement "Mode is recorded." refs Mode.value

verify ModeVerification kind field:
  claim "Mode invariants are checked."
  verifies ModeFieldPromise
  methods unit
  scenario "mode invariants":
    covers Mode.must_be_auto,Mode.must_be_manual
    when "The Promise is linted."
    then "Opposed intent assertions are reported."
  fail "Opposed assertions are invisible."
"""
        spec = parse_text(promise_text)
        issues = lint_spec(spec)
        analysis = analyze_intent_conflicts(spec)

        self.assertTrue(
            any(issue.code == "intent-auto-conflict-candidate" and issue.severity == "warning" for issue in issues),
            "expected an automatic assertion conflict warning",
        )
        self.assertEqual(1, len(analysis["detected"]))
        self.assertEqual("opposed-field-assertion", analysis["detected"][0]["detector"])
        self.assertEqual("blocking", analysis["detected"][0]["severity"])
        self.assertEqual("Mode.value", analysis["detected"][0]["evidence"][0]["subject"])

    def test_parse_allows_advisory_gaps(self) -> None:
        spec = parse_text(WARNING_PROMISE_TEXT)

        self.assertEqual("warning", spec["meta"]["domain"])
        self.assertEqual([], spec["fieldPromises"][0]["invariants"])
        self.assertEqual([], spec["fieldPromises"][0]["forbiddenImplicitState"])
        self.assertEqual([], spec["functionPromises"][0]["forbidden"])

    def test_lint_warnings_cover_advisory_gaps(self) -> None:
        issues = lint_spec(parse_text(WARNING_PROMISE_TEXT))
        issue_codes = {issue.code for issue in issues}
        error_count = sum(1 for issue in issues if issue.severity == "error")
        warning_count = sum(1 for issue in issues if issue.severity == "warning")

        self.assertEqual(0, error_count)
        self.assertEqual(3, warning_count)
        self.assertIn("field-missing-invariant-coverage", issue_codes)
        self.assertIn("field-missing-forbid-coverage", issue_codes)
        self.assertIn("function-missing-forbid-coverage", issue_codes)

    def test_tooling_promise_matches_cli_commands(self) -> None:
        spec = parse_file(TOOLING_PROMISE)
        promised_actions = {item["action"] for item in spec["functionPromises"]}

        parser = build_parser()
        subparser_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        implemented_actions = set(subparser_action.choices.keys())

        self.assertEqual({"parse", "format", "lint", "check", "compile", "graph", "impact", "tooling"}, implemented_actions)
        self.assertEqual(promised_actions, implemented_actions)

    def test_tooling_promise_generates_command_steps(self) -> None:
        contract = load_cli_contract()
        command_steps = {
            name: command.steps for name, command in contract.commands.items()
        }

        self.assertEqual(
            {
                "parse": ["parse_source", "emit_spec_json"],
                "format": [
                    "load_source_text",
                    "parse_source",
                    "format_spec",
                    "emit_formatted_result",
                ],
                "lint": [
                    "parse_source",
                    "lint_spec",
                    "emit_lint_result",
                ],
                "check": [
                    "parse_source",
                    "lint_spec",
                    "emit_check_result",
                ],
                "compile": [
                    "parse_source",
                    "lint_spec",
                    "compile_go_contract",
                    "emit_compile_result",
                ],
                "graph": [
                    "parse_source",
                    "render_graph_html",
                    "emit_graph_result",
                ],
                "impact": [
                    "parse_source",
                    "compute_intent_impact",
                    "emit_impact_result",
                ],
                "tooling": [
                    "collect_tooling_verification",
                    "emit_tooling_verify_result",
                ],
            },
            command_steps,
        )

    def test_tooling_promise_generates_cli_option_surface(self) -> None:
        parser = build_parser()
        subparser_action = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        parse_parser = subparser_action.choices["parse"]
        format_parser = subparser_action.choices["format"]
        lint_parser = subparser_action.choices["lint"]
        check_parser = subparser_action.choices["check"]
        compile_parser = subparser_action.choices["compile"]
        graph_parser = subparser_action.choices["graph"]
        impact_parser = subparser_action.choices["impact"]
        tooling_parser = subparser_action.choices["tooling"]

        self.assertEqual({"path"}, _collect_parser_positionals(parse_parser))
        self.assertEqual(set(), _collect_parser_options(parse_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(format_parser))
        self.assertEqual({"--write", "--check"}, _collect_parser_options(format_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(lint_parser))
        self.assertEqual({"--profile", "--json"}, _collect_parser_options(lint_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(check_parser))
        self.assertEqual({"--profile", "--json"}, _collect_parser_options(check_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(compile_parser))
        self.assertEqual({"--target", "--out", "--type-map", "--profile"}, _collect_parser_options(compile_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(graph_parser))
        self.assertEqual({"--html"}, _collect_parser_options(graph_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(impact_parser))
        self.assertEqual({"--intent", "--json"}, _collect_parser_options(impact_parser))

        self.assertEqual({"mode"}, _collect_parser_positionals(tooling_parser))
        self.assertEqual({"--json"}, _collect_parser_options(tooling_parser))

    def test_format_round_trip(self) -> None:
        spec = parse_file(EXAMPLE)
        formatted = format_spec(spec)
        reparsed = parse_text(formatted)

        self.assertEqual(spec, reparsed)

    def test_format_command_stdout(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["format", str(EXAMPLE)])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())
        reparsed = parse_text(stdout.getvalue())
        self.assertEqual("task", reparsed["meta"]["domain"])

    def test_format_command_write(self) -> None:
        original = EXAMPLE.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.promise"
            path.write_text("\n" + original, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["format", str(path), "--write"])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertIn("Formatted", stdout.getvalue())

            reparsed = parse_file(path)
            self.assertEqual("task", reparsed["meta"]["domain"])

    def test_format_check_success(self) -> None:
        formatted = format_spec(parse_file(EXAMPLE))

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.promise"
            path.write_text(formatted, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["format", str(path), "--check"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())
        self.assertIn("already formatted", stdout.getvalue())

    def test_format_check_failure(self) -> None:
        original = EXAMPLE.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "task.promise"
            path.write_text("\n" + original, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["format", str(path), "--check"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("not formatted", stderr.getvalue())

    def test_check_json_success(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["check", str(EXAMPLE), "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual("full", report["profile"])
        self.assertEqual(0, report["issueCount"])
        self.assertIsNone(report["error"])
        self.assertEqual("task", report["spec"]["meta"]["domain"])

    def test_check_json_core_profile_success(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["check", str(CORE_TOOLING), "--profile", "core", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual("core", report["profile"])
        self.assertEqual(0, report["issueCount"])
        self.assertEqual("promise_tooling", report["spec"]["meta"]["domain"])

    def test_check_json_failure(self) -> None:
        broken_text = EXAMPLE.read_text(encoding="utf-8").replace(
            "writes Task.status,Task.completedAt,Task.updatedAt",
            "writes Task.status,Task.unknownField",
            1,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "broken.promise"
            path.write_text(broken_text, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["check", str(path), "--json"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(1, report["issueCount"])
        self.assertIsNone(report["error"])
        self.assertTrue(
            any(issue["code"] == "function-unknown-write" for issue in report["issues"])
        )

    def test_compile_go_command_writes_contract_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "promisegen"

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["compile", str(EXAMPLE), "--target", "go", "--out", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertIn("Compiled go Promise artifacts", stdout.getvalue())

            generated_files = {path.name for path in output_path.iterdir()}
            self.assertEqual(
                {"types.go", "constraints.go", "transitions.go"},
                generated_files,
            )

            types_go = (output_path / "types.go").read_text(encoding="utf-8")
            constraints_go = (output_path / "constraints.go").read_text(encoding="utf-8")
            transitions_go = (output_path / "transitions.go").read_text(encoding="utf-8")

            self.assertIn("package task", types_go)
            self.assertIn("type TaskID string", types_go)
            self.assertIn("ID TaskID", types_go)
            self.assertIn("type TaskStatus string", types_go)
            self.assertIn('TaskStatusTodo TaskStatus = "todo"', types_go)
            self.assertIn("CompletedAt *time.Time", types_go)
            self.assertIn("func ValidateTaskPromise(value Task) error", constraints_go)
            self.assertIn("value.Status == TaskStatusDone && value.CompletedAt == nil", constraints_go)
            self.assertIn("func CanTransitionTaskStatus(from TaskStatus, to TaskStatus) bool", transitions_go)
            self.assertIn("case TaskStatusDone:", transitions_go)
            self.assertFalse((output_path / "promise_test.go").exists())

    def test_compile_go_builds_for_non_state_enum_and_nullable_invariant(self) -> None:
        if shutil.which("go") is None:
            self.skipTest("go is not installed")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            promise_path = tmp_path / "edge.promise"
            output_path = tmp_path / "ticket"
            promise_path.write_text(GO_EDGE_PROMISE_TEXT, encoding="utf-8")
            (tmp_path / "go.mod").write_text("module ticket-edge\n\ngo 1.22\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["compile", str(promise_path), "--target", "go", "--out", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            types_go = (output_path / "types.go").read_text(encoding="utf-8")
            constraints_go = (output_path / "constraints.go").read_text(encoding="utf-8")
            self.assertIn("type TicketPriority string", types_go)
            self.assertIn('TicketPriorityHigh TicketPriority = "high"', types_go)
            self.assertIn("if value.Status == nil", constraints_go)
            self.assertIn("switch *value.Status", constraints_go)
            self.assertIn(
                "value.Status != nil && *value.Status == TicketStatusDone",
                constraints_go,
            )

            env = os.environ.copy()
            env["GOCACHE"] = str(tmp_path / "gocache")
            result = subprocess.run(
                ["go", "test", "./..."],
                cwd=tmp_path,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_compile_go_accepts_type_mapping_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            output_path = tmp_path / "promisegen"
            type_map_path = tmp_path / "go-type-map.json"
            type_map_path.write_text(
                json.dumps(
                    {
                        "target": "go",
                        "types": {
                            "TaskID": {"type": "string"},
                        },
                        "primitives": {
                            "datetime": {"type": "string"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "compile",
                        str(EXAMPLE),
                        "--target",
                        "go",
                        "--type-map",
                        str(type_map_path),
                        "--out",
                        str(output_path),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            types_go = (output_path / "types.go").read_text(encoding="utf-8")
            self.assertNotIn("type TaskID string", types_go)
            self.assertNotIn('import "time"', types_go)
            self.assertIn("ID string", types_go)
            self.assertIn("CompletedAt *string", types_go)

    def test_compile_go_requires_out_directory(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["compile", str(EXAMPLE), "--target", "go"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("requires an explicit --out directory", stderr.getvalue())

    def test_compile_go_removes_old_generated_verify_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "promisegen"
            output_path.mkdir()
            stale_test = output_path / "promise_test.go"
            stale_test.write_text(
                "// Code generated by promise-go. DO NOT EDIT.\n\npackage task\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["compile", str(EXAMPLE), "--target", "go", "--out", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertFalse(stale_test.exists())

    def test_compile_go_preserves_user_owned_verify_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "promisegen"
            output_path.mkdir()
            user_test = output_path / "promise_test.go"
            user_test.write_text("package task\n\n// user-owned test file\n", encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["compile", str(EXAMPLE), "--target", "go", "--out", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertTrue(user_test.exists())
            self.assertIn("user-owned test file", user_test.read_text(encoding="utf-8"))

    def test_graph_command_writes_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "task-graph.html"

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(EXAMPLE), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())
            self.assertTrue(output_path.exists())
            self.assertIn("Wrote Promise graph HTML", stdout.getvalue())

            html = output_path.read_text(encoding="utf-8")
            self.assertIn("<!DOCTYPE html>", html)
            self.assertIn("TaskFieldPromise", html)
            self.assertIn("CreateTaskFunctionPromise", html)
            self.assertIn("TaskFieldInvariantVerification", html)
            self.assertIn("TaskSystemIntent", html)
            self.assertIn("Promise Graph", html)
            self.assertIn("<h1>TaskSystemIntent</h1>", html)
            self.assertIn("<strong>System Promise</strong>", html)
            self.assertIn("<code>Task Promise Spec</code>", html)
            self.assertIn("full · single", html)
            self.assertIn("Layered Directed Graph", html)
            self.assertIn("full-graph-network", html)
            self.assertIn("network-edge", html)
            self.assertIn("marker-end", html)
            self.assertIn("graph-toolbar", html)
            self.assertIn("graph-minimap", html)
            self.assertIn("cad-status-bar", html)
            self.assertIn("BFS LAYERED", html)
            self.assertIn('data-graph-zoom="in"', html)
            self.assertIn('data-graph-zoom="out"', html)
            self.assertIn('data-graph-zoom="fit"', html)
            self.assertIn('data-graph-trackpad-pan="true"', html)
            self.assertIn('data-graph-pinch-zoom="true"', html)
            self.assertIn('data-graph-mouse-wheel-zoom="true"', html)
            self.assertIn('data-graph-modifier-wheel-zoom="true"', html)
            self.assertIn("layer-row-guide", html)
            self.assertIn("layoutBreadthFirstLayers", html)
            self.assertIn("data-graph-layout", html)
            self.assertIn("breadth-first-layers", html)
            self.assertIn("data-graph-layer", html)
            self.assertIn("data-layer-row", html)
            self.assertIn("data-layer-route", html)
            self.assertIn("rail routing", html)
            self.assertIn("createGraphViewportController", html)
            self.assertIn("graphViewportController", html)
            self.assertIn("graphContentBounds", html)
            self.assertIn("graphPanBounds", html)
            self.assertIn("graphWorkspacePadding", html)
            self.assertIn("bottomVisualTop", html)
            self.assertIn("data-graph-content-bounds", html)
            self.assertIn("data-graph-pan-bounds", html)
            self.assertIn("data-graph-workspace-padding", html)
            self.assertIn("chromeBottom", html)
            self.assertIn("height: clamp(620px", html)
            self.assertIn("focusInitialViewport", html)
            self.assertIn("normalizeWheelZoomFactor", html)
            self.assertIn("Math.exp(-scaledDelta / 420)", html)
            self.assertIn("panByWheel", html)
            self.assertIn("classifyWheelEvent", html)
            self.assertIn("resolveWheelGestureKind", html)
            self.assertIn("wheelGestureIdleMs", html)
            self.assertIn("isTrackpadPinchWheel", html)
            self.assertIn("isDiscreteMouseWheel", html)
            self.assertIn('return "trackpad-scroll-pan"', html)
            self.assertIn('return "trackpad-pinch-zoom"', html)
            self.assertIn('return "mouse-wheel-zoom"', html)
            self.assertIn("gesturechange", html)
            self.assertIn("beginDrag", html)
            self.assertIn("moveDrag", html)
            self.assertIn("board.addEventListener(\"pointerdown\"", html)
            self.assertIn("board.addEventListener(\"mousedown\"", html)
            self.assertIn("window.addEventListener(\"mousemove\"", html)
            self.assertIn("overscroll-behavior: contain", html)
            self.assertIn("touch-action: none", html)
            self.assertIn("pointerdown", html)
            self.assertIn("wheel", html)
            self.assertIn("incomingById", html)
            self.assertIn("outgoingById", html)
            self.assertIn("rootNodes", html)
            self.assertIn("return 54", html)
            self.assertIn("return 42", html)
            self.assertNotIn("for (let step = 0", html)
            self.assertNotIn("repulsion", html)
            self.assertNotIn("jitter", html)
            self.assertNotIn("Human Intent</text>", html)
            self.assertNotIn("System Promise Items</text>", html)
            self.assertNotIn("01 Human Intent", html)
            self.assertNotIn("02 System", html)
            self.assertNotIn("03 Field", html)
            self.assertNotIn("04 Function", html)
            self.assertNotIn("05 Verify", html)
            self.assertNotIn("matrix-lane", html)
            self.assertNotIn("Dense Matrix Graph", html)
            self.assertNotIn("5-LANE MATRIX", html)
            self.assertNotIn("data-matrix-column", html)
            self.assertNotIn("data-matrix-route", html)
            self.assertNotIn("intent-region", html)
            self.assertNotIn("promise-region", html)
            self.assertNotIn("network-region", html)
            self.assertIn("data-graph-region", html)
            self.assertIn("data-graph-root", html)
            self.assertIn("root-intent-node", html)
            self.assertIn("Nodes with no incoming parent edge form the first layer", html)
            self.assertNotIn("Node Explorer", html)
            self.assertNotIn("Composite Graph", html)

            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {(edge["source"], edge["target"]): edge["label"] for edge in graph["edges"]}

            self.assertEqual("System Promise", nodes["system::root"]["label"])
            self.assertEqual("TaskSystemIntent", graph["rootIntentLabel"])
            self.assertIn("lifecycle truth", graph["rootIntentSummary"])
            self.assertTrue(nodes["intent::TaskSystemIntent"]["root"])
            self.assertIn(("intent::TaskSystemIntent", "system::root"), edges)
            self.assertEqual("defines System Promise", edges[("intent::TaskSystemIntent", "system::root")])
            self.assertNotIn(("system::root", "intent::TaskSystemIntent"), edges)

    def test_graph_command_preserves_cyclic_directed_edges(self) -> None:
        promise_text = """meta:
  title "Cycle Promise"
  domain cycle
  version v1
  status active
  summary "Promise with reciprocal function dependencies."

field CycleFieldPromise for Cycle:
  summary "Defines a cycle object."
  field value type string required true nullable false default null semantic "Cycle value." mutable true system false

function FirstFunctionPromise action First:
  summary "First cyclic function."
  depends SecondFunctionPromise
  trigger "First is executed."
  reads Cycle.value
  writes Cycle.value
  ensure FirstFunctionPromise.records_value statement "First records the value." refs Cycle.value

function SecondFunctionPromise action Second:
  summary "Second cyclic function."
  depends FirstFunctionPromise
  trigger "Second is executed."
  reads Cycle.value
  writes Cycle.value
  ensure SecondFunctionPromise.records_value statement "Second records the value." refs Cycle.value

verify CycleVerification kind function:
  claim "The cyclic functions remain explicit."
  verifies FirstFunctionPromise,SecondFunctionPromise
  methods unit
  scenario "cyclic relation is visible":
    covers FirstFunctionPromise.records_value,SecondFunctionPromise.records_value
    when "The graph is rendered."
    then "Both directed dependency edges are preserved."
  fail "A reciprocal dependency is collapsed or hidden."
"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "cycle.promise"
            output_path = Path(tmp_dir) / "cycle-graph.html"
            promise_path.write_text(promise_text, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
            nodes = {node["id"]: node for node in graph["nodes"]}

            self.assertEqual("System Promise", nodes["system::root"]["label"])
            self.assertIn(("function::FirstFunctionPromise", "function::SecondFunctionPromise"), edges)
            self.assertIn(("function::SecondFunctionPromise", "function::FirstFunctionPromise"), edges)
            self.assertIn("Cycles and reciprocal edges", html)

    def test_graph_command_marks_intent_graph_analysis_issues(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["maps"].append(
            {
                "target": "TaskSystemIntent",
                "relation": "supports",
                "note": "Unexpected reverse edge for graph analysis.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "intent-graph-issue.promise"
            output_path = Path(tmp_dir) / "intent-graph-issue.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            self.assertIn("graph-issue-edge", html)
            self.assertIn("graph-issue-node", html)
            self.assertIn('data-analysis-issue"', html)

            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {
                (edge["source"], edge["target"]): edge
                for edge in graph["edges"]
            }

            self.assertEqual(1, graph["intentGraphAnalysis"]["unexpectedCycles"][0]["nodeIds"].count("intent::TaskSystemIntent"))
            self.assertEqual("reciprocal", graph["intentGraphAnalysis"]["unexpectedCycles"][0]["kind"])
            self.assertGreater(nodes["intent::TaskSystemIntent"]["graphIssueCount"], 0)
            issue_edge = edges[("intent::PreserveTaskLifecycleTruth", "intent::TaskSystemIntent")]
            self.assertEqual("graph-issue", issue_edge["kind"])
            self.assertEqual("cycle", issue_edge["analysisIssue"])

    def test_graph_command_renders_intent_conflict_edges(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["conflicts"].append(
            {
                "target": "KeepTaskCreationSimple",
                "severity": "tension",
                "reason": "Lifecycle truth can pressure creation simplicity.",
                "resolution": "Creation remains input-simple while lifecycle fields stay explicit.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "conflict.promise"
            output_path = Path(tmp_dir) / "conflict-graph.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            self.assertIn("intent conflict edges", html)
            self.assertIn("conflict-edge", html)
            self.assertIn("conflicted-intent-node", html)
            self.assertIn("data-conflict-severity", html)

            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {
                (edge["source"], edge["target"]): edge
                for edge in graph["edges"]
            }

            conflict_edge = edges[("intent::PreserveTaskLifecycleTruth", "intent::KeepTaskCreationSimple")]
            self.assertEqual("conflict", conflict_edge["kind"])
            self.assertEqual("tension", conflict_edge["severity"])
            self.assertIn("conflicts tension", conflict_edge["label"])
            self.assertEqual(1, nodes["intent::PreserveTaskLifecycleTruth"]["conflictCount"])
            self.assertEqual(1, nodes["intent::KeepTaskCreationSimple"]["conflictCount"])

    def test_graph_command_renders_auto_intent_conflict_edges(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].append(
            {
                "name": "AvoidCompletionBehavior",
                "priority": "must",
                "status": "active",
                "root": False,
                "statement": "The task system must avoid exposing completion behavior.",
                "rationale": "This intent intentionally opposes the completion function for conflict detection.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [],
                "maps": [
                    {
                        "target": "CompleteTaskFunctionPromise",
                        "relation": "conflicts",
                        "note": "Completion behavior is intentionally opposed.",
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "auto-conflict.promise"
            output_path = Path(tmp_dir) / "auto-conflict-graph.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {
                (edge["source"], edge["target"]): edge
                for edge in graph["edges"]
            }

            conflict_edge = edges[("intent::AvoidCompletionBehavior", "intent::PreserveTaskLifecycleTruth")]
            self.assertEqual("conflict", conflict_edge["kind"])
            self.assertEqual("blocking", conflict_edge["severity"])
            self.assertIn("auto conflict blocking", conflict_edge["label"])
            self.assertEqual(1, nodes["intent::AvoidCompletionBehavior"]["conflictCount"])
            self.assertEqual(1, nodes["intent::PreserveTaskLifecycleTruth"]["conflictCount"])

    def test_graph_command_renders_auto_requirement_conflict_edges(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].extend(
            [
                {
                    "name": "RequireExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must be able to export a task.",
                    "rationale": "Export is a human requirement before implementation details are chosen.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "UserExportTask",
                            "kind": "requires",
                            "subject": "User",
                            "predicate": "can",
                            "object": "export_task",
                        }
                    ],
                    "maps": [],
                },
                {
                    "name": "ForbidExport",
                    "priority": "must",
                    "status": "active",
                    "root": False,
                    "statement": "Users must not be able to export a task.",
                    "rationale": "This intentionally opposes the export requirement.",
                    "sources": ["test"],
                    "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                    "conflicts": [],
                    "requirements": [
                        {
                            "id": "NoUserExportTask",
                            "kind": "forbids",
                            "subject": "User",
                            "predicate": "can",
                            "object": "export_task",
                        }
                    ],
                    "maps": [],
                },
            ]
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "auto-requirement-conflict.promise"
            output_path = Path(tmp_dir) / "auto-requirement-conflict-graph.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {
                (edge["source"], edge["target"]): edge
                for edge in graph["edges"]
            }

            conflict_edge = edges[("intent::ForbidExport", "intent::RequireExport")]
            self.assertEqual("conflict", conflict_edge["kind"])
            self.assertEqual("blocking", conflict_edge["severity"])
            self.assertIn("auto conflict blocking", conflict_edge["label"])
            self.assertEqual(1, nodes["intent::ForbidExport"]["conflictCount"])
            self.assertEqual(1, nodes["intent::RequireExport"]["conflictCount"])

    def test_graph_command_renders_intent_resource_nodes_and_operation_edges(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentResources"] = [
            {
                "name": "User",
                "kind": "actor",
                "summary": "Human user.",
                "aliases": ["end_user"],
                "maps": [],
            },
            {
                "name": "Task",
                "kind": "entity",
                "summary": "Task resource.",
                "aliases": [],
                "maps": [
                    {
                        "target": "TaskFieldPromise",
                        "relation": "constrains",
                    }
                ],
            },
        ]
        spec["intentTerms"] = [
            {
                "name": "user_workspace",
                "kind": "scope",
                "summary": "Workspace visible to one user.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "export",
                "kind": "action",
                "summary": "Export a resource.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "export_file",
                "kind": "effect",
                "summary": "A file is produced.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "authorized_user",
                "kind": "constraint",
                "summary": "Only an authorized user can operate.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [{"target": "TaskFieldPromise", "relation": "constrains"}],
            },
        ]
        spec["intentPromises"][1]["requirements"] = [
            {
                "id": "UserExportsTask",
                "kind": "requires",
                "actor": "User",
                "action": "export",
                "resource": "Task",
                "subject": "User",
                "predicate": "export",
                "object": "Task",
                "scope": "user_workspace",
                "effect": "export_file",
                "constraint": "authorized_user",
                "priority": "must",
            }
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "intent-resource-graph.promise"
            output_path = Path(tmp_dir) / "intent-resource-graph.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])
            nodes = {node["id"]: node for node in graph["nodes"]}
            edges = {
                (edge["source"], edge["target"]): edge
                for edge in graph["edges"]
            }

            self.assertEqual("resource", nodes["resource::Task"]["kind"])
            self.assertEqual("intent", nodes["resource::Task"]["lane"])
            self.assertEqual("Resource", nodes["resource::Task"]["anchor"])
            self.assertEqual("term", nodes["term::scope::user_workspace"]["kind"])
            self.assertEqual("Term:scope", nodes["term::scope::user_workspace"]["anchor"])
            self.assertEqual("actor", edges[("resource::User", "intent::PreserveTaskLifecycleTruth")]["label"])
            self.assertIn(
                "requires export -> export_file @user_workspace",
                edges[("intent::PreserveTaskLifecycleTruth", "resource::Task")]["label"],
            )
            self.assertEqual("action", edges[("intent::PreserveTaskLifecycleTruth", "term::action::export")]["label"])
            self.assertEqual("scope", edges[("intent::PreserveTaskLifecycleTruth", "term::scope::user_workspace")]["label"])
            self.assertEqual("effect", edges[("intent::PreserveTaskLifecycleTruth", "term::effect::export_file")]["label"])
            self.assertEqual(
                "constraint",
                edges[("intent::PreserveTaskLifecycleTruth", "term::constraint::authorized_user")]["label"],
            )
            self.assertTrue(
                any(
                    "constraint authorized_user" in detail
                    for detail in nodes["intent::PreserveTaskLifecycleTruth"]["details"]
                )
            )
            self.assertEqual("constrains", edges[("resource::Task", "field::TaskFieldPromise")]["label"])
            self.assertEqual("constrains", edges[("term::constraint::authorized_user", "field::TaskFieldPromise")]["label"])

    def test_impact_command_json_success(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["impact", str(EXAMPLE), "--intent", "PreserveTaskLifecycleTruth", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual("TaskSystemIntent", report["rootIntent"])
        self.assertEqual("PreserveTaskLifecycleTruth", report["selectedIntent"])
        self.assertEqual(["TaskSystemIntent"], [item["name"] for item in report["intentChain"]["ancestors"]])
        self.assertIn("Task.status", {item["target"] for item in report["directItems"]})
        self.assertIn("CompleteTaskFunctionPromise", {item["target"] for item in report["directItems"]})
        self.assertIn("TaskFieldInvariantVerification", {item["target"] for item in report["downstreamItems"]})
        self.assertIn("TaskSystemIntent", {item["name"] for item in report["relatedIntents"]})
        self.assertEqual([], report["conflicts"])

    def test_impact_command_json_reports_intent_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["conflicts"].append(
            {
                "target": "KeepTaskCreationSimple",
                "severity": "tension",
                "reason": "Lifecycle truth adds constraints near creation simplicity.",
                "resolution": "Keep creation input simple and derive lifecycle state.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "conflict.promise"
            promise_path.write_text(format_spec(spec), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["impact", str(promise_path), "--intent", "KeepTaskCreationSimple", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(1, len(report["intentConflicts"]))
        self.assertEqual(1, len(report["declaredIntentConflicts"]))
        self.assertEqual([], report["detectedIntentConflicts"])
        self.assertEqual("in", report["conflicts"][0]["direction"])
        self.assertEqual("PreserveTaskLifecycleTruth", report["conflicts"][0]["source"])
        self.assertEqual("KeepTaskCreationSimple", report["conflicts"][0]["target"])
        self.assertEqual("tension", report["conflicts"][0]["severity"])
        self.assertEqual("declared", report["conflicts"][0]["sourceType"])

    def test_impact_command_json_reports_auto_intent_conflicts(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"].append(
            {
                "name": "AvoidCompletionBehavior",
                "priority": "must",
                "status": "active",
                "root": False,
                "statement": "The task system must avoid exposing completion behavior.",
                "rationale": "This intent intentionally opposes the completion function for conflict detection.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [],
                "maps": [
                    {
                        "target": "CompleteTaskFunctionPromise",
                        "relation": "conflicts",
                        "note": "Completion behavior is intentionally opposed.",
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "auto-conflict.promise"
            promise_path.write_text(format_spec(spec), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["impact", str(promise_path), "--intent", "AvoidCompletionBehavior", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual([], report["declaredIntentConflicts"])
        self.assertEqual(1, len(report["detectedIntentConflicts"]))
        self.assertEqual(1, len(report["conflicts"]))
        self.assertEqual("out", report["conflicts"][0]["direction"])
        self.assertEqual("AvoidCompletionBehavior", report["conflicts"][0]["source"])
        self.assertEqual("PreserveTaskLifecycleTruth", report["conflicts"][0]["target"])
        self.assertEqual("detected", report["conflicts"][0]["sourceType"])
        self.assertEqual("opposed-map-relation", report["conflicts"][0]["detector"])

    def test_impact_command_json_reports_intent_graph_issues(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentPromises"][1]["maps"].append(
            {
                "target": "TaskSystemIntent",
                "relation": "supports",
                "note": "Unexpected reverse edge for impact reporting.",
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "intent-graph-issue.promise"
            promise_path.write_text(format_spec(spec), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "impact",
                        str(promise_path),
                        "--intent",
                        "TaskSystemIntent",
                        "--json",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertEqual(1, report["intentGraph"]["unexpectedCycleCount"])
        self.assertEqual("reciprocal", report["intentGraph"]["unexpectedCycles"][0]["kind"])
        self.assertTrue(any(issue["type"] == "unexpectedCycle" for issue in report["graphIssues"]))

    def test_impact_command_json_reports_intent_requirements(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        spec["intentResources"] = [
            {
                "name": "IntentLayer",
                "kind": "system",
                "summary": "Intent layer resource.",
                "aliases": [],
                "maps": [],
            },
            {
                "name": "HumanRequirementSyntax",
                "kind": "concept",
                "summary": "Common human requirement syntax resource.",
                "aliases": [],
                "maps": [],
            },
        ]
        spec["intentTerms"] = [
            {
                "name": "intent_layer",
                "kind": "scope",
                "summary": "Intent layer scope.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "extracts",
                "kind": "action",
                "summary": "Extract structured meaning.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "typed_atom",
                "kind": "effect",
                "summary": "Typed atom is produced.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
            {
                "name": "stable_token",
                "kind": "constraint",
                "summary": "Atoms use stable tokens.",
                "aliases": [],
                "parent": "",
                "disjoint": [],
                "opposites": [],
                "maps": [],
            },
        ]
        spec["intentPromises"].append(
            {
                "name": "CaptureHumanRequirementSyntax",
                "priority": "must",
                "status": "active",
                "root": False,
                "statement": "Intent must capture ordinary human requirement syntax as structured atoms.",
                "rationale": "Human-language requirements need a stable intermediate form before Promise items are generated.",
                "sources": ["test"],
                "parents": [{"target": "TaskSystemIntent", "relation": "refines"}],
                "conflicts": [],
                "requirements": [
                    {
                        "id": "HumanRequirementAtom",
                        "kind": "requires",
                        "actor": "IntentLayer",
                        "action": "extracts",
                        "resource": "HumanRequirementSyntax",
                        "subject": "IntentLayer",
                        "predicate": "extracts",
                        "object": "HumanRequirementSyntax",
                        "scope": "intent_layer",
                        "effect": "typed_atom",
                        "constraint": "stable_token",
                        "priority": "must",
                    }
                ],
                "maps": [],
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "intent-requirements.promise"
            promise_path.write_text(format_spec(spec), encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "impact",
                        str(promise_path),
                        "--intent",
                        "CaptureHumanRequirementSyntax",
                        "--json",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(2, report["resourceCount"])
        self.assertEqual(4, report["termCount"])
        self.assertEqual(1, len(report["requirements"]))
        self.assertEqual("HumanRequirementAtom", report["requirements"][0]["id"])
        self.assertEqual("extracts", report["requirements"][0]["predicate"])
        self.assertEqual("intent_layer", report["requirements"][0]["scope"])
        self.assertEqual("typed_atom", report["requirements"][0]["effect"])
        self.assertEqual("stable_token", report["requirements"][0]["constraint"])
        self.assertEqual("must", report["requirements"][0]["priority"])
        self.assertEqual(2, len(report["resources"]))
        self.assertEqual({"HumanRequirementSyntax", "IntentLayer"}, {item["name"] for item in report["resources"]})
        self.assertEqual({"intent_layer"}, {item["scope"] for item in report["resources"]})
        self.assertEqual({"typed_atom"}, {item["effect"] for item in report["resources"]})
        self.assertEqual({"extracts", "intent_layer", "stable_token", "typed_atom"}, {item["name"] for item in report["terms"]})
        self.assertEqual({"action", "constraint", "effect", "scope"}, {item["role"] for item in report["terms"]})
        self.assertEqual(1, report["intentChain"]["self"]["requirementCount"])

    def test_impact_command_json_unknown_intent(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["impact", str(EXAMPLE), "--intent", "MissingIntent", "--json"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual("unknown_intent", report["error"]["type"])

    def test_graph_command_keeps_large_graphs_as_full_directed_graphs(self) -> None:
        spec = clone_spec(parse_file(EXAMPLE))
        base_function = spec["functionPromises"][0]

        for index in range(32):
            generated = json.loads(json.dumps(base_function))
            generated["name"] = f"GeneratedFunctionPromise{index}"
            generated["action"] = f"GeneratedAction{index}"
            spec["functionPromises"].append(generated)

        with tempfile.TemporaryDirectory() as tmp_dir:
            promise_path = Path(tmp_dir) / "large.promise"
            output_path = Path(tmp_dir) / "large-graph.html"
            promise_path.write_text(format_spec(spec), encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["graph", str(promise_path), "--html", str(output_path)])

            self.assertEqual(0, exit_code)
            self.assertEqual("", stderr.getvalue())

            html = output_path.read_text(encoding="utf-8")
            marker = '<script id="promise-graph-data" type="application/json">'
            start = html.index(marker) + len(marker)
            end = html.index("</script>", start)
            graph = json.loads(html[start:end])

            self.assertEqual("full", graph["viewMode"])
            self.assertEqual("single", graph["composition"])
            self.assertEqual(len(graph["nodes"]), graph["nodeCount"])
            self.assertIn("full · single", html)
            self.assertIn("Layered Directed Graph", html)
            self.assertIn("full-graph-network", html)
            self.assertIn("network-edge", html)
            self.assertIn("graph-toolbar", html)
            self.assertIn("graph-minimap", html)
            self.assertIn("cad-status-bar", html)
            self.assertIn("BFS LAYERED", html)
            self.assertIn("createGraphViewportController", html)
            self.assertIn("graphContentBounds", html)
            self.assertIn("graphPanBounds", html)
            self.assertIn("graphWorkspacePadding", html)
            self.assertIn("bottomVisualTop", html)
            self.assertIn("data-graph-content-bounds", html)
            self.assertIn("data-graph-pan-bounds", html)
            self.assertIn("data-graph-workspace-padding", html)
            self.assertIn("focusInitialViewport", html)
            self.assertIn("layoutBreadthFirstLayers", html)
            self.assertIn("incomingById", html)
            self.assertIn("outgoingById", html)
            self.assertIn("data-graph-layer", html)
            self.assertIn("data-layer-route", html)
            self.assertNotIn("for (let step = 0", html)
            self.assertNotIn("repulsion", html)
            self.assertNotIn("jitter", html)
            self.assertNotIn("Human Intent</text>", html)
            self.assertNotIn("System Promise Items</text>", html)
            self.assertNotIn("01 Human Intent", html)
            self.assertNotIn("02 System", html)
            self.assertNotIn("03 Field", html)
            self.assertNotIn("04 Function", html)
            self.assertNotIn("05 Verify", html)
            self.assertNotIn("matrix-lane", html)
            self.assertNotIn("Dense Matrix Graph", html)
            self.assertNotIn("5-LANE MATRIX", html)
            self.assertNotIn("data-matrix-column", html)
            self.assertNotIn("data-matrix-route", html)
            self.assertNotIn("intent-region", html)
            self.assertNotIn("promise-region", html)
            self.assertNotIn("network-region", html)
            self.assertNotIn("Node Explorer", html)
            self.assertNotIn("Composite Graph", html)
            self.assertNotIn("cluster-graph-network", html)

    def test_tooling_verify_json_success(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["tooling", "verify", "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual("verify", report["mode"])
        self.assertEqual(0, report["issueCount"])
        self.assertTrue(any(check["name"] == "repo skill mirrors src/promise_cli/cli.py" for check in report["checks"]))

    def test_lint_json_success(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["lint", str(EXAMPLE), "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual("full", report["profile"])
        self.assertEqual(0, report["issueCount"])
        self.assertIsNone(report["error"])
        self.assertIsNone(report["spec"])

    def test_lint_json_core_profile_failure(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["lint", str(EXAMPLE), "--profile", "core", "--json"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual("core", report["profile"])
        self.assertGreater(report["issueCount"], 0)
        self.assertTrue(
            any(issue["code"].startswith("core-non-minimal-") for issue in report["issues"])
        )

    def test_lint_json_failure(self) -> None:
        broken_text = EXAMPLE.read_text(encoding="utf-8").replace(
            "writes Task.status,Task.completedAt,Task.updatedAt",
            "writes Task.status,Task.unknownField",
            1,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "broken.promise"
            path.write_text(broken_text, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["lint", str(path), "--json"])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertFalse(report["ok"])
        self.assertEqual(1, report["issueCount"])
        self.assertIsNone(report["error"])
        self.assertIsNone(report["spec"])
        self.assertTrue(
            any(issue["code"] == "function-unknown-write" for issue in report["issues"])
        )

    def test_lint_json_warning_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "warning.promise"
            path.write_text(WARNING_PROMISE_TEXT, encoding="utf-8")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["lint", str(path), "--json"])

        self.assertEqual(0, exit_code)
        self.assertEqual("", stderr.getvalue())

        report = json.loads(stdout.getvalue())
        self.assertTrue(report["ok"])
        self.assertEqual(3, report["issueCount"])
        self.assertEqual(0, report["errorCount"])
        self.assertEqual(3, report["warningCount"])
        self.assertTrue(
            any(issue["code"] == "field-missing-invariant-coverage" for issue in report["issues"])
        )

def _collect_parser_positionals(parser: argparse.ArgumentParser) -> set[str]:
    return {
        action.dest
        for action in parser._actions
        if action.option_strings == [] and action.dest != "help"
    }


def _collect_parser_options(parser: argparse.ArgumentParser) -> set[str]:
    options: set[str] = set()
    for action in parser._actions:
        options.update(
            option for option in action.option_strings if option not in {"-h", "--help"}
        )
    return options


if __name__ == "__main__":
    unittest.main()
