"""Collect only the QML families used by this application (no GPL-only add-ons)."""
from PyInstaller.utils.hooks.qt import pyside6_library_info
hiddenimports,binaries,datas=pyside6_library_info.collect_module('PySide6.QtQml')
qml_binaries,qml_datas=pyside6_library_info.collect_qtqml_files()

def needed(entry):
    path=entry[1].replace('\\','/').split('/qml/',1)[-1]
    if path in ('QtQuick','QtQml','QtCore','QtMultimedia'): return True
    return any(path==p or path.startswith(p+'/') for p in (
        'QtQml/Models','QtQml/WorkerScript','QtQuick/Controls','QtQuick/Templates','QtQuick/Layouts',
        'QtQuick/Window','QtQuick/Dialogs','QtQuick/NativeStyle','QtQuick/Effects',
        'Qt/labs/folderlistmodel','Qt/labs/platform'))

binaries += [x for x in qml_binaries if needed(x)]
datas += [x for x in qml_datas if needed(x)]
