"""Pure CPU checks; all systemd commands are mocked."""

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from tests import run_capacity_profile as runner


class CapacityProfileTests(unittest.TestCase):
    def arguments(self, *extra):
        return ['--label', 'cpu-check', '--capacity', '4', '--kv-gib', '2',
                '--live-runtime', '/tmp/capacity-live', *extra, 'pair-ab:120']

    def rejected(self, arguments):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.parse_args(arguments)

    def test_live_runtime_is_required_and_api_port_is_fixed(self):
        self.rejected(['--label', 'cpu', '--capacity', '4', '--kv-gib', '2', 'pair-ab:120'])
        self.rejected(self.arguments('--url', 'http://127.0.0.1:8097'))
        args = runner.parse_args(self.arguments())
        self.assertEqual(runner.ROOT, Path(__file__).resolve().parents[1])
        self.assertEqual(args.runtime_dir, runner.ROOT / 'runtime/capacity')
        self.assertEqual(args.work_dir, runner.ROOT / 'work/capacity')
        self.assertEqual(runner.URL, 'http://127.0.0.1:18098')

    def test_invalid_capacity_paths_and_stage_names_are_rejected(self):
        for extra in (('--capacity', '1'), ('--capacity', '17'), ('--kv-gib', 'nan'),
                      ('--kv-gib', '.49'), ('--label', '../unsafe'),
                      ('--runtime-dir', '/tmp/capacity-live'),
                      ('--runtime-dir', '/tmp/capacity-live/child'),
                      ('--work-dir', '/tmp/capacity-live/child')):
            with self.subTest(extra=extra):
                self.rejected(self.arguments(*extra))
        for stage in ('../unsafe:120', 'pair-ab:nan', 'pair-ab:0', 'pair-ab:120:extra'):
            self.rejected(self.arguments()[:-1] + [stage])
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / 'live'
            live.mkdir()
            link = Path(tmp) / 'alias'
            link.symlink_to(live)
            self.rejected(self.arguments('--live-runtime', str(live), '--runtime-dir', str(link)))

    def test_environment_allowlist_preserves_models_and_excludes_secrets(self):
        args = runner.parse_args(self.arguments())
        env = runner.experiment_environment(args, {
            'ONEAXE_VOICE_R2T2_MODEL_DIR': '/models/r2t2',
            'ONEAXE_VOICE_STREAM_PYTHON': '/interpreters/stream-python',
            'ONEAXE_VOICE_CUDA_DEVICE': '0',
            'ONEAXE_VOICE_STREAM_CUDAGRAPH_CAPTURE_SIZES': '1,4',
            'ONEAXE_VOICE_RUNTIME_DIR': '/production/runtime',
            'ONEAXE_VOICE_API_URL': 'http://127.0.0.1:8097',
            'ONEAXE_VOICE_FUTURE_TOKEN': 'PRIVATE', 'OPENAI_API_KEY': 'PRIVATE',
        })
        self.assertEqual(env['ONEAXE_VOICE_R2T2_MODEL_DIR'], '/models/r2t2')
        self.assertEqual(env['ONEAXE_VOICE_STREAM_CUDAGRAPH_CAPTURE_SIZES'], '1,4')
        self.assertEqual(env['ONEAXE_VOICE_RUNTIME_DIR'], str(args.runtime_dir))
        self.assertEqual(env['ONEAXE_VOICE_API_URL'], runner.URL)
        self.assertEqual(env['ONEAXE_VOICE_STREAM_MAX_NUM_SEQS'], '4')
        self.assertNotIn('ONEAXE_VOICE_FUTURE_TOKEN', env)
        self.assertNotIn('OPENAI_API_KEY', env)

    def command_environment(self, policy):
        args = runner.parse_args(self.arguments('--omp-wait-policy', policy))
        keys = ('OMP_WAIT_POLICY', 'GOMP_SPINCOUNT', 'MKL_NUM_THREADS',
                'KMP_BLOCKTIME', 'OMP_NUM_THREADS')
        env = dict(os.environ, **dict.fromkeys(keys, 'inherited-override'))
        env['OMP_NUM_THREADS'] = '4'
        command = runner.experiment_command(args)[:-3] + [sys.executable, '-c',
            'import json,os; print(json.dumps({k:os.environ.get(k) for k in ' + repr(keys) + '}))']
        output = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        return json.loads(output.stdout)

    def test_default_wait_policy_really_unsets_inherited_overrides(self):
        env = self.command_environment('default')
        self.assertEqual(env, {'OMP_WAIT_POLICY': None, 'GOMP_SPINCOUNT': None,
                              'MKL_NUM_THREADS': None, 'KMP_BLOCKTIME': None,
                              'OMP_NUM_THREADS': '4'})

    def test_passive_changes_only_wait_policy_after_clearing_overrides(self):
        env = self.command_environment('passive')
        self.assertEqual(env, {'OMP_WAIT_POLICY': 'PASSIVE', 'GOMP_SPINCOUNT': None,
                              'MKL_NUM_THREADS': None, 'KMP_BLOCKTIME': None,
                              'OMP_NUM_THREADS': '4'})

    def test_feature_device_controls_probe_without_inherited_override(self):
        for device, expected_probe in (('cpu', '0'), ('cuda:0', '1')):
            args = runner.parse_args(self.arguments('--feature-device', device))
            keys = ('ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE', 'ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE')
            command = runner.experiment_command(args)[:-3] + [sys.executable, '-c',
                'import json,os; print(json.dumps({k:os.environ.get(k) for k in ' + repr(keys) + '}))']
            result = subprocess.run(command, env=dict(os.environ, **dict.fromkeys(keys, 'invalid')),
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), dict(zip(keys, (device, expected_probe))))

    def test_cancellation_command_keeps_credentials_in_files_and_rejects_wrong_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / 'pair.json'
            rows = [{'role': role, 'token_file': role + '.token', 'audio_file': role + '.wav',
                     'keywords': ['expected ' + role]} for role in ('pc', 'mobile')]
            manifest.write_text(json.dumps({'streams': rows}))
            command = runner.cancellation_command(manifest, root / 'result.json')
            self.assertIn('--cancel-mobile-after', command)
            self.assertIn(str(root / 'pc.token'), command)
            self.assertIn('--mobile-keyword=expected mobile', command)
            rows[1]['role'] = 'pc'
            manifest.write_text(json.dumps({'streams': rows}))
            with self.assertRaises(RuntimeError):
                runner.cancellation_command(manifest, root / 'result.json')


class CapacityProfileCpuTests(unittest.TestCase):
    def samples(self):
        before = {'monotonic': 100.0,
                  'cpu_usec': {'usage_usec': 5_000_000, 'user_usec': 4_000_000,
                               'system_usec': 1_000_000},
                  'processes': [{'pid': pid, 'start_ticks': pid * 10, 'role': role,
                                 'cpu_seconds': 1.0}
                                for pid, role in ((10, 'api'), (20, 'worker'), (30, 'engine'))]}
        after = deepcopy(before)
        after['monotonic'] = 110.0
        after['cpu_usec'] = {'usage_usec': 17_000_000, 'user_usec': 14_000_000,
                             'system_usec': 3_000_000}
        for row, cpu in zip(after['processes'], (2.0, 4.0, 6.0)):
            row['cpu_seconds'] = cpu
        return before, after

    def test_cpu_total_includes_whole_window_and_is_not_added_to_role_totals(self):
        before, after = self.samples()
        report = runner.cpu_difference(before, after)
        self.assertTrue(report['valid'])
        self.assertTrue(report['core_processes_preserved'])
        self.assertEqual(report['wall_seconds'], 10.0)
        self.assertEqual(report['usage_seconds'], 12.0)
        self.assertEqual(report['user_seconds'], 10.0)
        self.assertEqual(report['system_seconds'], 2.0)
        self.assertEqual(report['mean_cores'], 1.2)
        self.assertEqual(report['matched_process_cpu_seconds_by_role'],
                         {'api': 1.0, 'worker': 3.0, 'engine': 5.0})

    def test_auxiliary_process_churn_does_not_lose_authoritative_cgroup_cpu(self):
        before, after = self.samples()
        before['processes'].append({'pid': 40, 'start_ticks': 400, 'role': 'auxiliary',
                                    'cpu_seconds': 2.0})
        after['processes'].append({'pid': 50, 'start_ticks': 500, 'role': 'auxiliary',
                                   'cpu_seconds': 3.0})
        report = runner.cpu_difference(before, after)
        self.assertTrue(report['valid'])
        self.assertEqual(report['usage_seconds'], 12.0)
        self.assertNotIn('auxiliary', report['matched_process_cpu_seconds_by_role'])

    def test_core_pid_reuse_replacement_or_disappearance_invalidates_measurement(self):
        for role in ('api', 'worker', 'engine'):
            for change in ('pid', 'start_ticks', 'missing'):
                before, after = self.samples()
                row = next(row for row in after['processes'] if row['role'] == role)
                if change == 'missing':
                    after['processes'].remove(row)
                else:
                    row[change] += 1
                with self.subTest(role=role, change=change):
                    report = runner.cpu_difference(before, after)
                    self.assertFalse(report['valid'])
                    self.assertFalse(report['core_processes_preserved'])

    def test_absent_core_role_in_both_snapshots_is_not_valid(self):
        for role in ('api', 'worker', 'engine'):
            before, after = self.samples()
            for sample in (before, after):
                sample['processes'] = [row for row in sample['processes'] if row['role'] != role]
            with self.subTest(role=role):
                self.assertFalse(runner.cpu_difference(before, after)['valid'])

    def test_nonpositive_wall_or_regressed_cgroup_counter_is_not_valid(self):
        for wall in (0, -1):
            before, after = self.samples()
            after['monotonic'] = before['monotonic'] + wall
            with self.subTest(wall=wall):
                report = runner.cpu_difference(before, after)
                self.assertFalse(report['valid'])
                self.assertIsNone(report['mean_cores'])
        for counter in ('usage_usec', 'user_usec', 'system_usec'):
            before, after = self.samples()
            after['cpu_usec'][counter] = before['cpu_usec'][counter] - 1
            with self.subTest(counter=counter):
                self.assertFalse(runner.cpu_difference(before, after)['valid'])


class CancellationEvidenceTests(unittest.TestCase):
    def test_closed_connection_without_cancel_final_is_not_success(self):
        from tests.e2e_concurrent import performance_evidence

        metrics = {'lag_early': {'p95': .1}, 'lag_late': {'p95': .1},
                   'processed_lag_seconds': {'p95': .1}, 'capture_stop_lag_seconds': .1,
                   'capture_seconds': 8, 'fixed_updates_seconds': [1, 6],
                   'max_fixed_interval_seconds': 5, 'sent_samples': 128000,
                   'expected_samples': 128000, 'cancel_requested': True}
        args = SimpleNamespace(max_processed_lag_seconds=2, max_lag_growth_seconds=.5,
                               max_fixed_gap_seconds=30)
        self.assertFalse(performance_evidence(metrics, args)['passed'])
        final = dict(metrics, complete=False, final_reason='cancelled', final_at_seconds=8.01)
        self.assertTrue(performance_evidence(final, args)['passed'])
        for omitted in ('complete', 'final_reason', 'final_at_seconds'):
            incomplete = {key: value for key, value in final.items() if key != omitted}
            self.assertFalse(performance_evidence(incomplete, args)['passed'])


class CapacityProfileLiveStatusTests(unittest.TestCase):
    def status(self, desktop=None, api=None):
        desktop = {'state': 'idle'} if desktop is None else desktop
        api = {'state': 'ready', 'busy': False, 'active_sessions': []} if api is None else api
        with patch.object(runner.socket, 'socket') as socket_factory, \
                patch.object(runner.httpx, 'Client') as client_factory, \
                patch.object(runner.Path, 'read_text', return_value='test-private-credential\n'):
            connection = socket_factory.return_value.__enter__.return_value
            stream = connection.makefile.return_value.__enter__.return_value
            stream.readline.return_value = json.dumps(desktop).encode() + b'\n'
            client = client_factory.return_value.__enter__.return_value
            client.get.return_value.json.return_value = api
            result = runner.live_status(Path('/tmp/live-runtime'))
            connection.sendall.assert_called_once_with(b'{"action":"status"}\n')
            client_factory.assert_called_once_with(trust_env=False, timeout=2)
            self.assertEqual(client.get.call_args.args,
                             ('http://127.0.0.1:8097/api/dictation/status',))
            client.get.return_value.raise_for_status.assert_called_once_with()
            return result

    def test_live_status_accepts_only_idle_desktop_and_idle_ready_api(self):
        self.assertEqual(self.status(), {'idle': True, 'state': 'idle'})

    def test_api_mobile_busy_or_active_session_blocks_experiment(self):
        for field, value in (('busy', True), ('active_sessions', [{'client_kind': 'mobile'}]),
                             ('model_unloading', True), ('state', 'loading')):
            api = {'state': 'ready', 'busy': False, 'active_sessions': [], field: value}
            with self.subTest(field=field):
                self.assertFalse(self.status(api=api)['idle'])

    def test_incomplete_api_status_is_rejected(self):
        for omitted in ('state', 'busy', 'active_sessions'):
            api = {'state': 'ready', 'busy': False, 'active_sessions': []}
            del api[omitted]
            with self.subTest(omitted=omitted):
                self.assertFalse(self.status(api=api)['idle'])

    def test_desktop_capture_prepare_unload_or_busy_blocks_experiment(self):
        cases = [{'state': 'recording'}, {'state': 'idle', 'capture_active': True},
                 {'state': 'idle', 'warming': True}, {'state': 'idle', 'preparing': True},
                 {'state': 'idle', 'model_unloading': True},
                 {'state': 'idle', 'model_status': {'busy': True}}]
        for desktop in cases:
            with self.subTest(desktop=desktop):
                self.assertFalse(self.status(desktop=desktop)['idle'])

    def test_desktop_status_error_aborts_before_api_request(self):
        with patch.object(runner.socket, 'socket') as socket_factory, \
                patch.object(runner.httpx, 'Client') as client_factory:
            connection = socket_factory.return_value.__enter__.return_value
            stream = connection.makefile.return_value.__enter__.return_value
            stream.readline.return_value = b'{"error":"STATUS_UNAVAILABLE"}\n'
            with self.assertRaisesRegex(RuntimeError, 'live status unavailable'):
                runner.live_status(Path('/tmp/live-runtime'))
            client_factory.assert_not_called()


class CapacityProfileCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_not_started_unit_performs_no_systemd_operation(self):
        unit = runner.IsolatedUnit()
        with patch.object(runner, 'unit_command', new=AsyncMock()) as command:
            self.assertTrue((await unit.stop())['passed'])
            command.assert_not_awaited()

    async def test_cleanup_accepts_stopped_unit_only_without_remaining_members(self):
        for remaining in ([], [123]):
            unit = runner.IsolatedUnit()
            unit.process = SimpleNamespace(returncode=0)
            before = {'LoadState': 'loaded', 'ActiveState': 'active', 'ControlGroup': '/test-unit'}
            after = {'LoadState': 'not-found', 'ActiveState': 'inactive'}
            with patch.object(runner, 'unit_state', new=AsyncMock(side_effect=[before, after])), \
                    patch.object(runner, 'unit_command', new=AsyncMock(return_value=(0, ''))) as command, \
                    patch.object(runner, 'remaining_pids', return_value=remaining):
                result = await unit.stop()
            self.assertEqual(result['passed'], not remaining)
            command.assert_awaited_once_with('stop', unit.name)
            self.assertTrue(unit.name.startswith('oneaxe-voice-capacity-'))

    async def test_cleanup_escalates_only_the_generated_unit(self):
        unit = runner.IsolatedUnit()
        unit.process = SimpleNamespace(returncode=0)
        active = {'LoadState': 'loaded', 'ActiveState': 'active', 'ControlGroup': '/test-unit'}
        inactive = {'LoadState': 'loaded', 'ActiveState': 'inactive'}
        with patch.object(runner, 'unit_state', new=AsyncMock(side_effect=[active, active, inactive])), \
                patch.object(runner, 'unit_command', new=AsyncMock(return_value=(0, ''))) as command, \
                patch.object(runner, 'remaining_pids', return_value=[]):
            result = await unit.stop()
        self.assertTrue(result['passed'])
        command.assert_any_await('kill', '--kill-whom=all', '--signal=SIGKILL', unit.name)
        self.assertTrue(all(call.args[-1] == unit.name for call in command.await_args_list))


if __name__ == '__main__':
    unittest.main()
