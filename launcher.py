"""必要なライブラリ（PySide6 / numpy）を anicut\vendor に用意してから anicut を起動する。"""
import os
import sys

_base = os.path.dirname(os.path.abspath(__file__))
if sys.stdout is None or sys.stderr is None:
    # pythonw で起動された場合（コンソールなし）は出力をログへ
    try:
        _log = open(os.path.join(_base, 'console.log'), 'w', encoding='utf-8', buffering=1)
    except OSError:
        _log = open(os.devnull, 'w')
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

import importlib.util  # noqa: E402
import runpy  # noqa: E402
import subprocess  # noqa: E402

vendor = os.path.join(_base, 'vendor')
sys.path.insert(0, vendor)

need = []
if importlib.util.find_spec('PySide6') is None or importlib.util.find_spec('PySide6.QtMultimedia') is None:
    need.append('PySide6-Addons>=6.6')
if importlib.util.find_spec('numpy') is None:
    need.append('numpy')
if need:
    print('初回セットアップ中: ' + ', '.join(need) + ' をインストールします（インターネットが必要です）', flush=True)
    r = subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                        '--target', vendor, *need])
    importlib.invalidate_caches()
    if r.returncode:
        print('インストールに失敗しました。', flush=True)

sys.argv[0] = os.path.join(_base, 'anicut.py')
runpy.run_path(os.path.join(_base, 'anicut.py'), run_name='__main__')
