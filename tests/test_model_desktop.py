"""Desktop model lifecycle races; HTTP and audio are isolated from the desktop."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from oneaxe_voice.config import Settings
from oneaxe_voice.desktop import Desktop


class ModelDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(runtime_dir=Path(self.temp.name))
        self.settings.token_path.write_text('a' * 40)
        (self.settings.runtime_dir / 'desktop.json').write_text(json.dumps({'mode': 'r2t2'}))
        self.desktop = Desktop(self.settings)
        self.api_state = {'state': 'unloaded', 'model_loaded': False, 'mode': 'r2t2',
                          'busy': False, 'auto_unload': False, 'idle_seconds': 0}
        self.calls = []
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()
        self.busy_unload = False
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.post.side_effect = self.post
        self.client.get.side_effect = lambda *_: httpx.Response(
            200, json=self.api_state, request=httpx.Request('GET', 'http://localhost/status'))
        patcher = patch('oneaxe_voice.desktop.httpx.AsyncClient', return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.desktop.closing = True
        self.proceed.set()
        for task in (self.desktop.prepare_task, self.desktop.unload_task, self.desktop.model_monitor_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.temp.cleanup()

    async def post(self, path, **kwargs):
        self.calls.append((path, kwargs))
        self.entered.set()
        await self.proceed.wait()
        if path.endswith('/prepare'):
            self.api_state.update(state='ready', model_loaded=True, mode=kwargs['json']['mode'])
        elif path.endswith('/unload'):
            if self.busy_unload:
                return httpx.Response(429, json={'detail': '引擎忙碌'})
            self.api_state.update(state='unloaded', model_loaded=False)
        elif path.endswith('/policy'):
            enabled = kwargs['json']['auto_unload']
            self.api_state.update(auto_unload=enabled, idle_seconds=120 if enabled else 0)
        return httpx.Response(200, json=self.api_state)

    async def finish_prepare(self):
        task = self.desktop.prepare_task
        if task:
            await asyncio.wait_for(task, 2)

    async def test_startup_loads_saved_mode_and_monitor_does_not_reload_after_unload(self):
        self.desktop.start_background()
        await self.finish_prepare()
        self.assertEqual(self.calls[0][1]['json']['mode'], 'r2t2')
        await self.desktop.dispatch('model_unload')
        await self.desktop.unload_task
        await asyncio.sleep(1.1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.desktop.status()['model_status']['state'], 'unloaded')
        self.assertIsNone(self.desktop.prepare_task)

    async def test_same_mode_click_reloads_and_repeated_load_shares_task(self):
        self.proceed.clear()
        await self.desktop.dispatch('configure', mode='r2t2')
        original = self.desktop.prepare_task
        await self.entered.wait()
        await self.desktop.dispatch('model_load')
        await self.desktop.dispatch('configure', mode='r2t2')
        self.assertIs(self.desktop.prepare_task, original)
        self.proceed.set()
        await self.finish_prepare()
        self.assertEqual(len(self.calls), 1)
        await self.desktop.dispatch('model_unload')
        await self.desktop.unload_task
        await self.desktop.dispatch('configure', mode='r2t2')
        await self.finish_prepare()
        self.assertTrue(self.desktop.status()['model_status']['model_loaded'])
        self.assertEqual(len(self.calls), 3)

    async def test_fast_selections_load_only_latest_after_inflight_load(self):
        self.proceed.clear()
        await self.desktop.dispatch('configure', mode='vad')
        await self.entered.wait()
        await self.desktop.dispatch('configure', mode='qwen-stream')
        await self.desktop.dispatch('configure', mode='r2t2')
        self.proceed.set()
        await self.finish_prepare()
        self.assertEqual([kwargs['json']['mode'] for _, kwargs in self.calls], ['vad', 'r2t2'])
        self.assertEqual(self.desktop.model_status['mode'], 'r2t2')

    async def test_recording_and_tail_guard_model_controls(self):
        self.desktop.task = object()
        for state in ('recording', 'finishing'):
            self.desktop.state['state'] = state
            for action in ('model_load', 'model_unload'):
                with self.assertRaises(ValueError):
                    await self.desktop.dispatch(action)
        await self.desktop.dispatch('configure', mode='vad')
        self.assertIsNone(self.desktop.prepare_task)
        self.assertFalse(self.calls)
        self.desktop.task = None

    async def test_old_mode_load_failure_still_prepares_latest_selection(self):
        self.proceed.clear()
        async def post(path, **kwargs):
            if kwargs['json']['mode'] == 'vad':
                self.entered.set()
                await self.proceed.wait()
                return httpx.Response(503, json={'detail': 'old model unavailable'})
            return await self.post(path, **kwargs)
        self.client.post.side_effect = post
        await self.desktop.dispatch('configure', mode='vad')
        await self.entered.wait()
        await self.desktop.dispatch('configure', mode='r2t2')
        self.proceed.set()
        await self.finish_prepare()
        self.assertEqual(self.desktop.model_status['mode'], 'r2t2')
        self.assertTrue(self.desktop.model_status['model_loaded'])
        self.assertIsNone(self.desktop.model_error)

    async def test_load_awaits_unload_without_blocking_dispatch(self):
        self.proceed.clear()
        await asyncio.wait_for(self.desktop.dispatch('model_unload'), .2)
        await self.entered.wait()
        unloading = self.desktop.unload_task
        await asyncio.wait_for(self.desktop.dispatch('model_load'), .2)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.desktop.status()['model_unloading'])
        self.proceed.set()
        await unloading
        await self.finish_prepare()
        self.assertEqual([path.rsplit('/', 1)[1] for path, _ in self.calls], ['unload', 'prepare'])
        self.assertTrue(self.desktop.model_status['model_loaded'])

    async def test_warmup_for_f8_waits_for_unload(self):
        self.proceed.clear()
        await self.desktop.dispatch('model_unload')
        await self.entered.wait()
        warmup = asyncio.create_task(self.desktop._warmup(self.client))
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 1)
        self.proceed.set()
        await warmup
        self.assertEqual([path.rsplit('/', 1)[1] for path, _ in self.calls], ['unload', 'warmup'])

    async def test_busy_unload_is_rejected_once_and_does_not_queue_retry(self):
        self.busy_unload = True
        self.api_state.update(state='ready', model_loaded=True)
        await self.desktop.dispatch('model_unload')
        await self.desktop.unload_task
        self.assertEqual(len(self.calls), 1)
        self.assertIn('429', self.desktop.model_error)
        self.assertIsNone(self.desktop.state['last_error'])

    async def test_policy_is_separate_from_desktop_preferences_and_validated(self):
        await self.desktop.dispatch('configure', auto_unload=True)
        self.assertTrue(self.desktop.status()['model_status']['auto_unload'])
        config = json.loads((self.settings.runtime_dir / 'desktop.json').read_text())
        self.assertNotIn('auto_unload', config)
        with self.assertRaises(ValueError):
            await self.desktop.dispatch('configure', auto_unload='true')
        self.assertEqual(len(self.calls), 1)

    async def test_prepare_retries_only_connection_not_uncertain_timeout(self):
        self.client.post.side_effect = [httpx.ConnectError('not ready'), httpx.Response(200, json=self.api_state)]
        await self.desktop.dispatch('model_load')
        await self.finish_prepare()
        self.assertEqual(self.client.post.await_count, 2)
        self.client.post.reset_mock()
        self.client.post.side_effect = httpx.ReadTimeout('uncertain')
        await self.desktop.dispatch('model_load')
        await self.finish_prepare()
        self.assertEqual(self.client.post.await_count, 1)
        self.assertTrue(self.desktop.model_error)

    async def test_policy_response_cannot_overwrite_concurrent_mode_selection(self):
        self.proceed.clear()
        policy = asyncio.create_task(self.desktop.dispatch('configure', auto_unload=True))
        await self.entered.wait()
        await self.desktop.dispatch('configure', mode='vad')
        self.proceed.set()
        await policy
        await self.finish_prepare()
        self.assertEqual(self.desktop.status()['selected_mode'], 'vad')

    async def test_f8_retry_clears_preload_failure_arriving_after_recording_begins(self):
        self.proceed.clear()
        async def post(path, **kwargs):
            if path.endswith('/prepare'):
                self.entered.set()
                await self.proceed.wait()
                return httpx.Response(503, json={'detail': 'temporary preload failure'})
            return httpx.Response(200, json={'model_loaded': True, 'device': 'cuda:0'})
        self.client.post.side_effect = post
        await self.desktop.dispatch('model_load')
        await self.entered.wait()
        warmup = asyncio.create_task(self.desktop._warmup(self.client))
        self.proceed.set()
        result = await warmup
        self.assertTrue(result['model_loaded'])
        self.assertIsNone(self.desktop.model_error)


if __name__ == '__main__':
    unittest.main()
