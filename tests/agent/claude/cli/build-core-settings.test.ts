import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// Builders are plain .mjs (run by start-cli.sh via `node <builder> <args>`), so
// we exercise them exactly as the shell does: exec and parse stdout.
const CORE_BUILDER = fileURLToPath(
	new URL('../../../../src/agent/claude/cli/build-core-settings.mjs', import.meta.url),
);
const OBS_BUILDER = fileURLToPath(
	new URL('../../../../src/observability/claude/hooks/build-hook-settings.mjs', import.meta.url),
);

function buildCore(
	guardPath: string,
	obsJson?: string,
	skillTelemetryHook?: string,
	gmailWriteGuardHook?: string,
	devappExecuteGuardHook?: string,
): any {
	const args =
		devappExecuteGuardHook !== undefined
			? [CORE_BUILDER, guardPath, obsJson ?? '', skillTelemetryHook ?? '',
				gmailWriteGuardHook ?? '', devappExecuteGuardHook]
		: gmailWriteGuardHook !== undefined
			? [CORE_BUILDER, guardPath, obsJson ?? '', skillTelemetryHook ?? '', gmailWriteGuardHook]
			: skillTelemetryHook === undefined
				? obsJson === undefined
					? [CORE_BUILDER, guardPath]
					: [CORE_BUILDER, guardPath, obsJson]
				: [CORE_BUILDER, guardPath, obsJson ?? '', skillTelemetryHook];
	return JSON.parse(execFileSync('node', args, { encoding: 'utf8' }));
}

function buildObs(hookPath: string): string {
	return execFileSync('node', [OBS_BUILDER, hookPath], { encoding: 'utf8' });
}

/** Strip a `python3 `/`bash ` prefix and let a real shell re-parse the quoted
 *  remainder — i.e. the path the shell would actually pass to the interpreter. */
function shellParsedPath(command: string): string {
	const arg = command.replace(/^(python3|bash) /, '');
	return execFileSync('/bin/bash', ['-c', `printf %s ${arg}`], { encoding: 'utf8' });
}

const GUARD = '/x/hooks/skip-ask-user-question.py';
const SKILL_TELEMETRY = '/x/hooks/skill-usage-telemetry.py';
const DEVAPP_GUARD = '/x/hooks/devapp-execute-guard.py';
const GMAIL_WRITE_GUARD = '/x/hooks/gmail-write-guard.py';

describe('build-core-settings.mjs', () => {
	it('always registers the AskUserQuestion guard (guard-only, obs off)', () => {
		const o = buildCore(GUARD, ''); // empty obs blob == capture off
		assert.deepEqual(Object.keys(o.hooks), ['PreToolUse']);
		const pre = o.hooks.PreToolUse;
		assert.equal(pre.length, 1);
		assert.equal(pre[0].matcher, 'AskUserQuestion');
		assert.match(pre[0].hooks[0].command as string, /^python3 '/);
		assert.equal(shellParsedPath(pre[0].hooks[0].command), GUARD);
	});

	it('omitting the obs arg entirely is equivalent to obs off', () => {
		const o = buildCore(GUARD);
		assert.deepEqual(Object.keys(o.hooks), ['PreToolUse']);
		assert.equal(o.hooks.PreToolUse.length, 1);
		assert.equal(o.hooks.PreToolUse[0].matcher, 'AskUserQuestion');
	});

	it('merges the guard with obs hooks (concat, guard first) when obs is on', () => {
		const obs = JSON.parse(buildObs('/x/obs-hook.sh'));
		const o = buildCore(GUARD, buildObs('/x/obs-hook.sh'));
		// Guard survives, obs survives: PreToolUse holds BOTH matchers, guard first.
		const pre = o.hooks.PreToolUse;
		assert.equal(pre.length, 2, 'guard entry must not replace the obs entry (or vice-versa)');
		assert.equal(pre[0].matcher, 'AskUserQuestion');
		assert.equal(pre[1].matcher, '*');
		// Every obs event key is preserved.
		for (const ev of Object.keys(obs.hooks)) {
			assert.ok(o.hooks[ev], `merged settings dropped obs event ${ev}`);
		}
		// obs-only lifecycle events pass through untouched.
		assert.deepEqual(o.hooks.SessionStart, obs.hooks.SessionStart);
	});

	it('registers skill telemetry when obs is off', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY);
		const post = o.hooks.PostToolUse;
		assert.equal(post.length, 1);
		assert.equal(post[0].matcher, 'Skill');
		assert.equal(shellParsedPath(post[0].hooks[0].command), SKILL_TELEMETRY);
	});

	it('registers skill telemetry exactly once when obs is on', () => {
		const o = buildCore(GUARD, buildObs('/x/obs-hook.sh'), SKILL_TELEMETRY);
		const skillEntries = o.hooks.PostToolUse.filter(
			(entry: { matcher?: string }) => entry.matcher === 'Skill',
		);
		assert.equal(skillEntries.length, 1, 'obs settings must not double-register skill telemetry');
		assert.equal(shellParsedPath(skillEntries[0].hooks[0].command), SKILL_TELEMETRY);
		assert.ok(
			o.hooks.PostToolUse.some((entry: { matcher?: string }) => entry.matcher === '*'),
			'obs PostToolUse collector must survive the merge',
		);
	});

	const ADVERSARIAL = [
		'/Users/o brien/hooks/skip-ask-user-question.py', // spaces
		'/Users/o"brien/hooks/skip-ask-user-question.py', // double quote
		"/Users/o'brien/hooks/skip-ask-user-question.py", // single quote (POSIX '\'' escape)
		'/path/with$dollar/and`tick`/skip-ask-user-question.py', // $ + backtick must not expand
	];
	for (const p of ADVERSARIAL) {
		it(`round-trips a guard path through valid JSON + shell quoting: ${p}`, () => {
			const o = buildCore(p, '');
			const cmd = o.hooks.PreToolUse[0].hooks[0].command as string;
			assert.match(cmd, /^python3 '/); // POSIX single-quoted
			assert.equal(shellParsedPath(cmd), p); // shell sees the EXACT original path
		});
	}

	it('exits non-zero when no guard path is given', () => {
		assert.throws(() => execFileSync('node', [CORE_BUILDER], { encoding: 'utf8', stdio: 'pipe' }));
	});

	it('exits non-zero on an unparseable obs-settings blob', () => {
		assert.throws(() =>
			execFileSync('node', [CORE_BUILDER, GUARD, '{not json'], { encoding: 'utf8', stdio: 'pipe' }),
		);
	});

	// Regression: the guard had no production registrar, so it stayed inert on any
	// install that never ran the README's manual cp by hand.
	it('registers the Gmail write guard when its path is supplied', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD);
		const blk = o.hooks.PreToolUse.find((b: any) =>
			b.hooks.some((h: any) => h.command.includes('gmail-write-guard')),
		);
		assert.ok(blk, 'no PreToolUse block registers gmail-write-guard');
		assert.equal(blk.matcher, 'mcp__.*[Gg][Mm][Aa][Ii][Ll].*');
		assert.equal(shellParsedPath(blk.hooks[0].command), GMAIL_WRITE_GUARD);
	});

	it('the Gmail matcher selects connector Gmail tools and nothing else', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD);
		const blk = o.hooks.PreToolUse.find((b: any) =>
			b.hooks.some((h: any) => h.command.includes('gmail-write-guard')),
		);
		const re = new RegExp(blk.matcher);
		assert.ok(re.test('mcp__claude_ai_Gmail__create_draft'));
		assert.ok(re.test('mcp__gmail__send_email'));
		assert.ok(!re.test('mcp__claude_ai_Slack__slack_send_message'));
		assert.ok(!re.test('Bash'));
	});

	it('omitting the Gmail guard path leaves the previous shape untouched', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY);
		assert.equal(o.hooks.PreToolUse.length, 1);
		assert.equal(o.hooks.PreToolUse[0].matcher, 'AskUserQuestion');
	});

	it('keeps the AskUserQuestion guard alongside the Gmail guard (concat, not replace)', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD);
		const matchers = o.hooks.PreToolUse.map((b: any) => b.matcher);
		assert.deepEqual(matchers, ['AskUserQuestion', 'mcp__.*[Gg][Mm][Aa][Ii][Ll].*']);
	});

	it('registers the DevApp guard on BOTH Pre and Post when its path is supplied', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD, DEVAPP_GUARD);
		// Pre decides; Post records the dispatch_state the Pre decision reads.
		// A Pre-only registration would deny nothing, so both are asserted.
		const pre = o.hooks.PreToolUse.map((b: any) => b.matcher);
		const post = o.hooks.PostToolUse.map((b: any) => b.matcher);
		assert.ok(pre.includes('mcp__.*__room\\.action\\.execute'));
		assert.ok(post.includes('mcp__.*__room\\.action\\.execute'));
		// operation.inspect is the way out of a block: without its Post
		// registration a `not_started` verdict could never unblock a resubmit.
		assert.ok(post.includes('mcp__.*__operation\\.inspect'));
		assert.ok(!pre.includes('mcp__.*__operation\\.inspect'),
			'inspection must never be gated');
	});

	it('the DevApp execute matcher selects the façade tool and nothing else', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD, DEVAPP_GUARD);
		const blk = o.hooks.PreToolUse.find(
			(b: any) => b.matcher === 'mcp__.*__room\\.action\\.execute',
		);
		const re = new RegExp(blk.matcher);
		// The server half is chosen by the Desktop credential bridge, so the
		// matcher must survive any name it picks.
		assert.ok(re.test('mcp__ag2-space__room.action.execute'));
		assert.ok(re.test('mcp__ag2space-dev__room.action.execute'));
		// Zero-effect reads, discovery and inspection are never blocked.
		assert.ok(!re.test('mcp__ag2-space__room.action.read'));
		assert.ok(!re.test('mcp__ag2-space__room.actions.search'));
		assert.ok(!re.test('mcp__ag2-space__operation.inspect'));
		assert.ok(!re.test('Bash'));
	});

	it('omitting the DevApp guard path leaves the previous shape untouched', () => {
		const o = buildCore(GUARD, '', SKILL_TELEMETRY, GMAIL_WRITE_GUARD);
		const matchers = o.hooks.PreToolUse.map((b: any) => b.matcher);
		assert.deepEqual(matchers, ['AskUserQuestion', 'mcp__.*[Gg][Mm][Aa][Ii][Ll].*']);
		assert.equal(o.hooks.PostToolUse.length, 1); // skill telemetry only
	});
});
