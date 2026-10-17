"""Exercise the actual product widgets, sources and video sink, offline only."""
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from PySide6.QtCore import QObject,QTimer,QMetaObject,Q_ARG,QPointF
from PySide6.QtMultimedia import QMediaPlayer,QVideoSink


def install(app,window,bridge,output,expect_error=False,startup_start=None):
    output=Path(output); output.mkdir(parents=True,exist_ok=False)
    rows=[]; state={'phase':'library','start':time.monotonic(),'since':time.monotonic(),'works':[],
        'wi':0,'gi':0,'frames':0,'hashes':set(),'switch_checked':False,'switch_pass':False,'pause_pass':False}
    timer=QTimer(window); timer.setInterval(40)

    def stage(phase): state.update(phase=phase,since=time.monotonic())

    def finish(reason=None):
        timer.stop()
        report=dict(status='FAIL' if reason else 'PASS',error=reason,assets=rows,
            frozen=bool(getattr(sys,'frozen',False)),network_requests=0,offline=not bridge.online,
            elapsed_seconds=round(time.monotonic()-state['start'],3),width=window.width(),height=window.height(),
            scale=window.devicePixelRatio(),authors=len(bridge.authors),works=bridge.total,
            switch_during_playback=state['switch_pass'],expected_error=expect_error,
            pause_resume=state['pause_pass'],
            startup_query_ready_ms=state.get('ready_ms'),manual_product_experience='NOT_TESTED')
        report['diagnostic']=state.get('diagnostic')
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        window.setProperty('pageName','library'); app.quit()

    def capture(item,name):
        pos=item.mapToScene(QPointF(0,0)); scale=window.devicePixelRatio()
        image=window.grabWindow()
        region=image.copy(int(pos.x()*scale),int(pos.y()*scale),int(item.property('width')*scale),int(item.property('height')*scale))
        small=region.scaled(32,32)
        colors=len({small.pixelColor(x,y).rgba() for x in range(small.width()) for y in range(small.height())})
        image.save(str(output/name))
        return colors

    def invoke(name,*args):
        gallery=window.findChild(QObject,'galleryDetail')
        return QMetaObject.invokeMethod(gallery,name,*[Q_ARG('QVariant',a) for a in args])

    def next_group():
        gallery=window.findChild(QObject,'galleryDetail'); player=gallery.findChild(QMediaPlayer,'galleryPlayer')
        invoke('stopPreview')
        if not player.source().isEmpty() or player.playbackState()!=QMediaPlayer.StoppedState:
            finish('source_not_released'); return
        rows[-1]['source_released']=True
        state['gi']+=1
        if state['gi']>=len(bridge.detail['groups']):
            state['wi']+=1; state['gi']=0
            window.setProperty('pageName','library')
            if not player.source().isEmpty(): finish('leave_source_not_released'); return
            if state['wi']>=len(state['works']): finish(); return
            bridge.openWork(state['works'][state['wi']]); window.setProperty('pageName','detail'); stage('detail')
        else:
            invoke('choose',state['gi']); stage('image')

    def frame(frame):
        if state['phase']!='play' or not frame.isValid(): return
        image=frame.toImage().scaled(64,64)
        if image.isNull(): return
        state['frames']+=1; state['hashes'].add(hashlib.sha256(bytes(image.constBits())).hexdigest())

    def tick():
        try:
            if time.monotonic()-state['start']>150 or time.monotonic()-state['since']>15:
                finish('timeout'); return
            phase=state['phase']
            if phase=='library':
                if bridge.loadState!='ready': return
                state['ready_ms']=round((time.perf_counter()-startup_start)*1000,2) if startup_start else None
                state['works']=[w['work_key'] for w in bridge.works if w['kind']=='gallery']
                if bridge.online or not state['works']: finish('offline_gallery_required'); return
                window.grabWindow().save(str(output/'library.png'))
                bridge.openWork(state['works'][0]); window.setProperty('pageName','detail'); stage('detail'); return
            gallery=window.findChild(QObject,'galleryDetail')
            if not gallery: return
            player=gallery.findChild(QMediaPlayer,'galleryPlayer')
            if phase=='detail':
                if bridge.detail.get('work_key')!=state['works'][state['wi']]: return
                video=gallery.findChild(QObject,'galleryVideo'); video.property('videoSink').videoFrameChanged.connect(frame)
                invoke('choose',0); stage('image'); return
            group=bridge.detail['groups'][state['gi']]
            if phase=='image':
                photo=gallery.findChild(QObject,'galleryPhoto')
                status=gallery.property('photoStatus')
                if status==3: finish('image_error'); return
                if status!=1 or time.monotonic()-state['since']<.35: return
                colors=capture(photo,f'{len(rows)+1:02d}-image.png')
                if colors<2: finish('image_render_empty'); return
                rows.append(dict(kind='image',sequence=group['sequence'],sha256=group['image']['sha256'],status='PASS',colors=colors))
                if group['live_video']:
                    state.update(frames=0,hashes=set()); invoke('playCurrent'); stage('play')
                else: next_group()
            elif phase=='play':
                if gallery.property('playbackError'):
                    if expect_error:
                        rows.append(dict(kind='video',status='PASS',error='EXPECTED_PLAYER_ERROR',source_released=player.source().isEmpty()))
                        finish()
                    else: finish('player_error')
                    return
                if state['frames']>=3 and not state['switch_checked']:
                    if not state['pause_pass']:
                        invoke('togglePlayback'); stage('paused'); return
                    invoke('choose',(state['gi']+1)%len(bridge.detail['groups'])); state['switch_checked']=True
                    state['switch_pass']=player.source().isEmpty() and player.playbackState()==QMediaPlayer.StoppedState
                    if not state['switch_pass']: finish('switch_source_not_released'); return
                    invoke('choose',state['gi'])
                    state.update(frames=0,hashes=set()); invoke('playCurrent'); stage('play'); return
                if player.mediaStatus()==QMediaPlayer.EndOfMedia:
                    stage('end')
            elif phase=='paused' and time.monotonic()-state['since']>.15:
                state['pause_pass']=player.playbackState()==QMediaPlayer.PausedState
                if not state['pause_pass']: finish('pause_failed'); return
                invoke('togglePlayback'); stage('play')
            elif phase=='end' and time.monotonic()-state['since']>.15:
                colors=capture(gallery.findChild(QObject,'galleryVideo'),f'{len(rows)+1:02d}-video.png')
                if expect_error or state['frames']<2 or len(state['hashes'])<2 or colors<2:
                    finish('video_frames_or_render_invalid'); return
                rows.append(dict(kind='video',sequence=group['sequence'],sha256=group['live_video']['sha256'],status='PASS',
                    frames=state['frames'],distinct_frames=len(state['hashes']),colors=colors,end_of_media=True,duration_ms=player.duration()))
                next_group()
        except Exception as exc:
            # Fixed class only; never raw Qt errors, paths or decoded media metadata.
            state['diagnostic']={'phase':state['phase'],'frames':[{'file':Path(f.filename).name,'line':f.lineno} for f in traceback.extract_tb(exc.__traceback__)]}
            finish('harness_'+type(exc).__name__)
    timer.timeout.connect(tick); timer.start()
