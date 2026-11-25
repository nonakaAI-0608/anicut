"""anicut - アニメのカット検出 & 無劣化切り出しツール

・動画を読み込むとカット（場面転換）を自動検出し、各カットの 1 フレーム目にタイムスタンプを付ける
・カット一覧はサムネイル付き。ダブルクリックでそこから再生
・一覧の右クリックメニューからカットごとに無劣化保存
・範囲（In/Out、または一覧で複数選択）を指定して手動で無劣化出力
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import threading
import time
import traceback
from bisect import bisect_right
from pathlib import Path

# EXE 版（PyInstaller）では EXE のあるフォルダを基準にする（設定・キャッシュ・ffmpeg の置き場所）
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR / 'vendor'))

from PySide6.QtCore import (Qt, QObject, QRunnable, QSettings, QSize, QThread, QThreadPool, QTimer,  # noqa: E402
                            QUrl, Signal, QRectF, QPointF)
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QKeySequence, QPainter, QPen,  # noqa: E402
                           QPixmap, QShortcut, QGuiApplication, QBrush)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer  # noqa: E402
from PySide6.QtMultimediaWidgets import QVideoWidget  # noqa: E402
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,  # noqa: E402
                               QLabel, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QStackedWidget,
                               QStyle, QToolButton, QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy)

import engine as E  # noqa: E402

VIDEO_EXTS = {'.mkv', '.mp4', '.m4v', '.mov', '.ts', '.m2ts', '.webm', '.avi', '.flv', '.wmv', '.mpg', '.mpeg'}
THUMB_W, THUMB_H = 192, 108
ACCENT = QColor('#3d8bfd')
PROJECT_EXT = '.anicut'
MAX_PARALLEL = 2      # 全話解析で同時に解析する話数


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


def collect_videos(paths) -> list[str]:
    """ファイル/フォルダの混在リストから動画ファイルを集め、話数順（自然順）に並べる。"""
    out = []
    for p in paths:
        p = os.path.normpath(p)
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                out += [os.path.join(root, f) for f in files if Path(f).suffix.lower() in VIDEO_EXTS]
        elif os.path.isfile(p) and Path(p).suffix.lower() in VIDEO_EXTS:
            out.append(p)
    seen, res = set(), []
    for p in sorted(out, key=lambda x: natural_key(os.path.basename(x))):
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            res.append(p)
    return res


class Episode:
    """1話（1ファイル）分の状態"""

    def __init__(self, path: str):
        self.path = os.path.normpath(path)
        self.info: E.MediaInfo | None = None
        self.kfs: list[float] | None = None        # None = 取得中
        self.an: E.Analysis | None = None          # None = 未解析
        self.added: set[int] = set()
        self.removed: set[int] = set()
        self.rin: float | None = None
        self.rout: float | None = None
        self.thumbs: dict[int, QPixmap] = {}
        self.state = ''                            # '' / 'queued' / 'running'
        self.progress = 0.0
        self.ncuts = 0
        self.last_pos = 0

    @property
    def name(self):
        return os.path.basename(self.path)


# ====================================================================== workers

class DetectWorker(QThread):
    progress = Signal(float, int)
    done = Signal(object, object)     # Analysis, keyframes
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, ep, ffmpeg, ffprobe, hwaccel):
        super().__init__()
        self.ep = ep
        self.path, self.info, self.ffmpeg, self.ffprobe, self.hwaccel = ep.path, ep.info, ffmpeg, ffprobe, hwaccel
        self.cancel_ev = threading.Event()

    def run(self):
        try:
            kfs = self.ep.kfs if self.ep.kfs is not None else E.keyframes(self.path, self.ffprobe, self.info)
            try:
                an = E.analyze(self.path, self.ffmpeg, self.info, self.progress.emit, self.cancel_ev, self.hwaccel)
            except RuntimeError:
                if not self.hwaccel or self.cancel_ev.is_set():
                    raise
                an = E.analyze(self.path, self.ffmpeg, self.info, self.progress.emit, self.cancel_ev, False)
            self.done.emit(an, kfs)
        except E.Cancelled:
            self.cancelled.emit()
        except Exception as ex:  # noqa: BLE001
            self.failed.emit(f'{ex}\n\n{traceback.format_exc()}')


class ExportWorker(QThread):
    progress = Signal(int, int, str)
    finished_all = Signal(list, list)   # 成功パス, エラー

    def __init__(self, src, jobs, ffmpeg, info):
        super().__init__()
        self.src, self.jobs, self.ffmpeg, self.info = src, jobs, ffmpeg, info
        self.cancel_ev = threading.Event()

    def run(self):
        ok, errs = [], []
        for i, (plan, out) in enumerate(self.jobs):
            if self.cancel_ev.is_set():
                break
            self.progress.emit(i, len(self.jobs), Path(out).name)
            try:
                E.export_copy(self.src, plan, out, self.ffmpeg, self.info)
                ok.append(out)
            except Exception as ex:  # noqa: BLE001
                errs.append(f'{Path(out).name}: {ex}')
        self.progress.emit(len(self.jobs), len(self.jobs), '')
        self.finished_all.emit(ok, errs)


class ThumbSignals(QObject):
    ready = Signal(int, int, bytes)   # generation, frame index, jpeg


class ThumbTask(QRunnable):
    def __init__(self, gen, frame, t, path, ffmpeg, fps, sig, alive):
        super().__init__()
        self.gen, self.frame, self.t, self.path, self.ffmpeg, self.fps = gen, frame, t, path, ffmpeg, fps
        self.sig, self.alive = sig, alive

    def run(self):
        if not self.alive(self.gen):
            return
        try:
            data = E.thumbnail_jpeg(self.path, self.ffmpeg, self.t, self.fps, THUMB_W * 2)
        except Exception:  # noqa: BLE001
            data = b''
        try:
            self.sig.ready.emit(self.gen, self.frame, data)
        except RuntimeError:   # アプリ終了中
            pass


class KfSignals(QObject):
    ready = Signal(object, object, object, str)   # Episode, MediaInfo, keyframes, error


class KfTask(QRunnable):
    """probe + キーフレーム一覧の取得（解析しなくても無劣化切り出しに必要）"""

    def __init__(self, ep, ffprobe, sig):
        super().__init__()
        self.ep, self.ffprobe, self.sig = ep, ffprobe, sig

    def run(self):
        try:
            info = self.ep.info or E.probe(self.ep.path, self.ffprobe)
            kfs = E.keyframes(self.ep.path, self.ffprobe, info)
            self.sig.ready.emit(self.ep, info, kfs, '')
        except RuntimeError:
            pass
        except Exception as ex:  # noqa: BLE001
            try:
                self.sig.ready.emit(self.ep, None, None, str(ex))
            except RuntimeError:
                pass


# ====================================================================== timeline

class Timeline(QWidget):
    seek = Signal(float)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.duration = 0.0
        self.cuts: list[float] = []
        self.pos = 0.0
        self.rin = None
        self.rout = None
        self.current_cut = -1
        self.setMouseTracking(True)
        self._drag = False

    def x_of(self, t):
        w = self.width() - 12
        return 6 + (t / self.duration * w if self.duration > 0 else 0)

    def t_of(self, x):
        w = self.width() - 12
        return min(max((x - 6) / w, 0.0), 1.0) * self.duration if w > 0 else 0.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        h = self.height()
        p.fillRect(self.rect(), QColor('#1e1f22'))
        bar = QRectF(6, 8, self.width() - 12, h - 16)
        p.fillRect(bar, QColor('#2b2d31'))
        if self.duration <= 0:
            return
        # 現在カット
        if 0 <= self.current_cut < len(self.cuts):
            a = self.cuts[self.current_cut]
            b = self.cuts[self.current_cut + 1] if self.current_cut + 1 < len(self.cuts) else self.duration
            p.fillRect(QRectF(self.x_of(a), bar.top(), max(1.0, self.x_of(b) - self.x_of(a)), bar.height()),
                       QColor(61, 139, 253, 60))
        # In/Out
        if self.rin is not None and self.rout is not None and self.rout > self.rin:
            r = QRectF(self.x_of(self.rin), 3, max(2.0, self.x_of(self.rout) - self.x_of(self.rin)), h - 6)
            p.fillRect(r, QColor(255, 196, 0, 70))
            p.setPen(QPen(QColor('#ffc400'), 2))
            p.drawLine(QPointF(r.left(), 2), QPointF(r.left(), h - 2))
            p.drawLine(QPointF(r.right(), 2), QPointF(r.right(), h - 2))
        # カット線
        p.setPen(QPen(QColor('#8a8f98'), 1))
        for t in self.cuts:
            x = int(self.x_of(t))
            p.drawLine(x, int(bar.top()), x, int(bar.bottom()))
        # 再生位置
        x = self.x_of(self.pos)
        p.setPen(QPen(QColor('#ff5252'), 2))
        p.drawLine(QPointF(x, 0), QPointF(x, h))

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.duration > 0:
            self._drag = True
            self.seek.emit(self.t_of(ev.position().x()))

    def mouseMoveEvent(self, ev):
        if self.duration > 0:
            self.setToolTip(E.fmt_tc(self.t_of(ev.position().x())))
        if self._drag:
            self.seek.emit(self.t_of(ev.position().x()))

    def mouseReleaseEvent(self, ev):
        self._drag = False


# ====================================================================== cut list

class CutList(QListWidget):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.IconMode)
        self.setIconSize(QSize(THUMB_W, THUMB_H))
        self.setGridSize(QSize(THUMB_W + 16, THUMB_H + 50))
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setUniformItemSizes(True)
        self.setWordWrap(True)
        self.setSpacing(2)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)
        self.setTextElideMode(Qt.ElideNone)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


# ====================================================================== main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('anicut')
        self.resize(1500, 880)
        self.setAcceptDrops(True)
        self.settings = QSettings(str(APP_DIR / 'anicut.ini'), QSettings.IniFormat)

        self.ffmpeg = E.find_executable('ffmpeg')
        self.ffprobe = E.find_executable('ffprobe')

        # 状態
        self.episodes: list[Episode] = []
        self.ep: Episode | None = None
        self.queue: list[Episode] = []               # 解析待ち
        self._combo_refreshed = 0.0
        self.auto_cuts: list[int] = []
        self.cuts: list[int] = []
        self.cut_times: list[float] = []
        self.thumb_pending: set[int] = set()
        self.thumb_gen = 0
        self.detect_workers: list[DetectWorker] = []   # 同時に動かす解析（全話解析は最大 MAX_PARALLEL 話）
        self.kf_pool = QThreadPool(self)
        self.kf_pool.setMaxThreadCount(1)
        self.kf_sig = KfSignals()
        self.kf_sig.ready.connect(self.on_keyframes)
        self.player_path: str | None = None
        self.pending_pos: int | None = None   # 読み込み完了後に適用するシーク位置(ms)
        self.export_worker: ExportWorker | None = None
        self.cur_cut = -1
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 4) // 2)))
        self.thumb_sig = ThumbSignals()
        self.thumb_sig.ready.connect(self.on_thumb)
        self._placeholder = self._make_placeholder()
        self.recalc_timer = QTimer(self, singleShot=True, interval=250)
        self.recalc_timer.timeout.connect(self.recalc_cuts)
        self.counts_timer = QTimer(self, singleShot=True, interval=600)
        self.counts_timer.timeout.connect(lambda: (self.update_counts(), self.refresh_episode_combo()))

        self._build_ui()
        self._build_player()
        self._build_shortcuts()
        self._restore_settings()

        if not self.ffmpeg or not self.ffprobe:
            QTimer.singleShot(200, lambda: QMessageBox.critical(
                self, 'ffmpeg が見つかりません',
                'ffmpeg.exe / ffprobe.exe が見つかりません。\n'
                'PATH に通すか、anicut フォルダ（または anicut\\bin）に置いてください。'))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        st = self.style()
        tb = QWidget()
        tl = QHBoxLayout(tb)
        tl.setContentsMargins(6, 4, 6, 4)

        self.btn_open = QPushButton(st.standardIcon(QStyle.SP_DialogOpenButton), ' 開く')
        self.btn_open.setToolTip('動画ファイルを開く（複数選択で全話まとめて読み込み）')
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_open_dir = QPushButton(st.standardIcon(QStyle.SP_DirOpenIcon), ' フォルダ')
        self.btn_open_dir.setToolTip('フォルダ内の動画をすべて読み込む（1作品の全話など）')
        self.btn_open_dir.clicked.connect(self.open_folder_dialog)
        self.btn_detect = QPushButton(' 解析')
        self.btn_detect.setIcon(st.standardIcon(QStyle.SP_BrowserReload))
        self.btn_detect.setToolTip('表示中の話のカットを検出する')
        self.btn_detect.clicked.connect(self.start_detect)
        self.btn_detect_all = QPushButton(' 全話解析')
        self.btn_detect_all.setToolTip('未解析の話をすべて順番に解析する')
        self.btn_detect_all.clicked.connect(self.start_detect_all)
        self.btn_cancel = QPushButton('中止')
        self.btn_cancel.clicked.connect(self.cancel_work)
        self.btn_cancel.setEnabled(False)
        self.btn_proj_save = QPushButton(st.standardIcon(QStyle.SP_DialogSaveButton), ' 解析結果を保存')
        self.btn_proj_save.setToolTip(f'読み込んだ全話の解析結果・手動修正・設定を1つのファイル（*{PROJECT_EXT}）に保存 (Ctrl+S)')
        self.btn_proj_save.clicked.connect(self.save_project)
        self.btn_proj_load = QPushButton(st.standardIcon(QStyle.SP_DialogOpenButton), ' 解析結果を読み込み')
        self.btn_proj_load.setToolTip(f'保存した解析結果（*{PROJECT_EXT}）を読み込んで作業を再開')
        self.btn_proj_load.clicked.connect(self.load_project_dialog)
        for b in (self.btn_open, self.btn_open_dir, self.btn_detect, self.btn_detect_all, self.btn_cancel):
            tl.addWidget(b)
        tl.addSpacing(10)
        tl.addWidget(self.btn_proj_save)
        tl.addWidget(self.btn_proj_load)
        tl.addSpacing(16)

        def lab(t, tip):
            w = QLabel(t)
            w.setToolTip(tip)
            return w

        tip_th = 'フレーム間の差分スコアの最低値。小さいほど敏感（誤検出が増える）。'
        tip_ra = '周囲の動きに比べてどれだけ大きな変化ならカットとみなすか。小さいほど敏感。'
        tip_mf = 'これより短いカットは作らない（フラッシュ等による連続検出を防ぐ）。'
        self.sp_th = QDoubleSpinBox(decimals=1, minimum=3.0, maximum=120.0, singleStep=1.0)
        self.sp_th.setToolTip(tip_th)
        self.sp_ra = QDoubleSpinBox(decimals=2, minimum=1.0, maximum=10.0, singleStep=0.1)
        self.sp_ra.setToolTip(tip_ra)
        self.sp_mf = QSpinBox(minimum=1, maximum=240)
        self.sp_mf.setToolTip(tip_mf)
        self.sp_mf.setSuffix(' F')
        tl.addWidget(lab('しきい値', tip_th))
        tl.addWidget(self.sp_th)
        tl.addWidget(lab('適応比', tip_ra))
        tl.addWidget(self.sp_ra)
        tl.addWidget(lab('最短カット', tip_mf))
        tl.addWidget(self.sp_mf)
        self.btn_default = QToolButton(text='既定値')
        self.btn_default.clicked.connect(self.reset_params)
        tl.addWidget(self.btn_default)
        self.cb_hw = QCheckBox('GPUで解析')
        self.cb_hw.setToolTip('NVIDIA GPU (CUDA) でデコードと縮小を行い、解析を2～3倍速くします\n'
                              '（使えない GPU・形式のときは自動で CPU に戻ります）')
        tl.addWidget(self.cb_hw)
        tl.addStretch(1)
        self.btn_export_ts = QPushButton(st.standardIcon(QStyle.SP_DialogSaveButton), ' タイムスタンプ書き出し')
        self.btn_export_ts.clicked.connect(self.export_timestamps)
        tl.addWidget(self.btn_export_ts)

        for w in (self.sp_th, self.sp_ra, self.sp_mf):
            w.valueChanged.connect(lambda *_: self.recalc_timer.start())
            w.valueChanged.connect(lambda *_: self.counts_timer.start())

        # 左：カット一覧
        self.list = CutList()
        self.list.itemDoubleClicked.connect(self.on_item_double)
        self.list.customContextMenuRequested.connect(self.on_context)
        self.list.itemSelectionChanged.connect(self.on_selection)
        self.list.files_dropped.connect(self.on_drop_paths)
        self.list_hint = QLabel('')
        self.list_hint.setAlignment(Qt.AlignCenter)
        self.list_hint.setWordWrap(True)
        self.list_hint.setAcceptDrops(False)
        self.list_hint.setStyleSheet('color:#999; font-size:14px; padding:20px;')
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        er = QHBoxLayout()
        er.setContentsMargins(4, 4, 4, 0)
        self.btn_ep_prev = QToolButton(text='◀')
        self.btn_ep_prev.setToolTip('前の話 (Ctrl+PageUp)')
        self.btn_ep_prev.clicked.connect(lambda: self.step_episode(-1))
        self.btn_ep_next = QToolButton(text='▶')
        self.btn_ep_next.setToolTip('次の話 (Ctrl+PageDown)')
        self.btn_ep_next.clicked.connect(lambda: self.step_episode(1))
        self.cmb_ep = QComboBox()
        self.cmb_ep.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_ep.setMinimumContentsLength(20)
        self.cmb_ep.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cmb_ep.setMaxVisibleItems(30)
        self.cmb_ep.currentIndexChanged.connect(self.on_episode_changed)
        lab_ep = QLabel('話:')
        lab_ep.setStyleSheet('font-weight:bold;')
        er.addWidget(lab_ep)
        er.addWidget(self.btn_ep_prev)
        er.addWidget(self.cmb_ep, 1)
        er.addWidget(self.btn_ep_next)
        ll.addLayout(er)
        self.lbl_list = QLabel('カット一覧')
        self.lbl_list.setStyleSheet('font-weight:bold; padding:2px 4px;')
        ll.addWidget(self.lbl_list)
        self.list_stack = QStackedWidget()
        self.list_stack.addWidget(self.list)
        self.list_stack.addWidget(self.list_hint)
        ll.addWidget(self.list_stack, 1)
        self.cb_follow = QCheckBox('再生位置に一覧を追従')
        self.cb_follow.setChecked(True)
        ll.addWidget(self.cb_follow)

        # 右：プレーヤー
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.video = QVideoWidget()
        self.video.setMinimumSize(480, 270)
        self.video.setStyleSheet('background:black;')
        rl.addWidget(self.video, 1)
        self.timeline = Timeline()
        self.timeline.seek.connect(self.seek_to)
        rl.addWidget(self.timeline)

        ctl = QHBoxLayout()
        def tbtn(icon, tip, fn, text=None):
            b = QToolButton()
            if icon is not None:
                b.setIcon(st.standardIcon(icon))
            if text:
                b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b.setAutoRaise(True)
            b.setIconSize(QSize(22, 22))
            ctl.addWidget(b)
            return b
        tbtn(QStyle.SP_MediaSkipBackward, '前のカット (PageUp)', self.prev_cut)
        tbtn(None, '1フレーム戻る (,)', lambda: self.step_frame(-1), text='◀1F')
        self.btn_play = tbtn(QStyle.SP_MediaPlay, '再生/一時停止 (Space)', self.toggle_play)
        tbtn(None, '1フレーム進む (.)', lambda: self.step_frame(1), text='1F▶')
        tbtn(QStyle.SP_MediaSkipForward, '次のカット (PageDown)', self.next_cut)
        self.lbl_time = QLabel('--:--:--.---')
        f = QFont('Consolas')
        f.setPointSize(11)
        self.lbl_time.setFont(f)
        self.lbl_time.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ctl.addSpacing(8)
        ctl.addWidget(self.lbl_time, 1)
        self.vol = QSlider(Qt.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setFixedWidth(100)
        self.vol.setToolTip('音量')
        ctl.addWidget(QLabel('音量'))
        ctl.addWidget(self.vol)
        rl.addLayout(ctl)

        rng = QHBoxLayout()
        b_in = QPushButton('In [I]')
        b_in.setToolTip('現在のフレームを範囲の開始にする')
        b_in.clicked.connect(self.set_in)
        b_out = QPushButton('Out [O]')
        b_out.setToolTip('現在のフレームまでを範囲の終了にする（このフレームを含む）')
        b_out.clicked.connect(self.set_out)
        b_clr = QPushButton('範囲クリア')
        b_clr.clicked.connect(self.clear_range)
        self.lbl_range = QLabel('範囲: 未設定')
        self.btn_export_range = QPushButton(st.standardIcon(QStyle.SP_DialogSaveButton), ' 範囲を無劣化出力 (Ctrl+E)')
        self.btn_export_range.clicked.connect(self.export_range)
        self.btn_export_range.setStyleSheet('font-weight:bold;')
        b_add = QPushButton('＋ ここにカット追加 [C]')
        b_add.setToolTip('現在のフレームをカットの先頭として追加（検出漏れの修正）')
        b_add.clicked.connect(self.add_cut_here)
        rng.addWidget(b_in)
        rng.addWidget(b_out)
        rng.addWidget(b_clr)
        rng.addWidget(self.lbl_range, 1)
        rng.addWidget(b_add)
        rng.addWidget(self.btn_export_range)
        rl.addLayout(rng)

        self.lbl_info = QLabel('')
        self.lbl_info.setStyleSheet('color:#999;')
        rl.addWidget(self.lbl_info)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([700, 800])

        central = QWidget()
        cl = QVBoxLayout(central)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.addWidget(tb)
        cl.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(320)
        self.progress.setTextVisible(True)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage('動画ファイル（全話まとめて可）またはフォルダをドラッグ＆ドロップしてください')
        self._update_enabled()
        self.show_unanalyzed()

    def _build_player(self):
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.positionChanged.connect(self.on_position)
        self.player.playbackStateChanged.connect(self.on_play_state)
        self.player.mediaStatusChanged.connect(self.on_media_status)
        self.player.errorOccurred.connect(lambda e, s: self.statusBar().showMessage(f'再生エラー: {s}'))
        self.vol.valueChanged.connect(lambda v: self.audio.setVolume(v / 100.0))

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.setContext(Qt.WindowShortcut)
            s.activated.connect(fn)
        sc(Qt.Key_Space, self.toggle_play)
        sc(Qt.Key_Comma, lambda: self.step_frame(-1))
        sc(Qt.Key_Period, lambda: self.step_frame(1))
        sc(Qt.Key_PageUp, self.prev_cut)
        sc(Qt.Key_PageDown, self.next_cut)
        sc(Qt.Key_I, self.set_in)
        sc(Qt.Key_O, self.set_out)
        sc(Qt.Key_C, self.add_cut_here)
        sc('Ctrl+E', self.export_range)
        sc('Ctrl+O', self.open_dialog)
        sc('Ctrl+S', self.save_project)
        sc('Ctrl+PgUp', lambda: self.step_episode(-1))
        sc('Ctrl+PgDown', lambda: self.step_episode(1))
        sc(Qt.Key_Escape, self.clear_range)

    def _make_placeholder(self):
        pm = QPixmap(THUMB_W, THUMB_H)
        pm.fill(QColor('#2b2d31'))
        p = QPainter(pm)
        p.setPen(QColor('#777'))
        p.drawText(pm.rect(), Qt.AlignCenter, '読み込み中…')
        p.end()
        return pm

    def _restore_settings(self):
        s = self.settings
        d = E.DetectParams()
        self.sp_th.setValue(float(s.value('threshold', d.threshold)))
        self.sp_ra.setValue(float(s.value('ratio', d.ratio)))
        self.sp_mf.setValue(int(s.value('min_frames', d.min_frames)))
        self.cb_hw.setChecked(s.value('hwaccel', 'false') in (True, 'true'))
        self.vol.setValue(int(s.value('volume', 70)))
        self.audio.setVolume(self.vol.value() / 100.0)
        g = s.value('geometry')
        if g is not None:
            self.restoreGeometry(g)
        sp = s.value('splitter')
        if sp is not None:
            self.splitter.restoreState(sp)

    def closeEvent(self, ev):
        s = self.settings
        s.setValue('threshold', self.sp_th.value())
        s.setValue('ratio', self.sp_ra.value())
        s.setValue('min_frames', self.sp_mf.value())
        s.setValue('hwaccel', self.cb_hw.isChecked())
        s.setValue('volume', self.vol.value())
        s.setValue('geometry', self.saveGeometry())
        s.setValue('splitter', self.splitter.saveState())
        self.thumb_gen += 1
        self.queue.clear()
        for w in self.detect_workers:
            w.cancel_ev.set()
        for w in self.detect_workers:
            w.wait(3000)
        self.player.stop()
        self.pool.clear()
        self.kf_pool.clear()
        self.pool.waitForDone(5000)
        self.kf_pool.waitForDone(5000)
        super().closeEvent(ev)

    def params(self) -> E.DetectParams:
        return E.DetectParams(threshold=self.sp_th.value(), ratio=self.sp_ra.value(), min_frames=self.sp_mf.value())

    def reset_params(self):
        d = E.DetectParams()
        self.sp_th.setValue(d.threshold)
        self.sp_ra.setValue(d.ratio)
        self.sp_mf.setValue(d.min_frames)

    # ------------------------------------------------------------------ 現在の話へのショートカット
    # 既存コードは self.src / self.info / self.an ... を参照するので、表示中の話（self.ep）へ委譲する
    @property
    def src(self):
        return self.ep.path if self.ep else None

    @property
    def info(self):
        return self.ep.info if self.ep else None

    @property
    def an(self):
        return self.ep.an if self.ep else None

    @property
    def kfs(self):
        return (self.ep.kfs or []) if self.ep else []

    @property
    def added(self):
        return self.ep.added if self.ep else set()

    @property
    def removed(self):
        return self.ep.removed if self.ep else set()

    @property
    def thumbs(self):
        return self.ep.thumbs if self.ep else {}

    @property
    def rin(self):
        return self.ep.rin if self.ep else None

    @rin.setter
    def rin(self, v):
        if self.ep:
            self.ep.rin = v

    @property
    def rout(self):
        return self.ep.rout if self.ep else None

    @rout.setter
    def rout(self, v):
        if self.ep:
            self.ep.rout = v

    def _update_enabled(self):
        busy = any(w.isRunning() for w in self.detect_workers)
        exporting = bool(self.export_worker and self.export_worker.isRunning())
        has = self.ep is not None and self.ep.info is not None
        self.btn_detect.setEnabled(has and self.ep.state == '')
        self.btn_detect_all.setEnabled(any(e.an is None and e.state == '' for e in self.episodes))
        self.btn_cancel.setEnabled(busy or exporting or bool(self.queue))
        self.btn_export_ts.setEnabled(bool(self.cuts))
        self.btn_export_range.setEnabled(has and self.ep.kfs is not None and not exporting)
        self.btn_proj_save.setEnabled(bool(self.episodes))
        n = len(self.episodes)
        i = self.episodes.index(self.ep) if self.ep in self.episodes else -1
        self.btn_ep_prev.setEnabled(i > 0)
        self.btn_ep_next.setEnabled(0 <= i < n - 1)
        self.cmb_ep.setEnabled(n > 0)

    def kfs_ready(self):
        return self.ep is not None and self.ep.kfs is not None

    # ------------------------------------------------------------------ open
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.on_drop_paths(paths)

    def on_drop_paths(self, paths):
        if len(paths) == 1 and paths[0].lower().endswith(PROJECT_EXT):
            self.load_project(paths[0])
            return
        self.open_paths(paths, ask=True)

    def open_dialog(self):
        start = self.settings.value('last_open_dir', '')
        exts = ' '.join('*' + e for e in sorted(VIDEO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, '動画を開く（複数選択で全話まとめて）', start, f'動画 ({exts});;すべてのファイル (*.*)')
        if paths:
            self.open_paths(paths, ask=True)

    def open_folder_dialog(self):
        start = self.settings.value('last_open_dir', '')
        d = QFileDialog.getExistingDirectory(self, '作品フォルダを開く（中の動画をすべて読み込み）', start)
        if d:
            self.open_paths([d], ask=True)

    def open_file(self, path):
        """互換用：1ファイルを開く"""
        self.open_paths([path])

    def open_paths(self, paths, ask=False):
        files = collect_videos(paths)
        if not files:
            QMessageBox.information(self, '読み込み', '動画ファイルが見つかりませんでした。')
            return
        if not self.ffprobe:
            QMessageBox.critical(self, 'エラー', 'ffprobe が見つかりません。')
            return
        mode = 'replace'
        if ask and self.episodes:
            mb = QMessageBox(self)
            mb.setWindowTitle('読み込み')
            mb.setText(f'{len(files)} 本の動画を読み込みます。\n今の {len(self.episodes)} 話はどうしますか？')
            b_rep = mb.addButton('置き換える（別の作品）', QMessageBox.AcceptRole)
            b_add = mb.addButton('追加する（同じ作品の続き）', QMessageBox.AcceptRole)
            mb.addButton('キャンセル', QMessageBox.RejectRole)
            mb.exec()
            if mb.clickedButton() is b_add:
                mode = 'add'
            elif mb.clickedButton() is not b_rep:
                return
        self.settings.setValue('last_open_dir', os.path.dirname(files[0]))
        if mode == 'replace':
            self.clear_episodes()
        known = {os.path.normcase(e.path) for e in self.episodes}
        new = []
        for f in files:
            if os.path.normcase(os.path.normpath(f)) in known:
                continue
            ep = Episode(f)
            self.apply_cache(ep)
            new.append(ep)
        self.add_episodes(new)
        n_cached = sum(1 for e in new if e.an is not None)
        msg = f'{len(new)} 話を読み込みました'
        if n_cached:
            msg += f'（うち {n_cached} 話は前回の解析結果あり）'
        self.statusBar().showMessage(msg + '。カット検出は「解析」/「全話解析」で行います。', 15000)

    def apply_cache(self, ep):
        """以前に解析したファイルなら、その結果を読み込む（解析はしない）"""
        cache = E.load_cache(ep.path)
        if cache:
            ep.an = cache['analysis']
            ep.kfs = cache.get('keyframes') or None
            ep.added = set(cache.get('added') or [])
            ep.removed = set(cache.get('removed') or [])

    def clear_episodes(self):
        self.queue.clear()
        for w in self.detect_workers:
            w.cancel_ev.set()
        for w in self.detect_workers:
            w.wait(5000)
        self.kf_pool.clear()
        self.player.stop()
        self.player.setSource(QUrl())
        self.player_path = None
        self.episodes = []
        self.ep = None
        self.cmb_ep.blockSignals(True)
        self.cmb_ep.clear()
        self.cmb_ep.blockSignals(False)
        self.show_episode(None)

    def add_episodes(self, eps):
        if not eps:
            self._update_enabled()
            return
        first_new = len(self.episodes)
        self.episodes += eps
        self.cmb_ep.blockSignals(True)
        for e in eps:
            self.cmb_ep.addItem(e.name)
        self.cmb_ep.blockSignals(False)
        self.update_counts()
        self.refresh_episode_combo()
        # probe/キーフレーム取得をバックグラウンドで（表示する話を先に）
        target = self.ep if self.ep is not None else eps[0]
        for e in sorted(eps, key=lambda x: x is not target):
            if e.kfs is None or e.info is None:
                self.kf_pool.start(KfTask(e, self.ffprobe, self.kf_sig))
        if self.ep is None:
            self.cmb_ep.setCurrentIndex(first_new)
            self.on_episode_changed(first_new)
        self._update_enabled()

    def on_keyframes(self, ep, info, kfs, err):
        if ep not in self.episodes:
            return
        if err:
            self.statusBar().showMessage(f'{ep.name}: 読み込みエラー {err[:200]}', 15000)
            return
        ep.info = ep.info or info
        if ep.kfs is None:
            ep.kfs = kfs
            if ep.an is not None:
                self.save_state(ep)
        if ep is self.ep:
            if ep.an is not None:
                self.rebuild_list()
            self.update_info_label()
            self.update_range_label()
            self._update_enabled()
        self.refresh_episode_combo()

    # ------------------------------------------------------------------ 話の切り替え
    def episode_label(self, i, e):
        if e.state == 'running':
            st = f'解析中 {e.progress * 100:.0f}%'
        elif e.state == 'queued':
            st = '解析待ち'
        elif e.an is not None:
            st = f'解析済 {e.ncuts}カット'
        else:
            st = '未解析'
        return f'{i + 1:02d}. {e.name}   [{st}]'

    def refresh_episode_combo(self):
        for i, e in enumerate(self.episodes):
            if i < self.cmb_ep.count():
                t = self.episode_label(i, e)
                if self.cmb_ep.itemText(i) != t:
                    self.cmb_ep.setItemText(i, t)
        n_done = sum(1 for e in self.episodes if e.an is not None)
        if self.episodes:
            self.lbl_eps = f'{len(self.episodes)} 話（解析済 {n_done}）'
            self.cmb_ep.setToolTip(self.lbl_eps)

    def update_counts(self):
        """パラメータ変更時など：解析済みの全話のカット数を更新"""
        p = self.params()
        for e in self.episodes:
            if e.an is not None and e is not self.ep:
                cuts = (set(E.find_cuts(e.an, p)) - e.removed) | e.added | {0}
                e.ncuts = len(cuts)

    def step_episode(self, d):
        if not self.episodes:
            return
        i = self.cmb_ep.currentIndex() + d
        if 0 <= i < len(self.episodes):
            self.cmb_ep.setCurrentIndex(i)

    def on_episode_changed(self, i):
        if not (0 <= i < len(self.episodes)):
            return
        ep = self.episodes[i]
        if ep is self.ep:
            return
        if self.ep is not None:
            self.ep.last_pos = self.player.position()
        self.player.pause()
        self.show_episode(ep)

    def show_episode(self, ep):
        self.ep = ep
        self.thumb_gen += 1
        self.thumb_pending.clear()
        self.pool.clear()
        self.list.blockSignals(True)
        self.list.clear()
        self.list.blockSignals(False)
        self.cuts, self.cut_times, self.auto_cuts = [], [], []
        self.cur_cut = -1
        self.timeline.cuts = []
        self.timeline.current_cut = -1
        if ep is None:
            self.setWindowTitle('anicut')
            self.lbl_info.setText('')
            self.timeline.duration = 0.0
            self.lbl_time.setText('--:--:--.---')
            self.show_unanalyzed()
            self.update_range_label()
            self._update_enabled()
            return
        if ep.info is None:
            try:
                ep.info = E.probe(ep.path, self.ffprobe)
            except Exception as ex:  # noqa: BLE001
                self.lbl_info.setText(f'読み込みエラー: {ex}')
                self.show_unanalyzed()
                self._update_enabled()
                return
        idx = self.episodes.index(ep)
        self.setWindowTitle(f'anicut - [{idx + 1}/{len(self.episodes)}] {ep.name}')
        self.update_info_label()
        self.timeline.duration = ep.info.duration
        if self.player_path != ep.path:
            self.player_path = ep.path
            self.pending_pos = ep.last_pos or None
            self.player.setSource(QUrl.fromLocalFile(ep.path))
            self.player.pause()
        if ep.an is not None:
            self.recalc_cuts()
        else:
            self.show_unanalyzed()
        self.update_range_label()
        self.on_position(self.player.position())
        self._update_enabled()

    def update_info_label(self):
        ep = self.ep
        if ep is None or ep.info is None:
            return
        info = ep.info
        self.lbl_info.setText(
            f'{ep.name}   {info.width}x{info.height}  {info.vcodec}  {info.fps:.3f}fps  {E.fmt_tc(info.duration)}  '
            f'音声{info.nb_audio}  字幕{info.nb_subs}' + ('' if ep.kfs is not None else '  （キーフレーム取得中…）'))

    def on_media_status(self, st):
        if st in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self.pending_pos is not None:
            pos, self.pending_pos = self.pending_pos, None
            self.player.setPosition(pos)
            self.on_position(pos)

    def show_unanalyzed(self):
        """未解析：カット一覧の代わりに案内を出す（手動切り出しモード）"""
        self.list_stack.setCurrentWidget(self.list_hint)
        self.timeline.update()
        if self.ep is None:
            self.lbl_list.setText('カット一覧')
            self.list_hint.setText('動画ファイル（複数可）や作品フォルダをここにドラッグ＆ドロップ\n'
                                   'または「開く」「フォルダ」\n\n'
                                   f'保存した解析結果（*{PROJECT_EXT}）もドロップで読み込めます')
        elif self.ep.state:
            self.lbl_list.setText('カット一覧（解析中）')
            self.list_hint.setText('解析中です… 完了するとここにカット一覧が表示されます。\n\n'
                                   'その間も右のプレーヤーで In [I] / Out [O] を指定して無劣化出力できます。')
        else:
            self.lbl_list.setText('カット一覧（未解析）')
            self.list_hint.setText('手動切り出しモード（未解析）\n\n'
                                   '右のプレーヤーで In [I] / Out [O] を指定し、\n「範囲を無劣化出力」(Ctrl+E) で切り出せます。\n\n'
                                   'カットを自動検出するには「解析」（この話）\nまたは「全話解析」を押してください。')

    # ------------------------------------------------------------------ 解析（キュー）
    def start_detect(self):
        ep = self.ep
        if ep is None or not self.ffmpeg or ep.state:
            return
        if ep.an is not None:
            msg = 'この話は解析済みです。再解析しますか？'
            if ep.added or ep.removed:
                msg += '\n（手動で追加/削除したカットはリセットされます）'
            if QMessageBox.question(self, '再解析', msg) != QMessageBox.Yes:
                return
        self.enqueue([ep], front=True)

    def start_detect_all(self):
        todo = [e for e in self.episodes if e.an is None and e.state == '']
        if not todo:
            return
        if self.ep in todo:   # 表示中の話を最初に
            todo.remove(self.ep)
            todo.insert(0, self.ep)
        self.enqueue(todo)

    def enqueue(self, eps, front=False):
        for e in eps:
            e.state = 'queued'
            e.progress = 0.0
        self.queue = (eps + self.queue) if front else (self.queue + eps)
        self.refresh_episode_combo()
        if self.ep in eps and self.ep.an is None:
            self.show_unanalyzed()
        self.run_next()
        self._update_enabled()

    def run_next(self):
        done = self.sender() if isinstance(self.sender(), DetectWorker) else None
        # 終わった worker は自分の finished 通知の時だけ外す（先に参照を消すと完了通知が届かなくなる）
        self.detect_workers = [w for w in self.detect_workers if w is not done]
        while self.queue and sum(not w.isFinished() for w in self.detect_workers) < MAX_PARALLEL:
            ep = self.queue.pop(0)
            if ep not in self.episodes:
                continue
            if ep.info is None:
                try:
                    ep.info = E.probe(ep.path, self.ffprobe)
                except Exception as ex:  # noqa: BLE001
                    ep.state = ''
                    self.statusBar().showMessage(f'{ep.name}: {ex}', 10000)
                    continue
            ep.state = 'running'
            w = DetectWorker(ep, self.ffmpeg, self.ffprobe, self.cb_hw.isChecked())
            w.progress.connect(self.on_detect_progress)
            w.done.connect(self.on_detect_done)
            w.failed.connect(self.on_detect_failed)
            w.cancelled.connect(self.on_detect_cancelled)
            w.finished.connect(self.run_next)
            w.finished.connect(self._update_enabled)
            self.detect_workers.append(w)
            self.progress.setRange(0, 1000)
            self.progress.show()
            self.statusBar().showMessage(f'解析中: {ep.name}（全フレームを解析しています）')
            self.refresh_episode_combo()
            if ep is self.ep:
                self.show_unanalyzed() if ep.an is None else None
            w.start()
        if self.detect_workers:
            self.update_detect_progress()
        else:
            self.progress.hide()
        self._update_enabled()

    def on_detect_progress(self, frac, n):
        w = self.sender()
        ep = getattr(w, 'ep', None)
        if ep is None:
            return
        ep.progress = frac
        self.update_detect_progress()
        now = time.monotonic()
        if now - self._combo_refreshed > 1.0:
            self._combo_refreshed = now
            self.refresh_episode_combo()

    def update_detect_progress(self):
        eps = [w.ep for w in self.detect_workers if w.isRunning()]
        if not eps:
            return
        left = len(self.queue)
        self.progress.setValue(int(sum(e.progress for e in eps) / len(eps) * 1000))
        if len(eps) == 1:
            text = f'{eps[0].name[:24]} {eps[0].progress * 100:.0f}%'
        else:
            text = f'{len(eps)}話同時に解析中 ' + ' / '.join(f'{e.progress * 100:.0f}%' for e in eps)
        self.progress.setFormat(text + (f'（残り{left}話）' if left else ''))

    def on_detect_done(self, an, kfs):
        ep = self.sender().ep
        ep.an = an
        if ep.kfs is None:
            ep.kfs = kfs
        ep.added, ep.removed = set(), set()
        ep.state = ''
        ep.thumbs.clear()
        if ep is self.ep:
            self.recalc_cuts()
        else:
            cuts = set(E.find_cuts(an, self.params())) | {0}
            ep.ncuts = len(cuts)
            self.save_state(ep)
        self.refresh_episode_combo()
        self.statusBar().showMessage(f'解析完了: {ep.name}  {ep.ncuts} カット / {len(an.times)} フレーム', 10000)

    def on_detect_failed(self, msg):
        ep = self.sender().ep
        ep.state = ''
        self.refresh_episode_combo()
        if ep is self.ep:
            self.show_unanalyzed() if ep.an is None else None
        self.statusBar().showMessage(f'解析に失敗しました: {ep.name}')
        QMessageBox.critical(self, '解析エラー', f'{ep.name}\n\n{msg[:3000]}')

    def on_detect_cancelled(self):
        ep = self.sender().ep
        ep.state = ''
        self.refresh_episode_combo()
        if ep is self.ep and ep.an is None:
            self.show_unanalyzed()
        self.statusBar().showMessage('解析を中止しました', 5000)

    def cancel_work(self):
        for e in self.queue:
            e.state = ''
        self.queue.clear()
        for w in self.detect_workers:
            w.cancel_ev.set()
        if self.export_worker and self.export_worker.isRunning():
            self.export_worker.cancel_ev.set()
        self.refresh_episode_combo()
        if self.ep is not None and self.ep.an is None and not self.ep.state:
            self.show_unanalyzed()
        self._update_enabled()

    # ------------------------------------------------------------------ 解析結果の保存 / 読み込み
    def save_project(self):
        if not self.episodes:
            return
        busy = [e for e in self.episodes if e.state]
        if busy and QMessageBox.question(
                self, '保存', f'{len(busy)} 話が解析中/解析待ちです（それらは未解析として保存されます）。保存しますか？'
        ) != QMessageBox.Yes:
            return
        first = self.episodes[0].path
        folder = os.path.dirname(first)
        last = self.settings.value('last_project', '')
        if last and os.path.dirname(last) == folder:
            default = last
        else:
            default = os.path.join(folder, (os.path.basename(folder) or 'anicut') + PROJECT_EXT)
        out, _ = QFileDialog.getSaveFileName(self, '解析結果を保存', default, f'anicut 解析結果 (*{PROJECT_EXT})')
        if not out:
            return
        if not out.lower().endswith(PROJECT_EXT):
            out += PROJECT_EXT
        eps = []
        for e in self.episodes:
            try:
                size = os.path.getsize(e.path)
            except OSError:
                size = None
            d = {'path': e.path, 'name': e.name, 'size': size,
                 'added': sorted(e.added), 'removed': sorted(e.removed),
                 'keyframes': e.kfs, 'in': e.rin, 'out': e.rout}
            if e.an is not None:
                d['analysis'] = {'times': e.an.times, 'scores': e.an.scores, 'fps': e.an.fps}
            eps.append(d)
        data = {'app': 'anicut', 'version': 1, 'params': {
            'threshold': self.sp_th.value(), 'ratio': self.sp_ra.value(), 'min_frames': self.sp_mf.value()},
            'current': self.episodes.index(self.ep) if self.ep in self.episodes else 0, 'episodes': eps}
        try:
            tmp = out + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            os.replace(tmp, out)
        except OSError as ex:
            QMessageBox.critical(self, '保存エラー', str(ex))
            return
        self.settings.setValue('last_project', out)
        n = sum(1 for e in self.episodes if e.an is not None)
        self.statusBar().showMessage(f'保存しました（{len(self.episodes)} 話、うち解析済 {n} 話）: {out}', 15000)

    def load_project_dialog(self):
        last = self.settings.value('last_project', '') or self.settings.value('last_open_dir', '')
        path, _ = QFileDialog.getOpenFileName(self, '解析結果を読み込み', last,
                                              f'anicut 解析結果 (*{PROJECT_EXT});;すべてのファイル (*.*)')
        if path:
            self.load_project(path)

    def load_project(self, path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            if data.get('app') != 'anicut':
                raise ValueError('anicut の解析結果ファイルではありません')
        except (OSError, ValueError) as ex:
            QMessageBox.critical(self, '読み込みエラー', f'{path}\n\n{ex}')
            return
        pdir = os.path.dirname(os.path.abspath(path))
        eps, missing, changed = [], [], []
        for d in data.get('episodes', []):
            p = d.get('path', '')
            if not os.path.isfile(p):
                # 動画を移動した場合：解析結果ファイルと同じフォルダを探す
                alt = os.path.join(pdir, d.get('name') or os.path.basename(p))
                if os.path.isfile(alt):
                    p = alt
                else:
                    missing.append(d.get('name') or p)
                    continue
            e = Episode(p)
            e.added = set(d.get('added') or [])
            e.removed = set(d.get('removed') or [])
            e.rin, e.rout = d.get('in'), d.get('out')
            an = d.get('analysis')
            size_ok = d.get('size') is None or d.get('size') == os.path.getsize(p)
            if an and size_ok:
                e.an = E.Analysis(times=an['times'], scores=an['scores'], fps=an['fps'])
                e.kfs = d.get('keyframes')
            elif an:
                changed.append(e.name)
                e.added, e.removed = set(), set()
            elif size_ok:
                e.kfs = d.get('keyframes')
            eps.append(e)
        if not eps:
            QMessageBox.warning(self, '読み込み', '動画ファイルが1つも見つかりませんでした。\n\n' + '\n'.join(missing[:30]))
            return
        self.clear_episodes()
        p = data.get('params') or {}
        for w in (self.sp_th, self.sp_ra, self.sp_mf):
            w.blockSignals(True)
        self.sp_th.setValue(p.get('threshold', self.sp_th.value()))
        self.sp_ra.setValue(p.get('ratio', self.sp_ra.value()))
        self.sp_mf.setValue(p.get('min_frames', self.sp_mf.value()))
        for w in (self.sp_th, self.sp_ra, self.sp_mf):
            w.blockSignals(False)
        for e in eps:   # 個別キャッシュにも反映（次に単体で開いたときも使える）
            if e.an is not None:
                self.save_state(e)
        cur = min(max(int(data.get('current') or 0), 0), len(eps) - 1)
        self.add_episodes(eps)
        if cur:
            self.cmb_ep.setCurrentIndex(cur)
        self.settings.setValue('last_project', path)
        n = sum(1 for e in eps if e.an is not None)
        self.statusBar().showMessage(f'解析結果を読み込みました: {len(eps)} 話（解析済 {n} 話）', 15000)
        warn = ''
        if missing:
            warn += f'見つからない動画（{len(missing)}）:\n' + '\n'.join(missing[:20]) + '\n\n'
        if changed:
            warn += (f'ファイルが変更されているため解析結果を破棄した話（{len(changed)}）:\n'
                     + '\n'.join(changed[:20]))
        if warn:
            QMessageBox.warning(self, '読み込みの注意', warn)

    # ------------------------------------------------------------------ cuts
    def recalc_cuts(self):
        if self.an is None:
            self.show_unanalyzed()
            return
        self.auto_cuts = E.find_cuts(self.an, self.params())
        cuts = (set(self.auto_cuts) - self.removed) | self.added
        cuts.add(0)
        self.cuts = sorted(c for c in cuts if 0 <= c < len(self.an.times))
        self.cut_times = [self.an.times[c] for c in self.cuts]
        self.timeline.cuts = self.cut_times
        self.timeline.update()
        self.ep.ncuts = len(self.cuts)
        self.rebuild_list()
        self.save_state()
        self.refresh_episode_combo()
        self._update_enabled()

    def save_state(self, ep=None):
        ep = ep or self.ep
        if ep is not None and ep.an is not None:
            E.save_cache(ep.path, ep.an, ep.kfs or [], list(ep.added), list(ep.removed), self.params())

    def cut_range(self, i):
        a = self.cut_times[i]
        b = self.cut_times[i + 1] if i + 1 < len(self.cut_times) else self.info.duration
        return a, b

    def rebuild_list(self):
        sel_frames = {self.cuts_item_frame(it) for it in self.list.selectedItems()}
        self.list.blockSignals(True)
        self.list.clear()
        self.thumb_gen += 1
        self.thumb_pending.clear()
        self.pool.clear()
        fps = self.info.fps
        for i, f in enumerate(self.cuts):
            a, b = self.cut_range(i)
            plan = E.plan_export(a, b, self.kfs, fps, self.info.duration)
            mark = '◆' if plan.lead_frames == 0 else '◇'
            flag = ' ＋' if f in self.added else ''
            text = f'#{i + 1:03d} {E.fmt_tc(a)}\n{mark} {b - a:.2f}s  F{f}{flag}'
            it = QListWidgetItem(QIcon(self.thumbs.get(f, self._placeholder)), text)
            it.setData(Qt.UserRole, i)
            it.setData(Qt.UserRole + 1, f)
            it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            tip = (f'カット #{i + 1}\n開始 {E.fmt_tc(a)}（フレーム {f}）\n終了 {E.fmt_tc(b)}\n長さ {b - a:.3f} 秒'
                   f'（{int(round((b - a) * fps))} フレーム）\n')
            if plan.lead_frames == 0:
                tip += '◆ 開始がキーフレーム：無劣化でもぴったり切り出せます'
            else:
                tip += (f'◇ 開始がキーフレームではありません：無劣化出力では {plan.lead_frames} フレーム手前の'
                        f'キーフレーム（{E.fmt_tc(plan.actual_start)}）から始まります')
            if f in self.added:
                tip += '\n＋ 手動で追加したカット'
            it.setToolTip(tip)
            self.list.addItem(it)
            if f in sel_frames:
                it.setSelected(True)
        self.list.blockSignals(False)
        self.list_stack.setCurrentWidget(self.list)
        self.lbl_list.setText(f'カット一覧（{len(self.cuts)} カット）  ◆=キーフレーム開始 ◇=無劣化だと少し手前から')
        self.cur_cut = -1
        self.on_position(self.player.position())
        self.request_thumbs()

    @staticmethod
    def cuts_item_frame(it):
        return it.data(Qt.UserRole + 1)

    def request_thumbs(self):
        if not self.src or not self.ffmpeg:
            return
        gen = self.thumb_gen
        alive = lambda g: g == self.thumb_gen  # noqa: E731
        for i, f in enumerate(self.cuts):
            if f in self.thumbs or f in self.thumb_pending:
                continue
            self.thumb_pending.add(f)
            self.pool.start(ThumbTask(gen, f, self.cut_times[i], self.src, self.ffmpeg, self.info.fps,
                                      self.thumb_sig, alive))

    def on_thumb(self, gen, frame, data):
        self.thumb_pending.discard(frame)
        if gen != self.thumb_gen or not data:
            return
        img = QImage.fromData(data)
        if img.isNull():
            return
        pm = QPixmap.fromImage(img.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.thumbs[frame] = pm
        try:
            i = self.cuts.index(frame)
        except ValueError:
            return
        it = self.list.item(i)
        if it is not None:
            it.setIcon(QIcon(pm))

    def selected_cut_indices(self):
        return sorted(it.data(Qt.UserRole) for it in self.list.selectedItems())

    def on_selection(self):
        idx = self.selected_cut_indices()
        if not idx or self.info is None:
            return
        a, _ = self.cut_range(idx[0])
        _, b = self.cut_range(idx[-1])
        self.set_range(a, b)

    def on_item_double(self, it):
        i = it.data(Qt.UserRole)
        self.seek_to(self.cut_times[i])
        self.player.play()

    def add_cut_here(self):
        if self.an is None:
            return
        f = self.frame_at(self.cur_ms() / 1000.0)
        if f <= 0 or f in self.cuts:
            self.statusBar().showMessage('ここには既にカットがあります', 3000)
            return
        self.removed.discard(f)
        if f not in self.auto_cuts:
            self.added.add(f)
        self.recalc_cuts()
        self.statusBar().showMessage(f'フレーム {f} にカットを追加しました', 4000)

    def remove_cuts(self, indices):
        for i in indices:
            f = self.cuts[i]
            if f == 0:
                continue
            if f in self.added:
                self.added.discard(f)
            else:
                self.removed.add(f)
        self.recalc_cuts()

    # ------------------------------------------------------------------ context menu
    def on_context(self, pos):
        it = self.list.itemAt(pos)
        if it is None or self.info is None:
            return
        if not it.isSelected():
            self.list.clearSelection()
            it.setSelected(True)
        idx = self.selected_cut_indices()
        i = it.data(Qt.UserRole)
        m = QMenu(self)
        a_play = m.addAction('ここから再生')
        m.addSeparator()
        if len(idx) == 1:
            a_save1 = m.addAction(f'カット #{i + 1} を保存（無劣化）…')
            a_saveN = None
            a_saveR = None
        else:
            a_save1 = None
            a_saveN = m.addAction(f'選択した {len(idx)} カットを個別に保存（無劣化）…')
            a_saveR = m.addAction(f'カット #{idx[0] + 1}〜#{idx[-1] + 1} を1本にまとめて保存（無劣化）…')
        a_all = m.addAction(f'全 {len(self.cuts)} カットを個別に保存（無劣化）…')
        m.addSeparator()
        a_copy = m.addAction('タイムスタンプをコピー')
        a_range = m.addAction('選択カットを In/Out 範囲に設定')
        m.addSeparator()
        rem = [j for j in idx if self.cuts[j] != 0]
        a_del = m.addAction(f'カット点を削除（前のカットと結合）×{len(rem)}') if rem else None
        act = m.exec(self.list.viewport().mapToGlobal(pos))
        if act is None:
            return
        if act == a_play:
            self.on_item_double(it)
        elif act == a_save1:
            self.save_cuts([i], single=True)
        elif act == a_saveN:
            self.save_cuts(idx)
        elif act == a_saveR:
            a, _ = self.cut_range(idx[0])
            _, b = self.cut_range(idx[-1])
            self.export_span(a, b, label=f'cut{idx[0] + 1:03d}-{idx[-1] + 1:03d}')
        elif act == a_all:
            self.save_cuts(list(range(len(self.cuts))))
        elif act == a_copy:
            lines = [f'{E.fmt_tc(self.cut_times[j])}\t#{j + 1}' for j in idx]
            QGuiApplication.clipboard().setText('\n'.join(lines))
            self.statusBar().showMessage(f'{len(lines)} 件のタイムスタンプをコピーしました', 3000)
        elif act == a_range:
            self.on_selection()
        elif a_del is not None and act == a_del:
            self.remove_cuts(rem)

    # ------------------------------------------------------------------ export
    def _out_ext(self):
        ext = Path(self.src).suffix.lower()
        return ext if ext in VIDEO_EXTS else '.mkv'

    def _default_dir(self):
        d = self.settings.value('last_export_dir', '')
        return d if d and os.path.isdir(d) else os.path.dirname(self.src)

    def _confirm_lead(self, plans):
        bad = [p for p in plans if p.lead_frames > 0]
        if not bad:
            return True
        if len(plans) == 1:
            p = bad[0]
            msg = (f'開始位置 {E.fmt_tc(p.start)} はキーフレームではありません。\n\n'
                   f'無劣化（再エンコードなし）では、直前のキーフレーム {E.fmt_tc(p.actual_start)} から\n'
                   f'出力されます（{p.lead_frames} フレーム / {p.start - p.actual_start:.3f} 秒 前から）。\n\n続けますか？')
        else:
            mx = max(p.lead_frames for p in bad)
            msg = (f'{len(plans)} 本のうち {len(bad)} 本は開始位置がキーフレームではないため、\n'
                   f'直前のキーフレームから出力されます（最大 {mx} フレーム手前から）。\n\n続けますか？')
        return QMessageBox.question(self, '無劣化出力の注意', msg) == QMessageBox.Yes

    def keyframes_ready_or_warn(self):
        if self.ep is not None and self.ep.kfs is None:
            QMessageBox.information(self, 'お待ちください', 'キーフレーム情報を取得中です。数秒後にもう一度お試しください。')
            return False
        return True

    def save_cuts(self, indices, single=False):
        if not self.src or not self.keyframes_ready_or_warn():
            return
        stem = Path(self.src).stem
        ext = self._out_ext()
        fps, dur = self.info.fps, self.info.duration
        jobs = []
        if single:
            i = indices[0]
            a, b = self.cut_range(i)
            name = f'{stem}_cut{i + 1:03d}_{E.fmt_tc_file(a)}{ext}'
            out, _ = QFileDialog.getSaveFileName(self, 'カットを保存', os.path.join(self._default_dir(), name),
                                                 f'動画 (*{ext});;すべてのファイル (*.*)')
            if not out:
                return
            self.settings.setValue('last_export_dir', os.path.dirname(out))
            jobs.append((E.plan_export(a, b, self.kfs, fps, dur), out))
        else:
            d = QFileDialog.getExistingDirectory(self, f'{len(indices)} カットの保存先フォルダ', self._default_dir())
            if not d:
                return
            self.settings.setValue('last_export_dir', d)
            for i in indices:
                a, b = self.cut_range(i)
                jobs.append((E.plan_export(a, b, self.kfs, fps, dur),
                             os.path.join(d, f'{stem}_cut{i + 1:03d}_{E.fmt_tc_file(a)}{ext}')))
            exist = [o for _, o in jobs if os.path.exists(o)]
            if exist and QMessageBox.question(
                    self, '上書き確認', f'{len(exist)} 個のファイルが既に存在します。上書きしますか？') != QMessageBox.Yes:
                return
        if not self._confirm_lead([p for p, _ in jobs]):
            return
        self.run_export(jobs)

    def export_span(self, a, b, label=None):
        if not self.src or b <= a or not self.keyframes_ready_or_warn():
            return
        stem = Path(self.src).stem
        ext = self._out_ext()
        name = f'{stem}_{label + "_" if label else ""}{E.fmt_tc_file(a)}-{E.fmt_tc_file(b)}{ext}'
        out, _ = QFileDialog.getSaveFileName(self, '範囲を無劣化出力', os.path.join(self._default_dir(), name),
                                             f'動画 (*{ext});;すべてのファイル (*.*)')
        if not out:
            return
        self.settings.setValue('last_export_dir', os.path.dirname(out))
        plan = E.plan_export(a, b, self.kfs, self.info.fps, self.info.duration)
        if not self._confirm_lead([plan]):
            return
        self.run_export([(plan, out)])

    def export_range(self):
        if not self.src:
            return
        if self.rin is None or self.rout is None or self.rout <= self.rin:
            QMessageBox.information(self, '範囲が未設定',
                                    'In [I] / Out [O] で範囲を指定するか、カット一覧で出力したいカットを選択してください。\n'
                                    '（Shift / Ctrl + クリックで複数選択）')
            return
        self.export_span(self.rin, self.rout)

    def run_export(self, jobs):
        if self.export_worker and self.export_worker.isRunning():
            QMessageBox.information(self, '出力中', '前の出力が終わるまでお待ちください。')
            return
        if self.src and any(os.path.normcase(os.path.abspath(o)) == os.path.normcase(os.path.abspath(self.src))
                            for _, o in jobs):
            QMessageBox.critical(self, 'エラー', '元の動画と同じファイル名には出力できません。')
            return
        w = ExportWorker(self.src, jobs, self.ffmpeg, self.info)
        w.progress.connect(self.on_export_progress)
        w.finished_all.connect(self.on_export_done)
        w.finished.connect(self._update_enabled)
        self.export_worker = w
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.show()
        w.start()
        self._update_enabled()

    def on_export_progress(self, i, n, name):
        self.progress.setRange(0, n)
        self.progress.setValue(i)
        self.progress.setFormat(f'出力 {i}/{n}')
        if name:
            self.statusBar().showMessage(f'出力中: {name}')

    def on_export_done(self, ok, errs):
        self.progress.hide()
        if errs:
            QMessageBox.warning(self, '出力エラー', f'{len(ok)} 本成功、{len(errs)} 本失敗\n\n' + '\n\n'.join(errs)[:3000])
        msg = f'{len(ok)} 本を無劣化で出力しました'
        if ok:
            msg += f'： {os.path.dirname(ok[0])}'
        self.statusBar().showMessage(msg, 15000)

    def export_timestamps(self):
        if not self.cuts:
            return
        stem = Path(self.src).stem
        out, flt = QFileDialog.getSaveFileName(
            self, 'タイムスタンプ書き出し', os.path.join(self._default_dir(), f'{stem}_cuts.txt'),
            'テキスト (*.txt);;CSV (*.csv);;チャプター (ffmetadata) (*.ffmeta)')
        if not out:
            return
        fps = self.info.fps
        ext = Path(out).suffix.lower()
        try:
            if ext == '.csv':
                with open(out, 'w', newline='', encoding='utf-8-sig') as f:
                    w = csv.writer(f)
                    w.writerow(['No', '開始', '終了', '長さ(秒)', '開始フレーム', 'フレーム数', 'キーフレーム開始'])
                    for i, fr in enumerate(self.cuts):
                        a, b = self.cut_range(i)
                        p = E.plan_export(a, b, self.kfs, fps, self.info.duration)
                        nf = (self.cuts[i + 1] if i + 1 < len(self.cuts) else len(self.an.times)) - fr
                        w.writerow([i + 1, E.fmt_tc(a), E.fmt_tc(b), f'{b - a:.3f}', fr, nf,
                                    'はい' if p.lead_frames == 0 else 'いいえ'])
            elif ext == '.ffmeta':
                with open(out, 'w', encoding='utf-8') as f:
                    f.write(';FFMETADATA1\n')
                    for i in range(len(self.cuts)):
                        a, b = self.cut_range(i)
                        f.write(f'\n[CHAPTER]\nTIMEBASE=1/1000\nSTART={int(round(a * 1000))}\n'
                                f'END={int(round(b * 1000))}\ntitle=Cut {i + 1:03d}\n')
            else:
                with open(out, 'w', encoding='utf-8') as f:
                    for i in range(len(self.cuts)):
                        f.write(f'{E.fmt_tc(self.cut_times[i])}  #{i + 1:03d}  F{self.cuts[i]}\n')
        except OSError as ex:
            QMessageBox.critical(self, '書き出しエラー', str(ex))
            return
        self.settings.setValue('last_export_dir', os.path.dirname(out))
        self.statusBar().showMessage(f'書き出しました: {out}', 10000)

    # ------------------------------------------------------------------ player
    def frame_at(self, t):
        """時刻 t に表示されているフレーム番号"""
        if self.an is not None and self.an.times:
            return max(0, bisect_right(self.an.times, t + 0.0005) - 1)
        fps = self.info.fps if self.info else 24.0
        return int(t * fps + 1e-6)

    def frame_time(self, f):
        if self.an is not None and self.an.times:
            f = min(max(f, 0), len(self.an.times) - 1)
            return self.an.times[f]
        return f / (self.info.fps if self.info else 24.0)

    def seek_to(self, t):
        if self.info is None:
            return
        t = min(max(t, 0.0), self.info.duration)
        # フレーム開始時刻ちょうどは丸め誤差で前フレームになり得るので +2ms
        ms = int(t * 1000) + 2
        if self.player.mediaStatus() in (QMediaPlayer.LoadingMedia, QMediaPlayer.BufferingMedia,
                                         QMediaPlayer.NoMedia):
            self.pending_pos = ms     # 読み込み中のシークは捨てられることがあるので、完了後にもう一度
        self.player.setPosition(ms)
        self.on_position(ms)

    def cur_ms(self):
        return self.pending_pos if self.pending_pos is not None else self.player.position()

    def toggle_play(self):
        if self.src is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def on_play_state(self, st):
        icon = QStyle.SP_MediaPause if st == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))

    def step_frame(self, d):
        if self.info is None:
            return
        self.player.pause()
        f = self.frame_at(self.cur_ms() / 1000.0) + d
        self.seek_to(self.frame_time(f))

    def current_cut_index(self, t):
        if not self.cut_times:
            return -1
        return max(0, bisect_right(self.cut_times, t + 0.0005) - 1)

    def prev_cut(self):
        if not self.cut_times:
            return
        t = self.cur_ms() / 1000.0
        i = self.current_cut_index(t)
        # カット先頭付近にいるなら1つ前へ
        if i > 0 and t - self.cut_times[i] < 0.3:
            i -= 1
        self.seek_to(self.cut_times[i])

    def next_cut(self):
        if not self.cut_times:
            return
        i = self.current_cut_index(self.cur_ms() / 1000.0) + 1
        if i < len(self.cut_times):
            self.seek_to(self.cut_times[i])

    def on_position(self, ms):
        if self.info is None:
            return
        t = ms / 1000.0
        f = self.frame_at(t)
        ci = self.current_cut_index(t)
        txt = f'{E.fmt_tc(t)} / {E.fmt_tc(self.info.duration)}   F {f}'
        if ci >= 0:
            a, b = self.cut_range(ci)
            txt += f'   カット #{ci + 1}/{len(self.cuts)}（{t - a:.2f}/{b - a:.2f}s）'
        self.lbl_time.setText(txt)
        self.timeline.pos = t
        if ci != self.cur_cut:
            old = self.list.item(self.cur_cut) if self.cur_cut >= 0 else None
            if old is not None:
                old.setBackground(QBrush())
            self.cur_cut = ci
            it = self.list.item(ci) if ci >= 0 else None
            if it is not None:
                it.setBackground(QBrush(QColor(61, 139, 253, 70)))
                if self.cb_follow.isChecked():
                    self.list.scrollToItem(it, QAbstractItemView.EnsureVisible)
            self.timeline.current_cut = ci
        self.timeline.update()

    # ------------------------------------------------------------------ range
    def set_in(self):
        if self.info is None:
            return
        self.rin = self.frame_time(self.frame_at(self.cur_ms() / 1000.0))
        if self.rout is not None and self.rout <= self.rin:
            self.rout = None
        self.update_range_label()

    def set_out(self):
        if self.info is None:
            return
        f = self.frame_at(self.cur_ms() / 1000.0)
        n = len(self.an.times) if self.an else int(self.info.duration * self.info.fps)
        self.rout = self.frame_time(f + 1) if f + 1 < n else self.info.duration
        if self.rin is None:
            self.rin = 0.0
        if self.rout <= self.rin:
            self.rin = None
        self.update_range_label()

    def set_range(self, a, b):
        self.rin, self.rout = a, b
        self.update_range_label()

    def clear_range(self):
        self.rin = self.rout = None
        self.list.clearSelection()
        self.update_range_label()

    def update_range_label(self):
        self.timeline.rin, self.timeline.rout = self.rin, self.rout
        self.timeline.update()
        if self.rin is None and self.rout is None:
            self.lbl_range.setText('範囲: 未設定（In/Out またはカット一覧で選択）')
            return
        a = E.fmt_tc(self.rin) if self.rin is not None else '--'
        b = E.fmt_tc(self.rout) if self.rout is not None else '--'
        txt = f'範囲: {a} → {b}'
        if self.rin is not None and self.rout is not None and self.info is not None:
            txt += f'  ({self.rout - self.rin:.2f}s)'
            if self.kfs:
                p = E.plan_export(self.rin, self.rout, self.kfs, self.info.fps, self.info.duration)
                txt += '  ◆キーフレーム開始' if p.lead_frames == 0 else f'  ◇無劣化だと {p.lead_frames}F 手前から'
        self.lbl_range.setText(txt)


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName('anicut')
    app.setStyle('Fusion')
    pal = app.palette()
    from PySide6.QtGui import QPalette
    dark = {
        QPalette.Window: '#1e1f22', QPalette.WindowText: '#e6e6e6', QPalette.Base: '#16171a',
        QPalette.AlternateBase: '#232428', QPalette.ToolTipBase: '#2b2d31', QPalette.ToolTipText: '#e6e6e6',
        QPalette.Text: '#e6e6e6', QPalette.Button: '#2b2d31', QPalette.ButtonText: '#e6e6e6',
        QPalette.Highlight: '#3d8bfd', QPalette.HighlightedText: '#ffffff', QPalette.Link: '#6ea8fe',
    }
    for role, c in dark.items():
        pal.setColor(role, QColor(c))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor('#666'))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor('#666'))
    app.setPalette(pal)
    w = MainWindow()
    w.show()
    args = [a for a in sys.argv[1:] if os.path.exists(a)]
    if args:
        if len(args) == 1 and args[0].lower().endswith(PROJECT_EXT):
            QTimer.singleShot(100, lambda: w.load_project(args[0]))
        else:
            QTimer.singleShot(100, lambda: w.open_paths(args))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
