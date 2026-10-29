# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- ag2-sparrow: opt-in AgentActivity task lifecycle. `REMOTE_TASK_LIFECYCLE_V2=1` sends the lease `worker_id` and `execution_generation` on ack and results, beats each task on `/v1/tasks/heartbeat`, stops the core when the broker cancels or reclaims an attempt (withdrawn when a newer generation is acked here first; a newer attempt it stopped anyway is posted as failed); a task the broker closes, or cancels before the ack, leaves the inbox if the core has not taken it up, and the core is stopped if it has, and reconciles with `/v1/tasks/active`. An attempt nobody will answer stops beating and is posted as failed: its task file left the queue unanswered for 10 min (never dropped from flight as completed elsewhere), or the core took the queued file up and then wrote no turn activity or result for 30 min. `REMOTE_TASK_TURN_CONTEXT=1` adds per-attempt turn files and gates each result on the turn's room actions. Both default off. ([#5883])
- ag2-sparrow: with `REMOTE_TASK_TURN_CONTEXT=1`, a task the owner answered resumes as a new turn carrying the answer as quoted room data, once per question episode, and each task file tells the agent to ask a blocking question with `room.question.ask` on its task id, sending any visible text with `room.message.send` first (its result file is not posted while the question is open). A wake's task file says it is a new turn of the task and must end with its own result. The Stop hook no longer counts a result older than the queued task file as its answer (`task_dispatch.py has-result … --task-file`), and a turn that recorded a turn-ledger no-send loses the in-progress exemption, so a queued task it left unanswered blocks. A woken attempt is judged answered only by its own result, not the first attempt's archived reply, so a wake stopped or closed before the core takes it is withdrawn and a lost wake is failed. ([#5885])

## Fixed

<!-- fix() PRs go here -->

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
