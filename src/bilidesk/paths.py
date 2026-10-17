import os
from pathlib import Path
import sys


def data_dir():
    # Each candidate generation has an isolated default library.
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'BiliBatchDownloaderGalleryOnlineVerify'


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def worker_command():
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name('BiliDeskWorker.exe'))]
    return [sys.executable, '-m', 'bilidesk.worker']


def gallery_worker_command():
    if getattr(sys,'frozen',False):
        return [str(Path(sys.executable).parent/'gallery-worker'/'BiliGalleryWorker.exe')]
    return [sys.executable,'-m','bilidesk.gallery_worker']
