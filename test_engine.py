"""engine の自己テスト: 合成動画を作り、カット検出と無劣化切り出しのフレーム数を検証する。

    py -3 test_engine.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as e  # noqa: E402

FF = e.find_executable('ffmpeg')
FP = e.find_executable('ffprobe')


def make_video(d: Path, ext: str, gop_args):
    out = d / f'src{ext}'
    subprocess.run([FF, '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24000/1001:duration=3',
                    '-f', 'lavfi', '-i', 'mandelbrot=size=640x360:rate=24000/1001',
                    '-f', 'lavfi', '-i', 'smptebars=size=640x360:rate=24000/1001:duration=1.5',
                    '-f', 'lavfi', '-i', 'color=c=0x334488:size=640x360:rate=24000/1001:duration=2.2',
                    '-f', 'lavfi', '-i', 'testsrc=size=640x360:rate=24000/1001:duration=2.7',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:duration=11.6',
                    '-filter_complex',
                    '[1]trim=duration=2.2,setpts=PTS-STARTPTS[m];'
                    '[3]drawbox=x=40+t*100:y=120:w=120:h=120:color=yellow:t=fill[c];'
                    '[0][m][2][c][4]concat=n=5:v=1:a=0[v]',
                    '-map', '[v]', '-map', '5', '-c:v', 'libx264', *gop_args, '-c:a', 'aac', str(out)],
                   check=True)
    return out


def nframes(p):
    r = subprocess.run([FP, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'frame=pts_time',
                        '-of', 'csv=p=0', str(p)], capture_output=True, text=True)
    return len([x for x in r.stdout.split() if x.strip(', ')])


def main():
    ok = True
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for ext in ('.mkv', '.mp4'):
            for gop in (['-g', '48', '-sc_threshold', '0', '-bf', '2'], ['-bf', '3']):
                src = make_video(d, ext, gop)
                info = e.probe(str(src), FP)
                kf = e.keyframes(str(src), FP, info)
                an = e.analyze(str(src), FF, info)
                cuts = e.find_cuts(an, e.DetectParams())
                expect = [0, 72, 125, 161, 214]
                print(ext, gop, 'cuts', cuts, 'OK' if cuts == expect else 'NG')
                ok &= cuts == expect
                bounds = cuts + [len(an.times)]
                for a, b in zip(bounds, bounds[1:]):
                    end = an.times[b] if b < len(an.times) else info.duration
                    pl = e.plan_export(an.times[a], end, kf, info.fps, info.duration)
                    out = d / f'o{a}{ext}'
                    e.export_copy(str(src), pl, str(out), FF, info)
                    got, want = nframes(out), b - a + pl.lead_frames
                    print(f'   cut {a:3d}-{b:3d} lead={pl.lead_frames:2d} frames {got}/{want}',
                          'OK' if got == want else 'NG')
                    ok &= got == want
    print('ALL OK' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
