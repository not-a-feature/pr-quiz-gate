# Quiz gate

A GitHub PR bot that checks whether the PR author understands important changes.
Claude or GitHub Copilot generates evidence-grounded questions and grades free text; Python checks
multiple choice, applies the pass threshold, and publishes the `quiz-gate` status.
Default: advisory mode, at most three questions, strict majority to pass.

Ambiguous behavior and consequential decisions without documented human approval
are priority topics. The bot asks what the implementation chooses and what the
human intends. A reasoned disagreement is not a wrong answer. A proposed change
or unresolved requirement blocks required mode until resolved or explicitly overridden.
The model cannot execute tools, edit code, or merge a PR.

## Copilot and quick setup

Select `engine: copilot` to use the official GitHub Copilot SDK. Its runtime is
downloaded at the version pinned by the locked SDK. Sessions have no tools, skills,
config discovery, file hooks, or host Git operations. The runtime receives neither
the state encryption key nor the Anthropic key. JSON responses are locally schema-validated.

Use `examples/quiz-gate-copilot.yml` for Copilot instead of the Claude workflow.
Configure `COPILOT_GITHUB_TOKEN` as a repository secret and `COPILOT_MODEL` as a
repository variable. For personal authentication, use a fine-grained token owned
by a user with Copilot access, with the account-level Copilot Requests permission
set to Read. The action's repository `GITHUB_TOKEN` is separate from inference auth.
No Anthropic account or key is needed for Copilot.

Once the Action is published, the installer can prepare the workflow, secrets, and
model variable together. Authenticate `gh` with permission to create PRs, write
workflow files, and manage repository Actions secrets/variables. Supply the selected
provider credential through the environment, never as a command-line argument.

```sh
uv run python install.py OWNER/REPO --action ACTION_OWNER/quiz-gate@v1 --model MODEL_ID
```

This previews the workflow without contacting GitHub. Add `--apply` to configure
the repository and open a setup PR; merge that PR after reviewing it. The installer
defaults to Copilot and advisory mode. Use `--engine claude` for Claude. It preserves
an existing state key and refuses to overwrite an existing quiz workflow. It does
not merge the setup PR or change branch protection. If an API operation fails,
earlier successful operations may remain; errors are not silently retried.

This is one-command setup, not a one-click hosted App installation. Marketplace
Actions do not automatically install workflows or grant Copilot access. A true
"Install → choose repositories" flow needs a hosted GitHub App, webhook processing,
and an eligible Copilot authentication flow. No hosted App has been created here.
GitHub Agentic Workflows offers organization-billed token authentication where
supported; that separate integration is not implemented by this SDK action.
See [Copilot SDK authentication](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/authenticate)
and [Agentic Workflows authentication](https://github.github.com/gh-aw/reference/auth/).

## Local development

```sh
uv sync --locked
uv run python -m unittest discover -s tests -v
```

No external API calls occur in tests.

## Install the reusable action

The project includes a root `action.yml` for distribution as a GitHub Action.
Consumers need only one workflow file; no Python files, dependency manifests,
configuration files, or repository checkout are needed in the target repository.
The action installs its own locked dependencies and reads PR evidence via the API.

This action has not been published yet. After publishing, replace `YOUR_ORG` in
`examples/quiz-gate.yml` with the actual owner and copy that file to the target
repository's default branch as `.github/workflows/quiz-gate.yml`. Use the released
version or commit SHA. The key step is:

```yaml
- uses: YOUR_ORG/quiz-gate@v1
  with:
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
    state-key: ${{ secrets.QUIZ_STATE_KEY }}
    model: ${{ vars.CLAUDE_MODEL }}
    mode: advisory
```

Use the complete example workflow: the triggers, permissions, event filter, and
per-PR concurrency are required for generation, answers, and reliable state updates.

Configure these GitHub Actions secrets/variables:

| Name | Kind | Purpose |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Secret | Claude API access and billing, only for Claude |
| `COPILOT_GITHUB_TOKEN` | Secret | Copilot inference access, only for Copilot |
| `QUIZ_STATE_KEY` | Secret | Encrypt and authenticate persisted answer keys |
| `CLAUDE_MODEL` | Variable | Model ID supporting Anthropic structured JSON output |
| `COPILOT_MODEL` | Variable | Model ID available to the Copilot account |

Generate the state key locally and store it as a secret; do not commit it:

```sh
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The workflow runs on non-draft PR openings/updates and `/quiz` comments. The reusable
action needs no checkout; PR files are fetched as data through GitHub's API.
It does not run contributed code or use the Claude Code GitHub App.
Source code and human PR discussion are sent to the selected inference provider.
Answer keys are encrypted in bot comments, not stored in plaintext public artifacts.
Rotating the state key invalidates existing quizzes.

After testing advisory mode, set the action input `mode: required` in the workflow.
Require the `quiz-gate` commit status in the target branch's ruleset and restrict
its expected source to GitHub Actions where supported. Avoid bypass permissions
if all ordinary merges must pass. The workflow itself passing is not the gate:
require the `quiz-gate` status, not its Actions job. Retain existing tests/reviews.
If your plan does not support rulesets/required statuses for the repository's
visibility, advisory comments alone cannot enforce a merge block.

## Interaction

The bot posts all questions in one comment. The author replies:

```text
/quiz answer <current-quiz-id>
1: B
2: Explanation of the behavior and its consequences.
3: The code chose X; I intend Y because ...
```

`/quiz` starts a missing or stale quiz. `/quiz retry` creates fresh questions after
a failed attempt, up to `max_attempts`. The bot explains mistakes before retries.
Unresolved decisions cannot be bypassed by retrying different topics: they require
a new commit addressing the issue or a recorded maintainer override.
`/quiz override <reason>` records an override by a user with maintain/admin
permission, if `allow_override` is enabled. Other humans cannot answer for the author.
Bot-authored PRs therefore need an override in this initial version.

## Configuration

Action inputs control mode, question count/types, attempts, override availability,
and evidence size limits. Defaults live inside the action's `quiz-gate.toml`; local
runs may edit that file. `strict_majority` requires more
than half correct; `percentage` rounds the configured percentage up to a whole
question. Partial/uncertain grades do not count. All questions must be answered.
Decision topics require free text; retain `free_text` when configuring question types.

```yaml
with:
  # Include the credentials/model shown above.
  mode: required
  max-questions: '5'
  pass-rule: percentage
  pass-percentage: '80'
  question-types: multiple_choice,free_text
  max-attempts: '2'
  allow-override: 'false'
```

All inputs and defaults are documented in `action.yml`. GitHub's built-in token
is used unless `github-token` is supplied. The action does not change repository
settings; required-status enforcement still needs a branch ruleset.

## Publish to GitHub Marketplace

Publish this project as a dedicated public repository with the root `action.yml`.
Choose an available Marketplace action name, create a versioned release, and select
"Publish this Action to the GitHub Marketplace" when releasing. The account must
accept the Marketplace Developer Agreement and use two-factor authentication.
No publication or Git operation has been performed by this local implementation.
See [GitHub's publishing requirements](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace).

Marketplace provides discovery and the `uses:` installation snippet; it does not
install workflow triggers, secrets, or branch protection automatically. A GitHub
App would be a separate route for a one-click repository installation.

## Limits and verification

This is an initial implementation, not a deployed GitHub integration. Unit tests
cover scoring, answer parsing, authenticated state, stale revisions, bot-origin
checks, action input configuration, provider isolation, and incomplete model grading. Live API calls,
the hosted composite action, and GitHub ruleset behavior
require an integration trial in a target repository with credentials.

The bot supplies full changed-file text, patches where available, and human issue
comments. It does not yet fetch unchanged dependencies, review threads, or local
agent sessions. Missing decision provenance stays unknown; Git author metadata
does not establish who made a decision. Binary/oversized files or excessive context
fail rather than silently omit evidence. For a context-limit error, set the action
input `max-context-chars` above the reported size, within the selected model's
input limit (for example, `max-context-chars: '500000'`). The budget includes PR
discussion and previous questions on retries. Requests use compact JSON without
escaping Unicode. If the evidence still does not fit, narrow the PR or use a
recorded override.

New head commits reset the quiz. A changed base or policy invalidates submitted
answers; request `/quiz` to refresh. Base-branch changes without a PR event do not
automatically reset a previously successful commit status; use strict up-to-date
branch protection and refresh the PR before merging. Merge queues are not supported
yet; do not use this gate as a required merge-queue check.

LLM grading and question quality remain fallible, and the bot cannot prove the
respondent answered without AI help. Retry novelty is prompted rather than formally
guaranteed. Overrides and bot comments provide an audit trail; maintainers can
delete comments, and repository administrators ultimately control enforcement.
For organization-wide tamper resistance, deploy the controller as a dedicated
GitHub App or centrally protected workflow rather than trusting per-repository admins.

References: [required checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets),
[commit statuses](https://docs.github.com/en/rest/commits/statuses),
[Claude structured output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs).
