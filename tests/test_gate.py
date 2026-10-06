import asyncio
import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet, InvalidToken

import install
import quiz_gate as gate


def question(kind="comprehension", question_type="free_text"):
    return {"topic": "behavior", "kind": kind, "type": question_type,
            "question": "What does the code do and what do you intend?",
            "options": [], "correct_option": "", "rubric": "Describe behavior accurately.",
            "explanation": "The code chooses a default.", "evidence": ["main.py:12"]}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = gate.config()

    def test_strict_majority_even_and_odd(self):
        self.assertEqual(gate.required_correct(3, self.cfg), 2)
        self.assertEqual(gate.required_correct(4, self.cfg), 3)

    def test_percentage_rounds_up(self):
        self.cfg["pass_rule"] = "percentage"
        self.assertEqual(gate.required_correct(3, self.cfg), 3)

    def test_action_inputs_override_bundled_defaults(self):
        with patch.dict(os.environ, {"QUIZ_MODE": "required", "QUIZ_MAX_QUESTIONS": "5",
                                     "QUIZ_PASS_RULE": "percentage", "QUIZ_PASS_PERCENTAGE": "60",
                                     "QUIZ_QUESTION_TYPES": "free_text, multiple_choice",
                                     "QUIZ_ALLOW_OVERRIDE": "false"}):
            cfg = gate.config()
            self.assertEqual(cfg["mode"], "required")
            self.assertEqual(cfg["max_questions"], 5)
            self.assertEqual(cfg["question_types"], ["free_text", "multiple_choice"])
            self.assertFalse(cfg["allow_override"])
            self.assertEqual(gate.required_correct(5, cfg), 3)

    def test_invalid_action_boolean_rejected(self):
        with patch.dict(os.environ, {"QUIZ_ALLOW_OVERRIDE": "yes"}):
            with self.assertRaises(AssertionError):
                gate.config()

    def test_action_question_count_limit_enforced(self):
        with patch.dict(os.environ, {"QUIZ_MAX_QUESTIONS": "11"}):
            with self.assertRaises(AssertionError):
                gate.config()

    def test_partial_answers_do_not_count(self):
        results = [{"verdict": value, "needs_followup": False}
                   for value in ["correct", "partial", "uncertain"]]
        self.assertFalse(gate.passed(results, self.cfg))

    def test_unresolved_decision_blocks_even_perfect_score(self):
        results = [{"verdict": "correct", "needs_followup": True}]
        self.assertFalse(gate.passed(results, self.cfg))

    def test_quiz_accepts_no_material_topics(self):
        gate.validate_quiz({"summary": "Only whitespace changed.", "questions": []}, self.cfg)

    def test_ambiguous_decision_cannot_be_multiple_choice(self):
        item = question("decision", "multiple_choice")
        item["options"] = ["one", "two", "three", "four"]
        item["correct_option"] = "A"
        with self.assertRaises(AssertionError):
            gate.validate_quiz({"summary": "Choice", "questions": [item]}, self.cfg)

    def test_empty_or_duplicate_options_rejected(self):
        item = question(question_type="multiple_choice")
        item["options"] = ["same"] * 4
        item["correct_option"] = "A"
        with self.assertRaises(AssertionError):
            gate.validate_quiz({"summary": "Choice", "questions": [item]}, self.cfg)

    def test_answers_are_complete_and_bound_to_quiz(self):
        state = {"id": "abc", "questions": [{"id": 1}, {"id": 2}]}
        self.assertEqual(gate.parse_answers("/quiz answer abc\n1: A\n2: explain\nmore", state),
                         {1: "A", 2: "explain\nmore"})
        for body in ["/quiz answer old\n1: A\n2: B", "/quiz answer abc\n1: A",
                     "/quiz answer abc\n1: A\n1: B\n2: B"]:
            with self.assertRaises(AssertionError):
                gate.parse_answers(body, state)

    def test_state_is_encrypted_and_authenticated(self):
        with patch.dict(os.environ, {"QUIZ_STATE_KEY": Fernet.generate_key().decode()}):
            state = {"rubric": "secret answer"}
            token = gate.encode_state(state)
            self.assertNotIn("secret answer", token)
            self.assertEqual(gate.decode_state(f"<!-- quiz-gate:{token} -->"), state)
            with patch.dict(os.environ, {"QUIZ_STATE_KEY": Fernet.generate_key().decode()}):
                with self.assertRaises(InvalidToken):
                    gate.decode_state(f"<!-- quiz-gate:{token} -->")

    def test_derived_state_key_is_bound_to_repository_and_token(self):
        with patch.dict(os.environ, {"QUIZ_STATE_KEY": "", "QUIZ_ENGINE": "copilot",
                                     "COPILOT_GITHUB_TOKEN": "first-token", "GITHUB_REPOSITORY": "owner/one"}):
            state = {"answer": "secret"}
            encrypted = gate.encode_state(state)
            self.assertEqual(gate.decode_state(f"<!-- quiz-gate:{encrypted} -->"), state)
            for changed in [{"GITHUB_REPOSITORY": "owner/two"}, {"COPILOT_GITHUB_TOKEN": "second-token"}]:
                with patch.dict(os.environ, changed), self.assertRaises(InvalidToken):
                    gate.decode_state(f"<!-- quiz-gate:{encrypted} -->")

    def test_stale_commit_base_policy_or_respondent_rejected(self):
        pr = {"head": {"sha": "head"}, "base": {"sha": "base"}, "user": {"login": "human"}}
        state = {"repo": "owner/repo", "pr": 1, "sha": "head", "base_sha": "base",
                 "policy": gate.policy_digest(self.cfg), "respondent": "human"}
        gate.assert_binding(state, "owner/repo", 1, pr, self.cfg)
        for field in ["sha", "base_sha", "policy", "respondent"]:
            changed = copy.deepcopy(state)
            changed[field] = "different"
            with self.assertRaises(AssertionError):
                gate.assert_binding(changed, "owner/repo", 1, pr, self.cfg)

    def test_mc_grading_does_not_call_model(self):
        item = question(question_type="multiple_choice")
        item.update({"id": 1, "correct_option": "B"})
        with patch.object(gate, "model_output") as model:
            result = gate.grade({"questions": [item]}, {1: "b"})
            self.assertEqual(result[0]["verdict"], "correct")
            model.assert_not_called()

    def test_missing_and_duplicate_model_grades_rejected(self):
        items = [dict(question(), id=1), dict(question(), id=2)]
        output = {"results": [{"id": 1, "verdict": "correct", "feedback": "ok", "needs_followup": False}] * 2}
        with patch.object(gate, "model_output", return_value=output):
            with self.assertRaises(AssertionError):
                gate.grade({"questions": items}, {1: "explain", 2: "explain"})

    def test_advisory_and_required_statuses(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "1"}), patch.object(gate, "github") as api:
            gate.status("owner/repo", "sha", self.cfg, "failure", "wrong answers")
            self.assertEqual(api.call_args.args[1]["state"], "success")
            self.cfg["mode"] = "required"
            gate.status("owner/repo", "sha", self.cfg, "failure", "wrong answers")
            self.assertEqual(api.call_args.args[1]["state"], "failure")

    def test_human_cannot_forge_bot_state(self):
        with patch.object(gate, "pages", return_value=[{
            "id": 1, "user": {"login": "human", "type": "User"},
            "body": "<!-- quiz-gate:forged -->",
        }]):
            self.assertEqual(gate.load_state("/repo/issues/1"), {})

    def test_truncated_model_response_rejected(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "fake", "CLAUDE_MODEL": "fake"}), \
                patch.object(gate, "request_json", return_value={"stop_reason": "max_tokens"}):
            with self.assertRaises(AssertionError):
                gate.claude("generate.md", {}, gate.QUIZ_SCHEMA)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.cfg = gate.config()
        self.pr = {"title": "Change", "body": "Details",
                   "head": {"sha": "head", "repo": {"full_name": "owner/repo"}},
                   "base": {"sha": "base"}}
        self.files = [{"filename": "main.py", "status": "modified", "patch": "+new behavior"}]
        self.content = {"type": "file", "encoding": "base64", "size": 400,
                        "content": base64.b64encode(("x" * 400).encode()).decode()}

    def context(self, previous=()):
        with patch.object(gate, "pages", side_effect=[self.files, []]), \
                patch.object(gate, "github", return_value=self.content):
            return gate.context("owner/repo", 1, self.pr, self.cfg, previous)

    def test_budget_can_be_increased_without_losing_evidence(self):
        self.cfg["max_context_chars"] = 450
        with self.assertRaisesRegex(AssertionError, "Context needs .*main.py.*Increase max-context-chars"):
            self.context()
        self.cfg["max_context_chars"] = 2000
        data = self.context()
        self.assertEqual(data["files"][0]["source"], "x" * 400)
        self.assertEqual(data["files"][0]["patch"], "+new behavior")

    def test_retry_questions_count_toward_the_transmitted_budget(self):
        data = self.context()
        self.cfg["max_context_chars"] = len(gate.model_input(data))
        self.context()
        with self.assertRaisesRegex(AssertionError, "Context needs"):
            self.context([question()])

    def test_unicode_is_preserved_in_the_measured_payload(self):
        self.pr["body"] = "β" * 100
        data = self.context()
        self.cfg["max_context_chars"] = len(gate.model_input(data))
        transmitted = gate.model_input(self.context())
        self.assertIn("β" * 100, transmitted)
        self.assertEqual(json.loads(transmitted), data)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.event_path = Path(self.temp.name) / "event.json"
        self.cfg = gate.config()
        self.cfg["mode"] = "required"
        self.pr = {"state": "open", "head": {"sha": "head"}, "base": {"sha": "base"},
                   "user": {"login": "author"}}
        self.comments = []
        self.statuses = []
        self.submitted = {}
        self.env = patch.dict(os.environ, {
            "GITHUB_EVENT_PATH": str(self.event_path), "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "1", "QUIZ_STATE_KEY": Fernet.generate_key().decode(),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def api(self, path, payload=None, method="GET"):
        if path == "/repos/owner/repo/pulls/1":
            return self.pr
        if path == "/repos/owner/repo/issues/1/comments?per_page=100&page=1":
            return self.comments
        if path == "/repos/owner/repo/issues/comments/100":
            return self.submitted
        if path == "/repos/owner/repo/issues/1/comments" and method == "POST":
            self.comments.append({"id": len(self.comments) + 1,
                                  "user": {"login": "github-actions[bot]", "type": "Bot"},
                                  "body": payload["body"]})
            return self.comments[-1]
        if path.startswith("/repos/owner/repo/statuses/") and method == "POST":
            self.statuses.append(payload)
            return payload
        if path == "/repos/owner/repo/collaborators/maintainer/permission":
            return {"permission": "maintain"}
        raise AssertionError(f"Unexpected API request: {method} {path}")

    def run_event(self, name, body="", actor="author", output=None):
        event = {"pull_request": {"number": 1}}
        if name == "issue_comment":
            event = {"issue": {"number": 1, "pull_request": {}}, "comment": {"id": 100}}
            self.submitted = {"user": {"login": actor, "type": "User"}, "body": body}
        self.event_path.write_text(json.dumps(event))
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": name}), \
                patch.object(gate, "config", return_value=self.cfg), \
                patch.object(gate, "github", side_effect=self.api), \
                patch.object(gate, "context", return_value={}), \
                patch.object(gate, "model_output", return_value=output), \
                patch.object(gate.sys, "argv", ["quiz_gate.py"]):
            gate.main()

    def start_quiz(self, kind="comprehension"):
        item = question(kind)
        item["rubric"] = "PRIVATE_ANSWER_KEY"
        self.run_event("pull_request_target", output={"summary": "Meaningful change", "questions": [item]})
        return gate.decode_state(self.comments[-1]["body"])

    def test_pr_generation_then_human_answer_unlocks_current_revision(self):
        state = self.start_quiz()
        self.assertEqual(self.statuses[-1]["state"], "pending")
        self.assertNotIn("PRIVATE_ANSWER_KEY", self.comments[-1]["body"])
        self.run_event("issue_comment", f"/quiz answer {state['id']}\n1: explanation", output={
            "results": [{"id": 1, "verdict": "correct", "feedback": "Understood", "needs_followup": False}]})
        self.assertEqual(self.statuses[-1]["state"], "success")
        self.assertEqual(gate.decode_state(self.comments[-1]["body"])["phase"], "passed")

    def test_unresolved_decision_cannot_be_bypassed_with_retry(self):
        state = self.start_quiz("decision")
        self.run_event("issue_comment", f"/quiz answer {state['id']}\n1: I intend a different behavior", output={
            "results": [{"id": 1, "verdict": "correct", "feedback": "Change requested", "needs_followup": True}]})
        self.assertEqual(self.statuses[-1]["state"], "failure")
        with self.assertRaises(AssertionError):
            self.run_event("issue_comment", "/quiz retry")

    def test_other_user_cannot_change_gate(self):
        state = self.start_quiz()
        before = (len(self.statuses), len(self.comments))
        self.run_event("issue_comment", f"/quiz answer {state['id']}\n1: explanation", actor="stranger")
        self.assertEqual(before, (len(self.statuses), len(self.comments)))

    def test_maintainer_override_is_audited(self):
        self.start_quiz()
        self.run_event("issue_comment", "/quiz override manually reviewed decision", actor="maintainer")
        self.assertEqual(self.statuses[-1]["state"], "success")
        self.assertIn("manually reviewed decision", self.comments[-1]["body"])
        self.assertEqual(gate.decode_state(self.comments[-1]["body"])["phase"], "overridden")

    def test_fresh_head_resets_passed_quiz(self):
        self.start_quiz()
        self.run_event("issue_comment", "/quiz override reviewed", actor="maintainer")
        self.pr["head"]["sha"] = "new-head"
        self.run_event("pull_request_target", output={"summary": "New change", "questions": [question()]})
        self.assertEqual(self.statuses[-1]["state"], "pending")
        self.assertEqual(gate.decode_state(self.comments[-1]["body"])["phase"], "awaiting")

    def test_reopening_same_revision_preserves_attempt_and_result(self):
        state = self.start_quiz()
        self.run_event("issue_comment", f"/quiz answer {state['id']}\n1: unclear", output={
            "results": [{"id": 1, "verdict": "incorrect", "feedback": "Misunderstood", "needs_followup": False}]})
        count = len(self.comments)
        self.run_event("pull_request_target")
        self.assertEqual(len(self.comments), count)
        self.assertEqual(self.statuses[-1]["state"], "failure")
        self.assertEqual(gate.decode_state(self.comments[-1]["body"])["attempt"], 1)


class ProviderTests(unittest.TestCase):
    def test_copilot_dispatch(self):
        with patch.dict(os.environ, {"QUIZ_ENGINE": "copilot"}), \
                patch.object(gate, "copilot", new_callable=AsyncMock, return_value={"ok": True}) as provider:
            self.assertEqual(gate.model_output("generate.md", {}, gate.QUIZ_SCHEMA), {"ok": True})
            provider.assert_awaited_once()

    def test_unknown_engine_fails_before_inference(self):
        with patch.dict(os.environ, {"QUIZ_ENGINE": "unknown"}):
            with self.assertRaises(AssertionError):
                gate.model_output("generate.md", {}, gate.QUIZ_SCHEMA)

    def test_copilot_has_no_tools_and_does_not_receive_other_secrets(self):
        output = {"summary": "Whitespace only", "questions": []}
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.send_and_wait.return_value = SimpleNamespace(data=SimpleNamespace(content=json.dumps(output)))
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.create_session.return_value = session
        with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "test-token", "COPILOT_MODEL": "test-model",
                                     "ANTHROPIC_API_KEY": "private", "QUIZ_STATE_KEY": "private"}), \
                patch.object(gate, "CopilotClient", return_value=client) as constructor:
            result = asyncio.run(gate.copilot("generate.md", {}, gate.QUIZ_SCHEMA))
            self.assertEqual(result, output)
            settings = client.create_session.call_args.kwargs
            self.assertEqual(settings["available_tools"], [])
            self.assertFalse(settings["enable_config_discovery"])
            self.assertNotIn("QUIZ_STATE_KEY", constructor.call_args.kwargs["env"])
            self.assertNotIn("ANTHROPIC_API_KEY", constructor.call_args.kwargs["env"])
            self.assertEqual(session.send_and_wait.call_args.kwargs["response_schema"], gate.QUIZ_SCHEMA)

    def test_unexpected_tool_request_fails_closed(self):
        with self.assertRaises(PermissionError):
            gate.deny_tools({}, {})


class InstallerTests(unittest.TestCase):
    def test_preview_does_not_contact_github(self):
        arguments = ["install.py", "owner/repo", "--action", "owner/quiz-gate@v1", "--model", "model-id"]
        with patch("sys.argv", arguments):
            with patch.object(install, "gh") as api, patch("builtins.print"):
                install.main()
                api.assert_not_called()

    def test_action_reference_cannot_inject_workflow_content(self):
        with self.assertRaises(AssertionError):
            install.workflow("owner/repo@v1\nmalicious: true", "copilot", "advisory")

    def test_existing_workflow_is_not_overwritten(self):
        responses = [json.dumps({"default_branch": "main"}),
                     json.dumps({"truncated": False, "tree": [{"path": install.WORKFLOW}]})]
        arguments = ["install.py", "owner/repo", "--action", "owner/quiz-gate@v1", "--apply"]
        with patch("sys.argv", arguments), patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "fake"}), \
                patch.object(install, "gh", side_effect=responses) as api:
            with self.assertRaises(AssertionError):
                install.main()
            self.assertEqual(api.call_count, 2)

    def test_apply_needs_only_provider_token_and_opens_setup_pr(self):
        calls = []

        def api(arguments, content=""):
            calls.append((arguments, content))
            if arguments == ["api", "repos/owner/repo"]:
                return json.dumps({"default_branch": "main"})
            if "git/trees/main" in arguments[1]:
                return json.dumps({"truncated": False, "tree": []})
            if "git/ref/heads/main" in arguments[1]:
                return json.dumps({"object": {"sha": "sha"}})
            if arguments[:2] == ["secret", "list"]:
                return json.dumps([{"name": "QUIZ_STATE_KEY"}])
            if arguments[1] == "repos/owner/repo/pulls":
                return json.dumps({"html_url": "https://github.com/owner/repo/pull/1"})
            return "{}"

        arguments = ["install.py", "owner/repo", "--action", "owner/quiz-gate@v1", "--apply"]
        with patch("sys.argv", arguments), patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "fake-secret"}), \
                patch.object(install, "gh", side_effect=api), patch("builtins.print"):
            install.main()
        self.assertFalse(any(args[:3] == ["secret", "set", "QUIZ_STATE_KEY"] for args, body in calls))
        self.assertTrue(any(args[:3] == ["secret", "set", "COPILOT_GITHUB_TOKEN"] and body == "fake-secret" for args, body in calls))
        self.assertFalse(any("fake-secret" in args for args, body in calls))
        self.assertFalse(any(args[0] == "variable" for args, body in calls))
        self.assertTrue(any(args[1] == "repos/owner/repo/pulls" for args, body in calls))


if __name__ == "__main__":
    unittest.main()
