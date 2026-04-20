from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
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


class PromiseCliTests(unittest.TestCase):
    def test_parse_example(self) -> None:
        spec = parse_file(EXAMPLE)

        self.assertEqual(spec["schemaVersion"], "1.0.0")
        self.assertEqual(spec["meta"]["domain"], "task")
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
        self.assertEqual(0, tooling_core["fieldPromises"][0]["fields"][2]["default"])
        tooling_document_fields = {
            field["name"]: field["default"]
            for field in tooling_promise["fieldPromises"][1]["fields"]
        }
        self.assertEqual(0, tooling_document_fields["issueCount"])
        self.assertEqual(1, len(task_core["fieldPromises"]))
        self.assertEqual(3, len(tooling_core["functionPromises"]))
        self.assertEqual(2, len(tooling_promise["fieldPromises"]))
        self.assertEqual(6, len(tooling_promise["functionPromises"]))

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
        self.assertEqual([], lint_spec(parse_file(TOOLING_PROMISE), profile="core"))

    def test_lint_core_profile_rejects_enhanced_promise(self) -> None:
        issues = lint_spec(parse_file(EXAMPLE), profile="core")

        self.assertTrue(
            any(issue.code.startswith("core-non-minimal-") for issue in issues),
            "expected core profile to reject enhanced Promise features",
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

        self.assertEqual({"parse", "format", "lint", "check", "graph", "tooling"}, implemented_actions)
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
                "graph": [
                    "parse_source",
                    "render_graph_html",
                    "emit_graph_result",
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
        graph_parser = subparser_action.choices["graph"]
        tooling_parser = subparser_action.choices["tooling"]

        self.assertEqual({"path"}, _collect_parser_positionals(parse_parser))
        self.assertEqual(set(), _collect_parser_options(parse_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(format_parser))
        self.assertEqual({"--write", "--check"}, _collect_parser_options(format_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(lint_parser))
        self.assertEqual({"--profile", "--json"}, _collect_parser_options(lint_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(check_parser))
        self.assertEqual({"--profile", "--json"}, _collect_parser_options(check_parser))

        self.assertEqual({"path"}, _collect_parser_positionals(graph_parser))
        self.assertEqual({"--html"}, _collect_parser_options(graph_parser))

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
            self.assertIn("Promise Graph", html)
            self.assertIn("full · single", html)

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
