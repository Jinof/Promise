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
from promise_cli.dsl import clone_spec, format_spec, lint_spec, parse_file, parse_text


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
        self.assertEqual(5, len(tooling_promise["intentPromises"]))
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
            self.assertIn("full · single", html)

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

    def test_graph_command_switches_to_composite_view_for_large_graphs(self) -> None:
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
            self.assertIn("overview · composite", html)
            self.assertIn("Node Explorer", html)
            self.assertIn("Aggregate Relations", html)
            self.assertIn("composite viewer", html)
            self.assertIn("Composite Graph", html)
            self.assertIn("cluster-graph-board", html)
            self.assertIn("data-overview-node-id", html)

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
