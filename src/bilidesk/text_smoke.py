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
    reports=[];index=0;labels=[];stage='body';pointer_checks=[]
    title='标题 <b>纯文本</b> & 可复制'
    author='合成作者 可复制'
    tags=['标签一','<话题二>']
    cases=[('detail','videoDescription','videoPublished'),
           ('gallery','galleryDescription','galleryPublished'),
           ('gallery_select','selectionDescription',None)]

    def finish(error=None):
        clipboard.clear()
        report=dict(status='FAIL' if error else 'PASS',cases=reports,labels=labels,network_requests=0)
        if error:
            trace=error.__traceback__
            while trace.tb_next:trace=trace.tb_next
            report.update(reason=type(error).__name__,case=index,test_line=trace.tb_lineno,stage=stage,pointer_checks=pointer_checks)
            window.grabWindow().save(str(output/'synthetic-failure.png'))
        window.setProperty('pageName','library')
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        QTimer.singleShot(100,app.quit)

    def prepare():
        nonlocal index
        page,name,date=cases[index]
        bridge._detail=dict(bvid='BV13x41117TL',work_key='synthetic',kind='gallery' if page=='gallery' else 'video',
                            title=title,author=author,description=body,description_status='present',
                            published=published,tags=tags,tags_status='present',groups=[],assets=[])
        bridge._gallery_draft=dict(draft_id='synthetic-text',metadata=dict(description=body,author=author),groups=[])
        bridge.changed.emit();window.setProperty('pageName','detail' if page=='gallery' else page)
        QTimer.singleShot(200,inspect)

    def inspect():
        nonlocal index,stage
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
            fields={'detail':['videoTitle','videoAuthor','videoPublished','videoTags'],
                    'gallery':['galleryTitle','galleryPublished','galleryTags'],
                    'gallery_select':['selectionAuthor']}[page]
            for field in fields:
                stage=field
                control=window.findChild(QObject,field);assert control
                text=control.property('text');assert text
                selectable=control.findChild(QObject,'selectableBodyEditor');assert selectable
                assert selectable.property('readOnly') and selectable.property('selectByMouse')
                y=min(8,selectable.height()/2)
                start=selectable.mapToScene(QPointF(3,y)).toPoint()
                end=selectable.mapToScene(QPointF(70,y)).toPoint()
                QTest.mouseMove(window,start,50)
                QTest.mousePress(window,Qt.LeftButton,Qt.NoModifier,start)
                QTest.mouseMove(window,end,40);QTest.mouseRelease(window,Qt.LeftButton,Qt.NoModifier,end)
                pointer_checks.append(dict(field=field,start=[start.x(),start.y()],end=[end.x(),end.y()],
                    height=selectable.height(),width=selectable.width(),selected=len(selectable.property('selectedText')),
                    focus=selectable.hasActiveFocus()))
                assert selectable.property('selectedText'),field
                selectable.forceActiveFocus();QTest.keyClick(window,Qt.Key_A,Qt.ControlModifier)
                QTest.keyClick(window,Qt.Key_C,Qt.ControlModifier);assert clipboard.text()==text,field
                QTest.keyClick(window,Qt.Key_X);assert selectable.property('text')==text
            if page!='gallery_select':
                prefix='video' if page=='detail' else 'gallery'
                assert window.findChild(QObject,prefix+'Title').property('text')==title
                assert all(tag in window.findChild(QObject,prefix+'Tags').property('text') for tag in tags)
            if date:
                date_item=window.findChild(QObject,date);assert expected in date_item.property('text')
                bridge._detail=dict(bridge._detail,published=0);bridge.changed.emit()
                assert '发布时间待获取' in date_item.property('text')
            reports.append(dict(page=page,mouse_selection=True,range_copy=True,whole_copy=True,keyboard_copy=True,
                                read_only=True,plain_text=True,all_paragraphs=True,seconds=bool(date),selectable_fields=fields))
            index+=1
            if index<len(cases): QTimer.singleShot(100,prepare)
            else: prepare_labels()
        except BaseException as exc: finish(exc)

    def prepare_labels():
        # The real card/author delegates have left-click navigation overlays.
        bridge._works=[dict(work_key='synthetic',title=title*18,author=author,category='待分类',kind='video',
                            parts=2,local_count=0,cover='')]
        bridge._authors=[dict(uid='1',name=author*18,count=1,group_name='合成分组')]
        bridge._total=1;bridge.changed.emit();window.setProperty('pageName','library')
        QTimer.singleShot(200,inspect_labels)

    def visual_find(item,name):
        if item.objectName()==name:return item
        for child in item.childItems():
            found=visual_find(child,name)
            if found:return found
        return None

    def inspect_labels():
        try:
            for name in ('authorName','workCardTitle'):
                item=visual_find(window.contentItem(),name);assert item,name
                text=item.property('text');assert len(text)>100
                before=(window.property('pageName'),window.property('selectedUid'),bridge._revision)
                point=item.mapToScene(QPointF(item.width()/2,item.height()/2)).toPoint()
                QTest.mouseClick(window,Qt.RightButton,Qt.NoModifier,point);app.processEvents()
                menu=item.findChild(QObject,'copyTextMenu');assert menu and menu.property('visible'),name
                action=menu.findChild(QObject,'copyLabelAction');assert action
                point=action.mapToScene(QPointF(action.width()/2,action.height()/2)).toPoint()
                QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point);app.processEvents()
                assert clipboard.text()==text,name
                assert before==(window.property('pageName'),window.property('selectedUid'),bridge._revision)
                labels.append(dict(field=name,actual_context_menu=True,full_unelided_copy=True,no_navigation=True))
            card=visual_find(window.contentItem(),'workCardTitle')
            point=card.mapToScene(QPointF(card.width()/2,card.height()/2)).toPoint()
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,point)
            assert window.property('pageName')=='detail'
            labels.append(dict(card_left_navigation=True))
            finish()
        except BaseException as exc:finish(exc)
    # Let the asynchronous initial library query finish before injecting fixtures.
    def ready():
        if bridge.loadState=='loading':QTimer.singleShot(50,ready)
        else:prepare()
    QTimer.singleShot(300,ready)
