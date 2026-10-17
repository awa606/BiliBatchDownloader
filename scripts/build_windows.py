"""Build a fresh portable directory; no private data, media, FFmpeg or old cleanup."""
import argparse
from importlib import metadata
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024): h.update(block)
    return h.hexdigest()

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--name',default='BiliBatchDownloader-0.2.0-verify')
    args=parser.parse_args(); name=args.name
    if not name.startswith('BiliBatchDownloader-') or Path(name).name!=name: raise ValueError('invalid_name')
    target=ROOT/'dist'/name
    if target.exists(): raise ValueError('existing_bundle_preserved_use_new_name')
    build=ROOT/'build'/name; build.mkdir(parents=True,exist_ok=False)
    fingerprints={str(p.relative_to(ROOT)):sha(p) for folder in ('src','resources','scripts') for p in (ROOT/folder).rglob('*')
                  if p.is_file() and p.suffix in ('.py','.qml','.json') and '__pycache__' not in p.parts}
    # Adapt PixivDesk's proven isolation: unrelated PATH ICU DLLs break frozen QtCore.
    env=os.environ.copy()
    windows=Path(os.environ.get('SystemRoot','C:/Windows'))
    env['PATH']=os.pathsep.join([str(Path(sys.executable).parent),sys.base_prefix,str(windows/'System32'),str(windows)])
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH','QML2_IMPORT_PATH'):
        env.pop(key,None)
    spec=build/'desktop.spec'
    spec.write_text(f'''
from pathlib import Path
root=Path({str(ROOT)!r})
common=dict(pathex=[str(root/'src')],hookspath=[str(root/'scripts/hooks')],excludes=['tkinter','pytest','gallery_dl','PIL','numpy','matplotlib','PySide6.QtWebEngineCore','PySide6.QtCharts','PySide6.QtGraphs','PySide6.QtVirtualKeyboard'])
gui=Analysis([str(root/'scripts/desktop_entry.py')],datas=[(str(root/'resources'),'resources')],**common)
worker=Analysis([str(root/'scripts/worker_entry.py')],hiddenimports=['yt_dlp.extractor.bilibili','yt_dlp.networking._requests','yt_dlp.networking._urllib','yt_dlp.postprocessor.ffmpeg'],**common)
gpyz=PYZ(gui.pure)
wpyz=PYZ(worker.pure)
gexe=EXE(gpyz,gui.scripts,[],exclude_binaries=True,name='BiliBatchDownloader',console=False,upx=False)
wexe=EXE(wpyz,worker.scripts,[],exclude_binaries=True,name='BiliDeskWorker',console=True,upx=False)
COLLECT(gexe,wexe,gui.binaries,gui.datas,worker.binaries,worker.datas,name={name!r},upx=False)
''',encoding='utf-8')
    log=build/'build.log'
    with log.open('w',encoding='utf-8') as stream:
        result=subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(ROOT/'dist'),
           '--workpath',str(build/'work'),str(spec)],cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode: raise RuntimeError('build_failed_see_local_log')
    if any(sha(ROOT/path)!=value for path,value in fingerprints.items()):
        raise RuntimeError('source_changed_during_build_preserved_for_diagnosis')
    notices=target/'licenses'; notices.mkdir()
    distributions=[]
    for name in ('PySide6','PySide6_Essentials','PySide6_Addons','shiboken6','yt-dlp','requests','urllib3','certifi','charset-normalizer','idna','pyinstaller'):
        distribution=metadata.distribution(name)
        files=[]
        for file in distribution.files or []:
            relative=Path(file)
            if '..' in relative.parts: continue
            if not any(t in str(file).lower() for t in ('license','copying','notice')): continue
            source=Path(distribution.locate_file(file))
            if source.is_file() and source.suffix.lower() in ('','.txt','.md','.rst','.html'):
                folder=notices/name; folder.mkdir(exist_ok=True)
                destination=folder/(sha(source)[:8]+'-'+source.name)
                shutil.copy2(source,destination); files.append(destination.relative_to(target).as_posix())
        distributions.append(dict(name=name,version=distribution.version,license=distribution.metadata.get('License-Expression') or distribution.metadata.get('License'),files=files))
    for name in ('LGPL-3.0.txt','GPL-3.0.txt'):
        source=ROOT/'build/license-cache'/name
        if not source.is_file(): raise RuntimeError('license_cache_required')
        shutil.copy2(source,notices/name)
    for source in (ROOT/'packaging/licenses').glob('*.txt'):
        shutil.copy2(source,notices/source.name)
    shutil.copy2(Path(sys.base_prefix)/'LICENSE.txt',notices/'Python-LICENSE.txt')
    shutil.copy2(ROOT/'LICENSE',target/'LICENSE')
    shutil.copy2(ROOT/'docs/离线使用手册.html',target/'使用手册.html')
    shutil.copy2(ROOT/'docs/第三方依赖.md',notices/'第三方说明.md')
    (notices/'manifest.json').write_text(json.dumps(distributions,ensure_ascii=False,indent=2),encoding='utf-8')
    (target/'VERSION.json').write_text(json.dumps(dict(version='0.2.0',status='VERIFY',python=sys.version,
       source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
       source_tree_dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT)),
       native_scope='one ordinary P1; offline gallery and paired livephoto preview',ffmpeg='external user selected tool pair',
       source_fingerprints=fingerprints),ensure_ascii=False,indent=2),encoding='utf-8')
    forbidden=[p for p in target.rglob('*') if p.is_file() and (p.suffix.lower() in ('.sqlite3','.db','.mp4','.png','.jpg') or 'cookie' in p.name.lower())]
    # Qt's own resource images may be bundled by an upstream hook, but private files may not.
    forbidden=[p for p in forbidden if not p.is_relative_to(target/'_internal/PySide6')]
    if forbidden: raise RuntimeError('bundle_content_audit_failed')
    if any(p.name.lower() in ('ffmpeg.exe','ffprobe.exe') for p in target.rglob('*')): raise RuntimeError('unexpected_ffmpeg_bundle')
    manifest={p.relative_to(target).as_posix():sha(p) for p in target.rglob('*') if p.is_file()}
    (target/'SHA256.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(dict(bundle=str(target),files=len(manifest),bytes=sum(p.stat().st_size for p in target.rglob('*') if p.is_file()))))

if __name__=='__main__': main()
