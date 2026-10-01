"""Run under dbus-run-session; Xvfb/xfwm4 keep test keys off the real desktop."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx


def validate_reuse(parser,args):
    if bool(args.reuse_api_url)!=bool(args.api_token_file):
        parser.error('--reuse-api-url and --api-token-file must be supplied together')
    if not args.reuse_api_url:return
    if args.mode!='r2t2':
        parser.error('Reusing an existing API requires --mode r2t2')
    try:
        url=urlsplit(args.reuse_api_url)
        port=url.port
    except ValueError:
        parser.error('Reuse requires a valid isolated localhost HTTP URL')
    if (url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost','::1'}
            or url.username or url.password or url.query or url.fragment or url.path not in {'','/'}
            or port in {None,8097}):
        parser.error('Reuse requires an explicit isolated localhost HTTP port other than 8097')
    if not args.api_token_file.is_file():
        parser.error('API token file does not exist')


def existing_api_status(url,token):
    with httpx.Client(base_url=url,trust_env=False,headers={'Authorization':'Bearer '+token}) as client:
        response=client.get('/api/dictation/status')
        response.raise_for_status()
        status=response.json()
    assert status['model_loaded'] and status['mode']=='r2t2' and not status['busy'], 'isolated R2T2 API must be ready and idle'
    assert status.get('model_generation') and status.get('worker_pid'), 'isolated API has no loaded worker identity'
    return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--xvfb',default=shutil.which('Xvfb'))
    parser.add_argument('--display',default=':99')
    parser.add_argument('--mode',default='all',choices=['all','vad','qwen-stream','r2t2'])
    parser.add_argument('--safety',action='store_true')
    parser.add_argument('--pause-check',action='store_true',
                        help='Verify a long pause, continued speech, final F8 flush and caption placement')
    parser.add_argument('--reuse-api-url',help='Reuse an already loaded isolated localhost R2T2 API')
    parser.add_argument('--api-token-file',type=Path,help='Private PC token belonging to the reused API')
    args=parser.parse_args()
    validate_reuse(parser,args)
    if not args.xvfb:parser.error('Install Xvfb or supply --xvfb /path/to/Xvfb')
    if not os.environ.get('ONEAXE_TEST_PRIVATE_DBUS'):
        # Re-exec with a private bus so menus and notifications stay isolated too.
        return subprocess.call(['dbus-run-session','--',sys.executable,__file__,*sys.argv[1:]],
                               env=dict(os.environ,ONEAXE_TEST_PRIVATE_DBUS='1'))
    root=Path(__file__).resolve().parents[1];work=root/'work';work.mkdir(exist_ok=True)
    runtime=Path(tempfile.mkdtemp(prefix='e2e-',dir=work))
    existing=None
    if args.reuse_api_url:
        token=args.api_token_file.read_text().strip()
        assert token,'API token file is empty'
        existing=existing_api_status(args.reuse_api_url,token)
        (runtime/'client.token').write_text(token+'\n')
        (runtime/'client.token').chmod(0o600)
        api_url=args.reuse_api_url.rstrip('/')
    else:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
        api_url=f'http://127.0.0.1:{port}'
    env=dict(os.environ,DISPLAY=args.display,XDG_SESSION_TYPE='x11',ONEAXE_VOICE_RUNTIME_DIR=str(runtime),
             ONEAXE_VOICE_API_URL=api_url,XDG_CONFIG_HOME=str(runtime/'config'),
             XDG_CACHE_HOME=str(runtime/'cache'),XDG_STATE_HOME=str(runtime/'state'),
             ONEAXE_TEST_EVIDENCE_DIR=str(runtime/'evidence'),
             PYTHONPATH=str(root),ONEAXE_VOICE_IDLE_SECONDS='0')
    if existing:
        env.update(ONEAXE_TEST_REUSED_API='1',
                   ONEAXE_TEST_MODEL_GENERATION=str(existing['model_generation']),
                   ONEAXE_TEST_WORKER_PID=str(existing['worker_pid']))
    else:
        for key in ('ONEAXE_TEST_REUSED_API','ONEAXE_TEST_MODEL_GENERATION','ONEAXE_TEST_WORKER_PID'):
            env.pop(key,None)
    (runtime/'desktop.json').write_text(json.dumps({'mode':'vad' if args.mode=='all' else args.mode,'source':None}))
    procs=[];logs=[]
    def start(name,arguments):
        log=(runtime/(name+'.log')).open('w');logs.append(log)
        proc=subprocess.Popen(arguments,env=env,cwd=root,stdout=log,stderr=log,start_new_session=True)
        procs.append(proc);return proc
    try:
        xserver=start('xvfb',[args.xvfb,args.display,'-screen','0','1280x800x24','-nolisten','tcp','-ac'])
        time.sleep(1)
        assert xserver.poll() is None,'Xvfb failed; choose an unused --display'
        start('xfwm',['xfwm4','--replace','--compositor=off'])
        if not existing:
            subprocess.run([sys.executable,'-m','oneaxe_voice.cli','init'],env=env,cwd=root,check=True)
            start('api',[sys.executable,'-m','uvicorn','oneaxe_voice.server:create_app','--factory',
                         '--host','127.0.0.1','--port',str(port),'--no-access-log','--no-proxy-headers'])
        start('desktop',[sys.executable,'-m','oneaxe_voice.cli','desktop-run'])
        start('hotkey',['/usr/bin/python3',str(root/'tests/x11_hotkey.py')])
        tray=start('tray',['/usr/bin/python3','-m','oneaxe_voice.tray'])
        env['ONEAXE_TEST_TRAY_PID']=str(tray.pid)
        time.sleep(3)
        assert all(proc.poll() is None for proc in procs),'test component failed to start'
        command=[sys.executable,'tests/e2e_desktop.py','--audio',str(args.audio.resolve()),'--mode',args.mode]
        if args.safety:command.append('--safety')
        if args.pause_check:command.append('--pause-check')
        return subprocess.call(command,env=env,cwd=root)
    finally:
        for proc in reversed(procs):
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        for log in logs:log.close()
        print('Isolated test diagnostics:',runtime,flush=True)
        if existing:
            after=existing_api_status(api_url,token)
            assert (after['model_generation'],after['worker_pid'])==(existing['model_generation'],existing['worker_pid']), 'reused API model or worker changed'


if __name__=='__main__':raise SystemExit(main())
