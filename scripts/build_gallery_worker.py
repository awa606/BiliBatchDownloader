"""Build the native gallery worker separately, with no Qt runtime."""
from pathlib import Path
import json
import subprocess
import sys


def build(root,target,work,env):
    work.mkdir(parents=True,exist_ok=False)
    spec=work/'gallery.spec'
    spec.write_text(f'''
from PyInstaller.utils.hooks import copy_metadata
data=[]
for package in ('gallery-dl','requests','urllib3'): data+=copy_metadata(package)
a=Analysis([{str(root/'scripts/gallery_worker_entry.py')!r}],pathex=[{str(root/'src')!r}],
    datas=data,hiddenimports=['gallery_dl.extractor.bilibili','gallery_dl.downloader.http',
    'yt_dlp.cookies','yt_dlp.networking._requests','yt_dlp.networking._urllib'],
    excludes=['PySide6','shiboken6','tkinter','pytest','numpy','matplotlib','PIL'])
p=PYZ(a.pure)
e=EXE(p,a.scripts,[],exclude_binaries=True,name='BiliGalleryWorker',console=True,upx=False)
COLLECT(e,a.binaries,a.datas,name='gallery-worker',upx=False)
''',encoding='utf-8')
    with (work/'build.log').open('w',encoding='utf-8') as stream:
        result=subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(target),
            '--workpath',str(work/'work'),str(spec)],cwd=root,env=env,stdout=stream,stderr=subprocess.STDOUT)
    if result.returncode: raise RuntimeError('gallery_worker_build_failed')
    if any('pyside' in str(p).lower() or p.name.lower().startswith('qt6') for p in (target/'gallery-worker').rglob('*')):
        raise RuntimeError('gallery_worker_must_be_qt_free')
