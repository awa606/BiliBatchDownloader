"""Actual read-only selection/copy and second-resolution dates; synthetic only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run(output,bundle=None):
    root=Path(__file__).resolve().parents[1];output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();windows=Path(env.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join([str(windows/'System32'),str(windows)])
    for key in ('PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH','QML2_IMPORT_PATH'):env.pop(key,None)
    env['QT_QPA_PLATFORM']='offscreen'
    if bundle:command=[str(Path(bundle).resolve()/'BiliBatchDownloader.exe')]
    else:
        env['PYTHONPATH']=str(root/'src');command=[sys.executable,'-m','bilidesk.app']
    child=subprocess.run(command+['--offline','--data-dir',str(output/'data'),'--text-smoke-output',str(output/'ui')],
        cwd=output,env=env,capture_output=True,timeout=30)
    (output/'process.json').write_text(json.dumps(dict(exit_code=child.returncode,stderr_bytes=len(child.stderr))),encoding='utf-8')
    report=json.loads((output/'ui/report.json').read_text(encoding='utf-8'))
    assert child.returncode==0 and report['status']=='PASS',report
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--bundle',type=Path)
    args=parser.parse_args();print(json.dumps(run(args.output,args.bundle)))
