"""Isolated wait-policy profiling support, reused from the capacity runner."""
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import socket
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[1]
CGROUP_ROOT = Path('/sys/fs/cgroup')


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
            if (state.get('LoadState') == 'loaded' and state.get('ActiveState') == 'active'
                    and int(state.get('MainPID', '0')) > 0 and state.get('ControlGroup')):
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
