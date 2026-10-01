"""Geometry and drag checks for the caption in the private Xvfb desktop."""

import json
import re
import subprocess
import time

from oneaxe_voice.paste import current_target


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=10).stdout.strip()


def wait_for(predicate, seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=predicate()
        if result:return result
        time.sleep(.1)
    raise AssertionError('caption did not reach expected state')


def preview_window():
    found=subprocess.run(['xdotool','search','--onlyvisible','--class','OneAxeVoicePreview'],
                         capture_output=True,text=True)
    return found.stdout.splitlines()[-1] if found.returncode==0 and found.stdout.strip() else None


def geometry(window):
    info=run('xwininfo','-id',window)
    fields={name:int(value) for name,value in re.findall(
        r'^\s*(Absolute upper-left X|Absolute upper-left Y|Width|Height):\s*(\d+)\s*$',info,re.M)}
    return {'x':fields['Absolute upper-left X'],'y':fields['Absolute upper-left Y'],
            'width':fields['Width'],'height':fields['Height']}


def caption_geometry(target_window):
    overlay=wait_for(preview_window)
    bounds=geometry(overlay)
    screen_w,screen_h=map(int,run('xdotool','getdisplaygeometry').split())
    assert 0<bounds['width']<min(screen_w,900),'caption has a fixed/full-screen width'
    assert 0<bounds['height']<=130,'caption exceeds two lines'
    assert abs(bounds['x']+bounds['width']/2-screen_w/2)<=90,'caption is not centered on target monitor'
    assert 0<=bounds['y']<min(screen_h/4,160),'caption obscures the lower input area'
    assert current_target().window==target_window,'caption stole input focus'
    return bounds


def menu_click(owner,label):
    menu='/org/ayatana/NotificationItem/oneaxe_voice/Menu'
    layout=run('gdbus','call','--session','--dest',owner,'--object-path',menu,
               '--method','com.canonical.dbusmenu.GetLayout','--','0','-1','[]')
    match=re.search(r"\((\d+), \{[^}]*'label': <'"+re.escape(label)+r"'>",layout)
    assert match,f'caption menu item missing: {label}'
    run('gdbus','call','--session','--dest',owner,'--object-path',menu,
        '--method','com.canonical.dbusmenu.Event',match.group(1),'clicked','<0>','0')


def verify_position_persistence(settings,menu_owner,target_window):
    config=settings.runtime_dir/'desktop.json'
    menu_click(menu_owner,'调整字幕位置')
    try:
        overlay=wait_for(preview_window)
        before=geometry(overlay)
        start_x=before['x']+before['width']//2
        start_y=before['y']+before['height']//2
        destination=(max(120,min(900,start_x-210)),max(190,min(470,start_y+200)))
        run('xdotool','mousemove',str(start_x),str(start_y))
        run('xdotool','mousedown','1')
        run('xdotool','mousemove','--sync',str(destination[0]),str(destination[1]))
        run('xdotool','mouseup','1')
        saved=wait_for(lambda: json.loads(config.read_text()) if
                       json.loads(config.read_text()).get('preview_position')=='custom' else None)
        anchor=saved.get('preview_anchor')
        assert isinstance(anchor,list) and len(anchor)==2 and all(0<=v<=1 for v in anchor),anchor
        moved=geometry(overlay)
        assert abs(moved['x']-before['x'])>80 or abs(moved['y']-before['y'])>80,'caption did not move'
        assert current_target().window==target_window,'adjusting caption stole target focus'
    finally:
        menu_click(menu_owner,'调整字幕位置')
    menu_click(menu_owner,'调整字幕位置')
    try:
        restored=geometry(wait_for(preview_window))
        assert abs(restored['x']-moved['x'])<=8 and abs(restored['y']-moved['y'])<=8,\
            'caption did not restore its custom position'
        assert current_target().window==target_window,'restored caption stole target focus'
    finally:
        menu_click(menu_owner,'调整字幕位置')
    return True
