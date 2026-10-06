import argparse
import base64
import json
import os
import re
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WORKFLOW = ".github/workflows/quiz-gate.yml"


def gh(arguments, content=""):
    return subprocess.run(["gh", *arguments], input=content, text=True,
                          capture_output=True, check=True).stdout


def workflow(action, engine, mode):
    assert re.fullmatch(r"[\w.-]+/[\w.-]+@[\w./-]+", action), "Specify owner/repo@release-or-SHA."
    assert engine in {"claude", "copilot"} and mode in {"advisory", "required"}
    filename = "quiz-gate-copilot.yml" if engine == "copilot" else "quiz-gate.yml"
    text = (ROOT / "examples" / filename).read_text()
    return text.replace("YOUR_ORG/quiz-gate@v1", action).replace("mode: advisory", f"mode: {mode}")


def main():
    parser = argparse.ArgumentParser(description="Prepare a quiz-gate setup PR and repository secrets.")
    parser.add_argument("repository", help="owner/repository")
    parser.add_argument("--action", required=True, help="Published action owner/repo@version-or-SHA")
    parser.add_argument("--model", help="Model ID available to the selected provider")
    parser.add_argument("--engine", choices=["claude", "copilot"], default="copilot")
    parser.add_argument("--mode", choices=["advisory", "required"], default="advisory")
    parser.add_argument("--apply", action="store_true", help="Create a setup PR and store credentials; otherwise preview only")
    args = parser.parse_args()
    assert re.fullmatch(r"[\w.-]+/[\w.-]+", args.repository), "Specify owner/repository."
    text = workflow(args.action, args.engine, args.mode)
    credential = "COPILOT_GITHUB_TOKEN" if args.engine == "copilot" else "ANTHROPIC_API_KEY"
    if args.model:
        assert re.fullmatch(r"[\w.-]+", args.model), "Invalid model ID."
        text = text.replace(f"mode: {args.mode}", f"mode: {args.mode}\n          model: {args.model}")
    if not args.apply:
        print(text)
        print(f"Apply will store {credential}, open a setup PR in {args.repository}.")
        print("It will not change branch protection or merge the PR.")
        return
    assert os.environ[credential].strip(), f"Set {credential} in the environment before applying."
    metadata = json.loads(gh(["api", f"repos/{args.repository}"]))
    default_branch = metadata["default_branch"]
    tree = json.loads(gh(["api", f"repos/{args.repository}/git/trees/{default_branch}?recursive=1"]))
    assert not tree["truncated"], "Repository tree is incomplete."
    assert WORKFLOW not in {item["path"] for item in tree["tree"]}, "A quiz workflow already exists; update it explicitly."
    sha = json.loads(gh(["api", f"repos/{args.repository}/git/ref/heads/{default_branch}"]))["object"]["sha"]
    gh(["secret", "set", credential, "--repo", args.repository], os.environ[credential])
    branch = "codex/quiz-gate-setup-" + uuid.uuid4().hex[:8]
    gh(["api", f"repos/{args.repository}/git/refs", "--method", "POST", "--input", "-"],
       json.dumps({"ref": "refs/heads/" + branch, "sha": sha}))
    gh(["api", f"repos/{args.repository}/contents/{WORKFLOW}", "--method", "PUT", "--input", "-"],
       json.dumps({"message": "Add PR comprehension quiz gate", "branch": branch,
                   "content": base64.b64encode(text.encode()).decode()}))
    result = json.loads(gh(["api", f"repos/{args.repository}/pulls", "--method", "POST", "--input", "-"], json.dumps({
        "title": "Add PR comprehension quiz gate", "head": branch, "base": default_branch,
        "body": f"Adds the quiz-gate Action using {args.engine} in {args.mode} mode. The provider token is the only required secret; the state encryption key is derived automatically. Review the workflow before merging. To enforce merging, require the quiz-gate status through branch protection after testing.",
    })))
    print(result["html_url"])


if __name__ == "__main__":
    main()
