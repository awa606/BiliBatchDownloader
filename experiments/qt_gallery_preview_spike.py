"""Offline Qt gallery deployment spike. Inputs and captures stay local.

This is an isolated experiment, not a product importer or download engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_manifest(path: Path) -> list[dict]:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError('manifest_limit')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('manifest_shape')
    rows = data.get('assets')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
        raise ValueError('manifest_assets')
    found = set()
    result = []
    for row in rows:
        if not isinstance(row, dict) or row.get('kind') not in ('image', 'video'):
            raise ValueError('asset_kind')
        if not isinstance(row.get('expected_error', False), bool):
            raise ValueError('negative_control_type')
        target = Path(row.get('path', ''))
        if not target.is_absolute() or str(target).startswith(('\\\\', '//')):
            raise ValueError('local_path_required')
        if any(p.is_symlink() or p.is_junction() for p in (target, *target.parents)):
            raise ValueError('linked_path')
        if not target.is_file():
            raise ValueError('file_missing')
        target = target.resolve(strict=True)
        if target in found:
            raise ValueError('duplicate_path')
        found.add(target)
        if not 0 < target.stat().st_size <= 64 * 1024 * 1024:
            raise ValueError('asset_size_limit')
        if target.stat().st_size != row.get('size') or digest(target) != row.get('sha256'):
            raise ValueError('file_changed')
        result.append({**row, 'path': str(target)})
    return result


QML = b'''
import QtQuick
import QtQuick.Window
import QtMultimedia
Window {
    id: root
    width: 900; height: 680; visible: true
    title: "Bili gallery offline Qt spike"
    color: "#202028"
    property url imageSource
    property bool videoMode: false
    signal imageResult(bool success)
    Image {
        anchors.fill: parent; anchors.margins: 20
        visible: !root.videoMode
        source: root.imageSource; asynchronous: true; autoTransform: true
        sourceSize.width: 1024; sourceSize.height: 1024
        fillMode: Image.PreserveAspectFit
        onStatusChanged: {
            if (status === Image.Ready) root.imageResult(true)
            else if (status === Image.Error) root.imageResult(false)
        }
    }
    VideoOutput {
        objectName: "videoOutput"
        anchors.fill: parent; anchors.margins: 20
        visible: root.videoMode; fillMode: VideoOutput.PreserveAspectFit
        endOfStreamPolicy: VideoOutput.KeepLastFrame
    }
}
'''


def run(rows: list[dict], output: Path, timeout_ms: int = 12000) -> int:
    # FFmpeg may write directly to C stderr, bypassing Qt's message handler.
    # No raw decoder metadata or error strings are retained by this experiment.
    silent = os.open(os.devnull, os.O_WRONLY)
    os.dup2(silent, 2)
    os.close(silent)
    from PySide6.QtCore import QObject, QTimer, QUrl, qInstallMessageHandler
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuick import QQuickWindow
    from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
    import PySide6

    # Neither Qt decoder errors nor image paths are copied to console/reports.
    qt_messages = []
    qInstallMessageHandler(lambda kind, context, text: qt_messages.append(int(kind.value)))
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    engine.loadData(QML)
    if not engine.rootObjects():
        (output / 'report.json').write_text(json.dumps({'status': 'FAIL', 'error': 'qml_load'}))
        return 1
    window = engine.rootObjects()[0]
    video = window.findChild(QObject, 'videoOutput')
    sink: QVideoSink = video.property('videoSink')
    player = QMediaPlayer()
    player.setVideoSink(sink)
    results = []
    state = {'index': -1, 'active': False, 'frame_hashes': set(), 'frames': 0,
             'last_timestamp_us': None, 'started': time.monotonic(), 'row_started': 0}
    timer = QTimer()
    timer.setSingleShot(True)

    def save_report(final=False):
        report = {
            'schema_version': 1, 'status': ('PASS' if len(results) == len(rows) and all(
                r['status'] == 'PASS' for r in results) else 'FAIL') if final else 'RUNNING',
            'pyside': PySide6.__version__, 'frozen': bool(getattr(sys, 'frozen', False)),
            'network_requests': 0, 'elapsed_seconds': round(time.monotonic() - state['started'], 3),
            'qt_message_categories': sorted(set(qt_messages)), 'assets': results,
        }
        (output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')

    def finished(reason=None):
        if not state['active']:
            return
        state['active'] = False
        timer.stop()
        row = rows[state['index']]
        video_mode = row['kind'] == 'video'
        if video_mode and not reason and (state['frames'] < 2 or len(state['frame_hashes']) < 2):
            reason = 'insufficient_motion_frames'
        if reason:
            status = 'EXPECTED_ERROR' if row.get('expected_error') else 'FAIL'
        else:
            status = 'FAIL' if row.get('expected_error') else 'PASS'
        # A negative control is PASS only if the player reported an actual error.
        if status == 'EXPECTED_ERROR':
            status = 'PASS' if reason in ('player_error', 'image_error') else 'FAIL'
        capture = f'{state["index"] + 1:02d}.png'
        captured = window.grabWindow()
        capture_saved = captured.save(str(output / capture))
        small = captured.scaled(16, 16)
        visible_colors = len({small.pixelColor(x, y).rgba() for x in range(small.width()) for y in range(small.height())})
        if not capture_saved or (not row.get('expected_error') and visible_colors < 2):
            reason, status = 'render_capture_empty', 'FAIL'
        record = {
            'ordinal': state['index'] + 1, 'kind': row['kind'], 'sha256': row['sha256'],
            'status': status, 'error': reason, 'expected_error': bool(row.get('expected_error')),
            'frames': state['frames'] if video_mode else None,
            'distinct_frame_hashes': len(state['frame_hashes']) if video_mode else None,
            'last_frame_timestamp_us': state['last_timestamp_us'] if video_mode else None,
            'end_of_media': player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia if video_mode else None,
            'duration_ms': player.duration() if video_mode else None,
            'position_ms': player.position() if video_mode else None,
            'capture': capture, 'elapsed_seconds': round(time.monotonic() - state['row_started'], 3),
            'capture_saved': capture_saved, 'rendered_color_count': visible_colors,
        }
        player.stop()
        player.setSource(QUrl())
        window.setProperty('imageSource', QUrl())
        record['source_released'] = player.source().isEmpty()
        results.append(record)
        save_report()
        # All failures are retained; no retry or fallback decoder.
        if status != 'PASS':
            QTimer.singleShot(0, app.quit)
        else:
            QTimer.singleShot(100, next_asset)

    def next_asset():
        state['index'] += 1
        if state['index'] >= len(rows):
            app.quit()
            return
        row = rows[state['index']]
        state.update(active=True, frames=0, frame_hashes=set(), last_timestamp_us=None,
                     row_started=time.monotonic())
        window.setProperty('videoMode', row['kind'] == 'video')
        timer.start(timeout_ms)
        source = QUrl.fromLocalFile(row['path'])
        if row['kind'] == 'image':
            window.setProperty('imageSource', source)
        else:
            player.setSource(source)
            player.play()

    def frame_changed(frame):
        if not state['active'] or not frame.isValid():
            return
        converted = frame.toImage().scaled(64, 64)
        if converted.isNull():
            return
        state['frames'] += 1
        state['frame_hashes'].add(hashlib.sha256(bytes(converted.constBits())).hexdigest())
        state['last_timestamp_us'] = frame.startTime()
        if state['frames'] == 2:
            ordinal = state['index'] + 1
            QTimer.singleShot(80, lambda: window.grabWindow().save(str(output / f'{ordinal:02d}-playing.png')))

    sink.videoFrameChanged.connect(frame_changed)
    player.mediaStatusChanged.connect(lambda status: finished() if
        status == QMediaPlayer.MediaStatus.EndOfMedia else None)
    player.errorOccurred.connect(lambda *args: finished('player_error'))
    window.imageResult.connect(lambda ok: QTimer.singleShot(180, lambda: finished(None if ok else 'image_error')))
    timer.timeout.connect(lambda: finished('asset_timeout'))
    QTimer.singleShot(150000, lambda: (finished('worker_timeout'), app.quit()))
    QTimer.singleShot(200, next_asset)
    app.exec()
    save_report(final=True)
    player.stop()
    player.setSource(QUrl())
    player.setVideoSink(None)
    window.close()
    return 0 if len(results) == len(rows) and all(r['status'] == 'PASS' for r in results) else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        rows = load_manifest(args.manifest)
    except (ValueError, OSError, TypeError, KeyError):
        (args.output / 'report.json').write_text(json.dumps({'status': 'FAIL', 'error': 'input_invalid'}))
        return 1
    return run(rows, args.output)


if __name__ == '__main__':
    sys.exit(main())
