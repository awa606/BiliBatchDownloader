from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time

from PySide6.QtCore import QObject,Property,Signal,Slot,QProcess,QTimer,QUrl
from PySide6.QtGui import QDesktopServices

from .library import Library
from .paths import resource_root,worker_command
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

    def __init__(self,directory):
        super().__init__()
        self.lib=Library(directory); self.lib.initialize(); self.lib.recover()
        self.pool=ThreadPoolExecutor(max_workers=2)
        self._authors=[]; self._works=[]; self._detail={}; self._tasks=[]; self._message=''
        self._total=0; self._offset=0; self._uid=''; self._search=''; self._category=''; self._undownloaded=False
        self._bvid=''; self._revision=0; self._paused=self.lib.setting('paused','1')=='1'
        self.process=None; self.job=None; self.active=None; self.buffer=bytearray(); self.terminal=False
        self._online=self._feature()
        self.finishedQuery.connect(self._receive)
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
    @Property(str,notify=changed)
    def authorLabel(self):
        return next((a['name'] for a in self._authors if a['uid']==self._uid),'全部作者')
    @Property('QVariantMap',notify=changed)
    def settings(self):
        return {k:self.lib.setting(k) for k in ('profile','root','ffmpeg')}

    def tell(self,text): self._message=text; self.changed.emit()

    @Slot()
    def refresh(self):
        self._revision+=1; rev=self._revision
        args=(self._uid,self._search,self._category,self._undownloaded,self._offset)
        bvid=self._bvid
        def read():
            return self.lib.snapshot(*args),self.lib.detail(bvid) if bvid else {},self.lib.tasks()
        future=self.pool.submit(read)
        def done(f):
            try: self.finishedQuery.emit(rev,f.result())
            except Exception: pass
        future.add_done_callback(done)

    @Slot(int,object)
    def _receive(self,rev,result):
        if rev!=self._revision: return
        snap,detail,tasks=result
        self._authors=snap['authors']; self._works=snap['works']; self._total=snap['total']; self._detail=detail; self._tasks=tasks
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
    def openWork(self,bvid): self._bvid=bvid; self.refresh()
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

    def network_ready(self):
        if not self._online: self.tell('原生联网下载尚未通过验证；当前可离线浏览与播放。'); return False
        if not all(self.lib.setting(k) for k in ('profile','root','ffmpeg')):
            self.tell('请先在设置中选择专用 Chrome 档案、下载目录及 FFmpeg。'); return False
        return True

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
        self.lib.enqueue('check_file',dict(bvid=self._bvid,cid=cid,folder=folder),'open:'+self._bvid+':'+cid)
        self.refresh()

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
        task=self.lib.next_task(local_only=self._paused,offline_only=self.lib.setting('online_stopped','0')=='1')
        if not task: return
        if task['kind'] in ('metadata','download') and not self._online:
            self.lib.update_task(task['id'],state='failed',error='online_gate_not_passed'); self.refresh(); return
        self.active=task; self.terminal=False; self.buffer=bytearray()
        self.lib.update_task(task['id'],state='running',phase='启动后台进程')
        self.process=QProcess(self)
        self.process.readyReadStandardOutput.connect(self.readWorker)
        self.process.readyReadStandardError.connect(lambda:self.process.readAllStandardError() if self.process else None)
        self.process.started.connect(self.started)
        self.process.errorOccurred.connect(self.processError)
        self.process.finished.connect(self.exited)
        command=worker_command(); self.process.start(command[0],command[1:])
        self.refresh()

    def started(self):
        try:
            self.job=Job(); self.job.assign(int(self.process.processId()))
            spec={'data_dir':str(self.lib.directory),'action':self.active['kind'],'payload':json.loads(self.active['payload'])}
            spec['payload']['task_id']=self.active['id']
            self.process.write((json.dumps(spec)+'\n').encode('utf-8'))
            ident=self.active['id']
            QTimer.singleShot(230000,lambda:self.timeout(ident))
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
                          'cookie_load_failed','platform_api_stop','verification_page'):
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
                if self.active['kind']=='check_file':
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
        self.timer.stop()
        if self.active and not self.terminal:
            self.lib.update_task(self.active['id'],state='interrupted',error='app_closed',phase='退出时中断，等待重试')
            self.terminal=True
        if self.job: self.job.close()
        if self.process: self.process.kill(); self.process.waitForFinished(3000)
        self.pool.shutdown(wait=True,cancel_futures=True)
