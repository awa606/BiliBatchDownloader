"""Synthetic product-query benchmark. Does not touch the user's library."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from bilidesk.library import Library

parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args(); args.output.mkdir(parents=True,exist_ok=False)
lib=Library(args.output/'data'); lib.initialize()
with lib.connection() as db:
    db.executemany('INSERT INTO authors(uid,name) VALUES(?,?)',[(str(i+1),'合成作者 '+str(i+1)) for i in range(50)])
    db.executemany('INSERT INTO works(bvid,uid,title,description,tags,updated) VALUES(?,?,?,?,?,?)',
      [('BV'+str(i).zfill(10),str(i%50+1),'合成视频 '+str(i),'离线性能样本，不含真实媒体','["合成"]',time.time()) for i in range(5000)])
    db.executemany('INSERT INTO assets(bvid,cid,part) VALUES(?,?,?)',[('BV'+str(i).zfill(10),str(i+1),1) for i in range(5000)])
timings=[]
for i in range(100):
    start=time.perf_counter()
    result=lib.snapshot(search='合成' if i%2 else '',uid=str(i%50+1) if i%3 else '')
    assert result['works']
    timings.append((time.perf_counter()-start)*1000)
report=dict(works=5000,queries=100,p95_ms=round(sorted(timings)[94],3),maximum_ms=round(max(timings),3),network_requests=0)
(args.output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))
