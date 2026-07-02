# 動画シーン検索・切り抜きツール

自然言語クエリ(例: 「奨学金について話しているところ」)で動画内の該当シーンを検索し、
プレビューで確認しながら区間を調整してmp4に切り抜けるローカルツールです。
文字起こし・埋め込み・検索・切り抜きのすべてがローカルで完結します(外部APIは使いません)。

## 主な機能

- **自然言語でのシーン検索**: faster-whisperによる文字起こしをBGE-M3で埋め込み、FAISSで意味検索
- **WebUI** (Gradio): 検索 → プレビュー再生 → シークバー/±ボタンで区間調整 → クリップ保存
- **WebUIから新規動画の追加**: 文字起こし〜インデックス化を進捗ログ付きで実行
- **境界の自動補正**: ヒット区間を「話の切れ目」(無音ギャップ)まで自動拡張し、話が途中で切れにくい
- **該当なし判定**: 類似度閾値を下回る候補しかない場合は「見つかりませんでした」と返す
- CLIも同梱 (`index_video.py` / `search_video.py` / `cut_clip.py`)

## アーキテクチャ

```
動画 → faster-whisper (文字起こし・単語タイムスタンプ)
     → 15秒チャンク化 (5秒オーバーラップ)
     → BGE-M3 埋め込み → SQLite + FAISS に保存      … インデックス構築 (動画ごとに1回)

クエリ → BGE-M3 埋め込み → FAISS 類似検索
      → 話の切れ目まで境界拡張 → ffmpeg で切り抜き   … 検索 (1秒以内)
```

## 動作環境

- Python 3.10+
- ffmpeg / ffprobe (PATHに通っていること)
- NVIDIA GPU (VRAM 6GB以上推奨、RTX 4070で動作確認済み)。CPUのみでも動作しますが文字起こしが大幅に遅くなります

## セットアップ

### Windows: ダブルクリックで完結

**`start.bat` をダブルクリック**するだけで、初回は自動セットアップ
(ffmpegのwingetインストール → 仮想環境作成 → PyTorch/依存パッケージのインストール)、
2回目以降は即アプリ起動になります。Pythonのみ事前にインストールしておいてください。

### 手動セットアップ

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
