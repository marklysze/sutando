import assert from 'node:assert/strict';
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import {
  Observer, TAIL_BYTES, apply, buildRecord, descendants, fileDeps, lineChange, lsofRollouts, newState, parseArgs, pickRollout,
  rolloutPaths,
} from '../src/agent/codex/cli/codex-observer.mjs';

const ev = (type, extra = {}, ts = '2026-10-06T02:57:50.000Z') => ({timestamp: ts, type: 'event_msg', payload: {type, ...extra}});
const item = (type, ts = '2026-10-06T02:57:51.000Z') => ({timestamp: ts, type: 'response_item', payload: {type}});
const run = (lines, state = newState()) => {
  for (const line of lines) apply(state, lineChange(state, line), Date.parse(line.timestamp) / 1000);
  return state;
};

test('a turn reads moving, a completed response healthy, its end idle', () => {
  const s = run([ev('task_started'), item('function_call')]);
  assert.deepEqual([s.phase, s.motion, s.condition], ['tool', 'moving', 'unknown']);
  run([item('function_call_output'), ev('token_count', {info: {total_token_usage: {}}}, '2026-10-06T02:57:53.000Z')], s);
  assert.deepEqual([s.phase, s.condition], ['requesting', 'healthy']);
  assert.equal(s.lastSuccessAt, Date.parse('2026-10-06T02:57:53.000Z') / 1000);
  run([ev('task_complete')], s);
  assert.deepEqual([s.phase, s.motion, s.condition], ['idle', 'idle', 'healthy']);
});

test('a turn that ends without a completed response is unknown, never abnormal', () => {
  const s = run([ev('task_started'), ev('token_count', {info: {}}), ev('task_complete'), ev('task_started'),
    ev('token_count', {info: null}), ev('task_complete')]);
  assert.deepEqual([s.phase, s.motion, s.condition], ['idle', 'idle', 'unknown']);
  assert.equal(buildRecord(s, {observerId: 'o', startedAt: 1, session: 's'}, 2).reason, null);
});

test('an aborted turn goes idle; tool items outside a turn and other lines change nothing', () => {
  const s = run([ev('task_started'), ev('turn_aborted')]);
  assert.deepEqual([s.phase, s.motion], ['idle', 'idle']);
  const seq = s.seq;
  run([item('function_call'), ev('agent_message'), {type: 'turn_context'}], s);
  assert.equal(s.seq, seq);
});

test('the record is schema 1 for the core seat', () => {
  const s = run([ev('task_started')]);
  const r = buildRecord(s, {observerId: 'abc', startedAt: 10, session: 'sutando-core'}, 20);
  assert.deepEqual([r.schema, r.seat, r.session, r.observer, r.claude_session_id, r.heartbeat_at, r.condition_since],
    [1, 'core', 'sutando-core', 'codex-observer', null, 20, null]);
  assert.equal(r.seq, s.seq);
});

test('only the one top-level rollout is the core', () => {
  assert.equal(pickRollout([{path: 'a'}, {path: 'b', parent_thread_id: 'x'}, {path: 'c', agent_path: '/sub'}]), 'a');
  assert.equal(pickRollout([{path: 'a'}, {path: 'b'}]), null);
  assert.equal(pickRollout([{path: 'b', parent_thread_id: 'x'}]), null);
  assert.equal(pickRollout([]), null);
});

test('process tree and lsof parsing', () => {
  assert.deepEqual(descendants(10, ' 10 1\n 11 10\n 12 11\n 13 1\n 14 10\n'), [10, 11, 14, 12]);
  assert.deepEqual(rolloutPaths('p11\nn/x/sessions/2026/10/06/rollout-a.jsonl\nn/x/log.txt\nn/x/sessions/2026/10/06/rollout-a.jsonl\n'),
    ['/x/sessions/2026/10/06/rollout-a.jsonl']);
});

test('arguments are required', () => {
  assert.throws(() => parseArgs(['--engine', '/e']), /missing --tmux-socket/);
  assert.deepEqual(parseArgs(['--engine', '/e', '--tmux-socket', '/s', '--session', 'c']),
    {engine: '/e', tmuxSocket: '/s', session: 'c', workspace: ''});
});

function fake(files, opts = {}) {
  const writes = [];
  const timers = [];
  const deps = {
    now: () => 1_791_000_000,
    after: (ms, fn) => timers.push(fn),
    panePid: async () => (opts.gone ? 0 : 42),
    openRollouts: async () => Object.keys(files),
    readMeta: async (p) => files[p].meta,
    readTail: (p, bytes) => { const t = files[p].text; return {text: t.slice(-bytes), size: Buffer.byteLength(t)}; },
    readFrom: (p, off) => { const t = files[p].text.slice(off); return t ? {text: t, bytes: Buffer.byteLength(t)} : null; },
    resolvePython: async () => (opts.python === undefined ? '/py' : opts.python),
    write: async (py, rec) => { writes.push({py, rec}); },
  };
  return {deps, writes, flush: async () => { for (const fn of timers.splice(0)) fn(); await new Promise((r) => setTimeout(r, 5)); }};
}
const jl = (...lines) => lines.map((l) => JSON.stringify(l)).join('\n') + '\n';

test('the observer follows the core rollout and writes through the resolved python', async () => {
  const files = {'/r/main': {meta: {id: 'm'}, text: jl(ev('task_started'))}, '/r/sub': {meta: {parent_thread_id: 'm'}, text: ''}};
  const f = fake(files);
  const o = new Observer({session: 'sutando-core'}, f.deps);
  assert.equal(await o.tick(), true);
  await f.flush();
  assert.equal(f.writes.at(-1).py, '/py');
  assert.deepEqual([f.writes.at(-1).rec.phase, f.writes.at(-1).rec.motion], ['requesting', 'moving']);
  const done = JSON.stringify(ev('task_complete'));
  files['/r/main'].text += jl(ev('token_count', {info: {}})) + done.slice(0, 5);
  await o.tick();
  await f.flush();
  assert.equal(f.writes.at(-1).rec.condition, 'healthy');
  assert.equal(f.writes.at(-1).rec.phase, 'requesting', 'the half-written last line is held back');
});

test('a split line is applied once it completes', async () => {
  const line = JSON.stringify(ev('task_complete'));
  const files = {'/r/main': {meta: {}, text: jl(ev('task_started')) + line.slice(0, 10)}};
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  assert.equal(o.state.phase, 'requesting');
  files['/r/main'].text += line.slice(10) + '\n';
  await o.tick();
  assert.equal(o.state.phase, 'idle');
});

test('a large rollout is read from its tail, skipping the cut first line', async () => {
  const filler = jl(...Array.from({length: 4000}, () => ev('agent_message', {message: 'x'.repeat(80)})));
  const files = {'/r/main': {meta: {}, text: filler + jl(ev('task_started'))}};
  assert(Buffer.byteLength(files['/r/main'].text) > TAIL_BYTES);
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  assert.equal(o.state.phase, 'requesting');
});

test('no core rollout, two top-level rollouts, or no python mean no write', async () => {
  for (const files of [{}, {'/a': {meta: {}, text: jl(ev('task_started'))}, '/b': {meta: {}, text: ''}}]) {
    const f = fake(files);
    const o = new Observer({session: 's'}, f.deps);
    await o.tick();
    o.heartbeat();
    await f.flush();
    assert.equal(f.writes.length, 0);
  }
  const f = fake({'/a': {meta: {}, text: jl(ev('task_started'))}}, {python: ''});
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  await f.flush();
  assert.equal(f.writes.length, 0);
});

test('a core session gone on two discoveries in a row stops the observer; one miss does not', async () => {
  const f = fake({'/a': {meta: {}, text: ''}}, {gone: true});
  const o = new Observer({session: 's'}, f.deps);
  assert.equal(await o.discover(), true);
  assert.equal(await o.discover(), false);
});

test('one discovery that finds no rollout keeps the target; a second drops it', async () => {
  const files = {'/a': {meta: {}, text: jl(ev('task_started'))}};
  const f = fake(files);
  const o = new Observer({session: 's'}, f.deps);
  await o.discover();
  f.deps.openRollouts = async () => [];
  await o.discover();
  assert.equal(o.target, '/a');
  await o.discover();
  assert.equal(o.target, null);
});

test('reselecting a rollout never lowers seq, so the real writer keeps accepting records', async () => {
  const ws = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-ws-'));
  const writer = new URL('../src/runtime_observation.py', import.meta.url).pathname;
  const stored = () => JSON.parse(fs.readFileSync(path.join(ws, 'state/runtime-observations/core.json'), 'utf8'));
  const busy = jl(...Array.from({length: 15}, (_, i) => ev(i % 2 ? 'task_complete' : 'task_started')));
  const files = {'/a': {meta: {}, text: busy}, '/b': {meta: {}, text: jl(ev('task_started'))}};
  const f = fake(files);
  f.deps.now = () => Date.now() / 1000;
  f.deps.write = async (py, rec) => execFileSync('python3', [writer, 'write', '--workspace', ws], {input: JSON.stringify(rec)});
  f.deps.openRollouts = async () => ['/a'];
  const o = new Observer({session: 'sutando-core'}, f.deps);
  await o.discover();
  await f.flush();
  const before = stored();
  f.deps.openRollouts = async () => ['/b'];
  await o.discover();
  await o.discover();
  await f.flush();
  const after = stored();
  assert(after.seq > before.seq, `seq ${after.seq} after ${before.seq}`);
  assert.deepEqual([after.phase, after.motion], ['requesting', 'moving']);
  fs.rmSync(ws, {recursive: true});
});

test('lsof output still counts when one listed pid has already exited', {skip: !fs.existsSync('/usr/sbin/lsof') && !fs.existsSync('/usr/bin/lsof')}, async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-lsof-'));
  const file = fs.realpathSync(dir) + '/rollout-held.jsonl';
  fs.writeFileSync(file, '');
  const holder = spawn(process.execPath, ['-e', `require('fs').openSync(${JSON.stringify(file)}, 'r'); setTimeout(() => {}, 20000)`]);
  const gone = spawn(process.execPath, ['-e', '0']);
  await new Promise((r) => gone.on('exit', r));
  await new Promise((r) => setTimeout(r, 300));
  try {
    assert.deepEqual(await lsofRollouts([holder.pid, gone.pid]), [file]);
  } finally {
    holder.kill();
    fs.rmSync(dir, {recursive: true});
  }
});

test('a failing write never throws out of the observer', async () => {
  const f = fake({'/a': {meta: {}, text: jl(ev('task_started'))}});
  f.deps.write = async () => { throw new Error('no python'); };
  const o = new Observer({session: 's'}, f.deps);
  await o.tick();
  await f.flush();
  o.heartbeat();
  await f.flush();
});

test('fileDeps reads a long first line and file ranges', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'codex-observer-'));
  const file = path.join(dir, 'rollout-x.jsonl');
  const meta = {type: 'session_meta', payload: {id: 't', base_instructions: 'y'.repeat(600_000)}};
  fs.writeFileSync(file, JSON.stringify(meta) + '\n' + JSON.stringify(ev('task_started')) + '\n');
  const deps = fileDeps({engine: '/e', tmuxSocket: '/s', session: 's'});
  assert.equal((await deps.readMeta(file)).id, 't');
  const {size} = deps.readTail(file, 100);
  assert.equal(size, fs.statSync(file).size);
  assert.equal(deps.readFrom(file, size), null);
  fs.appendFileSync(file, 'abc');
  assert.deepEqual(deps.readFrom(file, size), {text: 'abc', bytes: 3});
  fs.rmSync(dir, {recursive: true});
});
