"""Reproducible isolated freeze of the Qt preview experiment, never a release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build(output):
    output = output.resolve()
    if not output.is_relative_to(ROOT / '.artifacts') or output.exists():
        raise ValueError('fresh_artifact_directory_required')
    output.mkdir(parents=True)
    inputs = [Path(__file__), ROOT / 'experiments/qt_gallery_preview_spike.py',
              ROOT / 'scripts/hooks/hook-PySide6.QtQml.py', ROOT / 'requirements-desktop.lock.txt']
    hashes = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    hooks = output / 'hooks'
    hooks.mkdir()
    text = inputs[2].read_text(encoding='utf-8')
    marker = "'QtQuick','QtQml','QtCore'"
    if text.count(marker) != 1:
        raise ValueError('hook_contract_changed')
    (hooks / inputs[2].name).write_text(text.replace(marker, marker + ",'QtMultimedia'"), encoding='utf-8')
    windows = Path(os.environ.get('SystemRoot', 'C:/Windows'))
    env = os.environ.copy()
    env['PATH'] = os.pathsep.join((str(Path(sys.executable).parent), sys.base_prefix,
                                 str(windows / 'System32'), str(windows)))
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH'):
        env.pop(key, None)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir', '--name', 'gallery-qt-spike',
               '--distpath', str(output / 'dist'), '--workpath', str(output / 'build'), '--specpath', str(output),
               '--additional-hooks-dir', str(hooks), '--hidden-import', 'PySide6.QtMultimedia',
               '--hidden-import', 'PySide6.QtQuick', str(inputs[1])]
    with (output / 'build.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=360)
    unchanged = all(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
    bundle = output / 'dist/gallery-qt-spike'
    files = {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in bundle.rglob('*') if p.is_file()}
    receipt = {'status': 'PASS' if result.returncode == 0 and unchanged else 'FAIL',
               'source_fingerprints': hashes, 'sources_unchanged': unchanged, 'files': files,
               'scope': 'local_research_only_not_for_redistribution'}
    (output / 'build-receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    return 0 if receipt['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(build(parser.parse_args().output))
