import argparse
import json
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QLockFile,QTimer,QUrl,qInstallMessageHandler
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtQuick import QQuickWindow
from PySide6 import QtMultimedia

from .bridge import Bridge
from .paths import data_dir,resource_root


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,default=data_dir())
    parser.add_argument('--smoke-output',type=Path)
    parser.add_argument('--gallery-smoke-output',type=Path)
    parser.add_argument('--gallery-smoke-expect-error',action='store_true')
    parser.add_argument('--offline',action='store_true',default=True)
    parser.add_argument('--require-existing-library',action='store_true')
    parser.add_argument('--width',type=int,default=1240)
    parser.add_argument('--height',type=int,default=820)
    args=parser.parse_args()
    # Qt's FFmpeg backend also writes metadata directly to C stderr.
    try:
        silent=os.open(os.devnull,os.O_WRONLY); os.dup2(silent,2); os.close(silent)
    except OSError: pass
    if args.require_existing_library and not (args.data_dir/'library.sqlite3').is_file(): return 4
    args.data_dir.mkdir(parents=True,exist_ok=True)
    lock=QLockFile(str(args.data_dir/'app.lock')); lock.setStaleLockTime(0)
    if not lock.tryLock(100): return 2
    start=time.perf_counter()
    QQuickStyle.setStyle('Basic')
    app=QGuiApplication(sys.argv[:1]); app.setApplicationName('BiliBatchDownloader'); app.setOrganizationName('BiliDesk')
    bridge=Bridge(args.data_dir,offline=args.offline)
    engine=QQmlApplicationEngine()
    engine.rootContext().setContextProperty('bridge',bridge)
    messages=[]
    def qt_message(kind,context,message):
        messages.append(str(message))
    if args.smoke_output:
        qInstallMessageHandler(qt_message)
    engine.load(QUrl.fromLocalFile(str(resource_root()/'resources/Main.qml')))
    if not engine.rootObjects():
        if args.smoke_output:
            args.smoke_output.mkdir(parents=True,exist_ok=False)
            (args.smoke_output/'qml-errors.json').write_text(json.dumps(messages,ensure_ascii=False),encoding='utf-8')
        bridge.shutdown(); return 3
    window=engine.rootObjects()[0]; window.setWidth(args.width); window.setHeight(args.height)
    app.aboutToQuit.connect(bridge.shutdown)
    if args.gallery_smoke_output:
        from .gallery_smoke import install
        install(app,window,bridge,args.gallery_smoke_output,args.gallery_smoke_expect_error,start)
    if args.smoke_output:
        args.smoke_output.mkdir(parents=True,exist_ok=False)
        ready=[]
        def first():
            if bridge.works and not ready: ready.append(round((time.perf_counter()-start)*1000,2))
        bridge.changed.connect(first)
        def capture():
            window.grabWindow().save(str(args.smoke_output/'library.png'))
            if bridge.works:
                bridge.openWork(bridge.works[0]['work_key']); window.setProperty('pageName','detail')
            QTimer.singleShot(500,finish)
        def finish():
            window.grabWindow().save(str(args.smoke_output/'detail.png'))
            report={'startup_query_ready_ms':ready[0] if ready else None,'authors':len(bridge.authors),
              'works':bridge.total,'detail_assets':len(bridge.detail.get('assets',[])),
              'width':window.width(),'height':window.height(),'qt_scale':window.devicePixelRatio(),'online':bridge.online,
              'native_window':'PASS','manual_experience':'NOT_TESTED'}
            report['qt_messages']=messages
            (args.smoke_output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            app.quit()
        QTimer.singleShot(1600,capture)
    result=app.exec(); lock.unlock(); return result


if __name__=='__main__': raise SystemExit(main())
