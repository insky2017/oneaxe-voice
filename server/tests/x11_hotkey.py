"""Isolated X11 F8 binding for E2E; never edits GNOME or starts user services."""
import ctypes as C,ctypes.util,os,socket,json
lib=C.CDLL(ctypes.util.find_library('X11'))
lib.XOpenDisplay.argtypes=[C.c_char_p];lib.XOpenDisplay.restype=C.c_void_p
lib.XDefaultRootWindow.argtypes=[C.c_void_p];lib.XDefaultRootWindow.restype=C.c_ulong
lib.XKeysymToKeycode.argtypes=[C.c_void_p,C.c_ulong];lib.XKeysymToKeycode.restype=C.c_uint
lib.XGrabKey.argtypes=[C.c_void_p,C.c_int,C.c_uint,C.c_ulong,C.c_int,C.c_int,C.c_int]
lib.XNextEvent.argtypes=[C.c_void_p,C.c_void_p]
lib.XSync.argtypes=[C.c_void_p,C.c_int]
d=lib.XOpenDisplay(None);assert d
root=lib.XDefaultRootWindow(d);key=lib.XKeysymToKeycode(d,0xffc5)
for modifiers in (0,2,16,18):lib.XGrabKey(d,key,modifiers,root,False,1,1)
lib.XSync(d,False)
event=(C.c_long*24)()
while True:
    lib.XNextEvent(d,C.byref(event))
    if C.cast(event,C.POINTER(C.c_int))[0]!=2:continue
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(os.environ['ONEAXE_VOICE_RUNTIME_DIR']+'/desktop.sock')
        s.sendall(b'{"action":"toggle"}\n');s.recv(65536)
