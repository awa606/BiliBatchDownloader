"""Actual selection widgets with synthetic data and dispatch disabled."""
import json
from pathlib import Path
from PySide6.QtCore import QObject,QTimer,QMetaObject,Q_ARG
from PySide6.QtGui import QImage,QColor


def install(app,window,bridge,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    # Freeze dispatch before enabling the UI-only capability for synthetic data.
    bridge.timer.stop();bridge._gallery_online=True
    picture=output/'synthetic.png';im=QImage(80,60,QImage.Format_RGB32);im.fill(QColor('#ef629b'));im.save(str(picture))
    bridge._gallery_draft={'draft_id':'synthetic','metadata':{'author':'合成作者','description':'第一段\n第二段','tags_status':'present','tags':['原站合成标签'],'topics':['话题']},
        'groups':[dict(sequence=i,preview_url=picture.as_uri(),livephoto=i%2==1) for i in range(1,5)]}
    bridge.changed.emit();window.setProperty('pageName','gallery_select')
    def inspect():
        stage='selection_component'
        try:
            item=window.findChild(QObject,'gallerySelection');assert item
            stage='initial_button_disabled'
            button=item.findChild(QObject,'downloadGallerySelection');assert button and not button.property('enabled')
            stage='initial_choice_empty'
            def find_visual(node,name):
                if node.objectName()==name: return node
                for child in node.childItems() if hasattr(node,'childItems') else []:
                    found=find_visual(child,name)
                    if found: return found
            choice=find_visual(item,'galleryChoice1');assert choice and not choice.property('checked')
            choice.setProperty('checked',True);QMetaObject.invokeMethod(choice,'toggled')
            stage='selected_button_enabled'
            assert button.property('enabled')
            window.grabWindow().save(str(output/'selection.png'))
            window.setProperty('pageName','library')
            report=dict(status='PASS',default_selection_empty=True,choice_enables_download=True,network_requests=0)
        except BaseException as exc:
            report=dict(status='FAIL',reason=type(exc).__name__,stage=stage,network_requests=0)
            window.grabWindow().save(str(output/'failure.png'))
        (output/'report.json').write_text(json.dumps(report),encoding='utf-8');app.quit()
    QTimer.singleShot(900,inspect)
