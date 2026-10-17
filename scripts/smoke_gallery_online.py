"""Exercise actual selection UI and native worker using synthetic inputs only."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run(output,ffmpeg,bundle=None):
    root=Path(__file__).resolve().parents[1];output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();windows=Path(env.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join([str(windows/'System32'),str(windows)])
    for key in ('PYTHONHOME','PYTHONPATH','QT_PLUGIN_PATH','QML2_IMPORT_PATH'):env.pop(key,None)
    if bundle:
        bundle=Path(bundle).resolve();ui=[str(bundle/'BiliBatchDownloader.exe')];worker=[str(bundle/'gallery-worker/BiliGalleryWorker.exe')]
    else:
        env['PYTHONPATH']=str(root/'src');ui=[sys.executable,'-m','bilidesk.app'];worker=[sys.executable,'-m','bilidesk.gallery_worker']
    cases=[(ui+['--data-dir',str(output/'data'),'--offline','--selection-smoke-output',str(output/'ui')],output/'ui/report.json'),
           (worker+['--offline-self-test',str(output/'native'),str(Path(ffmpeg).resolve())],output/'native/report.json')]
    for command,report in cases:
        child=subprocess.run(command,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=50,cwd=output)
        (report.parent/'process.json').write_text(json.dumps({'exit_code':child.returncode,'stdout_bytes':len(child.stdout),'stderr_bytes':len(child.stderr)}),encoding='utf-8')
        assert child.returncode==0 and json.loads(report.read_text(encoding='utf-8'))['status']=='PASS'
    result=dict(status='PASS',frozen=bool(bundle),real_network_requests=0,selection='PASS',native_pipeline='PASS')
    (output/'report.json').write_text(json.dumps(result),encoding='utf-8');return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True,type=Path);p.add_argument('--ffmpeg',required=True,type=Path);p.add_argument('--bundle',type=Path)
    a=p.parse_args();print(json.dumps(run(a.output,a.ffmpeg,a.bundle)))
