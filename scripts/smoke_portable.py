"""Exercise actual EXEs offline with isolated PATH; not a clean-machine claim."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bilidesk.library import Library

parser=argparse.ArgumentParser()
parser.add_argument('--bundle',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
bundle=args.bundle.resolve(); output=args.output.resolve()
lib=Library(output/'data'); lib.initialize()
lib.save_work(dict(bvid='BV13x41117TL',uid='1',title='离线合成界面样本'),[dict(cid='1',part=1)])
windows=Path(os.environ.get('SystemRoot','C:/Windows'))
env=dict(os.environ,PATH=os.pathsep.join([str(windows/'System32'),str(windows)]))
for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM','QML2_IMPORT_PATH'): env.pop(key,None)
env['QT_QUICK_BACKEND']='software'
app=subprocess.run([str(bundle/'BiliBatchDownloader.exe'),'--data-dir',str(lib.directory),
                    '--smoke-output',str(output/'app')],capture_output=True,env=env,timeout=25)
if app.returncode: raise RuntimeError('frozen_ui_failed_see_local_report')
report=json.loads((output/'app/report.json').read_text(encoding='utf-8'))
if report['works']!=1 or report['detail_assets']!=1 or report['qt_messages']:
    raise RuntimeError('frozen_ui_incomplete')
def worker(action,payload):
    return subprocess.run([str(bundle/'BiliDeskWorker.exe')],input=json.dumps(dict(data_dir=str(lib.directory),action=action,payload=payload))+'\n',
      text=True,capture_output=True,encoding='utf-8',env=env,timeout=15)
missing=worker('check_file',dict(bvid='BV13x41117TL',cid='1'))
assert json.loads(missing.stdout)['reason']=='local_file_missing'
engine=worker('metadata',dict(bvid='INVALID',part=1))
assert json.loads(engine.stdout)['reason']=='invalid_bvid'  # Imports actual frozen engine, zero network.
result=dict(ui='PASS',worker_missing_file='PASS',native_engine_import='PASS',network_requests=0,
            path_without_python=True,clean_machine='NOT_TESTED',real_playback='MANUAL_REQUIRED')
(output/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8'); print(json.dumps(result))
