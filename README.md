# 動画シーン検索・切り抜きツール (Windows向け)

自然言語クエリ(例: 「奨学金について話しているところ」)で動画内の該当シーンを検索し、
プレビューで確認しながら区間を調整してmp4に切り抜けるローカルツールです。
文字起こし・埋め込み・検索・切り抜きのすべてがローカルで完結します(外部APIは使いません)。

> **Windows + NVIDIA GPU環境を主対象としています。**
> Linux/macOSでも動作しますが、動作確認はWindows (RTX 4070) で行っています。

## 🚀 クイックスタート (Windows)

**[Code] → [Download ZIP] でダウンロードして展開し、`start.bat` をダブルクリックするだけ**です。
初回実行時に必要なものがすべて自動でインストールされます:

| 何が | どうやって |
|---|---|
| Python 3.12 | winget で自動インストール (未導入の場合) |
| ffmpeg | winget で自動インストール (未導入の場合) |
| PyTorch (CUDA) ・依存パッケージ | 仮想環境に自動インストール (約2.5GB) |
| Whisper large-v3 / BGE-M3 モデル | 初回の文字起こし/検索時に自動ダウンロード (約5GB) |

2回目以降は数秒でアプリが起動し、ブラウザ (http://127.0.0.1:7860) が自動で開きます。
手動でのインストール作業は一切不要です。

※ Python / ffmpeg を新規インストールした場合は、PATH反映のため一度ウィンドウを閉じて
`start.bat` をもう一度実行してください(画面に案内が出ます)。

## 主な機能

- **文字列・自然言語でのシーン検索**: Unicode・かな表記を正規化した文字列の部分一致と、BGE-M3/FAISSによる意味検索を統合
- **WebUI** (Gradio): 検索 → プレビュー再生 → シークバー/±ボタンで区間調整 → クリップ保存
- **WebUIから新規動画の追加**: 文字起こし〜インデックス化を進捗ログ付きで実行
- **境界の自動補正**: ヒット区間を「話の切れ目」(無音ギャップ)まで自動拡張し、話が途中で切れにくい
- **該当なし判定**: 文字一致がなく、類似度閾値を下回る意味検索候補しかない場合は「見つかりませんでした」と返す
- CLIも同梱 (`index_video.py` / `search_video.py` / `cut_clip.py`)

## アーキテクチャ

```
動画 → faster-whisper (文字起こし・単語タイムスタンプ)
     → 15秒チャンク化 (5秒オーバーラップ)
     → BGE-M3 埋め込み → SQLite + FAISS に保存      … インデックス構築 (動画ごとに1回)

クエリ → Unicode正規化した文字列の部分一致 ┐
       → BGE-M3 埋め込み → FAISS 類似検索  ├→ 結果統合
       (動画指定時はその動画内だけで順位計算) ┘
       → 話の切れ目まで境界拡張 → ffmpeg で切り抜き   … 検索
```

## 動作環境

- Python 3.10+
- ffmpeg / ffprobe (PATHに通っていること)
- NVIDIA GPU (VRAM 6GB以上推奨、RTX 4070で動作確認済み)。CPUのみでも動作しますが文字起こしが大幅に遅くなります

## 手動セットアップ (上のstart.batを使わない場合)

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File setup.ps1        # GPU版
powershell -ExecutionPolicy Bypass -File setup.ps1 -Cpu   # CPUのみ
```

```bash
# Linux / macOS
./setup.sh          # GPU版
./setup.sh --cpu    # CPUのみ
```

仮想環境の作成、PyTorch (CUDA 12.4ビルド)、依存パッケージのインストールまで自動で行います。
初回の文字起こし/検索時にWhisper large-v3 (約3GB) とBGE-M3 (約2GB) がHugging Faceから
自動ダウンロードされます。

## 使い方 (WebUI)

```powershell
.\venv\Scripts\python.exe app.py     # Windows
./venv/bin/python app.py             # Linux / macOS
```

ブラウザで http://127.0.0.1:7860 が開きます。

1. **「動画の追加」タブ**で動画ファイルを選び「インデックス作成」(動画の長さに応じて数分)
2. **「検索・切り抜き」タブ**でクエリを入力して検索
3. 候補を選ぶとプレビューが再生される。シークバー・±ボタン(0.1〜600秒)・数値入力で区間を調整
4. 保存先フォルダ・ファイル名を指定して「クリップ保存」

## 使い方 (CLI)

```bash
python index_video.py --video input.mp4              # インデックス構築 (--force で作り直し)
python search_video.py --query "〇〇について話しているところ"
python search_video.py --query "..." --cut           # 上位候補を自動切り抜き
python cut_clip.py --video input.mp4 --start 123.4 --end 156.7 --output clip.mp4
```

主なオプション: `--min-score`(該当なし判定の閾値、既定0.55)、`--gap`(話の切れ目とみなす
無音秒数、既定1.0)、`--no-expand`(境界拡張の無効化)、`--precise`(フレーム精度の再エンコード)、
`--asr-model medium`(高速・低精度なモデルへの変更)。

## データの保存場所

| パス | 内容 |
|---|---|
| `data/index.db` | メタデータ・文字起こし (SQLite) |
| `data/text.index` | 埋め込みベクトル (FAISS) |
| `data/previews/` | プレビュー用の一時クリップ |
| `clips/` | 保存したクリップ (既定) |

いずれも `.gitignore` 済みで、リポジトリには含まれません。

## 既知の制約

- 検索対象は発話内容のみ。映像にしか映らないもの(無言で画面に出る物体など)は検索できない
  (CLIP/SigLIP系の映像埋め込み検索を将来拡張として検討中)
- プレビューは高速コピー切り出しのため、開始位置がキーフレーム単位で1〜2秒ずれて見えることがある
  (保存時のフレーム精度モードではずれない)
- pyarrowはWindowsでのクラッシュ回避のため16.1.0に固定している

## ライセンス

MIT License
