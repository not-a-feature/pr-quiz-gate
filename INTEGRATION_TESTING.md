# Testing without a Marketplace release

Push a development branch and pin its full commit SHA in a test repository's
default-branch workflow. Marketplace publication and release tags are not needed.
Use the complete example workflow, `engine: copilot`, the
`COPILOT_GITHUB_TOKEN` secret, and `mode: required`. The model and encryption key
have automatic defaults. Keep research PRs separate from synthetic test fixtures.

Configure an active branch ruleset on the test target branch requiring the
`quiz-gate` status, with GitHub Actions as its source and an empty bypass list.
Enable strict up-to-date checks. The Actions job is not the required status:
successful question generation must leave `quiz-gate` pending.

Test a small synthetic function with an explicit documented policy:

| Case | Expected status | Expected merge state |
| --- | --- | --- |
| No answers | pending | blocked |
| Answers below threshold | failure | blocked |
| Majority correct, no unresolved decisions | success | allowed |
| New head commit after passing | pending for new quiz | blocked |
| Context budget exceeded | error | blocked |
| Provider or API failure | error | blocked |
| Answer submitted by another account | unchanged | unchanged |
| Unresolved decision despite correct understanding | failure | blocked |

Inspect the PR's latest commit status and mergeability through the GitHub API;
do not actually merge the fixture merely to test enforcement. Confirm both
multiple-choice and free-text grading. Test token rotation on disposable quizzes:
derived state keys change with the provider token. Use an optional stable
`state-key` if quiz state must survive credential rotation.

For a large PR, set `max-context-chars` above its reported evidence size and within
the chosen model's input limit. Evidence is retained in full rather than truncated.
Only promote the tested commit after the hosted test passes. Existing release tags
remain unchanged throughout testing.
