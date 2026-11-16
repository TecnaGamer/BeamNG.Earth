"""Render all assembled building kits and changed material galleries."""
import sys,subprocess
from pathlib import Path
sys.dont_write_bytecode=True
from specs import ROOT,REND
for task in sys.argv[1:] or ['A1','A2','A3','A4','A5','M1','M2','M4']:
    print('Rendering '+task,flush=True)
    with (REND/(task+'_render.log')).open('w',encoding='utf-8') as log:
        subprocess.run(['D:/blender-4.5/blender.exe','--background','--threads','8','--python','tools/cis_assets/preview.py','--',task],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    text=(REND/(task+'_render.log')).read_text(encoding='utf-8')
    if 'Traceback' in text or 'Saved:' not in text:raise RuntimeError(text[-3000:])
    print('Rendered '+task,flush=True)
