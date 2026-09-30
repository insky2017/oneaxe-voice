import json
import sys
from pathlib import Path
import tkinter as tk
root=tk.Tk(className='OneAxeVoiceTest')
root.title('OneAxe Voice — input verification')
root.geometry('560x240+100+100')
label=tk.Label(root,text='OneAxe Voice 自动输入验证窗口（临时）')
label.pack()
text=tk.Text(root,font=('Sans',16),wrap='word')
text.pack(expand=True,fill='both')
text.focus_set()
state=Path(sys.argv[1])
def update():
    temporary=state.with_suffix('.tmp')
    temporary.write_text(json.dumps({'window':root.winfo_id(),'text':text.get('1.0','end-1c')},ensure_ascii=False))
    temporary.replace(state)
    root.after(100,update)
update()
root.after(300000,root.destroy)
root.mainloop()
