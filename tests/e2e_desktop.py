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


def verify_pause(settings,status,state_file,mode,expected_count):
    """Check what the target received while F8 capture is still running."""
    content=json.loads(state_file.read_text())['text']
    saved=(settings.runtime_dir/'last-transcript.txt').read_text()
    assert status['state']=='recording' and status['capture_active'],'capture ended during a pause'
    assert status['mode']==mode
    assert content and content.count('大家好')==expected_count,'pause did not deliver the known phrase once'
    assert content==saved,'saved transcript differs from target at pause'
    assert content==status.get('committed_text'),'UI committed text differs from target at pause'
    assert not status.get('pending_text'),'unconfirmed tail remained after pause'
    assert status.get('delivery_state')=='pasted','pause text was not reported as pasted'
    return {'text':content,'length':len(content)}


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
    parser.add_argument('--pause-check',action='store_true')
    args=parser.parse_args()
    if args.pause_check and args.repeats!=2:
        parser.error('--pause-check requires two passes of the known sample')
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
    pause_seconds=34 if args.pause_check else 0
    pcm=sample+b'\0'*(pause_seconds*32000)+sample if args.pause_check else (sample+b'\0'*96000)*(args.repeats-1)+sample
    module=None;target=None;writer=None;abort=threading.Event();errors=[]
    results=[]
    fifo=runtime/'stream-e2e.pipe'
    test_state=runtime/'stream-target.json'
    first_audio_done=threading.Event()
    first_audio_done_at=[None]
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
                    if args.pause_check and not first_audio_done.is_set() and pos+640>=len(sample):
                        first_audio_done_at[0]=time.monotonic()
                        first_audio_done.set()
            finally:os.close(fd)
        except Exception as exc:errors.append(type(exc).__name__)
    try:
        module=run('pactl','load-module','module-pipe-source','source_name=oneaxe_voice_stream_e2e',
                   'file='+str(fifo),'format=s16le','rate=16000','channels=1',
                   'source_properties=device.description=OneAxeVoiceStreamE2E')
        for mode in MODES if args.mode=='all' else [args.mode]:
            first_audio_done.clear();first_audio_done_at[0]=None
            options=json.loads(original)
            options.update(source='oneaxe_voice_stream_e2e',mode=mode,max_session_seconds=300,
                           clipboard_only=False,preview=True,preview_position='top',preview_anchor=[.5,.08])
            config.write_text(json.dumps(options))
            menu_owner=menu_select(mode)
            def selected_ready():
                state = control(settings, 'status')
                model = state.get('model_status', {})
                return (not state['preparing'] and model.get('mode') == mode
                        and model.get('model_loaded') and not model.get('busy'))
            # Exercise the actual DBus menu activation, including the selected item.
            wait_for(selected_ready,250)
            assert control(settings,'status')['last_error'] is None,control(settings,'status')
            assert control(settings,'status').get('model_error') is None
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
            pause_first=None;pause_last=None;caption_geometry=None
            while writer.is_alive():
                status=control(settings,'ui')
                content=json.loads(test_state.read_text())['text']
                if content and first is None:
                    first={'at_seconds':round(time.monotonic()-started,3),'capture_active':status['capture_active']}
                previews |= bool(status.get('preview'))
                if previews and not visible_caption:
                    visible_caption = subprocess.run(['xdotool','search','--onlyvisible','--class','OneAxeVoicePreview'],capture_output=True).returncode==0
                if args.pause_check and visible_caption and caption_geometry is None:
                    from e2e_preview import caption_geometry as inspect_caption
                    caption_geometry=inspect_caption(window)
                actual=current_target()
                if actual.window!=window and first_focus_change is None:
                    first_focus_change={'seconds':round(time.monotonic()-started,3),
                                        'window':actual.window,'wm_class':actual.wm_class,
                                        'expected':window,'recorded':status.get('target_window')}
                max_queue=max(max_queue,status.get('queued_segments',0))
                assert status['state']!='error',status
                if args.pause_check and first_audio_done.is_set():
                    elapsed=time.monotonic()-first_audio_done_at[0]
                    if elapsed>=8 and pause_first is None:
                        pause_first=verify_pause(settings,control(settings,'ui'),test_state,mode,expected_count=1)
                    if elapsed>=29 and pause_last is None:
                        pause_last=verify_pause(settings,control(settings,'ui'),test_state,mode,expected_count=1)
                        assert pause_first is not None
                        assert pause_last['text']==pause_first['text'],'long silence changed delivered text'
                time.sleep(.1)
            writer.join(2);assert not errors,errors
            if args.pause_check:
                assert pause_first is not None and pause_last is not None,'pause checkpoints were not reached'
                if mode in {'qwen-stream','r2t2'}:
                    wait_for(lambda: json.loads(test_state.read_text())['text'].count('大家好')==2,20)
                    assert control(settings,'status')['capture_active'],'capture stopped before F8'
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
            if args.pause_check:
                assert caption_geometry is not None,'caption geometry was not inspected'
            result={'mode':mode,'first_output':first,'stop_to_completion_seconds':round(time.monotonic()-stopped,3),
                    'status':status,'text_matches':True,'known_sample_count':args.repeats,'preview_seen':previews,'caption_visible':visible_caption,
                    'max_queued_packets':max_queue,'menu_owner':menu_owner,'text':content}
            if args.pause_check:
                result.update(pause_seconds=pause_seconds,pause_committed=True,pause_stable=True,
                              caption_geometry=caption_geometry)
            if args.safety and mode=='r2t2':
                from e2e_controls import verify_controls
                if args.pause_check:
                    # The menu/resume test needs new speech within its own wait window.
                    pcm=sample*3
                verify_controls(settings,window,test_state,feed,errors,menu_owner,evidence_dir,abort)
            if args.pause_check:
                from e2e_preview import verify_position_persistence
                result['position_persisted']=verify_position_persistence(settings,menu_owner,window)
            results.append(result)
            (evidence_dir/'stream-e2e.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            print(json.dumps({key:value for key,value in result.items() if key!='text'},ensure_ascii=False),flush=True)
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
