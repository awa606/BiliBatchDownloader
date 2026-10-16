import os
from pathlib import Path
import sys


def data_dir():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.local/share'))) / 'BiliBatchDownloaderDesk'


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))


def worker_command():
    if getattr(sys, 'frozen', False):
        return [str(Path(sys.executable).with_name('BiliDeskWorker.exe'))]
    return [sys.executable, '-m', 'bilidesk.worker']
