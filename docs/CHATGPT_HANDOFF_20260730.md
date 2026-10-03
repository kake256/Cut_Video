# Cut_Video 引き継ぎ資料（2026-07-30）

## 0. この文書の目的

この文書は、別のChatGPTアカウントまたは別の開発担当者が、過去の会話履歴を参照できなくてもCut_Videoの開発を再開できるようにするための引き継ぎ資料である。

対象は次の内容である。

- 現在利用できる機能と利用者向けの操作フロー
- 現在の実装構造と重要な設計判断
- Gitブランチ、未Pushコミット、復帰点
- 実装済みだが評価中の機能
- 既知の制約、不具合候補、今後の優先課題
- セットアップ、起動、テスト、プライバシー上の注意
- 次の担当者が最初に確認すべき事項

この文書だけで全仕様を置き換えるものではない。ユーザー向け挙動の正本は [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md)、責務分割と実装順の正本は [ARCHITECTURE_IMPLEMENTATION_PLAN.md](ARCHITECTURE_IMPLEMENTATION_PLAN.md) である。両文書と実装が食い違う場合は、すぐにコードへ合わせて仕様を書き換えず、バグ、意図した移行、未実装要件のどれかを判断すること。

## 1. 最重要サマリー

Cut_Videoは、Windows上でローカルに動作する「動画の登録・文字起こし・自然言語検索・範囲編集・切り抜き保存」ツールである。現在はGradio WebUIを主UIとし、faster-whisper、BGE-M3、SQLite、FAISS、ffmpegを組み合わせている。

プロダクトの中心的な流れは次のとおりである。

```text
URLまたはローカル動画
  -> yt-dlpによる取得（URLの場合）
  -> faster-whisperによる文字起こし
  -> 15秒チャンク化（5秒オーバーラップ）
  -> BGE-M3による埋め込み
  -> SQLite + immutable FAISS generationへ公開
  -> 正規化文字列一致 / 意味検索
  -> 候補を編集画面へ読み込み
  -> 全体範囲・途中除外・字幕・出力形式を調整
  -> ffmpegで検証付き保存
```

現行UIの上位タブは次の順番である。

1. 検索・編集・切り抜き
2. LLM要約・見どころ
3. 動画保存
4. インデックスの共有

旧来の検索・切り抜きUIは削除せず、`CUT_VIDEO_ENABLE_LEGACY_UI=1` のときだけ表示する。通常運用では直感編集UIのみを利用する。

## 2. 現在のGit状態

この節は2026-07-30時点の状態である。作業再開直後に必ず `git status` と `git log` で再確認すること。

| 項目 | 値 |
|---|---|
| リポジトリ | `kake256/Cut_Video` |
| ローカル作業場所 | `F:\myapp\cut` |
| 現在ブランチ | `experiment/short-video-finishing` |
| 現在HEAD | `fcaadc48e120230bfbb0a3987376427a102042bc` (`fcaadc4`) |
| `origin/main` | `b99ba0a` |
| `origin/main`との差 | current branchが8コミット先行、取り込み待ち |
| 実験前の復帰ブランチ | `checkpoint/pre-finishing-20260730` |
| 実験前の復帰コミット | `c752471d5d49ce81d3301334ffe3ef8f97878440` |

現在ブランチの追加コミットは次のとおりである。

| コミット | 内容 |
|---|---|
| `eb46cc8` | 出力プロファイルと字幕プリセット基盤 |
| `4f29627` | 出力プロファイルを編集UIへ接続 |
| `9badb59` | 保存ジョブの進捗と停止処理 |
| `06496a0` | 見どころ一括保存と中間動画再利用 |
| `03b1d97` | 音量正規化とローカルBGM仕上げ |
| `61fbf10` | 正方形出力と投稿補助メタデータ |
| `fcaadc4` | 仕上げ機能の実装結果と復帰手順を文書化 |

重要事項:

- この実験ブランチの8コミットは、引き継ぎ資料作成時点でリモートへPushされていない。
- mainへ直接上書きせず、実動画での主観評価後に、実験ブランチをPushしてPRまたは明示的な統合作業を行うのが安全である。
- 実験を取り消して直前の安定状態へ戻す場合は `git switch checkpoint/pre-finishing-20260730` を使う。
- 実験へ戻る場合は `git switch experiment/short-video-finishing` を使う。
- この引き継ぎMarkdown自体は、作成直後は未コミット差分として残る。コミットやPushはユーザーの明示的な依頼後に行うこと。

## 3. プロダクトの目的と対象範囲

### 3.1 目的

長時間動画から、文字情報を手掛かりに必要な場面を素早く見つけ、切り抜き範囲を調整し、必要であれば途中の不要箇所を除外し、通常動画またはショート向け動画としてローカル保存する。

一般的なNLEを全面的に置き換えることは目的としていない。強みは次の一連の作業を一つのローカルツールでつなぐことである。

- 動画のダウンロードまたは登録
- 自動文字起こし
- 正規化文字列検索と意味検索
- 文字起こしを利用した範囲選択
- 全体範囲と複数の途中除外を使った粗編集
- 字幕焼き込み、縦型・正方形出力、簡易音声仕上げ
- LLM要約と見どころ候補の生成

### 3.2 明示的に対象外または未実装の領域

- 複数トラックを持つ本格的なNLE
- トランジション、速度変更、キーフレーム、カラーグレーディング
- CLIP/SigLIP等による映像内容検索
- VLMによる検索結果や見どころ候補の再検証
- 顔追跡、話者追跡、被写体を追うスマートクロップ
- TTS、ストック映像自動挿入、SNS自動投稿
- クラウドLLMへの文字起こし送信
- オンラインBGM取得や同梱BGMライブラリ

## 4. 利用者向け機能の現状

### 4.1 動画保存・インデックス作成

- ローカル動画ファイルまたはYouTube/Twitch等のURLを指定できる。
- URLはyt-dlpでダウンロードする。可能な場合は投稿日時を含むファイル名を使う。
- ASRモデルを選択できる。現在のUI候補は `large-v3`、`large-v3-turbo`、`medium`。
- GPUの空きVRAMを `nvidia-smi` から確認し、検索処理用の余白を残してバッチサイズを自動決定する。
- バッチ並列推論の有効・無効を選べる。
- インデックス処理は別プロセスで実行され、進捗ログと停止ボタンを提供する。
- 途中保存と再開に対応する。
- 登録完了後、動画、文字起こし、チャンク、埋め込み、検索generationが関連付けられる。

長時間動画では、ASR進捗が見え始める前に音声デコードとVADが数分かかることがある。12時間級動画で3〜4分程度無言に見える事例があり、UIには案内を表示している。

### 4.2 動画選択

- 動画一覧はサムネイルカードで表示する。
- ファイル名、長さ、文字起こし済み、索引あり、要約済み等の状態をカードで判断できる。
- ファイル名による絞り込みと一覧更新がある。
- LLM要約画面でも検索・編集画面と同様にサムネイルから動画を選べる。
- 未要約動画は視覚的に薄く表示し、要約状態が分かるようにしている。
- 検索対象の既定値は、利用者が選択した動画である。「すべての動画」は明示的に選ぶ。
- 動画ファイル、出力動画、インデックス等について、Windows Explorerで保存場所を開く導線がある。

### 4.3 検索

利用者へ表示する検索結果は、原則として次の2分類だけである。

1. 正規化後の文字列一致
2. 意味検索結果

正規化文字列一致の特徴:

- 完全な部分一致をスコア閾値なしで採用する。
- Unicode NFKC、ひらがな・カタカナ差、空白や表記揺れを吸収する正規化を行う。
- 「ワンチャン」と「ワンちゃん」のような表記差を同一検索意図として扱えるようにしている。
- 一致位置は実在するASR単位へ対応付ける。文字数から時間を比例推定しない。
- ベクトル類似度が低くても、文字列一致結果は消さない。

意味検索の特徴:

- BGE-M3でクエリを埋め込み、FAISSで検索する。
- 指定動画内検索では、その動画に属する候補だけで順位を計算する。
- 既定の最低スコアは0.55。
- 文字列一致とは別分類で表示する。

UIは段階表示を採用する。正規化文字列一致を先に返し、意味検索を後から追加する。古い意味検索結果が新しいクエリの画面を上書きしないよう、リクエストtokenと固定publicationを使う。

検索候補からは再生、候補区間から編集、動画切替ができる。検索なしで選択動画を直接編集することもできる。

### 4.4 直感編集

編集状態の正本は整数ミリ秒の `EditPlan` である。

```text
source duration
  + overall range [start, end)
  + zero or more exclusion ranges [start, end)
  = kept ranges
```

編集機能:

- 保存対象となる全体開始・全体終了の設定
- 複数の途中除外区間の追加、変更、削除
- 重複または接する除外区間の正規化・統合
- 文字起こしのASR時刻単位から境界を選択
- プレビューの現在位置を選択境界へ適用
- 数値入力による時刻適用
- 0.1 / 1 / 10 / 30 / 60 / 600秒単位の前後微調整
- 元動画全体を俯瞰する概要タイムライン
- 選択範囲を拡大して編集する詳細タイムライン
- 全体編集と詳細編集をタブで切り替えて画面領域を確保
- Source timelineと、途中除外適用後のResult timelineの対応
- Undo / Redo
- 編集結果プレビューと元動画プレビューの切替

時刻は半開区間 `[start, end)` として扱う。浮動小数点秒をドメイン状態の正本にしない。プレビュー、文字起こし、タイムライン、数値操作は、すべて同じEditPlanへコマンドを直列適用する。

ASRが単語時刻を持つ場合は単語単位、セグメント時刻しかない場合はセグメント単位で境界を選ぶ。情報がない文字単位の時刻を捏造してはいけない。

### 4.5 保存と出力

通常編集の保存では次を行う。

- immutableな編集snapshotを作る。
- 保存開始時と公開直前に元動画fingerprintを検証する。
- 出力先と同じファイルシステム上の一時領域へ生成する。
- 同名ファイルを上書きせず、claimを取得する。
- ffprobeで出力のduration、映像stream、フレーム許容差を検証する。
- 必要に応じてSRT sidecarを生成する。
- manifestを最後に公開する。
- journalを利用してクラッシュ後の回復対象を判定する。
- 保存完了時のsnapshotをclean referenceにし、保存中に追加された編集をdirtyのまま維持する。

高速プレビューではキーフレームの都合で1〜2秒程度ずれる場合がある。フレーム精度保存では再エンコードを用いる。`-ss` は入力シークとして `-i` より前に置く設計であり、長時間動画の保存速度のためにこの順序を崩さない。

### 4.6 第3段階「出力・字幕」

検索・編集・切り抜き画面の第3段階で、範囲編集後の仕上げを設定する。

実装済みの `OutputProfile`:

- `source`: 元の縦横比を維持
- `portrait_blur`: 9:16、背景をぼかして元画面全体を残す
- `portrait_crop`: 9:16、中央クロップ
- `square_fit`: 1:1、正方形キャンバスに収める

字幕機能:

- 字幕なし / 字幕あり
- standard / large / boxed等の制約付きpreset
- 上 / 中央 / 下の位置
- safe margin
- 動画幅に応じた自動改行
- 長すぎる字幕をおおむね10文字前後、かつ時刻情報が許せば500ms以上を保つよう分割
- Windows GDIによるfont glyph検査
- previewとsaveで同一profileを使用

音声仕上げ:

- 音量正規化は既定OFF。現在はffmpeg `loudnorm` の1-passで、既定目標はおおむね-16 LUFS。
- ローカルBGMは既定OFF。ファイル、gain、fade in/outを指定できる。
- BGMの絶対パスはmanifestやログへ保存しない。basenameとcontent fingerprintだけを永続化する。
- 出力後にaudio streamとsample peakを確認し、クリッピングの可能性を警告する。

ExportJob:

```text
queued -> validating -> joining -> rendering -> probing -> publishing
       -> completed / failed / cancelled
```

- UIに段階的な進捗を表示する。
- 停止操作でffmpeg subprocessを終了する。
- ログへ個人パスや文字起こし本文を出さない。

### 4.7 LLM要約

ローカルOllamaだけを利用する実験機能である。外部LLMへデータを送らない。

- provider既定値: `ollama`
- endpoint既定値: `http://127.0.0.1:11434`
- model既定値: `qwen3:8b`
- 動画全体の要約、タグ、時刻付き章を生成する。
- 日本語出力を要求し、英語等の不適切な結果は検査して再試行する。
- LLM結果はASR原文を上書きせず、transcript revisionに紐づくderived dataとしてSQLiteへ保存する。
- 新規インデックス作成後に任意実行できるほか、既存動画へWebUIから後付け解析できる。
- Ollama未起動・model未導入でも動画登録、文字起こし、検索は成功扱いにし、LLM解析だけ失敗として後から再実行できる。

### 4.8 見どころ候補

- 保存済み要約から候補を作れる。
- 自然言語クエリに関連する見どころ候補を作れる。
- LLMへ実在するsegment IDを渡し、返却されたIDと境界をアプリ側で検証する。
- 候補数の既定値は6。
- 候補尺の既定範囲は20〜180秒。
- 候補ごとにタイトル、分類、説明、選定理由、タグ、開始・終了を表示する。
- 候補説明の番号付近にあるradio選択で対象を切り替える。
- 候補をプレビューできる。
- 「編集」から検索・編集・切り抜き画面へ遷移し、第3段階で字幕や出力形式を調整できる。
- 字幕なしで候補をまとめて保存する機能は維持する。
- 選択候補のみ、または表示中の全候補をbatch保存できる。
- 1候補の失敗で全件を破棄せず、部分成功と最終summaryを返す。
- 同一snapshotから複数出力を作る内部APIでは、中間join結果を再利用できる。

投稿補助metadata:

- 任意でJSON sidecarを生成する。
- title、description、tagsを含む。
- LLM由来であることと `requires_review` を記録する。
- SNSへの投稿はしない。

注意: 見どころ候補はテキストだけを根拠にしており、映像上の見栄え、表情、音量、盛り上がり、画面転換を理解しない。最終候補は人間がプレビューして判断する必要がある。

### 4.9 インデックス共有

- SQLite、FAISS、必要なmetadataをzip形式でexport/importできる。
- package schemaはversion 2。
- source path、private fingerprint、元のローカルファイル名等をredactする。
- 共有先ではローカル動画を再関連付けできる。
- 再関連付け時はduration等を検証する。
- 旧packageのimport互換性を残している。
- 文字起こし本文を含むため、生成zipは公開リポジトリへコミットしない。

### 4.10 CLI

version付きCLIは `video_tool.py` で提供する。

```powershell
venv\Scripts\python.exe video_tool.py search --query "命令" --video-id <public-video-id>
venv\Scripts\python.exe video_tool.py clip --video-id <public-video-id> --plan-json <edit-plan.json>
```

外部契約schemaはv1。互換入口として `search_video.py` と `cut_clip.py` も残っている。WebUIとCLIは検索の意味論を `SearchService` へ共通化し、別々の一致判定を実装しない。

## 5. アーキテクチャ

### 5.1 採用方針

全面的なdesktop-native再実装ではなく、現在はmodular monolithを採用している。

```text
Gradio adapter / CLI adapter
          |
Application services / job orchestration
          |
Pure domain (EditPlan, TimelineMap, contracts)
          |
Persistence / media / model adapters
```

将来デスクトップアプリ化する場合も、現行のdomain、application service、CLI/API契約を再利用し、UIだけを置き換えられる方向を維持する。

### 5.2 主要ファイルと責務

| ファイル | 責務 |
|---|---|
| `app.py` | Gradio WebUI、イベント結線、各画面のadapter。約8366行で現在の最大モノリス |
| `index_video.py` | ASR、チャンク、埋め込み、DB/FAISS公開を行う別プロセスCLI |
| `video_tool.py` | version付きCLI入口 |
| `search_video.py` / `cut_clip.py` | 旧CLI互換入口 |
| `moment_retrieval/edit_domain.py` | 整数msのEditPlan、range invariant、TimelineMap |
| `moment_retrieval/application.py` | document、history、dirty/clean、save ticket |
| `moment_retrieval/search_service.py` | 正規化一致と意味検索の共通サービス |
| `moment_retrieval/staged_search.py` | 段階検索、token、古い結果の抑止 |
| `moment_retrieval/publication.py` | immutable search generation、writer/read lease、公開 |
| `moment_retrieval/db.py` | SQLite schema/migration、動画・文字起こし・derived data。schema version 9 |
| `moment_retrieval/save_service.py` | ArtifactTransaction、variant保存、journal/recovery、検証 |
| `moment_retrieval/output_profile.py` | 出力形式、字幕、音声profile、font検証 |
| `moment_retrieval/short_video.py` | ASS生成、canvas変換、音声処理、ffmpeg組み立て |
| `moment_retrieval/export_jobs.py` | export job状態、進捗、cancel |
| `moment_retrieval/llm_analysis.py` | Ollama要約、validation、再試行、保存形式 |
| `moment_retrieval/highlight_analysis.py` | 見どころ生成、候補検証、batch処理 |
| `moment_retrieval/posting_metadata.py` | 投稿補助JSON metadata |
| `moment_retrieval/share.py` | privacy-redacted index package v2 |
| `moment_retrieval/preview_cache.py` | サムネイルとpreview cache |
| `moment_retrieval/asr.py` | faster-whisper adapter、単語/segment時刻 |
| `moment_retrieval/chunker.py` | 検索用チャンク生成 |
| `moment_retrieval/embedder.py` | BGE-M3埋め込み |
| `moment_retrieval/vector_index.py` | FAISS読み書き・検索 |
| `moment_retrieval/refine.py` | 話の切れ目を使った候補境界拡張 |
| `moment_retrieval/subtitles.py` | 字幕データ変換 |
| `moment_retrieval/transcript_types.py` | 文字起こしの型とtiming evidence |
| `assets/app.css` | 現行UIのstyle |
| `assets/intuitive_editor.js` | タイムライン、文字起こし選択、ブラウザ側同期 |
| `moment_retrieval/ui_assets.py` | UI asset読込と検証。asset欠損時はstartupを失敗させる |

`app.py` は機能を蓄積した結果大きい。今後はdomainロジックを戻さず、Gradio event adapter単位で段階的に分離する。ただし大規模な一括リライトは避け、characterization testを追加してから行う。

### 5.3 データ保存先

既定値:

| パス | 内容 |
|---|---|
| `data/index.db` | SQLite source of truth |
| `data/generations/` | immutable FAISS generation |
| `data/text.index` | 互換用index path |
| `data/previews/` | preview / thumbnail cache |
| `video/` | 元動画 |
| `clips/` | 保存した切り抜き、manifest、sidecar |
| `exports/` | 共有package等 |

SSD/HDDを分ける場合は、DB、FAISS、cache、model cacheをSSD、巨大な元動画をHDDへ置く構成が現実的である。動画の連続デコードはHDDでも動くが、ランダムシーク、thumbnail、DB/FAISS、cache生成はSSDの恩恵が大きい。ダウンロード速度は主に回線と配信元が律速し、SSD化による改善は後処理や書き込み詰まりがある場合に限られる。

主な環境変数:

| 変数 | 用途 / 既定値 |
|---|---|
| `CUT_VIDEO_DATA_DIR` | data root、既定 `data` |
| `CUT_VIDEO_LIBRARY_ROOT` | library metadata root |
| `CUT_VIDEO_SEARCH_ROOT` | search index root |
| `CUT_VIDEO_CACHE_ROOT` | preview/cache root |
| `CUT_VIDEO_SOURCE_ROOTS` | 元動画root一覧、既定 `video` |
| `CUT_VIDEO_ARTIFACT_ROOT` | 出力root、既定 `clips` |
| `CUT_VIDEO_SEARCH_GENERATIONS_DIR` | immutable generation格納先override |
| `CUT_VIDEO_ENABLE_LEGACY_UI` | 旧UIを表示 |
| `CUT_VIDEO_SQLITE_BUSY_TIMEOUT_MS` | SQLite待機、既定10000ms |
| `CUT_VIDEO_SQLITE_JOURNAL_MODE` | 既定WAL |
| `CUT_VIDEO_SQLITE_SYNCHRONOUS` | 既定NORMAL |
| `CUT_VIDEO_LLM_ANALYSIS_PROVIDER` | 既定 `ollama` |
| `CUT_VIDEO_LLM_ANALYSIS_MODEL` | 既定 `qwen3:8b` |
| `CUT_VIDEO_LLM_ANALYSIS_ENDPOINT` | loopback endpoint |
| `CUT_VIDEO_LLM_HIGHLIGHT_COUNT` | 既定6 |
| `CUT_VIDEO_LLM_HIGHLIGHT_MIN_DURATION_SEC` | 既定20 |
| `CUT_VIDEO_LLM_HIGHLIGHT_MAX_DURATION_SEC` | 既定180 |

## 6. 絶対に維持すべき設計上の不変条件

### 6.1 WhisperはGradio workerでloadしない

Windows上でGradio worker thread内に `WhisperModel` をloadするとaccess violationでプロセスごと終了した実績がある。インデックス処理を `subprocess.Popen` で完全な別プロセスにしているのは回避策ではなく必須設計である。便利に見えても `app.py` 内へ直接戻してはいけない。

### 6.2 PID cleanupでapp自身をkillしない

残留job cleanupは、command lineに `index_video` または対応する解析processを含む対象だけに限定し、`os.getpid()` と同じPIDを必ず除外する。PID再利用を考慮する。

### 6.3 時刻を捏造しない

- canonical timeは整数ms。
- rangeは `[start, end)`。
- ASRが持つ最小単位より細かい文字位置へ、文字数比例で時刻を割り当てない。
- 表示用浮動小数点値をdomainの正本へしない。

### 6.4 文字列一致を意味検索閾値へ従属させない

正規化後に実際の部分一致がある結果は、FAISS scoreが低くても採用する。文字列一致と意味検索を一つのscoreで混ぜない。

### 6.5 一検索要求でpublicationとrevisionを固定する

検索中に新generationが公開されても、同じ要求の途中で参照先を切り替えない。SQLiteとFAISSの世代不一致を起こさない。

### 6.6 ローカルpathを公開identityにしない

動画のpublic IDはopaqueにする。source path、ユーザー名、ドライブ構成をshare package、manifest、ログ、HTMLへ不要に出さない。

### 6.7 LLM結果でASR原文を上書きしない

LLMによる自然化、要約、章、見どころ、字幕修正はderived dataとして保持する。検索の根拠となるASR transcriptとtiming evidenceを破壊しない。

### 6.8 previewとsaveの設定を一致させる

preview専用の見た目とsave専用のprofileを別々に組み立てない。同じ `OutputProfile` と同じ編集snapshotを使う。

### 6.9 保存は上書きせず、manifestを最後に公開する

未完成artifactを完成品として見せない。claim、同一filesystem staging、probe、publish、manifest-last、cleanup/recoveryを維持する。

### 6.10 プライバシーを既定で守る

文字起こしや動画を外部サービスへ送らない。LLMはloopbackのOllamaだけにする。BGM pathやtranscript本文をログへ含めない。

## 7. 開発・実行環境

引き継ぎ時点で確認したローカル環境:

- Python: 3.10.11
- Python実行: 必ず `venv\Scripts\python.exe`
- ffmpeg: 2025-02-20 git build（gyan.dev系）
- Gradio: 6.19.0固定
- pyarrow: 16.1.0固定
- torch: 2.6以上
- ASR: faster-whisper
- 埋め込み: FlagEmbedding / BGE-M3、1024次元
- vector search: faiss-cpu
- downloader: yt-dlp

重要な依存関係上の理由:

- `pyarrow>=21` は対象Windows環境でimport時にcrashしたため16.1.0へ固定している。
- BGE-M3周辺のtransformers安全要件からtorch 2.6以上が必要。
- system Pythonに必要packageがある前提にしない。

セットアップ:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

通常起動:

```powershell
start.bat
```

または:

```powershell
venv\Scripts\python.exe app.py
```

既定URL:

```text
http://127.0.0.1:7860
```

Ollamaも導入する場合:

```powershell
start.bat --setup-ollama
```

単独setup scriptとして `setup_ollama.bat` と `scripts/setup_ollama.ps1` もある。Ollamaのservice起動と `qwen3:8b` のpull完了を確認してからLLM解析を行う。

## 8. テストと直近の検証結果

引き継ぎ資料作成前の直近の実行結果:

- Python test suite: 437 passed, 1 skipped
- Browser E2E: 2 passed
- 任意の600秒browser performance test: 通常はskip
- 実ffmpeg: 字幕、9:16、1:1、音量正規化+BGMの生成成功
- privacy guard: pass
- `git diff --check`: pass

標準テスト:

```powershell
$env:PYTHONIOENCODING='utf-8'
venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
venv\Scripts\python.exe -m unittest tests.test_intuitive_editor_browser -v
venv\Scripts\python.exe scripts\check_privacy.py --working-tree
git diff --check
```

600秒browser performance testを有効にする場合:

```powershell
$env:CUT_VIDEO_RUN_BROWSER_PERF='1'
venv\Scripts\python.exe -m unittest tests.test_intuitive_editor_browser -v
```

テスト成功だけでは完了しない領域:

- 字幕の読みやすさ
- 9:16 crop/blurと1:1 fitの構図
- 音量正規化の聞こえ方
- BGMの音量、fade、会話とのバランス
- HDD上の長時間動画での待ち時間
- 見どころ候補の内容品質と切れ目

これらはユーザー所有の実動画をローカルでプレビューして、人間が評価する必要がある。ただし動画、音声、文字起こしを外部サービスへ送ってはならない。

## 9. 既知の課題と優先順位

### P0: 引き継ぎ直後に判断すること

#### P0-1. 実験ブランチをどう統合するか

ショート動画仕上げの8コミットはテスト済みだが未Pushである。まず実動画で次を確認し、問題なければ実験ブランチをPushしてPR化する。

- source / portrait blur / portrait crop / square fit
- 字幕preset、位置、安全領域、自動改行、分割
- cancel後にpartial fileやclaimが残らないこと
- normalize/BGMの音質
- metadata sidecarの内容と個人情報非包含

#### P0-2. 主観評価を終える

自動テストで判断できない字幕、構図、音声を評価し、presetの既定値を確定する。特に `portrait_crop` は顔追跡がなく中央cropだけなので、素材によって人物やUIが切れる。

#### P0-3. UI密度を確認する

第3段階「出力・字幕」は設定が増えている。プレビューと字幕調整を横並びにしつつ、1画面で確定できる意図がある。小さい画面で重要操作が下へ押し出されていないか確認する。

### P1: 信頼性と整合性

#### P1-1. 見どころbatch保存をArtifactTransactionへ統合する

通常編集の保存はjournal付きの `ArtifactTransaction` を使う。一方、見どころbatchとposting metadataは、例外時cleanupとclaimを持つが、通常保存と同じクラッシュ回復protocolへ完全統合されていない。

突然のprocess終了や電源断が動画公開とmetadata公開の間に起きると、orphan sidecarが残る可能性がある。次の候補は、見どころ保存も `save_service` のtransactionへ載せ、動画、SRT、metadata、manifestを同じjournalで管理すること。

#### P1-2. 音量正規化の方式を評価する

現在は1-pass loudnorm。速度面では扱いやすいが、2-passと比べた音質・目標精度の評価が未完了。長時間出力での性能も測定する。

#### P1-3. true peak検証を強化する

現在のpost-render確認は `volumedetect` のsample peakが中心で、厳密なEBU true-peak測定ではない。必要ならloudnorm JSONまたはtrue-peak対応filterの結果を機械可読に検証する。

#### P1-4. 出力後のfull audio decode性能

音声stream/peak確認で出力音声を一度全decodeする。HDD上の長時間出力では待ち時間が増える可能性がある。正確性を落とさず、検査時間を進捗表示するか、計測の上で方式を見直す。

#### P1-5. immutable generationのGCを完成させる

reader leaseとwriter leaseの基盤はあるが、heartbeat/acquisitionのさらなるhardeningと、古いgenerationの物理削除policyは保留中。参照中generationを消す危険があるため、GCを安易に有効化しない。

### P2: コード保守性とUI

#### P2-1. `app.py` の段階的分割

`app.py` は約8366行。UI component、event adapter、job presentationを別moduleへ移す余地がある。domainロジックをUIへ戻さず、既存browser testで挙動を固定してから小分けに行う。

#### P2-2. Gradio互換stateの整理

domain history/application sessionへ移行済みだが、Gradio側に過去互換のundo stackやview state mirrorが一部残る。二重の正本にならないよう整理する。

#### P2-3. batch失敗項目の再試行UI

部分成功とsummaryはあるが、失敗した1件だけを明示的に再試行する導線が弱い。エラーcode、候補ID、再試行ボタンを揃える。

#### P2-4. multi-profile UIは未公開

`save_document_variants` は同一snapshotから複数profileへfan-outでき、内部テストもある。ただしUIでsource/portrait/squareを同時選択するmatrixは出していない。必要性を確認してから公開する。

### P3: 検索・LLM・映像理解の品質

#### P3-1. 見どころ候補を再評価する

最大尺を180秒へ変更した後の体系的評価が不足している。過去評価ではboundary warningの比率が高かった。開始・終了の自然さ、説明と実際の会話の一致、重複候補を評価する。

#### P3-2. 映像・音声特徴の利用

現在の見どころは文字起こし中心。将来価値が高い候補は次の順で検討する。

1. scene changeと無音区間を境界補助に使う
2. 音量、笑い、発話密度をranking featureにする
3. 顔・主対象の検出をcrop補助に使う
4. CLIP/SigLIP検索
5. VLMによる最終再検証

ただし新model導入はVRAM、初回download、privacy、処理時間を増やす。必ずoptional featureにする。

#### P3-3. 文字起こし自然化

句読点や誤変換をLLMで直す場合も、ASR原文を変更しない。`display transcript` や `caption revision` として別revisionに保存し、検索根拠とtiming evidenceを追跡可能にする。

### 既知の制約

- 高速previewはキーフレームにより1〜2秒ずれる場合がある。
- 長時間動画はASR表示開始前のdecode/VADが長い。
- `portrait_crop` は中央固定で、被写体追跡をしない。
- subtitle splitはASR timingに制約され、常に理想的な文節にはならない。
- local BGMの著作権・利用許諾は利用者の責任。
- `y.py` は旧yt-dlp scriptで未使用。現在は個人ドライブ絶対pathは検出されていないが、削除候補である。
- README内の「実験ブランチ」表記は、今後mainへ統合した時点で更新が必要。

## 10. プライバシーとGit運用

次の内容は、読む、外部へ送る、commitする、Pushする、画面共有用ログへ貼ることを避ける。

- `data/`
- `video/`
- `clips/`
- `exports/`
- `.env` と `.env.*`
- credential、token、秘密鍵
- private transcript
- 利用者の個人pathを含むログ

これらは `.gitignore` 対象である。zip形式のindex exportは文字起こし内容を含み得るため、共有packageだから安全とは考えない。

変更前後に次を実行する。

```powershell
git status --short
venv\Scripts\python.exe scripts\check_privacy.py --working-tree
git diff --check
```

commit、Push、branch削除、ユーザーデータ削除、認証変更、課金を伴う操作は、ユーザーの明示的な許可がある場合だけ行う。

## 11. 次の担当者が最初に行う手順

### 11.1 状態確認

```powershell
Set-Location F:\myapp\cut
git status --short --branch
git log --oneline --decorate -12
git rev-list --left-right --count origin/main...HEAD
```

期待状態は `experiment/short-video-finishing`、HEAD `fcaadc4`、origin/mainより8コミット先行である。ただしこの引き継ぎ文書が未コミット差分として表示される。

### 11.2 正本を読む

読む順番:

1. この文書
2. `docs/PRODUCT_SPECIFICATION.md`
3. `docs/ARCHITECTURE_IMPLEMENTATION_PLAN.md`
4. `docs/SHORT_VIDEO_FINISHING_REQUIREMENTS.md`
5. `docs/SHORT_VIDEO_FINISHING_EXPERIMENT.md`
6. 変更対象に対応するtest

`docs/INTUITIVE_EDITOR_ROADMAP.md` は過去の統合経緯であり、現行仕様の正本ではない。

### 11.3 起動前の軽量検証

```powershell
venv\Scripts\python.exe -m unittest tests.test_short_video tests.test_export_jobs tests.test_save_service_integration -v
```

注: test module名は今後変更され得る。存在しない場合は `tests/` を確認し、同等の短編出力、export job、保存transactionのtestを実行する。

### 11.4 実動画で手動確認

外部へ送信せず、ローカルUIで次を確認する。

1. 文字起こし済み動画を選ぶ。
2. 選択動画を既定対象として「命令」等の明示的な語を検索する。
3. 文字列一致と意味検索が別表示されることを確認する。
4. 候補から編集へ進む。
5. 全体開始・終了、途中除外、微調整、Undo/Redoを試す。
6. source、9:16 blur/crop、1:1をpreviewする。
7. 字幕位置と自動改行を確認する。
8. 短い範囲でnormalize/BGMを確認する。
9. cancelと再保存を試し、partial fileが残らないことを確認する。
10. LLM要約済み動画から見どころを生成し、編集へ遷移する。

### 11.5 統合判断

手動確認で重大問題がなければ、ユーザーへ次を明示して許可を得る。

- Pushするbranch名
- mainへ直接mergeするかPRにするか
- 未解決の主観課題
- rollback branchとcommit

## 12. 推奨する次の開発順序

短期:

1. 実験ブランチの実動画評価
2. 見どころbatch保存とmetadataをArtifactTransactionへ統合
3. 第3段階UIの密度とpreview/save parityを確認
4. 音声の1-pass/2-pass、true peak、HDD性能を測定
5. 未Push branchをPR化してバックアップ

中期:

1. `app.py` のadapter分割
2. batch再試行UI
3. caption revisionを導入し、ASR原文と字幕整形を分離
4. scene change/無音を候補境界へ利用
5. real-media acceptance testの手順と結果を文書化

長期:

1. 安定したapplication API/CLIの上にdesktop shellを比較実験
2. 被写体追跡付きsmart crop
3. 映像検索とVLM再検証
4. 複数profile同時出力のUI公開

## 13. 完了条件の考え方

新機能または修正を完了扱いにする前に、少なくとも次を満たす。

- user-visible behaviorが製品仕様と一致する。
- domain stateの正本が一つである。
- WebUIとCLIで検索・編集の意味論が分岐していない。
- private path、動画、文字起こしを外部へ送らない。
- 正常系だけでなくcancel、失敗、再試行、process終了を考慮する。
- 関連unit/integration/browser testを追加または更新する。
- `scripts/check_privacy.py --working-tree` と `git diff --check` が通る。
- 動画・字幕・音声の変更は、短い実動画で目視・聴取確認する。
- commit/Pushはユーザーの許可を得る。

## 14. 関連文書

| 文書 | 用途 |
|---|---|
| [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) | 現行の製品仕様・受け入れ条件 |
| [ARCHITECTURE_IMPLEMENTATION_PLAN.md](ARCHITECTURE_IMPLEMENTATION_PLAN.md) | 責務分割、状態所有権、段階実装 |
| [SHORT_VIDEO_FINISHING_REQUIREMENTS.md](SHORT_VIDEO_FINISHING_REQUIREMENTS.md) | 字幕、canvas、audio、batch、metadataの要件 |
| [SHORT_VIDEO_FINISHING_EXPERIMENT.md](SHORT_VIDEO_FINISHING_EXPERIMENT.md) | 現在実験ブランチの復帰・確認手順 |
| [PHASE2_TO_5_IMPLEMENTATION.md](PHASE2_TO_5_IMPLEMENTATION.md) | publication、save、CLI、直感UIの実装記録 |
| [HIGHLIGHT_EXPERIMENT_EVALUATION.md](HIGHLIGHT_EXPERIMENT_EVALUATION.md) | 見どころ候補の過去評価 |
| [PHASE0_BASELINE.md](PHASE0_BASELINE.md) | 初期性能・安全性baseline |
| [PHASE1_IMPLEMENTATION.md](PHASE1_IMPLEMENTATION.md) | identity、検索、EditPlan基盤の記録 |
| [INTUITIVE_EDITOR_ROADMAP.md](INTUITIVE_EDITOR_ROADMAP.md) | 歴史的なUI移行メモ（正本ではない） |

## 15. 新しいChatGPTアカウントへ渡す短い開始プロンプト

新しいアカウントでは、この文書を開いた状態で次のように依頼すると安全に再開しやすい。

```text
F:\myapp\cut\docs\CHATGPT_HANDOFF_20260730.md を最初から最後まで読み、
次に docs/PRODUCT_SPECIFICATION.md と
docs/ARCHITECTURE_IMPLEMENTATION_PLAN.md を確認してください。

現在は experiment/short-video-finishing ブランチで、
origin/main より8コミット先行している想定です。まずread-onlyでGit状態を検証し、
動画・文字起こし・data・clips・exports・秘密情報は読んだり外部送信したりしないでください。
Whisperの別プロセス設計と整数msのEditPlanを維持してください。
状態を要約し、次に行う作業と必要なテストを提案してください。
commitやPushは私が明示的に依頼するまで行わないでください。
```

---

最終更新: 2026-07-30  
対象HEAD: `fcaadc48e120230bfbb0a3987376427a102042bc`  
対象ブランチ: `experiment/short-video-finishing`
