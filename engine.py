"""anicut engine: カット検出・キーフレーム取得・無劣化切り出し（Qt非依存）。

時刻はすべて「ファイル先頭 = 0 秒」基準（ffmpeg の -ss と同じ基準）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from bisect import bisect_right
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / 'cache'
CACHE_VERSION = 2

# 解析用の縮小フレームサイズ（16:9 前提だが、他比率でも問題なく動く）
AW, AH = 128, 72

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0


class Cancelled(Exception):
    pass


def find_executable(name: str) -> str | None:
    """アプリフォルダ → PATH → よくある場所 の順に ffmpeg/ffprobe を探す。"""
    exe = name + ('.exe' if os.name == 'nt' else '')
    for cand in (APP_DIR / exe, APP_DIR / 'bin' / exe):
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    if found:
        return found
    for base in (r'C:\ffmpeg\bin', r'C:\Program Files\ffmpeg\bin', r'D:\Program Files\ffmpeg\bin'):
        cand = Path(base) / exe
        if cand.is_file():
            return str(cand)
    return None


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW, **kw)


# ---------------------------------------------------------------- probe

@dataclass
class MediaInfo:
    path: str
    duration: float
    fps: float
    width: int
    height: int
    format_start: float
    vcodec: str
    nb_audio: int
    nb_subs: int
    b_delay: int = 0        # has_b_frames（表示順と復号順のずれ。切り出し終端の補正に使う）


def _ratio(s: str) -> float:
    try:
        if '/' in s:
            a, b = s.split('/')
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str, ffprobe: str) -> MediaInfo:
    r = _run([ffprobe, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path])
    if r.returncode:
        raise RuntimeError('ffprobe に失敗しました:\n' + r.stderr.decode('utf-8', 'replace'))
    data = json.loads(r.stdout.decode('utf-8', 'replace'))
    streams = data.get('streams', [])
    vids = [s for s in streams if s.get('codec_type') == 'video'
            and not s.get('disposition', {}).get('attached_pic')]
    if not vids:
        raise RuntimeError('映像ストリームが見つかりません')
    v = vids[0]
    fps = _ratio(v.get('avg_frame_rate', '0')) or _ratio(v.get('r_frame_rate', '0')) or 23.976
    if fps > 200:  # mkv などで r_frame_rate が異常値になる場合
        fps = _ratio(v.get('r_frame_rate', '0')) or 23.976
    fmt = data.get('format', {})
    dur = float(fmt.get('duration') or v.get('duration') or 0)
    return MediaInfo(
        path=path, duration=dur, fps=fps,
        width=int(v.get('width', 0)), height=int(v.get('height', 0)),
        format_start=float(fmt.get('start_time') or 0.0),
        vcodec=v.get('codec_name', '?'),
        nb_audio=sum(1 for s in streams if s.get('codec_type') == 'audio'),
        nb_subs=sum(1 for s in streams if s.get('codec_type') == 'subtitle'),
        b_delay=int(v.get('has_b_frames') or 0),
    )


def keyframes(path: str, ffprobe: str, info: MediaInfo) -> list[float]:
    """映像のキーフレーム時刻（先頭=0基準）を昇順で返す。パケット読みのみなので高速。"""
    r = _run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
              '-show_entries', 'packet=pts_time,flags', '-of', 'csv=p=0', path])
    out = []
    for line in r.stdout.decode('ascii', 'replace').splitlines():
        parts = line.strip().split(',')
        if len(parts) >= 2 and 'K' in parts[1]:
            try:
                out.append(round(float(parts[0]) - info.format_start, 6))
            except ValueError:
                pass
    out.sort()
    return out


# ---------------------------------------------------------------- analysis

@dataclass
class Analysis:
    times: list[float]          # 各フレームの表示時刻（秒）
    scores: list[float]         # 直前フレームとの差分スコア（先頭は 0）
    fps: float
    version: int = CACHE_VERSION


_BATCH = 256     # 一度に numpy で処理するフレーム数


def _batch_scores(frames: np.ndarray, prev: np.ndarray | None) -> np.ndarray:
    """縮小フレーム列 (N,H,W,3 uint8) → 各フレームの直前フレームとの差分スコア (N,)。

    スコア = (画素差の平均 0..255 + 色ヒストグラム(各色8段階=512ビン)の差 0..100) / 2
    prev が None のとき先頭フレームのスコアは 0。
    """
    n = len(frames)
    seq = frames if prev is None else np.concatenate([prev[None], frames])
    f = seq.astype(np.int16)
    pix = np.abs(f[1:] - f[:-1]).mean(axis=(1, 2, 3))
    q = (seq >> 5).astype(np.int32)
    idx = (q[..., 0] * 64 + q[..., 1] * 8 + q[..., 2]).reshape(len(seq), -1)
    idx += (np.arange(len(seq), dtype=np.int32) * 512)[:, None]
    hist = np.bincount(idx.ravel(), minlength=len(seq) * 512).reshape(len(seq), 512).astype(np.float32)
    hist /= AW * AH
    hd = np.abs(hist[1:] - hist[:-1]).sum(axis=1) * 50.0
    s = np.round((pix + hd) * 0.5, 3)
    if prev is None:
        s = np.concatenate([[0.0], s])
    assert len(s) == n
    return s


def _analyze_args(ffmpeg: str, path: str, gpu: bool, stats: str) -> list[str]:
    args = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error']
    if gpu:
        # NVIDIA: デコードと縮小を GPU で行い、小さくしてから取り出す（CPU 負荷が大幅に下がる）
        args += ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
        vf = (f'scale_cuda={AW * 3}:{AH * 3}:format=yuv420p,hwdownload,format=yuv420p,'
              f'scale={AW}:{AH}:flags=area,format=rgb24')
    else:
        vf = f'scale={AW}:{AH}:flags=area,format=rgb24'
    return args + ['-i', path, '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', vf,
                   '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux',
                   # 各フレームの表示時刻はファイルへ書き出させる（showinfo はログ出力が重く、処理が半分以下の速度になる）
                   '-stats_enc_pre', stats, '-stats_enc_pre_fmt', '{n} {t}',
                   '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']


def _read_stats(stats: str) -> dict[int, float]:
    out: dict[int, float] = {}
    try:
        with open(stats, encoding='ascii', errors='replace') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        out[int(parts[0])] = float(parts[1])
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def analyze(path: str, ffmpeg: str, info: MediaInfo, progress=None, cancel: threading.Event | None = None,
            hwaccel: bool = False) -> Analysis:
    """全フレームをデコードして縮小し、フレーム間差分スコアを計算する。

    hwaccel=True のときは NVIDIA GPU (CUDA) で処理し、失敗したら RuntimeError（呼び出し側で CPU に切り替え）。
    """
    fd_, stats = tempfile.mkstemp(prefix='anicut_', suffix='.txt')
    os.close(fd_)
    proc = subprocess.Popen(_analyze_args(ffmpeg, path, hwaccel, stats), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    err_tail: list[bytes] = []

    def read_err():
        for line in proc.stderr:
            err_tail.append(line)
            if len(err_tail) > 40:
                del err_tail[0]

    th = threading.Thread(target=read_err, daemon=True)
    th.start()

    fsize = AW * AH * 3
    parts: list[np.ndarray] = []
    prev = None
    n = 0
    total = max(1, int(info.duration * info.fps))
    buf = bytearray(fsize * _BATCH)
    view = memoryview(buf)
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            # バッチ分（またはファイル末尾まで）読む
            got = 0
            while got < len(buf):
                k = proc.stdout.readinto(view[got:])
                if not k:
                    break
                got += k
            nf = got // fsize
            if nf:
                frames = np.frombuffer(buf, dtype=np.uint8, count=nf * fsize).reshape(nf, AH, AW, 3)
                parts.append(_batch_scores(frames, prev))
                prev = frames[-1].copy()
                n += nf
                if progress:
                    progress(min(n / total, 0.999), n)
            if got < len(buf):
                break
    finally:
        if cancel is not None and cancel.is_set():
            proc.kill()
        proc.wait()
        th.join(timeout=5)
        times_by_n = _read_stats(stats)
        try:
            os.remove(stats)
        except OSError:
            pass
    if n == 0 or (hwaccel and proc.returncode != 0):
        raise RuntimeError('フレームを読み込めませんでした:\n' + b''.join(err_tail).decode('utf-8', 'replace'))
    scores = np.concatenate(parts).tolist()
    if not scores:
        scores = [0.0]
    times = []
    fd = 1.0 / info.fps
    for i in range(n):
        t = times_by_n.get(i)
        times.append(round(t if t is not None else i * fd, 6))
    # まれに表示順が乱れる場合への保険
    for i in range(1, n):
        if times[i] <= times[i - 1]:
            times[i] = round(times[i - 1] + fd, 6)
    if progress:
        progress(1.0, n)
    return Analysis(times=times, scores=scores[:n], fps=info.fps)


@dataclass
class DetectParams:
    threshold: float = 22.0     # 差分スコアの最低値（小さいほど敏感）
    ratio: float = 2.6          # 周囲の動きに対する比（小さいほど敏感）
    min_frames: int = 6         # 最短カット長（フレーム）
    window: int = 2             # 比較する前後の「動いたフレーム」数


def find_cuts(an: Analysis, p: DetectParams) -> list[int]:
    """スコア列からカット先頭フレーム番号のリストを返す（先頭 0 を含む）。

    アニメは「2コマ/3コマ打ち」で同一フレームが続くため、
    変化のあったフレームだけを並べた列の中で前後と比較する（適応しきい値）。
    """
    s = np.asarray(an.scores, dtype=np.float64)
    n = len(s)
    cuts = [0]
    if n < 2:
        return cuts
    moving = np.nonzero(s >= 1.0)[0]
    if len(moving) == 0:
        return cuts
    ms = s[moving]
    w = max(1, p.window)
    k = len(ms)
    # 前後 w 個の平均（自分を除く）
    pad = np.concatenate([np.full(w, np.nan), ms, np.full(w, np.nan)])
    neigh = np.stack([pad[w + d: w + d + k] for d in range(-w, w + 1) if d != 0])
    with np.errstate(invalid='ignore'):
        avg = np.nanmean(neigh, axis=0)
    avg = np.nan_to_num(avg, nan=0.0)
    ratio = ms / (avg + 1.5)
    cand = moving[(ms >= p.threshold) & (ratio >= p.ratio)]
    # 極端に大きい差分は比に関係なくカットとみなす（カットが連続する場合の取りこぼし防止）
    strong = moving[ms >= max(p.threshold * 2.2, 55.0)]
    cand = np.union1d(cand, strong)
    last = 0
    for i in cand.tolist():
        if i - last >= p.min_frames:
            cuts.append(i)
            last = i
        elif last != 0 and s[i] > s[last] and i - cuts[-2] >= p.min_frames:
            # 近接候補はスコアの大きい方を採用
            cuts[-1] = i
            last = i
    return cuts


# ---------------------------------------------------------------- cache

def _cache_key(path: str) -> str:
    st = os.stat(path)
    raw = f'{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}'.encode('utf-8')
    return hashlib.sha1(raw).hexdigest()[:20]


def load_cache(path: str) -> dict | None:
    try:
        f = CACHE_DIR / (_cache_key(path) + '.json')
        if not f.is_file():
            return None
        data = json.loads(f.read_text('utf-8'))
        if data.get('version') != CACHE_VERSION:
            return None
        an = data['analysis']
        data['analysis'] = Analysis(times=an['times'], scores=an['scores'], fps=an['fps'])
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cache(path: str, an: Analysis, kfs: list[float], added: list[int], removed: list[int],
               params: DetectParams):
    """解析結果と手動編集を保存（次回同じファイルを開いたとき再解析不要）。"""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        f = CACHE_DIR / (_cache_key(path) + '.json')
        data = {'version': CACHE_VERSION, 'source': os.path.abspath(path),
                'analysis': {'times': an.times, 'scores': an.scores, 'fps': an.fps},
                'keyframes': kfs, 'added': sorted(added), 'removed': sorted(removed),
                'params': asdict(params)}
        f.write_text(json.dumps(data, separators=(',', ':')), 'utf-8')
    except OSError:
        pass


# ---------------------------------------------------------------- thumbnails

def thumbnail_jpeg(path: str, ffmpeg: str, t: float, fps: float, width: int = 256) -> bytes:
    """時刻 t のフレーム（そのフレームそのもの）を JPEG バイト列で返す。"""
    ss = max(0.0, t - 0.25 / max(fps, 1.0))
    r = _run([ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-ss', f'{ss:.6f}', '-i', path,
              '-map', '0:v:0', '-frames:v', '1', '-vf', f'scale={width}:-2:flags=bicubic',
              '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '4', 'pipe:1'])
    return r.stdout


# ---------------------------------------------------------------- lossless export

@dataclass
class ExportPlan:
    start: float          # 指定開始（カット先頭）
    end: float            # 指定終了（この時刻の直前まで）
    actual_start: float   # 実際の開始（キーフレームに合わせた時刻）
    lead_frames: int      # 指定より前に含まれてしまうフレーム数
    seek: float = 0.0     # ffmpeg に渡す -ss（キーフレームより少し後ろ。そのキーフレームから読み出される）
    to_eof: bool = False  # ファイル末尾まで


def plan_export(start: float, end: float, kfs: list[float], fps: float, duration: float) -> ExportPlan:
    fd = 1.0 / max(fps, 1.0)
    eps = 0.5 * fd
    i = bisect_right(kfs, start + eps) - 1
    ks = kfs[i] if i >= 0 else 0.0
    lead = int(round((start - ks) * fps)) if start - ks > eps else 0
    # ffmpeg は B フレームのある入力で -ss を少し手前（約0.13秒）にずらしてシークするため、
    # キーフレームから最大 0.2 秒後ろを指定する（次のキーフレームは越えない）。
    nk = kfs[i + 1] if i + 1 < len(kfs) else duration
    seek = 0.0 if ks <= 0.0005 else ks + min(0.2, max(0.001, (nk - ks) * 0.5))
    return ExportPlan(start=start, end=end, actual_start=ks, lead_frames=lead, seek=seek,
                      to_eof=end >= duration - eps)


def export_copy(src: str, plan: ExportPlan, out_path: str, ffmpeg: str, info: MediaInfo) -> None:
    """再エンコードせずに（-c copy）切り出す。開始はキーフレーム単位。"""
    ext = Path(out_path).suffix.lower()
    fd = 1.0 / max(info.fps, 1.0)
    ss = plan.seek
    # -t は出力側（-ss の位置が 0）。stream copy は復号順で打ち切られるため、
    # B フレームの遅延分を差し引くと表示上ちょうど end の直前で終わる。
    if plan.to_eof:
        dur = plan.end - ss + 10.0
    else:
        dur = max(fd * 0.5, plan.end - ss - 0.25 * fd - info.b_delay * fd)
    args = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y']
    if ss > 0:
        args += ['-ss', f'{ss:.6f}']
    args += ['-i', src, '-t', f'{dur:.6f}', '-map', '0:v:0', '-map', '0:a?']
    if ext in ('.mkv', '.mka', '.webm'):
        args += ['-map', '0:s?', '-map', '0:t?']
    elif ext in ('.mp4', '.m4v', '.mov'):
        args += ['-map', '0:s?', '-c:s', 'mov_text'] if info.nb_subs else []
    args += ['-c:v', 'copy', '-c:a', 'copy']
    if ext in ('.mkv', '.mka', '.webm'):
        args += ['-c:s', 'copy']
    args += ['-map_chapters', '-1', '-avoid_negative_ts', 'make_zero']
    if ext in ('.mp4', '.m4v', '.mov'):
        args += ['-movflags', '+faststart']
    args += [out_path]
    r = _run(args)
    if r.returncode:
        # 字幕などが原因の失敗に備え、映像+音声のみで再試行
        retry = [ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y']
        if ss > 0:
            retry += ['-ss', f'{ss:.6f}']
        retry += ['-i', src, '-t', f'{dur:.6f}', '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
                  '-map_chapters', '-1', '-avoid_negative_ts', 'make_zero', out_path]
        r2 = _run(retry)
        if r2.returncode:
            raise RuntimeError('ffmpeg による切り出しに失敗しました:\n'
                               + r.stderr.decode('utf-8', 'replace')[-1500:]
                               + '\n---\n' + r2.stderr.decode('utf-8', 'replace')[-1500:])


# ---------------------------------------------------------------- formatting

def fmt_tc(t: float, ms: bool = True) -> str:
    if t < 0:
        t = 0.0
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    return f'{h:02d}:{m:02d}:{s:02d}.{msec:03d}' if ms else f'{h:02d}:{m:02d}:{s:02d}'


def fmt_tc_file(t: float) -> str:
    return fmt_tc(t).replace(':', '-').replace('.', '_')
