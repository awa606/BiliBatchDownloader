"""Explicit local setup from the authorized research reports, never part of a release."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bilidesk.library import Library
from bilidesk.media import import_reports
from bilidesk.paths import data_dir

parser=argparse.ArgumentParser()
parser.add_argument('--source',type=Path,required=True)
parser.add_argument('--preview',type=Path,required=True)
parser.add_argument('--data-dir',type=Path,default=data_dir())
parser.add_argument('--profile',default='')
parser.add_argument('--ffmpeg',default='')
args=parser.parse_args()
lib=Library(args.data_dir); lib.initialize()
result=import_reports(lib,args.source,args.preview)
for key in ('profile','ffmpeg'):
    if getattr(args,key): lib.set_setting(key,getattr(args,key))
print(json.dumps(result))
