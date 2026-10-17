"""Parent supervision cannot retry a timed out or failed inspection worker."""
import io
import json
from pathlib import Path
import subprocess

import pytest

from experiments import gallery_metadata_probe as probe


@pytest.mark.parametrize('variant', ['success','timeout','invalid','oversize','failed','assign'])
def test_parent_single_process_job_cleanup_and_no_retry(monkeypatch, variant):
    calls=[]
    class Process:
        pid=12
        returncode=1 if variant=='failed' else 0
        def __init__(self,*args,**kwargs): calls.append(('spawn',args,kwargs))
        def communicate(self,data,timeout):
            calls.append(('input',json.loads(data),timeout))
            if variant=='timeout': self.returncode=None;raise subprocess.TimeoutExpired('private',timeout)
            if variant=='invalid':return b'private exception',None
            if variant=='oversize':return b'x'*(1024*1024+1),None
            return json.dumps({'type':'result','result':{'diagnostic':'COMPLETED','budget':{'network_requests':2}}}).encode(),None
        def poll(self):return self.returncode
        def kill(self): calls.append(('kill',));self.returncode=-1
        def wait(self):calls.append(('wait',))
    class Job:
        def assign(self,pid):
            calls.append(('assign',pid))
            if variant=='assign':raise RuntimeError('private job error')
        def close(self):calls.append(('close',))
    monkeypatch.setattr(probe.subprocess,'Popen',Process)
    monkeypatch.setattr(probe,'Job',Job)
    result=probe.run_worker(Path('worker.exe'),{'profile':'private'})
    assert len([c for c in calls if c[0]=='spawn'])==1
    assert ('close',) in calls and 'private' not in json.dumps(result)
    if variant=='success': assert result['diagnostic']=='COMPLETED'
    else: assert result['diagnostic']=='NOT_COMPLETED' and result['budget'] is None
    if variant=='timeout':assert ('kill',) in calls and ('wait',) in calls
    if variant!='assign':assert next(c for c in calls if c[0]=='input')[2]==60


def test_registration_fingerprint_rejects_history_change(monkeypatch,tmp_path):
    monkeypatch.setattr(probe,'digest',lambda path:'changed')
    with pytest.raises(ValueError,match='historical_fingerprint_changed'):
        probe.fingerprints(tmp_path/'worker.exe',None)


def test_execute_checks_fingerprints_before_child_or_execution_marker(monkeypatch,tmp_path):
    from types import SimpleNamespace
    root=tmp_path;out=root/'.artifacts/run';out.mkdir(parents=True)
    worker=root/'worker.exe';worker.touch();db=root/'db';db.touch()
    (out/'registration.json').write_text('{"fingerprints":{"old":true}}')
    monkeypatch.setattr(probe,'ROOT',root)
    monkeypatch.setattr(probe,'Library',lambda p:SimpleNamespace(path=db))
    monkeypatch.setattr(probe,'fingerprints',lambda *a:{'new':True})
    monkeypatch.setattr(probe,'load_baseline',lambda *a:{})
    monkeypatch.setattr(probe.sys,'argv',['probe','--output',str(out),'--worker',str(worker),'--source-library',str(root),'--execute'])
    with pytest.raises(ValueError,match='registered_fingerprint_changed'):probe.main()
    assert not (out/'execution-started.json').exists()
