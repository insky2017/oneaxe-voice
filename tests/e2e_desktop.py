"""Opt-in real F8 / PulseAudio / GPU / X11 test. Run from the repository root.

The supplied WAV must be a known, consented 16 kHz mono PCM16 sample. Uses a
separate input window, restores configuration/clipboard/results in finally.
Never imports or calls VPlus and never targets a terminal or sends Enter.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import wave

from oneaxe_voice.config import ROOT, Settings
from oneaxe_voice.desktop import control
from oneaxe_voice.modes import MODES
from oneaxe_voice.paste import current_target


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10, **kwargs).stdout.strip()


def wait_for(predicate, seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=predicate()
        if value:return value
        time.sleep(.1)
    raise AssertionError('timed out waiting for test condition')


def menu_select(mode):
    # Use the same exported DBusMenu Event protocol as Ubuntu AppIndicator.
    if os.getenv('ONEAXE_TEST_TRAY_PID'):
        names=run('gdbus','call','--session','--dest','org.freedesktop.DBus',
                  '--object-path','/org/freedesktop/DBus','--method','org.freedesktop.DBus.ListNames')
        owner=None
        for name in re.findall(r"'(:[0-9.]+)'",names):
            try:
                pid=run('gdbus','call','--session','--dest','org.freedesktop.DBus',
                        '--object-path','/org/freedesktop/DBus',
                        '--method','org.freedesktop.DBus.GetConnectionUnixProcessID',name)
            except subprocess.CalledProcessError:
                continue
            if re.search(r'uint32 '+os.environ['ONEAXE_TEST_TRAY_PID']+r'\b',pid):
                owner=name;break
        assert owner,'test tray did not register on private DBus'
    else:
        listed=run('gdbus','call','--session','--dest','org.kde.StatusNotifierWatcher',
                   '--object-path','/StatusNotifierWatcher','--method','org.freedesktop.DBus.Properties.Get',
                   'org.kde.StatusNotifierWatcher','RegisteredStatusNotifierItems')
        owner=re.search(r"(:[0-9.]+)@/org/ayatana/NotificationItem/oneaxe_voice",listed).group(1)
    menu='/org/ayatana/NotificationItem/oneaxe_voice/Menu'
    layout=run('gdbus','call','--session','--dest',owner,'--object-path',menu,
               '--method','com.canonical.dbusmenu.GetLayout','--','0','-1','[]')
    item=re.search(r"\((\d+), \{[^}]*'label': <'"+re.escape(MODES[mode])+r"'>",layout).group(1)
    run('gdbus','call','--session','--dest',owner,'--object-path',menu,
        '--method','com.canonical.dbusmenu.Event',item,'clicked','<0>','0')
    return owner


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--mode',choices=[*MODES,'all'],default='all')
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--safety',action='store_true')
    args=parser.parse_args()
    settings=Settings.from_env();runtime=settings.runtime_dir
    evidence_dir=ROOT/'work';evidence_dir.mkdir(exist_ok=True)
    config=runtime/'desktop.json';original=config.read_bytes()
    saved={name:(runtime/name).read_bytes() if (runtime/name).exists() else None for name in ('last-transcript.txt','last-session.json')}
    clipboard=subprocess.run(['xclip','-selection','clipboard','-out'],capture_output=True,timeout=3)
    original_window=subprocess.run(['xdotool','getactivewindow'],capture_output=True,text=True).stdout.strip()
    original_pointer=run('xdotool','getmouselocation','--shell')
    assert control(settings,'status')['state'] in ('idle','error')
    with wave.open(str(args.audio),'rb') as audio:
        assert (audio.getframerate(),audio.getnchannels(),audio.getsampwidth())==(16000,1,2)
        sample=audio.readframes(audio.getnframes())
    pcm=(sample+b'\0'*96000)*(args.repeats-1)+sample
    module=None;target=None;writer=None;abort=threading.Event();errors=[]
    results=[]
    fifo=runtime/'stream-e2e.pipe'
    test_state=evidence_dir/'stream-target.json'
    def feed():
        try:
            fd=os.open(fifo,os.O_WRONLY|os.O_NONBLOCK)
            try:
                start=time.monotonic()
                for pos in range(0,len(pcm),640):
                    if abort.is_set():break
                    block=memoryview(pcm[pos:pos+640])
                    while block and not abort.is_set():
                        try:block=block[os.write(fd,block):]
                        except BlockingIOError:abort.wait(.01)
                    time.sleep(max(0,start+(pos+640)/32000-time.monotonic()))
            finally:os.close(fd)
        except Exception as exc:errors.append(type(exc).__name__)
    try:
        module=run('pactl','load-module','module-pipe-source','source_name=oneaxe_voice_stream_e2e',
                   'file='+str(fifo),'format=s16le','rate=16000','channels=1',
                   'source_properties=device.description=OneAxeVoiceStreamE2E')
        for mode in MODES if args.mode=='all' else [args.mode]:
            options=json.loads(original)
            options.update(source='oneaxe_voice_stream_e2e',mode=mode,max_session_seconds=300,clipboard_only=False,preview=True)
            config.write_text(json.dumps(options))
            menu_owner=menu_select(mode)
            # Selecting an already active item may not emit a toggle; request prepare
            # explicitly so the test measures warm streaming rather than cold load.
            control(settings,'configure',mode=mode)
            wait_for(lambda:not control(settings,'status')['preparing'],250)
            assert control(settings,'status')['last_error'] is None,control(settings,'status')
            target=subprocess.Popen([sys.executable,str(ROOT/'tests/input_target.py'),str(test_state)])
            window=run('xdotool','search','--sync','--onlyvisible','--class','OneAxeVoiceTest').splitlines()[-1]
            run('xdotool','windowactivate','--sync',window)
            run('xdotool','mousemove','--window',window,'180','100','click','1')
            time.sleep(.3)
            assert current_target().window==window
            started=time.monotonic();run('xdotool','key','F8')
            wait_for(lambda:control(settings,'status')['state']=='recording')
            time.sleep(.4)
            writer=threading.Thread(target=feed);writer.start()
            first=None;previews=False;visible_caption=False;max_queue=0;first_focus_change=None
            while writer.is_alive():
                status=control(settings,'ui')
                content=json.loads(test_state.read_text())['text']
                if content and first is None:
                    first={'at_seconds':round(time.monotonic()-started,3),'capture_active':status['capture_active']}
                previews |= bool(status.get('preview'))
                if previews and not visible_caption:
                    visible_caption = subprocess.run(['xdotool','search','--onlyvisible','--class','OneAxeVoicePreview'],capture_output=True).returncode==0
                actual=current_target()
                if actual.window!=window and first_focus_change is None:
                    first_focus_change={'seconds':round(time.monotonic()-started,3),
                                        'window':actual.window,'wm_class':actual.wm_class,
                                        'expected':window,'recorded':status.get('target_window')}
                max_queue=max(max_queue,status.get('queued_segments',0))
                assert status['state']!='error',status
                time.sleep(.1)
            writer.join(2);assert not errors,errors
            assert current_target().window==window,first_focus_change
            stopped=time.monotonic();run('xdotool','key','F8')
            wait_for(lambda:control(settings,'status')['state'] in ('idle','error'),90)
            time.sleep(.4)
            status=control(settings,'status');audit=json.loads((runtime/'last-session.json').read_text())
            content=json.loads(test_state.read_text())['text']
            assert status['state']=='idle',status
            assert first and first['capture_active'],first
            assert status['device']=='cuda:0',status
            assert status['segments_pasted']>0 and not status.get('paste_paused'),status
            assert content==audit['text'],'target text differs from saved result'
            assert content.count('大家好')==args.repeats,'known sample count/order mismatch'
            assert previews,'no live preview received'
            assert visible_caption,'caption window never appeared'
            result={'mode':mode,'first_output':first,'stop_to_completion_seconds':round(time.monotonic()-stopped,3),
                    'status':status,'text_matches':True,'known_sample_count':args.repeats,'preview_seen':previews,'caption_visible':visible_caption,
                    'max_queued_packets':max_queue,'menu_owner':menu_owner,'text':content}
            results.append(result)
            (evidence_dir/'stream-e2e.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps({key:value for key,value in result.items() if key!='text'},ensure_ascii=False),flush=True)
            if args.safety and mode=='r2t2':
                from e2e_controls import verify_controls
                verify_controls(settings,window,test_state,feed,errors,menu_owner,evidence_dir,abort)
            target.terminate();target.wait(timeout=3);target=None
    finally:
        abort.set()
        if writer:writer.join(2)
        control(settings,'cancel')
        config.write_bytes(original)
        if target:target.terminate();target.wait(timeout=3)
        if module:subprocess.run(['pactl','unload-module',module],check=False)
        fifo.unlink(missing_ok=True)
        for name,data in saved.items():
            if data is None:(runtime/name).unlink(missing_ok=True)
            else:(runtime/name).write_bytes(data)
        if clipboard.returncode==0:
            subprocess.run(['xclip','-selection','clipboard','-in'],input=clipboard.stdout,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        if original_window:
            subprocess.run(['xdotool','windowactivate',original_window],check=False)
        coords=dict(line.split('=',1) for line in original_pointer.splitlines())
        subprocess.run(['xdotool','mousemove',coords['X'],coords['Y']],check=False)

if __name__=='__main__':main()
