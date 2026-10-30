# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- task watcher: with `SUTANDO_STOP_INTERRUPT=1` (off by default; also the agent-activity manifest's config), a Stop interrupts the core's running turn (one Escape to its pane) when that pane is mid-turn on exactly the stopped task, so a long tool call no longer delays the stop until it finishes. A turn parked on a terminal prompt (a live dialog in the pane, or an open terminal prompt card for the session) counts as on that task however old its last row, so the Escape answers the prompt, and the session's prompt cards are expired; a task the engine fails as stalled takes the same path. Otherwise the Stop stays cooperative. Each decision is logged as an owner activity row, and the task shows `stop requested` until the stop is confirmed, then CANCELLED. With the switch off, a cancel marks the task CANCELLED at once, as before. Cancel instructions and the agent-activity hook now handle a gateway instance's `task-<instance>~task-<id>` as one id, so lane tasks get their activity rows. ([#5913])
- ag2-sparrow: with `REMOTE_TASK_TURN_CONTEXT=1`, a task that asks this agent a room question carries the question as quoted room data and tells the agent to answer it once with `room.question.answer`; the turn gate ignores outcomes about another task, so answering never parks or closes the agent's own task. ([#5937])

## Fixed

<!-- fix() PRs go here -->

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
