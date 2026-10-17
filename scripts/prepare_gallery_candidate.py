"""Consistent SQLite copy into a new offline candidate directory. Never migrate the source."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bilidesk.library import Library
from bilidesk.gallery import scan_report,import_report
from bilidesk.media import digest


def prepare(source,destination,reports):
    source=Path(source).resolve(); destination=Path(destination).resolve()
    if not source.is_file() or destination.exists(): raise ValueError('source_missing_or_destination_exists')
    before=digest(source); destination.mkdir(parents=True,exist_ok=False)
    lib=Library(destination)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as old,sqlite3.connect(lib.path) as new:
        old.backup(new)
    lib.initialize()
    lib.set_setting('offline','1'); lib.set_setting('paused','1')
    with lib.connection() as db:
        db.execute("UPDATE tasks SET state='interrupted',error='candidate_offline_copy',phase='候选库保留，等待手动重试' WHERE state IN ('queued','running')")
    imported=[]
    for report in reports:
        scan=scan_report(lib,Path(report).resolve()); imported.append(import_report(lib,scan['path'],scan['report_sha256']))
    snap=lib.snapshot()
    receipt=dict(status='PASS',source_sha_before=before,source_sha_after=digest(source),
        source_unchanged=before==digest(source),authors=len(snap['authors']),works=snap['total'],imports=imported,offline=True)
    (destination/'preparation-receipt.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
    return receipt


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--source',required=True,type=Path); p.add_argument('--destination',required=True,type=Path)
    p.add_argument('--report',action='append',default=[],type=Path); a=p.parse_args()
    print(json.dumps(prepare(a.source,a.destination,a.report)))
