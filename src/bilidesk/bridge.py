from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
import traceback

from PySide6.QtCore import QObject,Property,Signal,Slot,QProcess,QTimer,QUrl
from PySide6.QtGui import QDesktopServices,QGuiApplication

from .library import Library
from .media import check_local_file
from .gallery import scan_report,import_report,check_gallery_file,ERRORS as GALLERY_ERRORS
from .paths import resource_root,worker_command,gallery_worker_command
from .process import Job

ERRORS={'local_file_missing':'文件已不存在，请检查原路径。','local_file_changed':'文件大小已变化，请重新核验。',
 'login_expired':'登录已失效，请在专用 Chrome 重新登录后关闭浏览器。','cookie_load_failed':'无法读取专用 Chrome 登录，请先关闭该浏览器。',
 'session_cookie_unavailable':'未找到B站登录，请先在专用 Chrome 登录。','http_412':'B站限制了本次请求，任务已停止。',
 'http_429':'B站请求频率限制，任务已停止。','request_budget_exhausted':'已达到请求上限，未追加尝试。',
 'worker_timeout':'任务超过时间限制，已终止其子进程。','destination_conflict':'目标已有不同文件，已保留双方文件。',
 'transfer_size_limit':'已达到本版本64MiB传输限制。','worker_operation_failed':'任务失败，原文件和中间文件已保留。'}


class Bridge(QObject):
    changed=Signal()
    finishedQuery=Signal(int,object)
    failedQuery=Signal(int)
    finishedLocal=Signal(object)
    finishedGallery=Signal(object)
    previewReady=Signal(str)
    draftReady=Signal()

    def __init__(self,directory,offline=False,gallery_online=False):
        super().__init__()
        self.lib=Library(directory); self.lib.initialize(); self.lib.recover()
        self.pool=ThreadPoolExecutor(max_workers=2)
        self.local_pool=ThreadPoolExecutor(max_workers=1)
        self.gallery_pool=ThreadPoolExecutor(max_workers=1)
        self._gallery_scan={}; self._gallery_revision=0; self._gallery_busy=False; self._preview_revision=0
        if gallery_online and not offline: self.lib.set_setting('offline','0')
        self._offline=offline or self.lib.setting('offline','0')=='1'
        if self._offline: self.lib.set_setting('offline','1')
        self._opening=set(); self._closing=False
        self._load_state='loading'
        self._authors=[]; self._works=[]; self._detail={}; self._tasks=[]; self._message=''
        self._total=0; self._offset=0; self._uid=''; self._search=''; self._category=''; self._undownloaded=False
        self._bvid=''; self._revision=0; self._paused=self.lib.setting('paused','1')=='1'
        self.process=None; self.job=None; self.active=None; self.buffer=bytearray(); self.terminal=False
        self._online=self._feature() and not self._offline
        self._gallery_online=bool(gallery_online and not self._offline)
        if gallery_online: self._online=False
        self.lib.set_setting('gallery_online','1' if self._gallery_online else '0')
        self._gallery_draft={}; self._gallery_requested_id=None
        self.finishedQuery.connect(self._receive)
        self.failedQuery.connect(self._queryFailed)
        self.finishedLocal.connect(self._receiveLocal)
        self.finishedGallery.connect(self._receiveGallery)
        self.timer=QTimer(self); self.timer.setInterval(500); self.timer.timeout.connect(self.tick); self.timer.start()
        self.refresh()

    def _feature(self):
        try: return bool(json.loads((resource_root()/'resources/features.json').read_text())['native_video'])
        except Exception: return False

    @Property('QVariantList',notify=changed)
    def authors(self): return self._authors
    @Property('QVariantList',notify=changed)
    def works(self): return self._works
    @Property('QVariantList',notify=changed)
    def tasks(self): return self._tasks
    @Property('QVariantMap',notify=changed)
    def detail(self): return self._detail
    @Property(str,notify=changed)
    def message(self): return self._message
    @Property(str,notify=changed)
    def loadState(self): return self._load_state
    @Property(str,constant=True)
    def libraryDirectory(self): return str(self.lib.directory)
    @Property(int,notify=changed)
    def total(self): return self._total
    @Property(int,notify=changed)
    def offset(self): return self._offset
    @Property(str,notify=changed)
    def searchText(self): return self._search
    @Property(str,notify=changed)
    def categoryFilter(self): return self._category
    @Property(bool,notify=changed)
    def missingFilter(self): return self._undownloaded
    @Property(bool,notify=changed)
    def paused(self): return self._paused
    @Property(bool,notify=changed)
    def online(self): return self._online
    @Property(bool,notify=changed)
    def galleryOnline(self): return self._gallery_online
    @Property('QVariantMap',notify=changed)
    def galleryDraft(self): return self._gallery_draft
    @Property('QVariantMap',notify=changed)
    def galleryScan(self): return self._gallery_scan
    @Property(bool,notify=changed)
    def galleryBusy(self): return self._gallery_busy
    @Property(str,notify=changed)
    def authorLabel(self):
        return next((a['name'] for a in self._authors if a['uid']==self._uid),'全部作者')
    @Property('QVariantMap',notify=changed)
    def settings(self):
        return {k:self.lib.setting(k) for k in ('profile','root','ffmpeg')}

    def tell(self,text): self._message=text; self.changed.emit()

    @Slot(str)
    def copyText(self,text):
        # An explicit local UI action only; never log or persist clipboard text.
        QGuiApplication.clipboard().setText(text)

    @Slot()
    def refresh(self):
        self._revision+=1; rev=self._revision
        self._load_state='loading'
        args=(self._uid,self._search,self._category,self._undownloaded,self._offset)
        bvid=self._bvid
        def read():
            return self.lib.snapshot(*args),self.lib.detail(bvid) if bvid else {},self.lib.tasks()
        future=self.pool.submit(read)
        def done(f):
            try:
                result=f.result()
                report={'state':'ready','authors':len(result[0]['authors']),'works':result[0]['total'],'revision':rev}
            except Exception as exc:
                result=None
                report={'state':'error','revision':rev,'error_type':type(exc).__name__,
                    'sqlite_error_code':getattr(exc,'sqlite_errorcode',None),
                    'frames':[{'file':Path(frame.filename).name,'line':frame.lineno}
                              for frame in traceback.extract_tb(exc.__traceback__)]}
            report['data_directory']=str(self.lib.directory)
            try:
                # Local diagnostics contain no titles, SQL, cookies, or exception text.
                (self.lib.directory/'library-status.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError: pass
            if self._closing: return
            try:
                if result is None: self.failedQuery.emit(rev)
                else: self.finishedQuery.emit(rev,result)
            except Exception as exc:
                report.update(state='signal_error',error_type=type(exc).__name__)
                try: (self.lib.directory/'library-status.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                except OSError: pass
        future.add_done_callback(done)

    @Slot(int)
    def _queryFailed(self,rev):
        if rev!=self._revision or self._closing: return
        self._load_state='error'
        self.tell('作品库读取失败，原数据未删除。可点击“全部作者”重试；设置中可查看作品库位置。')

    @Slot(int,object)
    def _receive(self,rev,result):
        if rev!=self._revision: return
        snap,detail,tasks=result
        self._load_state='ready'
        if self._message.startswith('作品库读取失败'): self._message=''
        self._authors=snap['authors']; self._works=snap['works']; self._total=snap['total']; self._detail=detail; self._tasks=tasks
        applied={'revision':rev,'authors':len(self._authors),'works':self._total}
        def record_applied():
            try: (self.lib.directory/'library-ui-status.json').write_text(json.dumps(applied),encoding='utf-8')
            except OSError: pass
        self.pool.submit(record_applied)
        self.changed.emit()

    @Slot(str)
    def selectAuthor(self,uid): self._uid=uid; self._offset=0; self._bvid=''; self.refresh()
    @Slot(str)
    def search(self,text): self._search=text; self._offset=0; self.refresh()
    @Slot(str)
    def category(self,value): self._category=value; self._offset=0; self.refresh()
    @Slot(bool)
    def onlyMissing(self,value): self._undownloaded=value; self._offset=0; self.refresh()
    @Slot(int)
    def page(self,delta):
        self._offset=max(0,min(max(0,self._total-1)//40*40,self._offset+delta*40)); self.refresh()
    @Slot(str)
    def openWork(self,bvid):
        self.invalidatePreview(); self._bvid=bvid.removeprefix('video:'); self._detail={}; self.refresh()
    @Slot(str)
    def classify(self,category): self.lib.classify(self._bvid,category); self.refresh()
    @Slot(str,str)
    def groupAuthor(self,uid,group): self.lib.group_author(uid,group); self.refresh()

    @Slot(str,str,str)
    def saveSettings(self,profile,root,ffmpeg):
        for k,v in (('profile',profile),('root',root),('ffmpeg',ffmpeg)):
            if v.startswith('file:'): v=QUrl(v).toLocalFile()
            self.lib.set_setting(k,v.strip())
        self.tell('设置已保存在独立用户目录，未保存 Cookie。')

    @Slot(str,str)
    def importReports(self,source,preview):
        paths=[QUrl(v).toLocalFile() if v.startswith('file:') else v for v in (source,preview)]
        self.lib.enqueue('import',dict(source=paths[0],preview=paths[1]),'import:'+paths[0])
        self.refresh()

    @Slot()
    def invalidatePreview(self): self._preview_revision+=1

    @Slot()
    def clearGalleryScan(self):
        self._gallery_revision+=1; self._gallery_scan={}; self.changed.emit()

    @Slot(str)
    def scanGallery(self,path):
        if self._gallery_busy: return
        self.clearGalleryScan()
        path=QUrl(path).toLocalFile() if path.startswith('file:') else path
        self._gallery_job('scan',lambda:scan_report(self.lib,path))

    @Slot()
    def importGallery(self):
        if self._gallery_busy or not self._gallery_scan: return
        scan=dict(self._gallery_scan)
        self._gallery_job('import',lambda:import_report(self.lib,scan['path'],scan['report_sha256']))

    def _gallery_job(self,action,operation):
        self._gallery_busy=True; revision=self._gallery_revision; self.changed.emit()
        future=self.gallery_pool.submit(operation)
        def done(f):
            try: result=dict(action=action,revision=revision,result=f.result())
            except BaseException as exc:
                reason=str(exc) if isinstance(exc,ValueError) and str(exc) in GALLERY_ERRORS else 'gallery_operation_failed'
                result=dict(action=action,revision=revision,error=reason)
            if not self._closing: self.finishedGallery.emit(result)
        future.add_done_callback(done)

    @Slot(object)
    def _receiveGallery(self,result):
        if self._closing: return
        if result['action'] in ('scan','import','draft'):
            self._gallery_busy=False
            if result['revision']!=self._gallery_revision: self.changed.emit(); return
            if result.get('error'):
                self._gallery_scan={}; self.tell('图库核验失败，未部分入库：'+result['error']); return
            if result['action']=='draft':
                self._gallery_draft=result['result']; self.changed.emit(); self.draftReady.emit()
            elif result['action']=='scan':
                self._gallery_scan=result['result']; self.tell('预扫描完成，请核对作者、作品及数量后确认登记。')
            else:
                self._gallery_scan={}; self.tell('图库已原位登记，原文件未移动。'); self.refresh()
        elif result['action']=='preview':
            if result['revision']!=self._preview_revision: return
            if result.get('error'):
                self.tell(ERRORS.get(result['error'],'无法播放：文件核验未通过，请检查原文件。')); self.previewReady.emit(''); return
            self.previewReady.emit(QUrl.fromLocalFile(result['result']['path']).toString())

    @Slot(int)
    def prepareGallery(self,sequence):
        self.invalidatePreview(); revision=self._preview_revision; work=self._bvid
        future=self.local_pool.submit(check_gallery_file,self.lib,work,sequence,'live_video')
        def done(f):
            try: result=dict(action='preview',revision=revision,result=f.result())
            except Exception as exc:
                result=dict(action='preview',revision=revision,error=str(exc) if str(exc) in GALLERY_ERRORS else 'gallery_operation_failed')
            if not self._closing: self.finishedGallery.emit(result)
        future.add_done_callback(done)

    @Slot(int,str,bool)
    def openGallery(self,sequence,role,folder):
        key=(self._bvid,sequence,role,folder)
        if self._closing or key in self._opening: return
        self._opening.add(key); self.tell('正在核验并打开本地文件…')
        future=self.local_pool.submit(check_gallery_file,self.lib,*key)
        def done(f):
            try: result={'key':key,'file':f.result()}
            except Exception as exc:
                result={'key':key,'error':str(exc) if str(exc) in GALLERY_ERRORS else 'local_open_failed'}
            if not self._closing: self.finishedLocal.emit(result)
        future.add_done_callback(done)

    def network_ready(self):
        if not self._online: self.tell('原生联网下载尚未通过验证；当前可离线浏览与播放。'); return False
        if not all(self.lib.setting(k) for k in ('profile','root','ffmpeg')):
            self.tell('请先在设置中选择专用 Chrome 档案、下载目录及 FFmpeg。'); return False
        return True

    @Slot(str)
    def addGallery(self,url):
        from .gallery_engine import canonical,Stop
        if not self._gallery_online: self.tell('当前入口为离线模式。'); return
        if not all(self.lib.setting(k) for k in ('profile','root','ffmpeg')):
            self.tell('请先保存专用Chrome、下载目录及FFmpeg设置。'); return
        try: ident=canonical(url.strip())
        except (Stop,ValueError): self.tell('请输入合法的B站opus或动态链接。'); return
        self._gallery_requested_id=ident
        self._gallery_draft={}; self._gallery_revision+=1; self.changed.emit()
        self.lib.enqueue('gallery_prepare',dict(url='https://www.bilibili.com/opus/'+ident),'gallery_prepare:'+ident)
        self.refresh()

    @Slot(str)
    def downloadGallery(self,selection_json):
        if not self._gallery_online or not self._gallery_draft: return
        from .gallery_store import selection
        from .gallery_engine import Stop
        try: chosen=selection(self._gallery_draft,json.loads(selection_json))
        except (Stop,ValueError,TypeError): self.tell('请勾选至少一组。'); return
        draft=self._gallery_draft
        self.lib.enqueue('gallery_download',dict(draft_id=draft['draft_id'],manifest_sha=draft['manifest_sha'],selected=chosen),
                         'gallery_download:'+draft['metadata']['dynamic_id'])
        self.tell('所选组已加入队列；暂停状态下请在下载中心继续派发。'); self.refresh()

    def loadGalleryDraft(self,ident):
        from .gallery_store import load_draft,verify_cache
        from .gallery import thumbnail
        def load():
            draft=load_draft(self.lib,ident)
            images=verify_cache(draft,list(range(1,draft['manifest']['group_count']+1)))
            draft['groups']=[]
            for image in images:
                a=image|dict(media_root=str(Path(image['path']).parent))
                preview=thumbnail(self.lib,a)
                draft['groups'].append(dict(sequence=a['sequence'],preview_url=Path(preview).as_uri(),
                    livephoto=any(r['sequence']==a['sequence'] and r['role']=='live_video' for r in draft['manifest']['assets'])))
            return draft
        self._gallery_job('draft',load)

    @Slot(str,int)
    def addVideo(self,text,part):
        text=text.strip()
        match=re.fullmatch(r'(?:https://www\.bilibili\.com/video/)?(BV[A-Za-z0-9]{10})/?(?:\?[^\s#]*)?',text)
        if not match or part<1: self.tell('请输入BV号或B站视频链接，以及有效分P序号。'); return
        if not self.network_ready(): return
        bvid=match[1]
        self.lib.enqueue('metadata',dict(bvid=bvid,part=part,root=self.lib.setting('root')),'metadata:'+bvid+':'+str(part))
        self.tell('已加入作品读取队列。修改输入后需重新读取。'); self.refresh()

    @Slot(str)
    def download(self,cid):
        if not self.network_ready(): return
        asset=next((a for a in self._detail.get('assets',[]) if a['cid']==cid),None)
        if not asset: return
        if asset['status']=='complete': self.tell('此资源已登记下载；如原文件丢失，请先核实位置。'); return
        self.lib.enqueue('download',dict(bvid=self._bvid,cid=cid,part=asset['part'],root=self.lib.setting('root')),'download:'+self._bvid+':'+cid)
        self.refresh(); self.tell('已加入下载队列。')

    @Slot(str,bool)
    def openLocal(self,cid,folder):
        key=(self._bvid,cid,bool(folder))
        if self._closing or key in self._opening: return
        self._opening.add(key); self.tell('正在打开所在文件夹…' if folder else '正在交给默认播放器…')
        future=self.local_pool.submit(check_local_file,self.lib,*key)
        def done(f):
            try: result={'key':key,'file':f.result()}
            except Exception as exc:
                reason=str(exc) if isinstance(exc,ValueError) and str(exc) in ('local_file_missing','local_file_changed') else 'local_open_failed'
                result={'key':key,'error':reason}
            if not self._closing: self.finishedLocal.emit(result)
        future.add_done_callback(done)

    @Slot(object)
    def _receiveLocal(self,result):
        self._opening.discard(result['key'])
        if self._closing: return
        if result.get('error'):
            self.tell(ERRORS.get(result['error'],'无法读取本地文件，请检查文件权限和磁盘连接。')); return
        file=result['file']; path=Path(file['path']); path=path.parent if file['folder'] else path
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.tell('已交给系统打开文件夹。' if file['folder'] else '已交给系统默认程序打开。')
        else: self.tell('默认程序启动失败，请检查系统文件关联。')

    @Slot()
    def togglePause(self):
        self._paused=not self._paused; self.lib.set_setting('paused','1' if self._paused else '0'); self.changed.emit()
        if not self._paused: self.lib.set_setting('online_stopped','0')

    @Slot(str)
    def cancel(self,ident):
        if self.active and ident==self.active['id']:
            self.terminal=True
            self.lib.update_task(ident,state='canceled',phase='已取消',error='user_canceled')
            if self.job: self.job.close()
            if self.process: self.process.kill()
        else: self.lib.update_task(ident,state='canceled',phase='已取消')
        self.refresh()

    @Slot(str)
    def retry(self,ident):
        task=next((t for t in self.lib.tasks() if t['id']==ident),None)
        if task and task['state'] in ('failed','canceled','interrupted'):
            self.lib.enqueue(task['kind'],json.loads(task['payload']),task['key'])
            self.refresh()

    def tick(self):
        if self.process: return
        task=self.lib.next_task(local_only=self._paused,offline_only=self._offline or self.lib.setting('online_stopped','0')=='1')
        if not task: return
        if task['kind'] in ('metadata','download') and not self._online:
            self.lib.update_task(task['id'],state='failed',error='online_gate_not_passed'); self.refresh(); return
        if task['kind'] in ('gallery_prepare','gallery_download') and not self._gallery_online:
            self.lib.update_task(task['id'],state='failed',error='online_gate_not_passed'); self.refresh(); return
        self.active=task; self.terminal=False; self.buffer=bytearray()
        self.lib.update_task(task['id'],state='running',phase='启动后台进程')
        self.process=QProcess(self)
        self.process.readyReadStandardOutput.connect(self.readWorker)
        self.process.readyReadStandardError.connect(lambda:self.process.readAllStandardError() if self.process else None)
        self.process.started.connect(self.started)
        self.process.errorOccurred.connect(self.processError)
        self.process.finished.connect(self.exited)
        command=gallery_worker_command() if task['kind'].startswith('gallery_') else worker_command()
        self.process.start(command[0],command[1:])
        self.refresh()

    def started(self):
        try:
            self.job=Job(); self.job.assign(int(self.process.processId()))
            spec={'data_dir':str(self.lib.directory),'action':self.active['kind'],'payload':json.loads(self.active['payload'])}
            spec['payload']['task_id']=self.active['id']
            self.process.write((json.dumps(spec)+'\n').encode('utf-8'))
            ident=self.active['id']
            QTimer.singleShot(180000 if self.active['kind'].startswith('gallery_') else 230000,lambda:self.timeout(ident))
        except Exception:
            self.fail('worker_start_failed'); self.process.kill()

    def timeout(self,ident):
        if self.active and self.active['id']==ident and self.process:
            self.fail('worker_timeout')
            if self.job: self.job.close()
            self.process.kill()

    def processError(self,error):
        if error==QProcess.ProcessError.FailedToStart:
            self.fail('worker_start_failed'); self.exited()

    def fail(self,reason):
        if self.active and not self.terminal:
            self.terminal=True; self.lib.update_task(self.active['id'],state='failed',phase='失败',error=reason)
            if reason in ('http_401','http_403','http_412','http_429','login_expired','session_cookie_unavailable',
                          'cookie_load_failed','platform_api_stop','verification_page','verification_page_detected',
                          'effective_blocked_module','account_cookie_identity_mismatch'):
                self._paused=True; self.lib.set_setting('paused','1')
                self.lib.set_setting('online_stopped','1')
            self.tell(ERRORS.get(reason,'任务停止：'+reason))

    def readWorker(self):
        if not self.process: return
        self.buffer.extend(bytes(self.process.readAllStandardOutput()))
        if len(self.buffer)>2*1024*1024: self.fail('worker_output_limit'); self.process.kill(); return
        while b'\n' in self.buffer:
            line,_,remaining=self.buffer.partition(b'\n'); self.buffer=bytearray(remaining)
            try: event=json.loads(line)
            except Exception: self.fail('worker_protocol_error'); self.process.kill(); return
            if self.terminal: continue
            kind=event.get('type')
            if kind=='progress':
                self.lib.update_task(self.active['id'],phase=str(event['phase'])[:80],progress=float(event.get('progress',0)),speed=float(event.get('speed',0)))
            elif kind=='error': self.fail(event.get('reason','worker_operation_failed'))
            elif kind=='result':
                self.terminal=True
                self.lib.update_task(self.active['id'],state='complete',phase='已完成',progress=100,speed=0)
                result=event['result']
                if self.active['kind']=='gallery_prepare':
                    from .gallery_store import load_draft
                    draft=load_draft(self.lib,result['draft_id'])
                    if draft['metadata']['dynamic_id']==self._gallery_requested_id: self.loadGalleryDraft(result['draft_id'])
                elif self.active['kind']=='gallery_download':
                    self._bvid=result['work_key']; self.tell('所选组已保存，未选组没有归档。')
                    def previews():
                        from .gallery import thumbnail
                        with self.lib.connection(True) as db:
                            assets=[dict(a) for a in db.execute("SELECT * FROM gallery_assets WHERE dynamic_id=? AND role='image'",(result['work_key'][8:],))]
                        for a in assets:
                            preview=thumbnail(self.lib,a)
                            with self.lib.connection() as db:
                                db.execute('UPDATE gallery_assets SET preview_path=? WHERE dynamic_id=? AND sequence=? AND role=?',
                                    (preview,result['work_key'][8:],a['sequence'],'image'))
                        return {}
                    self._gallery_job('import',previews)
                elif self.active['kind']=='check_file':
                    path=Path(result['path']); path=path.parent if result['folder'] else path
                    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))): self.tell('默认程序启动失败，请检查系统文件关联。')
                elif result.get('preview')=='FAILED_VIDEO_RETAINED': self.tell('视频下载成功，截图生成失败；可以正常打开视频。')
                else: self.tell('后台任务完成。')
            self.refresh()

    def exited(self,*args):
        self.readWorker()
        if not self.terminal: self.fail('worker_interrupted')
        if self.job: self.job.close(); self.job=None
        if self.process: self.process.deleteLater(); self.process=None
        self.active=None; self.refresh()

    def shutdown(self):
        self._closing=True
        self.timer.stop()
        if self.active and not self.terminal:
            self.lib.update_task(self.active['id'],state='interrupted',error='app_closed',phase='退出时中断，等待重试')
            self.terminal=True
        if self.job: self.job.close()
        if self.process: self.process.kill(); self.process.waitForFinished(3000)
        self.pool.shutdown(wait=True,cancel_futures=True)
        self.local_pool.shutdown(wait=True,cancel_futures=True)
        self.gallery_pool.shutdown(wait=True,cancel_futures=True)
