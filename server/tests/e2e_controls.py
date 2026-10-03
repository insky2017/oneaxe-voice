"""Real R2T2/X11 regressions for menus, next-round mode selection and cancel."""
import json
import subprocess
import sys
import threading
import time

import httpx

from oneaxe_voice.config import ROOT
from oneaxe_voice.desktop import control
from oneaxe_voice.paste import current_target
from e2e_desktop import menu_select, run, wait_for


def verify_controls(settings,window,state_file,feed,errors,menu_owner,evidence_dir,abort):
    menu='/org/ayatana/NotificationItem/oneaxe_voice/Menu'
    def event(kind):
        run('gdbus','call','--session','--dest',menu_owner,'--object-path',menu,
            '--method','com.canonical.dbusmenu.Event','0',kind,'<0>','0')
    def text():return json.loads(state_file.read_text())['text']
    headers={'Authorization':'Bearer '+settings.token_path.read_text().strip()}
    def api():
        with httpx.Client(base_url=settings.api_url,headers=headers,trust_env=False) as client:
            return client.get('/api/dictation/status').json()
    before=text();other=None;writer=None
    try:
        run('xdotool','key','F8')
        wait_for(lambda:control(settings,'status')['state']=='recording')
        writer=threading.Thread(target=feed);writer.start()
        wait_for(lambda:text()!=before,15)
        event('opened')
        wait_for(lambda:control(settings,'status')['menu_paused'])
        time.sleep(.4);paused_text=text()
        time.sleep(1)
        assert text()==paused_text,'menu did not pause paste'
        menu_select('vad')
        wait_for(lambda:control(settings,'status')['selected_mode']=='vad')
        assert control(settings,'status')['mode']=='r2t2','mode changed within a recording'
        assert api()['mode']=='r2t2'
        event('closed')
        wait_for(lambda:not control(settings,'status')['menu_paused'])
        wait_for(lambda:text()!=paused_text,12)
        assert current_target().window==window,'caption/menu stole focus'
        other_file=evidence_dir/'stream-other-target.json'
        other=subprocess.Popen([sys.executable,str(ROOT/'tests/input_target.py'),str(other_file)])
        def other_window():
            found=subprocess.run(['xdotool','search','--onlyvisible','--class','OneAxeVoiceTest'],
                                 capture_output=True,text=True,timeout=10)
            if other.poll() is not None:
                raise AssertionError(f'second Tk input target exited early ({other.returncode})')
            if found.returncode==1 and not found.stderr.strip():
                return None
            found.check_returncode()
            ids=found.stdout.splitlines()
            alternate=next((item for item in ids if item!=window),None)
            if alternate:
                assert window in ids,'original input target disappeared before focus test'
            return alternate
        alternate=wait_for(other_window)
        run('xdotool','windowactivate','--sync',alternate)
        wait_for(lambda:control(settings,'status').get('paste_paused'),15)
        time.sleep(.4)
        assert json.loads(other_file.read_text())['text']=='','text pasted into changed window'
        preserved=text()
        worker_pid=api()['worker_pid']
        group={int(pid) for pid,pgid in (line.split() for line in run('ps','-eo','pid=,pgid=').splitlines())
               if int(pgid)==worker_pid}
        control(settings,'cancel')
        abort.set();writer.join(3)
        assert not writer.is_alive() and not errors,errors
        wait_for(lambda:not api()['busy'] and not api()['model_loaded'],15)
        release_started=time.monotonic()
        deadline=release_started+15
        while True:
            gpu_pids={int(pid) for pid in run('nvidia-smi','--query-compute-apps=pid','--format=csv,noheader').splitlines()}
            remaining=group.intersection(gpu_pids)
            if not remaining or time.monotonic()>=deadline:
                break
            time.sleep(.2)
        gpu_release_seconds=round(time.monotonic()-release_started,3)
        assert not remaining,f'cancel left CUDA process IDs {sorted(remaining)} after 15s'
        time.sleep(.5)
        assert text()==preserved,'new text pasted after cancel'
        assert control(settings,'status')['last_action']=='cancelled'
        assert control(settings,'status')['queued_segments']==0
        assert json.loads(other_file.read_text())['text']==''
        result={'menu_protocol':'DBusMenu opened/closed/clicked','paste_paused_and_resumed':True,
                'mode_deferred':True,'caption_kept_focus':True,'changed_window_protected':True,
                'cancel_stopped_paste':True,'worker_reaped':True,'cuda_children_released':True,
                'worker_group_pids':sorted(group),'gpu_release_seconds':gpu_release_seconds}
        (evidence_dir/'stream-controls-e2e.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result),flush=True)
    finally:
        abort.set()
        event('closed')
        control(settings,'cancel')
        if writer:writer.join(25)
        assert not errors,errors
        if other:other.terminate();other.wait(timeout=3)
        run('xdotool','windowactivate','--sync',window)
