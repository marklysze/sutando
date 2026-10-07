#!/usr/bin/env python3
"""Persisted gateway retry and dependent lease completion, with controlled time."""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'packages' / 'ag2-sparrow'))
from ag2_sparrow import outbox, remote_gateway_bridge as gw, undelivered_quarantine
from ag2_sparrow.delivery_core import DeliveryCore, DesignAClaimBackend, RetryPolicy, DrainStatus
from ag2_sparrow.delivery_core.provider_ag2space import AG2SpaceResultProvider
from ag2_sparrow.delivery_core import ProviderPermanentRefused

ROOM = '!same:ag2.space'
HOLDER = 'task-holder'


class Gateway:
    def __init__(self):
        self.now = 1000.0
        self.available_at = self.now
        self.calls = []
        self.accepted = {}
        self.replies = []
        self.lose_response = False
        self.code = None

    def request(self, method, path, payload):
        self.calls.append(dict(payload))
        if self.code or self.now < self.available_at:
            raise urllib.error.HTTPError('https://gateway.invalid', self.code or 503,
                                         'unavailable', None, None)
        duplicate = payload['id'] in self.accepted
        if not duplicate:
            self.accepted[payload['id']] = dict(payload)
            if not payload.get('no_send'):
                self.replies.append(payload)
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError('response lost after acceptance')
        return {'ok': True, 'duplicate': duplicate}


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = Gateway()
        self.policy_outbox = outbox
        observer = patch.object(outbox, '_activity_completed', return_value=None)
        observer.start()
        self.addCleanup(observer.stop)
        self.results = self.root / 'results'
        self.tasks = self.root / 'tasks'
        self.results.mkdir()
        self.tasks.mkdir()
        self.outbox = self.results / '.outbox'
        self.payload = json.dumps({'id': HOLDER, 'body': 'Existing answer'}).encode()

    def core(self, timed=True):
        backend = DesignAClaimBackend(
            self.outbox, retry_schedule=self.policy_outbox.RetrySchedule() if timed else None,
            clock=lambda: self.server.now)
        return DeliveryCore(backend, AG2SpaceResultProvider(self.server.request),
                            RetryPolicy(max_attempts=5, defer_idempotent_resend=timed))

    def deliver(self, core):
        core.backend.publish(HOLDER, self.payload)
        return core.deliver_one(HOLDER, self.payload)

    def next_attempt(self):
        self.server.now = outbox.read_item(self.outbox, HOLDER)['retry']['next_attempt_at']

    def bridge(self):
        core = self.core()
        values = dict(RESULTS_DIR=self.results, ARCHIVE_RESULTS_DIR=self.results / 'archive',
                      UNDELIVERABLE_RESULTS_DIR=self.results / 'undelivered', TASKS_DIR=self.tasks,
                      _STATE=self.root / 'state', DEDUP_ALIAS_FILE=self.root / 'state' / 'aliases.json',
                      TASK_ROOMS_FILE=self.root / 'state' / 'rooms.json',
                      TASK_MEDIA_FILE=self.root / 'state' / 'media.json',
                      INFLIGHT_FILE=self.root / 'state' / 'inflight.json',
                      GATEWAY_INSTANCE='', _INST_SUFFIX='', _DELIVERY_CORE=core,
                      _req=self.server.request)
        stack = __import__('contextlib').ExitStack()
        for name, value in values.items():
            stack.enter_context(patch.object(gw, name, value))
        self.addCleanup(stack.close)
        return core

    def task(self, tid, sender='owner', room=ROOM):
        (self.tasks / f'{tid}.txt').write_text(
            f'id: {tid}\nsource: ag2space\nchannel_id: {room}\nuser_id: {sender}\n'
            'access_tier: owner\ntask: Same question\n')
        gw._record_task_room(tid, room)

    def seed_duplicates(self):
        for tid in (HOLDER, 'task-duplicate1', 'task-duplicate2'):
            self.task(tid)
        (self.results / f'{HOLDER}.txt').write_text('Existing answer')
        for tid in ('task-duplicate1', 'task-duplicate2'):
            (self.results / f'{tid}.txt').write_text(f'[deduped: {HOLDER}]')
        return {HOLDER, 'task-duplicate1', 'task-duplicate2'}

    def test_parent_failure_five_passes_ten_requests_then_stranded(self):
        core = self.core(timed=False)
        self.server.available_at += 120
        for _ in range(5):
            self.deliver(core)
        self.assertEqual(len(self.server.calls), 10)
        self.server.now = self.server.available_at
        self.assertIs(self.deliver(core).status, DrainStatus.TERMINAL)
        self.assertEqual(len(self.server.calls), 10)
        print('Legacy policy: 5 passes, 10 POSTs, terminal; recovery sends 0')

    def test_outages_60_and_120_seconds_recover_same_answer(self):
        for seconds in (60, 120):
            with self.subTest(seconds=seconds):
                self.outbox = self.results / f'.outbox-{seconds}'
                self.server = Gateway()
                self.server.available_at += seconds
                core = self.core()
                while self.server.now < self.server.available_at:
                    self.deliver(core)
                    self.assertFalse(core.backend.is_terminal(HOLDER))
                    count = len(self.server.calls)
                    self.deliver(core)
                    self.assertEqual(len(self.server.calls), count)
                    self.next_attempt()
                self.deliver(core)
                self.assertEqual(outbox.item_status(self.outbox, HOLDER), 'DELIVERED')
                self.assertEqual(len(self.server.replies), 1)
                self.assertEqual({c['id'] for c in self.server.calls}, {HOLDER})
                print(f'Outage {seconds}s: accepted at +{self.server.now - 1000:g}s; '
                      f'{len(self.server.calls)} POSTs, one answer, same result ID')

    def test_lost_response_after_acceptance_is_idempotent(self):
        core = self.core()
        self.server.lose_response = True
        self.deliver(core)
        self.assertFalse(core.backend.is_terminal(HOLDER))
        self.next_attempt()
        self.deliver(core)
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(len(self.server.replies), 1)
        self.assertEqual(outbox.item_status(self.outbox, HOLDER), 'DELIVERED')

    def test_restart_retains_timing_and_budget(self):
        core = self.core()
        self.server.available_at += 120
        self.deliver(core)
        record = outbox.read_item(self.outbox, HOLDER)
        restarted = self.core()
        restarted.recover()
        self.deliver(restarted)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['retry'], record['retry'])
        while self.server.now < self.server.available_at:
            self.next_attempt()
            self.deliver(restarted)
        self.assertEqual(len(self.server.replies), 1)
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['retry']['deadline'],
                         record['retry']['deadline'])

    def test_extended_outage_quarantines_and_operator_can_recover(self):
        core = self.bridge()
        self.task(HOLDER)
        result = self.results / f'{HOLDER}.txt'
        result.write_text('Existing answer')
        self.server.available_at += 900
        inflight = {HOLDER}
        gw._post_ready_results(inflight)
        deadline = outbox.read_item(self.outbox, HOLDER)['retry']['deadline']
        with patch.object(gw, '_DELIVERY_CORE', self.core()):
            self.server.now = deadline
            gw._post_ready_results(inflight)
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['reason'], 'retry-window-exhausted')
        self.assertFalse(result.exists())
        self.assertTrue(undelivered_quarantine.find_quarantined(self.results, HOLDER))
        self.server.now = self.server.available_at
        self.assertIs(self.deliver(core).status, DrainStatus.TERMINAL)
        outbox.requeue_item(self.outbox, HOLDER, reset_attempts=True, operator='test')
        undelivered_quarantine.restore(self.results, HOLDER)
        gw._post_ready_results(inflight)
        self.assertEqual(len(self.server.replies), 1)
        self.assertFalse(inflight)

    def test_permanent_failures_park_immediately(self):
        for code in (400, 401, 403, 404, 409, 410, 422):
            with self.subTest(code=code):
                self.outbox = self.results / f'.outbox-{code}'
                self.server.code = code
                core = self.core()
                self.deliver(core)
                self.assertTrue(core.backend.is_terminal(HOLDER))
                self.assertEqual(outbox.read_item(self.outbox, HOLDER)['reason'], 'permanent-refusal')
        for code in (408, 429, 500, 502, 503, 504):
            with self.subTest(retryable=code):
                self.outbox = self.results / f'.outbox-{code}'
                self.server.code = code
                core = self.core()
                self.deliver(core)
                self.assertFalse(core.backend.is_terminal(HOLDER))

    def test_pending_duplicates_wait_restart_then_close_each_lease(self):
        self.bridge()
        inflight = self.seed_duplicates()
        self.server.available_at += 120
        gw._post_ready_results(inflight)
        self.assertEqual(inflight, {HOLDER, 'task-duplicate1', 'task-duplicate2'})
        self.assertEqual(len(list(self.tasks.glob('*.txt'))), 3)
        with patch.object(gw, '_DELIVERY_CORE', self.core()):
            inflight = gw._load_inflight()
            while self.server.now < self.server.available_at:
                self.next_attempt()
                gw._post_ready_results(inflight)
            for _ in range(3):
                gw._post_ready_results(inflight)
        self.assertFalse(inflight)
        self.assertEqual(set(self.server.accepted), {HOLDER, 'task-duplicate1', 'task-duplicate2'})
        self.assertEqual([p['id'] for p in self.server.replies], [HOLDER])
        for tid in ('task-duplicate1', 'task-duplicate2'):
            self.assertTrue(self.server.accepted[tid]['no_send'])

    def test_quarantined_holder_is_reported_never_regenerated_or_replayed(self):
        self.bridge()
        inflight = self.seed_duplicates()
        undelivered_quarantine.quarantine(self.results / f'{HOLDER}.txt', self.results)
        gw._post_ready_results(inflight)
        self.assertEqual(len(list(self.tasks.rglob('*.txt'))), 3)
        self.assertNotIn(HOLDER, self.server.accepted)
        for tid in ('task-duplicate1', 'task-duplicate2'):
            self.assertIn('operator recovery', self.server.accepted[tid]['body'])
        self.assertTrue(undelivered_quarantine.find_quarantined(self.results, HOLDER))

    def test_concurrent_decisions_wait_for_one_holder(self):
        self.bridge()
        self.seed_duplicates()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            plans = list(pool.map(lambda tid: gw._dedup_plan(tid, HOLDER),
                                  ('task-duplicate1', 'task-duplicate2')))
        self.assertEqual([p[:2] for p in plans], [('wait', HOLDER), ('wait', HOLDER)])
        self.assertEqual(len(list(self.tasks.glob('*.txt'))), 3)

    def test_cross_sender_and_room_do_not_wait_or_honour(self):
        self.bridge()
        self.seed_duplicates()
        self.task('task-duplicate1', sender='someone-else')
        self.task('task-duplicate2', room='!other:ag2.space')
        for tid in ('task-duplicate1', 'task-duplicate2'):
            action, new_id, _ = gw._dedup_plan(tid, HOLDER)
            self.assertEqual(action, 'requeue')
            self.assertIn('dedup_requeue_count: 1', (self.tasks / f'{new_id}.txt').read_text())
            self.assertEqual(gw._dedup_plan(new_id, HOLDER)[0], 'report')

    def test_missing_holder_reasks_once_then_reports(self):
        self.bridge()
        self.task('task-duplicate1')
        action, new_id, _ = gw._dedup_plan('task-duplicate1', HOLDER)
        self.assertEqual(action, 'requeue')
        self.assertEqual(gw._dedup_plan(new_id, HOLDER)[0], 'report')

    def test_concurrent_drainer_cannot_expire_an_active_send(self):
        core = self.core()
        entered, release = threading.Event(), threading.Event()
        original = self.server.request

        def blocked(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        core.provider = AG2SpaceResultProvider(blocked)
        core.backend.publish(HOLDER, self.payload)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(core.deliver_one, HOLDER, self.payload)
            self.assertTrue(entered.wait(5))
            self.server.now += 601
            second = self.core().deliver_one(HOLDER, self.payload)
            self.assertIs(second.status, DrainStatus.NOT_CLAIMED)
            self.assertNotEqual(outbox.item_status(self.outbox, HOLDER), 'PARKED')
            release.set()
            self.assertEqual(first.result().outcome.value, 'confirmed')
        self.assertEqual(len(self.server.replies), 1)

    def test_redirected_answer_cannot_satisfy_original_room(self):
        self.bridge()
        self.seed_duplicates()
        body = '[channel: !other:ag2.space]\nExisting answer'
        payload = json.dumps({'id': HOLDER, 'body': body}).encode()
        core = gw._delivery_core()
        core.backend.publish(HOLDER, payload)
        token = core.backend.claim(HOLDER, 'fixture')
        from ag2_sparrow.delivery_core import DeliveryOutcome
        core.backend.complete(token, DeliveryOutcome.CONFIRMED)
        self.assertEqual(gw._dedup_plan('task-duplicate1', HOLDER)[0], 'report')

    def test_non_idempotent_provider_still_parks_unknown(self):
        from ag2_sparrow.delivery_core import ProviderCapabilities
        class NonIdempotent(AG2SpaceResultProvider):
            capabilities = ProviderCapabilities()
        core = self.core()
        core.provider = NonIdempotent(self.server.request)
        self.server.available_at += 60
        self.deliver(core)
        self.assertTrue(core.backend.is_terminal(HOLDER))
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['reason'], 'outcome-unknown')

    def test_invalid_retry_configuration_is_rejected(self):
        for config in ({'window_s': float('nan')}, {'initial_delay_s': 0},
                       {'initial_delay_s': 31}, {'window_s': 29}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.policy_outbox.RetrySchedule(**config)

    def test_failure_finishing_after_deadline_retains_terminal_answer(self):
        core = self.core()
        def stalled(method, path, payload):
            self.server.now += 601
            raise urllib.error.HTTPError('https://gateway.invalid', 503, 'outage', None, None)
        core.provider._request = stalled
        self.deliver(core)
        record = outbox.read_item(self.outbox, HOLDER)
        self.assertEqual(record['status'], 'PARKED')
        self.assertEqual(record['reason'], 'retry-window-exhausted')
        self.assertEqual(json.loads(record['payload'])['body'], 'Existing answer')

    def test_holder_markers_require_receipt_and_destination_evidence(self):
        for record, body, live, expected in (
            (None, '[no-send]', True, 'missing'),
            (None, '[REPLIED]', False, 'missing'),
            ({'status': 'DELIVERED'}, '[REPLIED]', False, 'accepted'),
            ({'status': 'PARKED'}, 'Existing answer', True, 'failed'),
            (None, '[channel: !other:ag2.space]\nExisting answer', True, 'failed'),
        ):
            with self.subTest(body=body, record=record):
                self.assertEqual(gw.classify_holder_delivery(record, body, live), expected)

    def test_unverified_holder_metadata_reports_without_reasking(self):
        self.bridge()
        self.seed_duplicates()
        (self.tasks / f'{HOLDER}.txt').unlink()
        action, body, room = gw._dedup_plan('task-duplicate1', HOLDER)
        self.assertEqual(action, 'report')
        self.assertIn('could not be verified', body)
        self.assertEqual(len(list(self.tasks.glob('task-*.txt'))), 2)

    def test_invalid_holder_identity_and_payload_fail_closed(self):
        core = self.bridge()
        self.assertEqual(gw._holder_delivery_state('../holder'), 'failed')
        with patch.object(gw, '_delivery_tid', return_value=None):
            self.assertEqual(gw._holder_delivery_state(HOLDER), 'failed')
        core.backend.publish(HOLDER, b'{broken')
        self.assertEqual(gw._holder_delivery_state(HOLDER), 'failed')
        self.assertEqual(self.server.calls, [])

    def test_malformed_envelopes_are_terminal_before_network_io(self):
        provider = AG2SpaceResultProvider(self.server.request)
        for payload in (b'\xff', b'{broken', b'[]', b'{}'):
            with self.subTest(payload=payload), self.assertRaises(ProviderPermanentRefused):
                provider.deliver(HOLDER, payload, HOLDER)
        self.assertEqual(self.server.calls, [])

    def test_invalid_retry_state_parks_without_resetting_budget(self):
        core = self.core()
        self.server.available_at += 120
        self.deliver(core)
        with outbox._item_lock(self.outbox, HOLDER):
            record = outbox.read_item(self.outbox, HOLDER)
            record['retry']['deadline'] = 'broken'
            outbox._write_item(self.outbox, HOLDER, record)
        self.assertIs(self.deliver(self.core()).status, DrainStatus.TERMINAL)
        self.assertEqual(len(self.server.calls), 1)
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['reason'], 'invalid-retry-state')

    def test_real_http_round_trip_after_process_restart(self):
        server_state = self.server

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                try:
                    response = server_state.request('POST', self.path, payload)
                    body = json.dumps(response).encode()
                    self.send_response(200)
                except urllib.error.HTTPError as exc:
                    body = b'{}'
                    self.send_response(exc.code)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.bridge()
        inflight = self.seed_duplicates()
        gw._save_inflight(inflight)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        code = r"""
import json, sys, urllib.request
from pathlib import Path
from ag2_sparrow import remote_gateway_bridge as gw
root, url, now = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
gw.RESULTS_DIR = root / 'results'
gw.ARCHIVE_RESULTS_DIR = gw.RESULTS_DIR / 'archive'
gw.UNDELIVERABLE_RESULTS_DIR = gw.RESULTS_DIR / 'undelivered'
gw.TASKS_DIR = root / 'tasks'
gw._STATE = root / 'state'
gw.DEDUP_ALIAS_FILE = gw._STATE / 'aliases.json'
gw.TASK_ROOMS_FILE = gw._STATE / 'rooms.json'
gw.TASK_MEDIA_FILE = gw._STATE / 'media.json'
gw.INFLIGHT_FILE = gw._STATE / 'inflight.json'
gw.GATEWAY_INSTANCE = gw._INST_SUFFIX = ''
def request(method, path, payload):
    req = urllib.request.Request(url + path, data=json.dumps(payload).encode(), method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)
gw._req = request
gw._delivery_core().backend.clock = lambda: now
inflight = gw._load_inflight()
for _ in range(3):
    gw._post_ready_results(inflight)
print('pending:', sorted(inflight))
"""
        env = {k: v for k, v in os.environ.items() if not k.startswith('SUTANDO_')}
        env['PYTHONPATH'] = str(REPO / 'packages' / 'ag2-sparrow')
        url = f'http://127.0.0.1:{server.server_port}'
        self.server.available_at += 120
        first = subprocess.run([sys.executable, '-c', code, str(self.root), url, '1000'],
                               env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        record = outbox.read_item(self.outbox, HOLDER)
        self.assertEqual(record['retry']['next_attempt_at'], 1002)
        self.server.now = 1120
        second = subprocess.run([sys.executable, '-c', code, str(self.root), url, '1120'],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('pending: []', second.stdout)
        self.assertEqual([p['id'] for p in self.server.replies], [HOLDER])
        self.assertEqual(set(self.server.accepted), {HOLDER, 'task-duplicate1', 'task-duplicate2'})
        self.assertEqual(outbox.read_item(self.outbox, HOLDER)['retry']['deadline'], 1600)
        print('HTTP across process restart: 503 -> same holder accepted at +120s; '
              'one answer, both dependent leases closed')

    def test_historical_parked_item_is_not_automatically_rearmed(self):
        old = self.core(timed=False)
        self.server.available_at += 120
        for _ in range(5):
            self.deliver(old)
        count = len(self.server.calls)
        self.server.now = self.server.available_at
        self.assertIs(self.deliver(self.core()).status, DrainStatus.TERMINAL)
        self.assertEqual(len(self.server.calls), count)
        self.assertNotIn('retry', outbox.read_item(self.outbox, HOLDER))

    def test_dependent_lease_close_failure_is_retained_for_recovery(self):
        core = self.bridge()
        self.seed_duplicates()
        gw._post_ready_results({HOLDER})
        self.server.code = 403
        inflight = {'task-duplicate1', 'task-duplicate2'}
        gw._post_ready_results(inflight)
        gw._post_ready_results(inflight)
        count = len(self.server.calls)
        gw._post_ready_results(inflight)
        self.assertEqual(len(self.server.calls), count)
        for tid in inflight:
            self.assertTrue(undelivered_quarantine.find_quarantined(self.results, tid))
            self.assertEqual(outbox.read_item(self.outbox, tid)['reason'], 'permanent-refusal')
            outbox.requeue_item(self.outbox, tid, reset_attempts=True, operator='test')
            undelivered_quarantine.restore(self.results, tid)
        self.server.code = None
        gw._post_ready_results(inflight)
        self.assertFalse(inflight)
        self.assertEqual([p['id'] for p in self.server.replies], [HOLDER])

    def test_archive_without_receipt_is_not_accepted_evidence(self):
        self.bridge()
        self.seed_duplicates()
        (self.results / 'archive').mkdir()
        (self.results / f'{HOLDER}.txt').rename(self.results / 'archive' / f'{HOLDER}-123.txt')
        self.assertEqual(gw._dedup_plan('task-duplicate1', HOLDER)[0], 'report')


class CanonicalRecoveryTest(RecoveryTest):
    """The same recovery contract exercises the canonical shared policy writers."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(REPO / 'src'))
        self.addCleanup(lambda: sys.path.remove(str(REPO / 'src')))
        import outbox as canonical_outbox
        import dedup_recovery as canonical_dedup
        self.policy_outbox = canonical_outbox
        from ag2_sparrow.delivery_core import backend_a
        for module, name, value in (
            (canonical_outbox, '_activity_completed', lambda item_id: None),
            (backend_a, 'outbox', canonical_outbox),
            (gw, 'plan_dedup_recovery', canonical_dedup.plan_dedup_recovery),
            (gw, 'classify_holder_delivery', canonical_dedup.classify_holder_delivery),
        ):
            override = patch.object(module, name, value)
            override.start()
            self.addCleanup(override.stop)


if __name__ == '__main__':
    unittest.main(verbosity=2)
