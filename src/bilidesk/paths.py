import os
from pathlib import Path
import sys


def data_dir():
    # 0.2 VERIFY never migrates the 0.1 default library on an accidental direct launch.
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'BiliBatchDownloaderGalleryVerify'


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def worker_command():
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name('BiliDeskWorker.exe'))]
    return [sys.executable, '-m', 'bilidesk.worker']
