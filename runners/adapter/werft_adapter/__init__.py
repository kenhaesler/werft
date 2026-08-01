"""The in-container adapter runtime (SPEC §4.3, [BP§P3.3]).

PID 1 inside the runner. Reads `task.json`, launches the provider CLI in its own
process group, tees a redacted transcript to `/outputs/log.jsonl`, and writes
`/outputs/result.json` atomically as its last act.

**These limits are hygiene, not containment.** The wall-clock ceiling, the
tree-kill and the redaction all live inside the agent's own box, where a root
agent can patch or kill them. The real ceiling, kill and teardown are enforced
manager-side over the Docker API (SPEC §4.3). If anyone later leans on this
module as a control against hostility, the design is wrong.
"""

EXIT_CONTRACT_FULFILLED = 0
EXIT_CLI_UNSTARTABLE = 2
EXIT_WORKSPACE_GIT_FAILURE = 4
EXIT_RESULT_SERIALIZATION_FAILURE = 5
