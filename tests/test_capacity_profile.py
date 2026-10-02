"""Pure CPU checks; all systemd commands are mocked."""

from contextlib import redirect_stderr
import io
from pathlib import Path
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
