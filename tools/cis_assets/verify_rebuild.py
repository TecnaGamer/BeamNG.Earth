"""Rebuild production assets in an isolated temporary directory and compare SHA256 bytes."""
import sys,tempfile,hashlib,json,contextlib,io
from pathlib import Path
sys.dont_write_bytecode=True
import build_atlas,build_modules,validate
from specs import ROOT,OUT,TASKS,KITS

def hashes(folder):return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file() and p.suffix in ('.png','.json','.dae')}
def run(until=None):
    selected=TASKS[:TASKS.index(until)+1] if until else [t for t in TASKS if t=='F1' or t in KITS and (OUT/('bne_cis_'+KITS[t]['kit']+'.cells.json')).exists() or t.startswith('M') and any(m['task']==t for m in json.loads((OUT/'bne_cis_modules.json').read_text())['modules'])]
    # Cleanup is confined to this newly allocated, resolved system-temp child.
    with tempfile.TemporaryDirectory(prefix='bne_cis_verify_') as td:
        target=Path(td).resolve();assert target.parent==Path(tempfile.gettempdir()).resolve() and target.name.startswith('bne_cis_verify_')
        for mod in (build_atlas,build_modules,validate):mod.OUT=target
        for task in selected:
            with contextlib.redirect_stdout(io.StringIO()):
                if task!='F1':(build_atlas.build if task.startswith('A') else build_modules.build)(task)
                validate.run(task)
            print('Clean rebuild PASS '+task,flush=True)
        expected=hashes(OUT);actual=hashes(target);diff=[n for n in sorted(set(expected)|set(actual)) if expected.get(n)!=actual.get(n)]
        result=dict(pass_all=not diff,files=len(actual),different=diff,sha256=actual)
        path=ROOT/'assets/cis_src/rebuild_verification.json';path.write_text(json.dumps(result,indent=2,sort_keys=True)+'\n',encoding='utf-8')
        if diff:raise RuntimeError('Nonreproducible: '+', '.join(diff))
        print(f'PASS: {len(actual)} production files byte-identical to a clean rebuild')
if __name__=='__main__':run(sys.argv[1] if len(sys.argv)>1 else None)
