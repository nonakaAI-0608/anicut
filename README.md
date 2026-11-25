# anicut

アニメ動画のカット（場面転換）を検出し、指定したカットや範囲を無劣化で切り出す Windows 向け GUI ツールです。

## 必要なもの
- Windows 10 / 11
- Python 3.10 以降（python.org 版、Python Launcher を含める）
- [ffmpeg](https://ffmpeg.org/download.html)（ffmpeg.exe / ffprobe.exe）

## インストール
1. このリポジトリを ZIP でダウンロードして展開するか、`git clone` します。
2. ffmpeg を用意します。PATH に通すか、`ffmpeg.exe` と `ffprobe.exe` を anicut フォルダ（または `anicut\bin`）に置きます。
3. `起動.bat` をダブルクリックします。初回のみ PySide6 と numpy を `anicut\vendor` に自動インストールします（インターネット接続が必要、数百MB）。
   手動で入れる場合：`py -3 -m pip install --target vendor -r requirements.txt`

## 使い方
詳しい操作説明は [README.txt](README.txt) を参照してください。
設定（`anicut.ini`）と解析キャッシュ（`cache/`）はフォルダ内に作られます。リポジトリには含めません。

## ライセンス
MIT License（[LICENSE](LICENSE)）。PySide6（LGPL）・ffmpeg はそれぞれのライセンスに従います（同梱していません）。
