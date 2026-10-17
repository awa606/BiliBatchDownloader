import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.qt_gallery_preview_spike import load_manifest


def render_environment():
    env = os.environ.copy()
    # QVideoSink can decode under offscreen while VideoOutput renders nothing.
    # Windows native rendering is the requirement; don't weaken pixel checks.
    if os.name == 'nt':
        env['QT_QPA_PLATFORM'] = 'windows'
    return env


@pytest.fixture
def sample(tmp_path):
    asset = tmp_path / 'synthetic.bin'
    asset.write_bytes(b'synthetic gallery spike fixture')
    row = {'path': str(asset), 'size': asset.stat().st_size,
           'sha256': hashlib.sha256(asset.read_bytes()).hexdigest(), 'kind': 'image'}
    def write(rows):
        path = tmp_path / 'manifest.json'
        path.write_text(json.dumps({'assets': rows}))
        return path
    return asset, row, write


def test_verified_local_manifest(sample):
    asset, row, write = sample
    assert load_manifest(write([row]))[0]['path'] == str(asset.resolve())


@pytest.mark.parametrize('field,value', [('kind', 'url'), ('path', 'https://private.invalid/media'),
    ('path', 'relative.jpg'), ('size', 1), ('sha256', '0' * 64)])
def test_invalid_manifest(sample, field, value):
    asset, row, write = sample
    row[field] = value
    with pytest.raises(ValueError):
        load_manifest(write([row]))


def test_duplicate_and_empty(sample):
    asset, row, write = sample
    for rows in ([], [row, row], [row] * 33):
        with pytest.raises(ValueError):
            load_manifest(write(rows))


def test_missing_file(sample):
    asset, row, write = sample
    asset.unlink()
    with pytest.raises(ValueError, match='file_missing'):
        load_manifest(write([row]))


@pytest.mark.parametrize('broken', [False, True])
def test_actual_qt_multiframe_end_and_error(tmp_path, broken):
    binary = os.environ.get('BILI_TEST_FFMPEG')
    if not binary:
        pytest.skip('Set BILI_TEST_FFMPEG for real Qt synthetic playback')
    target = tmp_path / 'synthetic.mp4'
    if broken:
        target.write_bytes(b'SYNTHETIC_PRIVATE_SENTINEL corrupted mp4')
    else:
        subprocess.run([binary, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
            'testsrc=size=160x120:rate=10', '-t', '0.8', '-c:v', 'libx264',
            '-pix_fmt', 'yuv420p', str(target)], check=True, timeout=30, capture_output=True)
    row = {'path': str(target), 'kind': 'video', 'size': target.stat().st_size,
           'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'expected_error': broken}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'assets': [row]}))
    output = tmp_path / 'output'
    command = ([os.environ['BILI_TEST_QT_SPIKE']] if os.environ.get('BILI_TEST_QT_SPIKE') else
               [sys.executable, 'experiments/qt_gallery_preview_spike.py'])
    process = subprocess.run([*command,
        '--manifest', str(manifest), '--output', str(output)],
        cwd=Path(__file__).parents[1], capture_output=True, timeout=35, env=render_environment())
    assert process.returncode == 0
    assert b'SYNTHETIC_PRIVATE_SENTINEL' not in process.stdout + process.stderr
    assert str(target).encode() not in process.stdout + process.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['status'] == 'PASS'
    assert report['frozen'] == bool(os.environ.get('BILI_TEST_QT_SPIKE'))
    item = report['assets'][0]
    assert item['source_released']
    if broken:
        assert item['error'] == 'player_error'
    else:
        assert item['frames'] >= 2 and item['distinct_frame_hashes'] >= 2
        assert item['end_of_media'] and item['position_ms'] == item['duration_ms']
        assert item['rendered_color_count'] > 1


@pytest.mark.parametrize('extension', ['png', 'jpg'])
def test_actual_qt_image(tmp_path, extension):
    from PySide6.QtGui import QImage, QColor
    target = tmp_path / ('synthetic.' + extension)
    picture = QImage(80, 60, QImage.Format.Format_RGB32)
    picture.fill(QColor('white'))
    for x in range(10, 50):
        for y in range(10, 40):
            picture.setPixelColor(x, y, QColor('red'))
    assert picture.save(str(target))
    row = {'path': str(target), 'kind': 'image', 'size': target.stat().st_size,
           'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'assets': [row]}))
    command = ([os.environ['BILI_TEST_QT_SPIKE']] if os.environ.get('BILI_TEST_QT_SPIKE') else
               [sys.executable, 'experiments/qt_gallery_preview_spike.py'])
    process = subprocess.run([*command, '--manifest', str(manifest), '--output', str(tmp_path / 'output')],
                             cwd=Path(__file__).parents[1], capture_output=True, timeout=35, env=render_environment())
    assert process.returncode == 0
    report = json.loads((tmp_path / 'output/report.json').read_text())
    assert report['status'] == 'PASS' and report['assets'][0]['rendered_color_count'] > 1
