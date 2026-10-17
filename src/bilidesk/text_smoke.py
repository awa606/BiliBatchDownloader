"""Check the actual three UI pages with synthetic plain text; never dispatch work."""
from datetime import datetime
import json
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,QMetaObject,Q_ARG,Qt,QPointF
from PySide6.QtTest import QTest


def install(app,window,bridge,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    bridge.timer.stop()
    # Offscreen has a private clipboard; never replace the user's OS clipboard.
    if app.platformName()!='offscreen': raise ValueError('private_test_clipboard_required')
    clipboard=app.clipboard()
    body='第一段 <b>保留原文</b> & 末尾\n'+'\n'.join('第%d段：可选择复制，完整保留。'%n for n in range(2,15))
    published=1488353834
    expected=datetime.fromtimestamp(published).strftime('%Y-%m-%d %H:%M:%S')
    reports=[];index=0
    cases=[('detail','videoDescription','videoPublished'),
           ('gallery','galleryDescription','galleryPublished'),
           ('gallery_select','selectionDescription',None)]

    def finish(error=None):
        window.setProperty('pageName','library')
        clipboard.clear()
        report=dict(status='FAIL' if error else 'PASS',cases=reports,network_requests=0)
        if error: report.update(reason=type(error).__name__,case=index)
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        QTimer.singleShot(100,app.quit)

    def prepare():
        nonlocal index
        page,name,date=cases[index]
        bridge._detail=dict(bvid='BV13x41117TL',work_key='synthetic',kind='gallery' if page=='gallery' else 'video',
                            title='合成测试',author='合成作者',description=body,description_status='present',
                            published=published,tags=[],tags_status='empty',groups=[],assets=[])
        bridge._gallery_draft=dict(draft_id='synthetic-text',metadata=dict(description=body),groups=[])
        bridge.changed.emit();window.setProperty('pageName','detail' if page=='gallery' else page)
        QTimer.singleShot(200,inspect)

    def inspect():
        nonlocal index
        try:
            page,name,date=cases[index]
            container=window.findChild(QObject,name);assert container
            editor=container.findChild(QObject,'selectableBodyEditor');assert editor
            assert editor.property('text')==body and editor.property('readOnly')
            assert editor.property('selectByMouse')
            start=editor.mapToScene(QPointF(3,8)).toPoint();end=editor.mapToScene(QPointF(110,8)).toPoint()
            QTest.mousePress(window,Qt.LeftButton,Qt.NoModifier,start)
            QTest.mouseMove(window,end,50)
            QTest.mouseRelease(window,Qt.LeftButton,Qt.NoModifier,end)
            assert editor.property('selectedText')
            # Native range selection/copy (includes literal markup and newlines).
            assert QMetaObject.invokeMethod(editor,'select',Q_ARG(int,0),Q_ARG(int,8))
            assert QMetaObject.invokeMethod(editor,'copy')
            assert clipboard.text()==body[:8]
            assert QMetaObject.invokeMethod(editor,'copyWholeText')
            assert clipboard.text()==body and editor.property('selectedText')==body
            editor.forceActiveFocus()
            QTest.keyClick(window,Qt.Key_X)
            assert editor.property('text')==body
            QTest.keyClick(window,Qt.Key_A,Qt.ControlModifier)
            QTest.keyClick(window,Qt.Key_C,Qt.ControlModifier)
            assert clipboard.text()==body
            if date:
                date_item=window.findChild(QObject,date);assert expected in date_item.property('text')
                bridge._detail=dict(bridge._detail,published=0);bridge.changed.emit()
                assert '发布时间待获取' in date_item.property('text')
            reports.append(dict(page=page,mouse_selection=True,range_copy=True,whole_copy=True,keyboard_copy=True,
                                read_only=True,plain_text=True,all_paragraphs=True,seconds=bool(date)))
            index+=1
            if index<len(cases): QTimer.singleShot(100,prepare)
            else: finish()
        except BaseException as exc: finish(exc)
    # Let the asynchronous initial library query finish before injecting fixtures.
    def ready():
        if bridge.loadState=='loading':QTimer.singleShot(50,ready)
        else:prepare()
    QTimer.singleShot(300,ready)
