"""Compare Qwen wait policies in a disposable service; no production mutations."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import time
import traceback

import httpx

from tests.wait_profile_support import (
    ROOT, IsolatedUnit, cpu_snapshot, cpu_difference, guard, live_status, unit_state,
)
from tests.e2e_qwen_wait import run_case
from tests.e2e_concurrent import gpu_query


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--policy', required=True, choices=('default', 'passive'),
                        help='Expected worker policy supplied by the source checkout')
    parser.add_argument('--audio', required=True, type=Path)
    parser.add_argument('--live-runtime', required=True, type=Path)
    parser.add_argument('--seconds', type=int, default=120)
    parser.add_argument('--flush-seconds', type=int, default=40)
    parser.add_argument('--check-cancel', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', args.label):
        parser.error('label must be a simple filename identifier')
    if args.seconds < 40 or not 0 < args.flush_seconds < args.seconds:
        parser.error('use at least 40 seconds and a positive shorter flush interval')
    args.audio = args.audio.expanduser().resolve()
    args.live_runtime = args.live_runtime.expanduser().resolve()
    return args


async def main(args):
    base = ROOT / 'work/qwen-passive'
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = base / (args.label + '.json')
    if output.exists():
        raise RuntimeError('refusing to overwrite a prior run')
    runtime = base / (args.label + '-runtime')
    if runtime.is_relative_to(args.live_runtime) or args.live_runtime.is_relative_to(runtime):
        raise RuntimeError('runtime must be isolated from production')
    runtime.mkdir(mode=0o700)
    token = secrets.token_urlsafe(32)
    descriptor = os.open(runtime / 'client.token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(token + '\n')
    url = 'http://127.0.0.1:18099'
    evidence = {'schema_version': 1, 'label': args.label, 'passed': False,
                'mode': 'qwen-stream', 'policy': args.policy, 'seconds': args.seconds,
                'flush_seconds': args.flush_seconds, 'started_at': time.time(),
                'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
                    for name in ('oneaxe_voice/engines.py', 'oneaxe_voice/stream_worker.py',
                                 'tests/e2e_qwen_wait.py', 'tests/run_qwen_wait_profile.py',
                                 'tests/wait_profile_support.py')}}
    unit = IsolatedUnit()
    watcher = operation = None
    log = (base / (args.label + '-api.log')).open('wb')
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    def cancel_once():
        if not current.cancelling():
            current.cancel()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancel_once)
    try:
        assert (await asyncio.to_thread(live_status, args.live_runtime))['idle'], 'production is busy'
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', 18099))
        env = {'ONEAXE_VOICE_RUNTIME_DIR': str(runtime), 'ONEAXE_VOICE_API_URL': url,
               'ONEAXE_VOICE_IDLE_SECONDS': '0', 'HF_HUB_OFFLINE': '1',
               'TRANSFORMERS_OFFLINE': '1', 'VLLM_NO_USAGE_STATS': '1'}
        for key in ('ONEAXE_VOICE_MODEL_DIR', 'ONEAXE_VOICE_STREAM_PYTHON',
                    'ONEAXE_VOICE_CUDA_DEVICE', 'CUDA_HOME', 'LD_LIBRARY_PATH'):
            if key in os.environ:
                env[key] = os.environ[key]
        command = ['/usr/bin/env']
        for key in ('OMP_WAIT_POLICY', 'OMP_NUM_THREADS', 'GOMP_SPINCOUNT', 'MKL_NUM_THREADS', 'KMP_BLOCKTIME'):
            command += ['-u', key]
        # Each checkout supplies its own worker policy. Keep the API parent clean
        # so the candidate uses the same child-only setting as the deployment.
        command += [str(ROOT / '.venv/bin/python'), '-m', 'oneaxe_voice.serve']
        launch = asyncio.create_task(unit.start(command, env, log))
        try:
            api = await asyncio.shield(launch)
        except asyncio.CancelledError:
            await asyncio.gather(launch, return_exceptions=True)
            raise
        state = await unit_state(unit.name)
        api_pid, group = int(state['MainPID']), state['ControlGroup']
        evidence['api_pid'] = api_pid
        watcher = asyncio.create_task(guard(args.live_runtime))

        async def experiment():
            async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=300,
                    headers={'Authorization': 'Bearer ' + token}) as client:
                for _ in range(100):
                    assert api.returncode is None, 'isolated API exited'
                    try:
                        r = await client.get('/api/dictation/status', timeout=2)
                        r.raise_for_status()
                        break
                    except (httpx.ConnectError, httpx.ReadTimeout):
                        await asyncio.sleep(.2)
                else:
                    raise RuntimeError('isolated API startup timeout')
                print(json.dumps({'label': args.label, 'event': 'loading'}), flush=True)
                r = await client.post('/api/dictation/prepare', json={'mode': 'qwen-stream'})
                r.raise_for_status()
                ready = r.json()
                assert ready['state'] == 'ready' and ready['device'] == 'cuda:0'
                evidence['ready'] = {k: ready.get(k) for k in
                    ('state', 'mode', 'device', 'worker_pid', 'model_generation')}
                before = cpu_snapshot(group, api_pid, ready['worker_pid'])
                worker = next(p for p in before['processes'] if p['role'] == 'worker')
                api_env = next(p for p in before['processes'] if p['role'] == 'api')['thread_environment']
                assert not any(k in api_env for k in ('OMP_WAIT_POLICY', 'OMP_NUM_THREADS',
                    'GOMP_SPINCOUNT', 'MKL_NUM_THREADS', 'KMP_BLOCKTIME')), 'API wait environment not clean'
                expected = 'PASSIVE' if args.policy == 'passive' else None
                assert worker['thread_environment'].get('OMP_WAIT_POLICY') == expected, 'worker policy mismatch'
                assert worker['thread_environment'].get('OMP_NUM_THREADS') == '4'
                gpu = await gpu_query(['pid', 'used_gpu_memory'], 'compute-apps')
                owned = {p['pid'] for p in before['processes']}
                evidence['gpu_processes_at_ready'] = [
                    {'pid': int(row['pid']), 'memory_mib': float(row['used_gpu_memory'])}
                    for row in gpu if int(row['pid']) in owned]
                assert any(row['pid'] == ready['worker_pid'] for row in evidence['gpu_processes_at_ready'])
                before = cpu_snapshot(group, api_pid, ready['worker_pid'])
                print(json.dumps({'label': args.label, 'event': 'case_started', 'seconds': args.seconds}), flush=True)
                evidence['case'] = await run_case(url, token, args.audio,
                    seconds=args.seconds, flush_seconds=args.flush_seconds)
                after = cpu_snapshot(group, api_pid, ready['worker_pid'])
                evidence['cpu'] = cpu_difference(before, after)
                evidence['cpu']['window'] = 'real_clock_protocol_case_excluding_load_and_warmup'
                assert evidence['case']['passed'], 'Qwen protocol case failed'
                assert evidence['cpu']['valid'], 'invalid CPU measurement'
                if args.check_cancel:
                    print(json.dumps({'label': args.label, 'event': 'cancel_started'}), flush=True)
                    evidence['cancel'] = await run_case(url, token, args.audio,
                        seconds=8, flush_seconds=40, cancel_after=8)
                    assert evidence['cancel']['passed'], 'Qwen cancel case failed'
                    evidence['after_cancel'] = await run_case(url, token, args.audio,
                        seconds=12, flush_seconds=40)
                    assert evidence['after_cancel']['passed'], 'new session after cancel failed'
                for _ in range(30):
                    final = (await client.get('/api/dictation/status')).json()
                    if not final['busy']:
                        break
                    await asyncio.sleep(.1)
                assert not final['busy'] and not final['active_sessions']
                assert final['worker_pid'] == ready['worker_pid'] and final['model_generation'] == ready['model_generation']
                evidence['worker_preserved'] = evidence['model_preserved'] = True

        operation = asyncio.create_task(experiment())
        done, _ = await asyncio.wait([operation, watcher], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        assert operation in done
        evidence['passed'] = True
    except BaseException as exc:
        evidence['failure_kind'] = type(exc).__name__
        evidence['failure_frames'] = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                                     for f in traceback.extract_tb(exc.__traceback__)]
    finally:
        # Only the first signal cancels work; repeated signals must not interrupt
        # cgroup cleanup, token removal or failure evidence.
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: None)
        for task in (operation, watcher):
            if task and not task.done():
                task.cancel()
        try:
            evidence['unit_cleanup'] = await unit.stop()
        except Exception as exc:
            evidence['unit_cleanup'] = {'passed': False, 'failure_kind': type(exc).__name__}
        await asyncio.gather(*(t for t in (operation, watcher) if t), return_exceptions=True)
        evidence['passed'] = evidence['passed'] and evidence['unit_cleanup']['passed']
        worker_log = runtime / 'stream-worker.log'
        if worker_log.exists():
            shutil.copyfile(worker_log, base / (args.label + '-worker.log'))
        (runtime / 'client.token').unlink(missing_ok=True)
        log.close()
        evidence['finished_at'] = time.time()
        output.write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'label': args.label, 'event': 'completed', 'passed': evidence['passed'],
                          'failure_kind': evidence.get('failure_kind'),
                          'mean_cpu_cores': evidence.get('cpu', {}).get('mean_cores')}), flush=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return evidence['passed']


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main(parse_args())) else 1)
