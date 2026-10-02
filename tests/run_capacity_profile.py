"""Run capacity stages in a disposable user systemd service on port 18098.

Supply --live-runtime with the production desktop.sock directory. Its status is
read only; recording resumes or unavailable status stop the experiment. Work
and runtime directories must be separate from that live directory. The isolated
runtime and manifests must already contain their own test credentials.
"""

import argparse
import asyncio
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://127.0.0.1:18098'
CGROUP_ROOT = Path('/sys/fs/cgroup')
NAME_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}')
ENV_ALLOWLIST = frozenset({
    'ONEAXE_VOICE_MODEL_DIR', 'ONEAXE_VOICE_R2T2_MODEL_DIR',
    'ONEAXE_VOICE_STREAM_PYTHON', 'ONEAXE_VOICE_CUDA_DEVICE',
    'ONEAXE_VOICE_MEMORY_FRACTION', 'ONEAXE_VOICE_STREAM_CUDAGRAPH_CAPTURE_SIZES',
    'CUDA_DEVICE_ORDER', 'CUDA_HOME', 'LD_LIBRARY_PATH',
})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--capacity', type=int, required=True)
    parser.add_argument('--kv-gib', type=float, required=True)
    parser.add_argument('--live-runtime', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, default=ROOT / 'work/capacity')
    parser.add_argument('--runtime-dir', type=Path, default=ROOT / 'runtime/capacity')
    parser.add_argument('--omp-wait-policy', choices=('default', 'passive'), default='default')
    parser.add_argument('--feature-device', choices=('cpu', 'cuda:0'), default='cpu')
    parser.add_argument('--check-cancel', action='store_true',
                        help='After performance stages, cancel mobile while PC continues for 24 seconds')
    parser.add_argument('stages', nargs='+', help='manifest name and duration, e.g. pair-ab:120')
    args = parser.parse_args(argv)
    if not NAME_PATTERN.fullmatch(args.label):
        parser.error('--label must be a simple filename identifier')
    if not 2 <= args.capacity <= 16:
        parser.error('--capacity must be in [2, 16]')
    if not math.isfinite(args.kv_gib) or args.kv_gib < .5:
        parser.error('--kv-gib must be finite and at least 0.5')
    for stage in args.stages:
        fields = stage.split(':')
        try:
            valid = (len(fields) == 2 and NAME_PATTERN.fullmatch(fields[0])
                     and math.isfinite(float(fields[1])) and float(fields[1]) > 0)
        except ValueError:
            valid = False
        if not valid:
            parser.error('stages must contain a simple manifest name and positive duration')
    for name in ('live_runtime', 'work_dir', 'runtime_dir'):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    if (args.runtime_dir.is_relative_to(args.live_runtime)
            or args.live_runtime.is_relative_to(args.runtime_dir)
            or args.work_dir.is_relative_to(args.live_runtime)):
        parser.error('experiment directories must be separate from --live-runtime')
    return args


def experiment_environment(args, environ=None):
    environ = os.environ if environ is None else environ
    env = {key: environ[key] for key in ENV_ALLOWLIST if key in environ}
    env.update(ONEAXE_VOICE_RUNTIME_DIR=str(args.runtime_dir),
               ONEAXE_VOICE_API_URL=URL, ONEAXE_VOICE_IDLE_SECONDS='0',
               ONEAXE_VOICE_STREAM_MAX_NUM_SEQS=str(args.capacity),
               ONEAXE_VOICE_STREAM_KV_CACHE_BYTES=str(int(args.kv_gib * 1024**3)),
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', VLLM_NO_USAGE_STATS='1')
    return env


def experiment_command(args):
    # systemd also inherits its manager environment, independently of our allowlist.
    # Unset waiting overrides before libgomp is imported; default is not ACTIVE.
    command = ['/usr/bin/env']
    for key in ('OMP_WAIT_POLICY', 'GOMP_SPINCOUNT', 'MKL_NUM_THREADS', 'KMP_BLOCKTIME',
                'ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE', 'ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE'):
        command.extend(('-u', key))
    if args.omp_wait_policy == 'passive':
        command.append('OMP_WAIT_POLICY=PASSIVE')
    command.append('ONEAXE_VOICE_EXPERIMENT_FEATURE_DEVICE=' + args.feature_device)
    command.append('ONEAXE_VOICE_EXPERIMENT_FEATURE_PROBE=' + ('1' if args.feature_device == 'cuda:0' else '0'))
    return command + [str(ROOT / '.venv/bin/python'), '-m', 'oneaxe_voice.serve']


def cpu_snapshot(group, api_pid, worker_pid):
    """Read only the experiment cgroup; never serialize arbitrary process env."""
    root = CGROUP_ROOT / group.lstrip('/')
    counters = dict(line.split() for line in (root / 'cpu.stat').read_text().splitlines())
    sample = {'monotonic': time.monotonic(),
              'cpu_usec': {k: int(counters[k]) for k in ('usage_usec', 'user_usec', 'system_usec')},
              'processes': []}
    keys = {'OMP_WAIT_POLICY', 'GOMP_SPINCOUNT', 'MKL_NUM_THREADS', 'KMP_BLOCKTIME',
            'OMP_NUM_THREADS', 'TOKENIZERS_PARALLELISM'}
    for pid in remaining_pids(group):
        proc = Path('/proc') / str(pid)
        try:
            fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            name = (proc / 'comm').read_text().strip()
            env = {}
            for entry in (proc / 'environ').read_bytes().split(b'\0'):
                key, sep, value = entry.partition(b'=')
                if sep and key.decode(errors='replace') in keys:
                    env[key.decode()] = value.decode(errors='replace')
            role = ('api' if pid == api_pid else 'worker' if pid == worker_pid else
                    'engine' if name.startswith('VLLM::Engine') else 'auxiliary')
            sample['processes'].append({'pid': pid, 'role': role, 'start_ticks': int(fields[19]),
                'cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK'),
                'thread_environment': env})
        except (FileNotFoundError, ProcessLookupError):
            continue
    return sample


def cpu_difference(before, after):
    wall = after['monotonic'] - before['monotonic']
    seconds = {k.removesuffix('_usec') + '_seconds':
               (after['cpu_usec'][k] - before['cpu_usec'][k]) / 1e6 for k in before['cpu_usec']}
    prior = {(r['pid'], r['start_ticks']): r for r in before['processes']}
    roles = {}
    core_roles = {'api', 'worker', 'engine'}
    identities = lambda sample: {(r['pid'], r['start_ticks'], r['role'])
                                 for r in sample['processes'] if r['role'] in core_roles}
    core_preserved = (identities(before) == identities(after)
                      and {r['role'] for r in before['processes']} >= core_roles)
    for row in after['processes']:
        old = prior.get((row['pid'], row['start_ticks']))
        if old is not None:
            role = row['role']
            roles[role] = roles.get(role, 0) + row['cpu_seconds'] - old['cpu_seconds']
    return {'valid': wall > 0 and all(v >= 0 for v in seconds.values()) and core_preserved,
            'core_processes_preserved': core_preserved,
            'scope': 'isolated_service_cgroup_including_all_descendants',
            'window': 'benchmark_process_start_to_exit_excluding_model_load_and_warmup',
            'wall_seconds': wall, **seconds,
            'mean_cores': seconds['usage_seconds'] / wall if wall > 0 else None,
            'matched_process_cpu_seconds_by_role': roles,
            'before': before, 'after': after}


def cancellation_command(manifest, output):
    rows = json.loads(manifest.read_text())['streams']
    if len(rows) != 2 or {r['role'] for r in rows} != {'pc', 'mobile'}:
        raise RuntimeError('cancellation check requires one PC and one mobile')
    command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'tests/e2e_concurrent.py'),
               '--url', URL, '--seconds', '24', '--cancel-mobile-after', '8',
               '--output', str(output)]
    for row in rows:
        role = row['role']
        if row.get('offset', 0) != 0 or not row.get('keywords'):
            raise RuntimeError('cancellation check requires zero offsets and distinct keywords')
        command.extend(('--' + role + '-token-file', str((manifest.parent / row['token_file']).resolve()),
                        '--' + role + '-audio', str((manifest.parent / row['audio_file']).resolve())))
        command.extend('--' + role + '-keyword=' + keyword for keyword in row['keywords'])
    return command


def live_status(runtime):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(2)
        conn.connect(str(runtime / 'desktop.sock'))
        conn.sendall(b'{"action":"status"}\n')
        with conn.makefile('rb') as stream:
            value = json.loads(stream.readline(65536))
    if 'error' in value:
        raise RuntimeError('live status unavailable')
    busy = (value.get('state') != 'idle' or value.get('capture_active') or
            value.get('warming') or value.get('preparing') or value.get('model_unloading') or
            value.get('model_status', {}).get('busy'))
    token = (runtime / 'client.token').read_text().strip()
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.get('http://127.0.0.1:8097/api/dictation/status',
                              headers={'Authorization': 'Bearer ' + token})
        response.raise_for_status()
        model = response.json()
    busy = (busy or model.get('state') != 'ready' or model.get('busy') is not False
            or model.get('active_sessions') != [] or model.get('model_unloading'))
    return {'idle': not bool(busy), 'state': value.get('state')}


async def unit_command(*args, timeout=12):
    process = await asyncio.create_subprocess_exec(
        'systemctl', '--user', '--no-pager', *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        return process.returncode, output.decode()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def unit_state(unit):
    code, output = await unit_command('show', unit,
        '--property=LoadState,ActiveState,SubState,ControlGroup,MainPID')
    state = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if not state or (code and state.get('LoadState') != 'not-found'):
        raise RuntimeError('isolated unit status unavailable')
    return state


def remaining_pids(group):
    if not group:
        return []
    members = CGROUP_ROOT / group.lstrip('/') / 'cgroup.procs'
    with suppress(FileNotFoundError):
        return [int(value) for value in members.read_text().split()]
    return []


class IsolatedUnit:
    def __init__(self):
        self.name = 'oneaxe-voice-capacity-' + uuid.uuid4().hex + '.service'
        self.process = None

    async def start(self, command, env, output):
        self.process = await asyncio.create_subprocess_exec(
            'systemd-run', '--user', '--unit=' + self.name, '--collect', '--wait', '--pipe',
            '--property=KillMode=control-group', '--property=TimeoutStopSec=5',
            '--working-directory=' + str(ROOT),
            *('--setenv=' + key + '=' + value for key, value in env.items()), *command,
            cwd=ROOT, stdout=output, stderr=asyncio.subprocess.STDOUT)
        # Settle registration before cleanup can race the transient-unit creation.
        for _ in range(100):
            state = await unit_state(self.name)
            if state.get('LoadState') == 'loaded':
                return self.process
            if self.process.returncode is not None:
                raise RuntimeError('isolated unit startup failed')
            await asyncio.sleep(.1)
        raise RuntimeError('isolated unit registration timeout')

    async def stop(self):
        if self.process is None:
            return {'unit': self.name, 'passed': True, 'state': 'not-started',
                    'remaining_owned_pids': []}
        before = await unit_state(self.name)
        group = before.get('ControlGroup')
        try:
            await unit_command('stop', self.name)
        except asyncio.TimeoutError:
            await unit_command('kill', '--kill-whom=all', '--signal=SIGKILL', self.name)
            await unit_command('stop', self.name)
        after = await unit_state(self.name)
        if after.get('LoadState') != 'not-found' and after.get('ActiveState') != 'inactive':
            await unit_command('kill', '--kill-whom=all', '--signal=SIGKILL', self.name)
            await unit_command('stop', self.name)
            after = await unit_state(self.name)
        remaining = remaining_pids(group)
        await stop(self.process, 5)
        stopped = after.get('LoadState') == 'not-found' or after.get('ActiveState') == 'inactive'
        return {'unit': self.name, 'passed': stopped and not remaining,
                'state': after, 'remaining_owned_pids': remaining}


async def guard(live_runtime):
    while True:
        state = await asyncio.to_thread(live_status, live_runtime)
        if not state['idle']:
            raise RuntimeError('live dictation resumed; isolated experiment stopped')
        await asyncio.sleep(.5)


async def stop(process, seconds=20):
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def main(args):
    base, runtime = args.work_dir, args.runtime_dir
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    logpath = base / f'{args.label}-api.log'
    evidence = {'label': args.label, 'capacity': args.capacity, 'kv_gib': args.kv_gib,
                'omp_wait_policy': args.omp_wait_policy,
                'feature_device': args.feature_device,
                'started_at': time.time(), 'stages': [], 'passed': False}
    api = bench = watcher = operation = None
    api_log = None
    isolated = IsolatedUnit()
    evidence['api_unit'] = isolated.name
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    cancellation_requested = False

    def cancel_once():
        nonlocal cancellation_requested
        if not cancellation_requested:
            cancellation_requested = True
            current.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancel_once)
    try:
        token = (runtime / 'client.token').read_text().strip()
        if not (await asyncio.to_thread(live_status, args.live_runtime))['idle']:
            raise RuntimeError('live dictation is active before experiment')
        probe = socket.socket()
        try:
            # Match the server's reuse semantics after the preceding isolated
            # run; TIME_WAIT is not a live listener occupying the port.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', 18098))
        finally:
            probe.close()
        env = experiment_environment(args)
        api_log = logpath.open('wb')
        launch = asyncio.create_task(isolated.start(
            experiment_command(args), env, api_log))
        try:
            api = await asyncio.shield(launch)
        except asyncio.CancelledError:
            await asyncio.gather(launch, return_exceptions=True)
            raise
        evidence['api_launcher_pid'] = api.pid
        evidence['api_pid'] = int((await unit_state(isolated.name)).get('MainPID', '0'))
        watcher = asyncio.create_task(guard(args.live_runtime))

        async def experiment():
            nonlocal bench
            async with httpx.AsyncClient(base_url=URL, trust_env=False, timeout=300,
                    headers={'Authorization': 'Bearer ' + token}) as client:
                for _ in range(100):
                    if api.returncode is not None:
                        raise RuntimeError('isolated API exited')
                    try:
                        response = await client.get('/api/dictation/status', timeout=2)
                        response.raise_for_status()
                        break
                    except (httpx.ConnectError, httpx.ReadTimeout):
                        await asyncio.sleep(.2)
                else:
                    raise RuntimeError('isolated API startup timeout')
                print(json.dumps({'phase': args.label, 'event': 'loading', 'capacity': args.capacity}), flush=True)
                response = await client.post('/api/dictation/prepare', json={'mode': 'r2t2'})
                response.raise_for_status()
                before = response.json()
                assert before['state'] == 'ready' and before['device'] == 'cuda:0'
                assert before['max_sessions'] == args.capacity
                evidence['ready'] = {k: before.get(k) for k in ('state', 'device', 'worker_pid',
                    'model_generation', 'max_sessions', 'engine_config', 'warmup')}
                print(json.dumps({'phase': args.label, 'event': 'ready',
                    'capacity': before['max_sessions'], 'worker_pid': before['worker_pid']}), flush=True)
                for stage in args.stages:
                    manifest, seconds = stage.split(':')
                    name = f'{args.label}-{manifest}-{seconds}s'
                    result_path = base / f'{name}.json'
                    group = (await unit_state(isolated.name))['ControlGroup']
                    cpu_before = cpu_snapshot(group, evidence['api_pid'], before['worker_pid'])
                    with (base / f'{name}.log').open('wb') as output:
                        bench = await asyncio.create_subprocess_exec(str(ROOT / '.venv/bin/python'),
                            str(ROOT / 'tests/e2e_capacity.py'), '--url', URL,
                            '--manifest', str(base / f'{manifest}.json'),
                            '--seconds', seconds, '--output', str(result_path),
                            cwd=ROOT, stdout=output, stderr=asyncio.subprocess.STDOUT)
                        print(json.dumps({'phase': args.label, 'event': 'stage_started', 'stage': stage}), flush=True)
                        code = await bench.wait()
                    cpu = cpu_difference(cpu_before, cpu_snapshot(
                        group, evidence['api_pid'], before['worker_pid']))
                    result = json.loads(result_path.read_text()) if result_path.exists() else {}
                    evidence['stages'].append({'name': name, 'exit_code': code,
                        'passed': result.get('passed', False), 'output': str(result_path), 'cpu': cpu})
                    print(json.dumps({'phase': args.label, 'event': 'stage_finished', 'stage': stage,
                        'exit_code': code, 'passed': result.get('passed')}), flush=True)
                    if code != 0 or not result.get('passed') or not result.get('measurement_valid') or not cpu['valid']:
                        raise RuntimeError('benchmark failed: ' + name)
                if args.check_cancel:
                    result_path = base / f'{args.label}-cancel.json'
                    command = cancellation_command(base / f'{args.stages[0].split(":")[0]}.json', result_path)
                    with (base / f'{args.label}-cancel.log').open('wb') as output:
                        bench = await asyncio.create_subprocess_exec(*command, cwd=ROOT,
                            stdout=output, stderr=asyncio.subprocess.STDOUT)
                        print(json.dumps({'phase': args.label, 'event': 'cancel_check_started'}), flush=True)
                        code = await bench.wait()
                    result = json.loads(result_path.read_text()) if result_path.exists() else {}
                    evidence['cancel_check'] = {'exit_code': code, 'passed': result.get('passed', False),
                                                'output': str(result_path)}
                    if code != 0 or not result.get('passed'):
                        raise RuntimeError('mobile cancellation isolation check failed')
                final = (await client.get('/api/dictation/status')).json()
                assert (not final['busy'] and final['worker_pid'] == before['worker_pid']
                        and final['model_generation'] == before['model_generation'])
                evidence['worker_preserved'] = True
        operation = asyncio.create_task(experiment())
        done, _ = await asyncio.wait([watcher, operation], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        assert operation in done
        evidence['passed'] = True
    except BaseException as exc:
        evidence['failure_kind'] = type(exc).__name__
        if isinstance(exc, OSError):
            evidence['failure_errno'] = exc.errno
        # Only locally defined errors and status codes; no response/token/body.
        evidence['failure_reason'] = str(exc) if isinstance(exc, (RuntimeError, AssertionError)) else type(exc).__name__
    finally:
        cancellation_requested = True
        if operation and not operation.done():
            operation.cancel()
        if watcher:
            watcher.cancel()
        try:
            evidence['unit_cleanup'] = await isolated.stop()
        except Exception as exc:
            evidence['unit_cleanup'] = {'unit': isolated.name, 'passed': False,
                                        'failure_kind': type(exc).__name__}
        await stop(bench)
        if operation:
            await asyncio.gather(operation, return_exceptions=True)
        if watcher:
            await asyncio.gather(watcher, return_exceptions=True)
        evidence['remaining_owned_pids'] = evidence['unit_cleanup'].get('remaining_owned_pids')
        if not evidence['unit_cleanup']['passed']:
            evidence['passed'] = False
            evidence['cleanup_failure'] = True
        worker_log = runtime / 'stream-worker.log'
        if worker_log.exists():
            shutil.copyfile(worker_log, base / f'{args.label}-worker.log')
        if api_log:
            api_log.close()
        evidence['finished_at'] = time.time()
        (base / f'{args.label}-profile.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps({'phase': args.label, 'event': 'stopped', 'passed': evidence['passed'],
                          'failure': evidence.get('failure_reason')}), flush=True)
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main(parse_args())))
