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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--xvfb',default=shutil.which('Xvfb'))
    parser.add_argument('--display',default=':99')
    parser.add_argument('--mode',default='all',choices=['all','vad','qwen-stream','r2t2'])
    parser.add_argument('--safety',action='store_true')
    parser.add_argument('--pause-check',action='store_true',
                        help='Verify a long pause, continued speech, final F8 flush and caption placement')
    args=parser.parse_args()
    if not args.xvfb:parser.error('Install Xvfb or supply --xvfb /path/to/Xvfb')
    if not os.environ.get('ONEAXE_TEST_PRIVATE_DBUS'):
        # Re-exec with a private bus so menus and notifications stay isolated too.
        return subprocess.call(['dbus-run-session','--',sys.executable,__file__,*sys.argv[1:]],
                               env=dict(os.environ,ONEAXE_TEST_PRIVATE_DBUS='1'))
    root=Path(__file__).resolve().parents[1];work=root/'work';work.mkdir(exist_ok=True)
    runtime=Path(tempfile.mkdtemp(prefix='e2e-',dir=work))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    env=dict(os.environ,DISPLAY=args.display,XDG_SESSION_TYPE='x11',ONEAXE_VOICE_RUNTIME_DIR=str(runtime),
             ONEAXE_VOICE_API_URL=f'http://127.0.0.1:{port}',XDG_CONFIG_HOME=str(runtime/'config'),
             PYTHONPATH=str(root),ONEAXE_VOICE_IDLE_SECONDS='300')
    (runtime/'desktop.json').write_text(json.dumps({'mode':'vad','source':None}))
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


if __name__=='__main__':raise SystemExit(main())
