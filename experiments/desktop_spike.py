"""Offline feasibility check: pinned Qt/QML, SQLite snapshots and worker IPC."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time


def run():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    app = QGuiApplication([])
    engine = QQmlApplicationEngine()
    engine.loadData(b'import QtQuick; import QtQuick.Controls; ApplicationWindow { visible: true; width: 960; height: 640; Label { text: "offline" } }')
    app.processEvents()
    assert engine.rootObjects()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'spike.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table works(id integer primary key, uid text, title text)')
            db.execute('create index authors on works(uid,id)')
            db.executemany('insert into works values(?,?,?)', [(i,str(i%50),f'作品 {i}') for i in range(5000)])
            db.commit()
        durations = []
        for _ in range(100):
            start = time.perf_counter()
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as db:
                assert len(db.execute('select * from works where uid=? order by id limit 40', ('1',)).fetchall()) == 40
            durations.append((time.perf_counter()-start)*1000)
    child = subprocess.run([sys.executable, '-c', 'import sys,json; print(json.dumps({"ok":json.loads(sys.stdin.readline())["run"]}))'],
                           input='{"run":true}\n', text=True, capture_output=True, timeout=10)
    assert json.loads(child.stdout)['ok']
    return {'qt_qml': 'PASS', 'sqlite_5000_p95_ms': round(sorted(durations)[94],3),
            'worker_jsonl': 'PASS', 'network_requests': 0, 'scope': 'offline_library_feasibility_only'}


if __name__ == '__main__':
    result = run()
    destination = Path(sys.argv[1])
    with destination.open('x', encoding='utf-8') as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
