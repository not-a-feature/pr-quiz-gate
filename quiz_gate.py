import asyncio
import base64
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import tomllib
import uuid
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet
from copilot import CopilotClient
from jsonschema import validate


ROOT = Path(__file__).resolve().parent
MARKER = "<!-- quiz-gate:"
QUESTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "topic": {"type": "string"}, "kind": {"enum": ["comprehension", "decision"]},
        "type": {"enum": ["multiple_choice", "free_text"]},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "correct_option": {"type": "string"}, "rubric": {"type": "string"},
        "explanation": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["topic", "kind", "type", "question", "options", "correct_option",
                 "rubric", "explanation", "evidence"],
}
QUIZ_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"},
                   "questions": {"type": "array", "items": QUESTION_SCHEMA}},
    "required": ["summary", "questions"],
}
GRADE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "id": {"type": "integer"},
            "verdict": {"enum": ["correct", "partial", "incorrect", "uncertain"]},
            "feedback": {"type": "string"}, "needs_followup": {"type": "boolean"},
        }, "required": ["id", "verdict", "feedback", "needs_followup"],
    }}}, "required": ["results"],
}


def request_json(url, headers, payload=None, method="GET"):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def github(path, payload=None, method="GET"):
    return request_json("https://api.github.com" + path, {
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json", "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }, payload, method)


def pages(path):
    result = []
    for page in range(1, 101):
        batch = github(f"{path}?per_page=100&page={page}")
        result.extend(batch)
        if len(batch) < 100:
            return result
    raise ValueError("Pagination limit reached; refusing incomplete evidence.")


def config():
    value = tomllib.loads((ROOT / "quiz-gate.toml").read_text())
    for key, default in value.items():
        env_key = "QUIZ_" + key.upper()
        if env_key not in os.environ:
            continue
        raw = os.environ[env_key]
        if type(default) is bool:
            assert raw in {"true", "false"}, f"{env_key} must be true or false."
            value[key] = raw == "true"
        elif type(default) is int:
            value[key] = int(raw)
        elif type(default) is list:
            value[key] = [item.strip() for item in raw.split(",")]
        else:
            value[key] = raw
    assert value["mode"] in {"advisory", "required"}
    assert value["pass_rule"] in {"strict_majority", "percentage"}
    assert type(value["max_questions"]) is int and 1 <= value["max_questions"] <= 10
    assert type(value["max_attempts"]) is int and 1 <= value["max_attempts"] <= 10
    assert 0 < value["pass_percentage"] <= 100
    assert value["question_types"] and set(value["question_types"]) <= {"free_text", "multiple_choice"}
    assert value["max_files"] > 0 and value["max_context_chars"] > 0
    assert type(value["allow_override"]) is bool
    return value


def policy_digest(cfg):
    policy = {"config": cfg, "prompts": [(ROOT / "prompts" / name).read_text()
                                        for name in ("generate.md", "grade.md")]}
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def claude(prompt_name, data, schema):
    response = request_json("https://api.anthropic.com/v1/messages", {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }, {
        "model": os.environ["CLAUDE_MODEL"], "max_tokens": 8000,
        "system": (ROOT / "prompts" / prompt_name).read_text(),
        "messages": [{"role": "user", "content": json.dumps(data)}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }, "POST")
    assert response["stop_reason"] == "end_turn", "Incomplete model response."
    blocks = [block["text"] for block in response["content"] if block["type"] == "text"]
    assert len(blocks) == 1
    result = json.loads(blocks[0])
    validate(result, schema)
    return result


def deny_tools(request, invocation):
    raise PermissionError("The quiz model must not execute tools.")


async def copilot(prompt_name, data, schema):
    runtime_env = {key: value for key, value in os.environ.items()
                   if key in {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "SSL_CERT_FILE"}}
    with tempfile.TemporaryDirectory(prefix="quiz-copilot-") as directory:
        async with CopilotClient(
            mode="empty", github_token=os.environ["COPILOT_GITHUB_TOKEN"],
            use_logged_in_user=False, base_directory=directory,
            working_directory=directory, env=runtime_env,
        ) as client:
            async with await client.create_session(
                model=os.environ["COPILOT_MODEL"], available_tools=[], tools=[],
                on_permission_request=deny_tools, enable_skills=False,
                enable_config_discovery=False, enable_file_hooks=False,
                enable_host_git_operations=False, enable_session_store=False,
                system_message={"mode": "append", "content": (ROOT / "prompts" / prompt_name).read_text()},
            ) as session:
                response = await session.send_and_wait(json.dumps(data), response_schema=schema, timeout=180)
                assert response is not None, "Copilot returned no answer."
                result = json.loads(response.data.content)
                validate(result, schema)
                return result


def model_output(prompt_name, data, schema):
    engine = os.environ["QUIZ_ENGINE"] if "QUIZ_ENGINE" in os.environ else "claude"
    assert engine in {"claude", "copilot"}, "Engine must be claude or copilot."
    if engine == "copilot":
        return asyncio.run(copilot(prompt_name, data, schema))
    return claude(prompt_name, data, schema)


def required_correct(total, cfg):
    assert total > 0
    if cfg["pass_rule"] == "strict_majority":
        return total // 2 + 1
    return math.ceil(total * cfg["pass_percentage"] / 100)


def passed(results, cfg):
    assert results
    return (sum(item["verdict"] == "correct" for item in results) >= required_correct(len(results), cfg)
            and not any(item["needs_followup"] for item in results))


def validate_quiz(quiz, cfg):
    validate(quiz, QUIZ_SCHEMA)
    assert len(quiz["questions"]) <= cfg["max_questions"]
    assert quiz["summary"].strip()
    for number, question in enumerate(quiz["questions"], 1):
        assert question["type"] in cfg["question_types"]
        assert all(question[field].strip() for field in ("topic", "question", "rubric", "explanation"))
        assert question["evidence"] and all(item.strip() for item in question["evidence"])
        if question["type"] == "multiple_choice":
            assert len(question["options"]) == 4 and question["correct_option"] in "ABCD"
            assert len(question["correct_option"]) == 1
            assert len(set(question["options"])) == 4 and all(item.strip() for item in question["options"])
        else:
            assert question["options"] == [] and question["correct_option"] == ""
        if question["kind"] == "decision":
            assert question["type"] == "free_text", "Ambiguous decisions need a human explanation."
        question["id"] = number


def parse_answers(body, state):
    lines = body.strip().splitlines()
    assert lines[0] == f"/quiz answer {state['id']}", "Use the current quiz ID."
    answers = {}
    current = 0
    for line in lines[1:]:
        match = re.match(r"^(\d+)[.:]\s*(.*)$", line)
        if match:
            current = int(match[1])
            assert current not in answers, "Duplicate question answer."
            answers[current] = match[2]
        elif line.strip():
            assert current in answers, "Number each answer."
            answers[current] += "\n" + line
    assert set(answers) == {question["id"] for question in state["questions"]}, "Answer every question."
    assert all(answer.strip() for answer in answers.values()), "Empty answer."
    return answers


def grade(state, answers):
    results = []
    free_text = []
    for question in state["questions"]:
        answer = answers[question["id"]]
        if question["type"] == "multiple_choice":
            assert answer.strip().upper() in {"A", "B", "C", "D"}, "Reply with one option letter."
            correct = answer.strip().upper() == question["correct_option"]
            results.append({"id": question["id"], "verdict": "correct" if correct else "incorrect",
                            "feedback": question["explanation"], "needs_followup": False})
        else:
            free_text.append({"question": question, "answer": answer})
    if free_text:
        output = model_output("grade.md", {"answers": free_text}, GRADE_SCHEMA)
        assert len(output["results"]) == len(free_text)
        assert {item["id"] for item in output["results"]} == {item["question"]["id"] for item in free_text}
        results.extend(output["results"])
    return sorted(results, key=lambda item: item["id"])


def encode_state(state):
    return Fernet(os.environ["QUIZ_STATE_KEY"].encode()).encrypt(json.dumps(state).encode()).decode()


def decode_state(body):
    tokens = re.findall(r"<!-- quiz-gate:([A-Za-z0-9_=-]+) -->", body)
    assert len(tokens) == 1, "Invalid state marker."
    return json.loads(Fernet(os.environ["QUIZ_STATE_KEY"].encode()).decrypt(tokens[0].encode()))


def load_state(prefix):
    comments = pages(prefix + "/comments")
    candidates = [comment for comment in comments if comment["user"]["login"] == "github-actions[bot]"
                  and comment["user"]["type"] == "Bot" and MARKER in comment["body"]]
    if not candidates:
        return {}
    return decode_state(max(candidates, key=lambda item: item["id"])["body"])


def assert_binding(state, repo, number, pr, cfg):
    assert state["repo"] == repo and state["pr"] == number
    assert state["sha"] == pr["head"]["sha"] and state["base_sha"] == pr["base"]["sha"], "Quiz is stale."
    assert state["policy"] == policy_digest(cfg), "Quiz policy changed; request a new quiz."
    assert state["respondent"] == pr["user"]["login"]


def status(repo, sha, cfg, outcome, description):
    state = outcome if cfg["mode"] == "required" else "success"
    github(f"/repos/{repo}/statuses/{sha}", {
        "state": state, "context": "quiz-gate", "description": description[:140],
        "target_url": f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
    }, "POST")


def ensure_current(repo, number, pr):
    latest = github(f"/repos/{repo}/pulls/{number}")
    assert latest["state"] == "open"
    assert latest["head"]["sha"] == pr["head"]["sha"] and latest["base"]["sha"] == pr["base"]["sha"], "PR changed during run."


def post_state(repo, number, pr, state, text):
    ensure_current(repo, number, pr)
    body = text + f"\n\n<!-- quiz-gate:{encode_state(state)} -->"
    assert len(body) <= 60000, "Quiz comment exceeds size limit."
    github(f"/repos/{repo}/issues/{number}/comments", {"body": body}, "POST")


def context(repo, number, pr, cfg):
    files = pages(f"/repos/{repo}/pulls/{number}/files")
    assert len(files) <= cfg["max_files"], "Change set exceeds configured file limit; narrow or override."
    evidence = []
    for item in files:
        entry = {"path": item["filename"], "status": item["status"]}
        if "patch" in item:
            entry["patch"] = item["patch"]
        if item["status"] == "removed":
            source_repo, source_sha = repo, pr["base"]["sha"]
        else:
            source_repo, source_sha = pr["head"]["repo"]["full_name"], pr["head"]["sha"]
        content = github(f"/repos/{source_repo}/contents/{quote(item['filename'], safe='/')}?ref={source_sha}")
        assert content["size"] <= cfg["max_context_chars"], "File exceeds evidence budget."
        assert content["type"] == "file" and content["encoding"] == "base64", "Unsupported file; refusing partial context."
        entry["source"] = base64.b64decode(content["content"]).decode("utf-8")
        evidence.append(entry)
    comments = pages(f"/repos/{repo}/issues/{number}/comments")
    discussions = [{"author": item["user"]["login"], "body": item["body"]}
                   for item in comments if item["user"]["type"] == "User"]
    data = {"title": pr["title"], "description": pr["body"], "files": evidence,
            "discussion": discussions, "max_questions": cfg["max_questions"],
            "question_types": cfg["question_types"]}
    assert len(json.dumps(data)) <= cfg["max_context_chars"], "Context limit exceeded; refusing silent truncation."
    return data


def create_quiz(repo, number, pr, cfg, attempt, previous):
    status(repo, pr["head"]["sha"], cfg, "pending", "Awaiting comprehension quiz")
    data = context(repo, number, pr, cfg)
    data["previous_questions"] = previous
    data["retry_instruction"] = "Use fresh questions, not previously revealed answers."
    quiz = model_output("generate.md", data, QUIZ_SCHEMA)
    validate_quiz(quiz, cfg)
    state = {"repo": repo, "pr": number, "sha": pr["head"]["sha"], "base_sha": pr["base"]["sha"],
             "respondent": pr["user"]["login"], "policy": policy_digest(cfg),
             "id": uuid.uuid4().hex[:12], "attempt": attempt, "phase": "awaiting",
             "questions": quiz["questions"]}
    text = f"### Quiz · {cfg['mode']} · `{state['id']}`\n\n{quiz['summary']}\n"
    if not state["questions"]:
        state["phase"] = "passed"
        post_state(repo, number, pr, state, text + "\nNo material quiz topics found.")
        status(repo, pr["head"]["sha"], cfg, "success", "No material quiz topics")
        return
    text += f"\n@{state['respondent']}: answer {required_correct(len(state['questions']), cfg)} of {len(state['questions'])} correctly to pass. Unresolved decisions also need follow-up.\n"
    for question in state["questions"]:
        text += f"\n**{question['id']}. {question['question']}**\n"
        for letter, option in zip("ABCD", question["options"]):
            text += f"\n{letter}. {option}\n"
    text += f"\nReply in one comment:\n```text\n/quiz answer {state['id']}\n"
    text += "\n".join(f"{question['id']}: your answer" for question in state["questions"])
    text += "\n```\nFor multiple choice use only A, B, C, or D."
    post_state(repo, number, pr, state, text)


def main():
    cfg = config()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repo = os.environ["GITHUB_REPOSITORY"]
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name == "issue_comment":
        if "pull_request" not in event["issue"]:
            return
        number = event["issue"]["number"]
    else:
        number = event["pull_request"]["number"]
    pr = github(f"/repos/{repo}/pulls/{number}")
    if pr["state"] != "open":
        return
    if len(sys.argv) == 2 and sys.argv[1] == "error":
        status(repo, pr["head"]["sha"], cfg, "error", "Quiz could not complete; see workflow logs")
        return
    prefix = f"/repos/{repo}/issues/{number}"
    state = load_state(prefix)
    if event_name != "issue_comment":
        if (state and state["sha"] == pr["head"]["sha"]
                and state["base_sha"] == pr["base"]["sha"] and state["policy"] == policy_digest(cfg)):
            outcome = {"awaiting": "pending", "passed": "success", "overridden": "success",
                       "failed": "failure", "needs_followup": "failure"}[state["phase"]]
            status(repo, pr["head"]["sha"], cfg, outcome, "Existing quiz: " + state["phase"])
            return
        create_quiz(repo, number, pr, cfg, 1, [])
        return
    comment = github(f"/repos/{repo}/issues/comments/{event['comment']['id']}")
    if comment["user"]["type"] != "User":
        return
    body = comment["body"].strip()
    actor = comment["user"]["login"]
    if body.startswith("/quiz override "):
        assert cfg["allow_override"], "Overrides are disabled."
        permission = github(f"/repos/{repo}/collaborators/{quote(actor, safe='')}/permission")["permission"]
        if permission not in {"admin", "maintain"}:
            return
        assert body.removeprefix("/quiz override ").strip(), "Provide an override reason."
        state = {"repo": repo, "pr": number, "sha": pr["head"]["sha"], "base_sha": pr["base"]["sha"],
                 "respondent": pr["user"]["login"], "policy": policy_digest(cfg),
                 "id": uuid.uuid4().hex[:12], "attempt": cfg["max_attempts"], "phase": "overridden", "questions": []}
        post_state(repo, number, pr, state, f"Quiz overridden by @{actor}:\n\n{body.removeprefix('/quiz override ')}")
        status(repo, pr["head"]["sha"], cfg, "success", "Maintainer override recorded")
        return
    if actor != pr["user"]["login"]:
        return
    if body == "/quiz":
        if state and state["sha"] == pr["head"]["sha"] and state["base_sha"] == pr["base"]["sha"] and state["policy"] == policy_digest(cfg):
            assert state["phase"] == "awaiting", "Use /quiz retry after a failed attempt."
            return
        create_quiz(repo, number, pr, cfg, 1, [])
        return
    assert state, "Request /quiz first."
    assert_binding(state, repo, number, pr, cfg)
    if body == "/quiz retry":
        assert state["phase"] == "failed", "Retry only after a failed quiz."
        assert state["attempt"] < cfg["max_attempts"], "Attempt limit reached."
        create_quiz(repo, number, pr, cfg, state["attempt"] + 1, state["questions"])
        return
    assert state["phase"] == "awaiting", "This quiz has already been graded."
    answers = parse_answers(body, state)
    results = grade(state, answers)
    success = passed(results, cfg)
    state["phase"] = "passed" if success else "failed"
    if any(item["needs_followup"] for item in results):
        state["phase"] = "needs_followup"
    text = f"### Quiz {'passed' if success else 'needs another attempt'} · `{state['id']}`\n"
    for item in results:
        question = state["questions"][item["id"] - 1]
        text += f"\n**{item['id']}: {item['verdict']}** — {item['feedback']}\n"
        text += "\nEvidence: " + "; ".join(question["evidence"]) + "\n"
        if item["needs_followup"]:
            text += "\nThis decision needs clarification or a code change before merging.\n"
    if state["phase"] == "needs_followup":
        text += "\nResolve the decision in a new commit or request a recorded maintainer override."
    elif not success:
        text += "\nReply `/quiz retry` for fresh questions, subject to the attempt limit."
    post_state(repo, number, pr, state, text)
    status(repo, pr["head"]["sha"], cfg, "success" if success else "failure",
           "Quiz passed" if success else "Comprehension or decision follow-up needed")


if __name__ == "__main__":
    main()
