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
| PyTorch 2.6以上 (CUDA) ・依存パッケージ | 仮想環境に自動インストール (約2.5GB) |
| Whisper large-v3 / BGE-M3 モデル | 初回の文字起こし/検索時に自動ダウンロード (約5GB) |

2回目以降は数秒でアプリが起動し、ブラウザ (http://127.0.0.1:7860) が自動で開きます。
手動でのインストール作業は一切不要です。

※ Python / ffmpeg を新規インストールした場合は、PATH反映のため一度ウィンドウを閉じて
`start.bat` をもう一度実行してください(画面に案内が出ます)。

異常終了したWebUIが7860番ポートだけを保持して応答しない場合、次回起動時にHTTP
ヘルスチェックを行い、このアプリが記録した`app.py`プロセスと確認できた場合だけ
自動停止して起動し直します。別アプリが7860番を使用している場合は誤停止せず、
バッチ画面に終了方法を表示します。

## 主な機能

- **文字列・自然言語でのシーン検索**: Unicode・かな表記を正規化した文字列の部分一致と、BGE-M3/FAISSによる意味検索を統合
- **WebUI** (Gradio): 検索 → プレビュー再生 → 区間調整 → 途中の不要部分を除外 → 残った複数区間を1本に連結保存
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

仮想環境の作成、PyTorch 2.6以上 (CUDA 12.4ビルド)、依存パッケージのインストールまで自動で行います。
初回の文字起こし/検索時にWhisper large-v3 (約3GB) とBGE-M3 (約2GB) がHugging Faceから
自動ダウンロードされます。

## 使い方 (WebUI)

```powershell
.\venv\Scripts\python.exe app.py     # Windows
./venv/bin/python app.py             # Linux / macOS
```

ブラウザで http://127.0.0.1:7860 が開きます。

1. **「動画の追加」タブ**で動画ファイルを選び「インデックス作成」(動画の長さに応じて数分)
2. **「検索・切り抜き」タブ**の「検索対象の動画を選ぶ」を開き、サムネイルカードから対象を選んでクエリを検索（ファイル名で絞り込み可能）
3. 「① 全体範囲」内で、プレビューの現在位置・文単位調整・シークバー・±ボタン(0.1〜600秒)のいずれかを使って開始と終了を決める
4. 必要なら「② 途中カット（任意）」内で、現在位置または文単位で除外開始・終了を決めて微調整する。複数回追加でき、残った区間を連結プレビューできる
5. 保存先フォルダ・ファイル名を指定して「クリップ保存」

## 使い方 (CLI)

```bash
python index_video.py --video input.mp4              # インデックス構築 (--force で作り直し)
python search_video.py --query "〇〇について話しているところ"
python search_video.py --query "..." --cut           # 上位候補を自動切り抜き
python cut_clip.py --video input.mp4 --start 123.4 --end 156.7 --output clip.mp4
```

自動化ではversion付きJSONを返す統合CLIを推奨します。

```powershell
venv\Scripts\python.exe video_tool.py search --query "命令" --video-id vid_xxx
venv\Scripts\python.exe video_tool.py clip --video-id vid_xxx --plan edit-plan.json --output clips\result.mp4 --precise --srt
```

`edit-plan.json`は整数ミリ秒です。

```json
{"source_duration_ms":60000,"overall":[10000,50000],"exclusions":[[20000,25000]]}
```

従来のCLIは互換wrapperとして残しています。

主なオプション: `--min-score`(該当なし判定の閾値、既定0.55)、`--gap`(話の切れ目とみなす
無音秒数、既定1.0)、`--no-expand`(境界拡張の無効化)、`--precise`(フレーム精度の再エンコード)、
`--asr-model medium`(高速・低精度なモデルへの変更)。

## データの保存場所

| パス | 内容 |
|---|---|
| `data/index.db` | メタデータ・文字起こし (SQLite) |
| `data/text.index` | 埋め込みベクトル (FAISS) |
| `data/previews/` | プレビューキャッシュ（最大8 GiB・200ファイルをLRUで自動管理） |
| `data/thumbnails/` | 動画選択メニュー用のローカルサムネイルキャッシュ |
| `clips/` | 保存したクリップ (既定) |

いずれも `.gitignore` 済みで、リポジトリには含まれません。

## コミット前のプライバシー確認

既定ではステージ済みの追加行だけを検査します。手動確認では`--working-tree`を付けると、
tracked working tree全体の追加行と未追跡source fileも検査できます。

```powershell
.\venv\Scripts\python.exe scripts\check_privacy.py
.\venv\Scripts\python.exe scripts\check_privacy.py --working-tree
```

pre-commit hookから使う場合は、`.git/hooks/pre-commit`から引数なしのコマンドを呼び出し、
終了コードが0以外ならcommitを中止してください。

## 既知の制約

- 検索対象は発話内容のみ。映像にしか映らないもの(無言で画面に出る物体など)は検索できない
  (CLIP/SigLIP系の映像埋め込み検索を将来拡張として検討中)
- プレビューは高速コピー切り出しのため、開始位置がキーフレーム単位で1〜2秒ずれて見えることがある
  (保存時のフレーム精度モードではずれない)
- pyarrowはWindowsでのクラッシュ回避のため16.1.0に固定している

### 実験ブランチ: ローカルLLM解析

`experiment/llm-transcript-analysis` では、動画追加時に任意でローカルOllamaを使い、
文字起こしから要約・タグ・時間付き章を生成できます。既定では無効です。
Ollamaと既定モデル `qwen3:8b` は、初回だけ `setup_ollama.bat` を実行して準備します。
または `start.bat --setup-ollama` を実行してください。通常の `start.bat` はOllamaを
ダウンロード・起動しないため、LLM解析を使わない場合は追加の容量や待ち時間は発生しません。
既にOllamaまたはモデルがある場合は再利用し、未取得のモデルだけを取得します。
このリポジトリが `F:\myapp\cut` にある場合、新規導入するOllama本体とモデルは
`F:\myapp\dependencies\ollama` に保存されます。約7GB以上の空き容量が必要です。

既存動画はCLIから再解析できます。

```powershell
venv\Scripts\python.exe analyze_transcript.py --video-id <公開動画ID> --model <Ollamaモデル名>
```

解析結果はSQLiteの派生データとして保存され、ASR原文と検索indexは変更されません。
Ollama endpointはloopbackだけを許可し、クラウド送信機能は含みません。
解析は日本語出力を検証し、約5分単位の決定的な範囲へ章ラベルを付けます。
全体要約・代表タグは第2段階で統合し、WebUIには文字起こしセグメント網羅率も表示します。

### 実験ブランチ: 見どころ候補

`experiment/llm-highlight-candidates` では、保存済みのLLM解析結果から3〜10件の
見どころ候補を作成できます。章の要約で候補章を絞った後、その章の元ASRを再評価して
30秒以内の核心となる実在segment IDを1件選び、アプリ側で最大尺以下の許可窓を作ります。
LLMはその窓内の実在segment IDだけで導入・本題・着地が収まる境界を選び、アプリが
anchor包含・順序・最小/最大尺を再検証します。
LLMが自由な時刻を生成することはありません。

WebUIでは独立した「LLM要約・見どころ」タブで `[要約済み]` の動画を選ぶと、
保存済みの要約・時間付き章・候補状態を自動で読み込みます。「見どころ候補を生成」
を押せば再要約せず候補を生成でき、個別preview、
または「検索・編集・切り抜き」画面へcleanな初期範囲として読み込めます。候補だけで
clipを自動保存することはありません。映像だけの出来事、表情、音の盛り上がりは評価対象外です。

候補の作り方はRadioで「要約から自動選定」と「自然言語クエリで検索」を切り替えられます。
クエリ検索は選択動画内の正規化文字一致とBGE-M3意味検索を共通検索サービスで実行し、検索hitを
実在ASR segment境界へ拡張します。生成候補は各候補の番号・時間・タイトルの横にあるRadioを
直接選択でき、説明全体のクリックでも選択できます。「選択候補のみ」または
「表示中の全候補」を明示的な保存操作でローカル動画として切り抜けます。既存ファイルは上書きしません。

既にreadyなLLM解析がある動画ではCLIからも生成できます。

```powershell
venv\Scripts\python.exe generate_highlights.py --video-id <公開動画ID> --count 6 --min-duration 20 --max-duration 90
```

候補runは元のTranscript revisionとanalysis runに紐づく派生データとしてSQLiteへ保存され、
ASR原文、検索index、編集内容は変更されません。

## ブラウザE2E（開発者向け）

合成動画と隔離DBだけを使用し、実際の `data/` や動画には触れません。Microsoft Edgeと
ffmpegを用意してから次を実行してください（未導入時は理由付きでskipされます）。

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -m unittest tests.test_intuitive_editor_browser -v
```

## ライセンス

MIT License
