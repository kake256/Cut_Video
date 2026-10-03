# CUT 引き継ぎプロンプト（2026-09-15時点）

以下の内容を前提として、CUTの開発・改善を引き継いでください。
この文書は現状の引き継ぎであり、記載された将来案を無断で実装する指示ではありません。
最初は現状理解と差分確認まで行い、次の具体的な依頼に合わせて作業してください。
リポジトリにアクセスできないチャットでは、確認したふりをせず、必要なソースや仕様書の添付を求めてください。

## 1. アプリの目的

CUT（CutVideo）は、長時間動画の発言を検索し、根拠を確認して必要な範囲を切り抜く、Windows中心のローカル動画編集アプリです。
主な流れは「動画登録 → 文字起こし・索引作成 → 発言検索 → プレビュー → 全体範囲と途中除外を調整 → 保存」です。
汎用NLEではありません。OpenCutのUIを参考にしていますが、OpenCutへ機能移植したものでも、OpenCutのコードを組み込んだものでもありません。
検索、ASR、編集、書き出し、任意のLLM解析はローカル処理です。モデル取得、利用者指定URLのダウンロード、明示的な依存更新等の通信とは区別してください。

## 2. 作業場所と状態

- 作業ディレクトリ: `F:\myapp\cut`
- シェル: PowerShell
- Pythonは必ず `F:\myapp\cut\venv\Scripts\python.exe` を使用。
- 確認時のbranch: `experiment/short-video-finishing`
- 確認時のHEAD: `fcaadc4`（仕上げ機能の実装結果と復帰手順を文書化）
- working treeには未コミット変更・未追跡ファイルが多数あります。現在の実装はHEADだけでは再現できません。新たにcloneするだけでは直近のUI/DL修正を引き継げません。
- `app.py`、製品仕様、LLMテスト等には元からの変更も含まれます。自分の変更だと決め付けず、既存差分を保護してください。
- 過去の引き継ぎ文書や実験前branchは履歴情報です。現状へ無断で上書き・切り戻ししないでください。

## 3. 優先して読む資料

1. `F:\myapp\cut\AGENTS.md` — 作業・安全ルール
2. `F:\myapp\cut\docs\PRODUCT_SPECIFICATION.md` — 正しい利用者向け動作の基準
3. `F:\myapp\cut\docs\ARCHITECTURE_IMPLEMENTATION_PLAN.md` — 設計方針、依存関係、移行順序
4. `F:\myapp\cut\docs\PHASE2_TO_5_IMPLEMENTATION.md` — 実装済み範囲と残る移行事項
5. `F:\myapp\cut\docs\UI_STUDIO_MIGRATION.md` — 新旧UI切り替えと退避
6. `F:\myapp\cut\docs\SHORT_VIDEO_FINISHING_EXPERIMENT.md` — 現在の仕上げ機能と未完了の手動評価
7. `F:\myapp\cut\README.md` — 起動・運用手順

製品仕様や設計書は目標・過去の所見も含み、全項目の実装完了証明ではありません。コード・テスト・実装記録を併せて判断してください。
`INTUITIVE_EDITOR_ROADMAP.md`は歴史的資料です。また、古い資料のPreview→Search→Transcriptという並びは、新しいStudioの既定配置とは異なります。
仕様と実装の差は、バグ・意図的移行・保留要件のどれかを明記してから変更してください。

## 4. 現在利用できる機能

### 登録・検索

- ローカル動画の登録とyt-dlpによるURLダウンロード。
- faster-whisperによる文字起こし・単語タイムスタンプ、BGE-M3の埋め込み、SQLite/FAISSによる検索。
- 基本の意味検索チャンクは15秒、5秒オーバーラップ。
- 正規化文字一致（NFKC、casefold、かな統一、CJK間空白吸収）と意味検索を区別して表示。
- 文字一致はSQLite上の完成済み文字起こしだけで動作し、FAISS・埋め込みモデルに依存しません。
- 最大2秒の間隔で隣接するASR区間をまたぐ文字一致と、実在の文字/単語時刻への根拠対応。
- 文字一致を先に、意味検索を後から表示。古い検索要求の遅延結果を新しい検索へ混ぜない仕組みがあります。
- サムネイル選択、ファイル名絞り込み、文字起こし/索引/要約の状態表示。
- 索引の停止・再開や世代公開を扱いますが、URLダウンロード自体の停止・完全なatomic登録などは仕様書だけで実装済みと判断しないでください。

### 共有・CLI

- 共有package v2の入出力、動画ID/内容digest/埋め込み情報の検証、互換import、元動画の再関連付け。
- 共有packageにはASR本文等が含まれ得ます。絶対パスの除去は内容の完全匿名化や送信承認を意味しません。
- `video_tool.py search/clip`のJSON schema v1、複数区間・精密保存・任意SRT。旧CLIも互換用に維持。

### 編集・保存

- 1本の元動画に対し、全体の開始/終了と複数の途中除外を指定し、残る区間を連結。
- 文字起こしの実時刻、プレビュー位置、タイムライン、直接入力、秒単位の微調整で境界を指定。
- Undo/Redo、未保存変更の確認、全体概要/詳細タイムライン、元動画/編集結果プレビュー。
- 高速コピー保存と精密再エンコード、複数区間保存、任意のSRT sidecar。
- 既存成果物の上書き防止、stagingと検証を伴う成果物公開、保存中の編集と保存完了の整合性管理。
- 保存場所をExplorerで開く導線。

### 仕上げ機能（現在の実験branchに実装あり）

- 元比率、縦型9:16、正方形1:1。余白ぼかし/中央crop。
- 字幕焼き込み、standard/large/boxed preset、上/中央/下、改行・分割・safe area・Windowsのfont glyph検査。
- セッション内の字幕本文/時刻編集。元ASRには書き戻しません。
- 任意の音量正規化、利用者所有のローカルBGM、音量/fade調整。
- 型付きOutputProfileをpreviewと保存で共有。
- ExportJobの段階進捗、停止、構造化失敗、候補batchの部分成功、結合済み中間動画の再利用。
- ローカル投稿補助metadata JSON。SNSへの自動投稿はありません。
- 合成素材の自動テストと、実素材での構図・字幕可読性・聴感評価は別です。後者を完了済みと扱わないでください。

### 任意のローカルLLM機能

- loopbackのOllamaを使う要約・タグ・時間付き章。既定モデル設定はqwen3:8b。
- 通常起動でOllamaやモデルを勝手に導入せず、明示セットアップと利用者操作で使用。
- 要約からの見どころ候補生成、または選択動画内の自然言語クエリによる候補検索。
- LLMに自由な時刻を生成させず、実在ASR segment IDからanchorと境界を解決・検証。
- 候補数は既定6件、最小20秒、最大180秒。最大尺は強制的に引き延ばす値ではありません。
- 候補preview、編集画面へ遷移、選択候補/全候補の明示保存。
- 見どころ保存は動画ごとのサブフォルダへ出力。既存のflat成果物は移動・削除しません。
- 要約・候補はrevisionに紐付く派生データであり、ASR・検索index・EditPlanを上書きしません。

## 5. UIと旧版の保護

上部タブは「検索・編集・切り抜き」「LLM要約・見どころ」「動画保存」「インデックスの共有」。
主編集画面には「① 全体を決める」「② 詳細編集（任意）」「③ 出力・字幕」があります。

現在の既定はGradio上のStudio配置です。
- 横幅に余裕がある場合、左から検索・中央プレビュー・右文字起こし。下にタイムラインと保存バー。
- 「レイアウトを調整」で左右パネルの交換、検索幅/文字起こし幅（各20〜32%）、高さ（300〜440px）を調整。
- 狭い画面では2列/縦積み。任意の場所へ自由にドラッグするドッキングUIではありません。
- 設定はlocalStorageの`cut-video-studio-layout-v1`に数値・選択肢のみ保存し、動画名/本文/パスは保存しません。
- レイアウト変更ではEditPlan・revision・Undo/Redo・dirtyを変更しません。

起動方法:
- 新UI: `F:\myapp\cut\start_studio_ui.bat`
- 旧配置: `F:\myapp\cut\start_classic_ui.bat`
- 通常起動: `F:\myapp\cut\start.bat`（既定Studio）
- 環境変数: `CUT_VIDEO_UI_LAYOUT=studio` または `classic`
- さらに古いタブを出す`CUT_VIDEO_ENABLE_LEGACY_UI=1`は、Classic配置とは別の設定です。

Studioは追加の`assets/studio_layout.css`/`assets/studio_layout.js`として分離し、元の`assets/app.css`/`assets/intuitive_editor.js`を変更しない方針です。
`moment_retrieval/ui_assets.py`が起動モードに応じて合成します。
変更前の実行用ソース48ファイルは`F:\myapp\cut\.ui-backups\20260905-before-studio\`へ退避済み。データ・venvのバックアップではなくGit管理対象外です。
比較・復帰にはClassic起動を優先し、退避ソースを現状へ一括上書きしないでください。
Studioと旧UIの操作時間比較は未実施です。Phase 5の性能比較gateに合格したとは主張しません。

## 6. 設計の重要原則

- 単一バックエンドのmodular monolith。GradioとCLIは共通サービスを使い、UIを増やしてもDB/編集状態の所有者を二重化しない。
- WhisperModelをGradio workerへロードしない。ASR/native処理は`index_video.py`の専用子プロセスに隔離する。
- 検索根拠（Search evidence）と編集の初期提案範囲（Suggested range）を混同しない。
- EditPlanはSource timeline上の整数ミリ秒・半開区間が基本。最小100ms、全区間除外の拒否、隣接除外の統合等をdomainで検証。
- Source（元動画）、Result（残る区間の連結結果）、Artifact（padding等を含む成果物）の時刻を分離し、TimelineMapで変換。
- Undo/Redoとdirtyは編集計画の意味的変更だけを扱い、表示操作を混ぜない。
- 公開動画IDはopaque。ローカルパスや旧path由来IDを公開識別子として流用しない。
- 完成したTranscript revision/検索publicationを参照し、途中のASR/indexや異なる世代を混ぜない。writer lease、reader snapshot、整合検証、公開機構の実装があります。
- superseded generationの本格的な物理GCは保留事項があります。既存のorphan cleanupと混同せず、読取り中世代の保護を確認せず削除しないでください。
- 保存はsnapshotを固定し、source再検査、staging、成果物検証、公開、clean reference更新を区別する。
- 検索・索引・字幕・保存等に失敗しても、既存成果物や編集状態を壊さない。
- Application層のEditorDocument/history/保存sequenceは実装済みですが、Gradio側に互換状態・Undo stackのミラーが残ります。「app.pyから状態管理を完全除去済み」ではありません。
- 全面React化、Tauri化、別HTTPサーバー化、storage migrationをUI改善と同時に行わない。

主要コード（いずれも`F:\myapp\cut\`配下）:
- `app.py`: Gradio構成・イベント・互換adapter。肥大化は残るため、境界ごとに段階改善。
- `moment_retrieval/search_service.py`, `staged_search.py`: 検索共通化と段階表示。
- `moment_retrieval/edit_domain.py`, `application.py`: EditPlan/TimelineMap/history/document。
- `moment_retrieval/db.py`, `publication.py`, `vector_index.py`: 永続化、世代と公開、検索index。
- `moment_retrieval/save_service.py`, `export_jobs.py`, `output_profile.py`, `short_video.py`, `subtitles.py`: 保存・仕上げ。
- `moment_retrieval/llm_analysis.py`, `highlight_analysis.py`: ローカル解析と候補。
- `moment_retrieval/downloader.py`, `share.py`, `config.py`: DL、共有/再関連付け、保存root等。
- `video_tool.py`: JSON schema v1のsearch/clip CLI。旧CLIは互換入口として存続。

## 7. 直近の修正と注意事項

### yt-dlp修正（2026-09-15）

- 仮想環境のyt-dlpを2026.6.9から2026.8.19へ更新。
- requirementsは`yt-dlp[default]>=2026.8.19`。不足していたyt-dlp-ejs 0.8.0等を導入。
- metadata取得/本DL双方へ`js_runtimes={"deno": {}, "node": {}}`を渡すよう修正。
- このPCでNode.js v24.18.0、EJS認識、pip check成功を確認。
- `F:\myapp\cut\update_ytdlp.bat`を追加。明示実行時のみ仮想環境のyt-dlpと依存を更新し、通常起動では自動更新しない。
- 更新後はCUTの再起動が必要。修正後の実YouTube URLでの取得成功は未確認です。
- Python 3.10の非推奨警告が出ますが、今回Python/venvの作り直しはしていません。無断で大型依存を総更新しないでください。

### 起動エラーの経緯

- 過去に起動時のエラーが報告されましたが、利用者のスクリーンショットは`brave.exe`のアプリケーションエラー（0x80000003）でした。
- 当時CUTのHTTP/JS/CSS応答はありました。既定ブラウザの自動起動時にBraveが落ちた可能性がありますが、原因は未確定です。
- Edge/Chromeで`http://127.0.0.1:7860/`を開く回避策を案内済み。利用者側で正常表示できたという確認は未取得です。
- HTTP 200だけで「アプリの全機能が正常起動した」と断定しないでください。現在サーバーが動いているかも再確認が必要です。

## 8. 検証と安全ルール

- 直近のDL修正後、全445テストを実行して失敗なし、任意性能計測1件skip。対象8テスト、pip check、py_compile、git diff --checkも成功。
- これは前回の検証結果であり、引き継ぎ時に再実行済みという意味ではありません。
- 制限環境の初回実行ではPlaywrightの子プロセス起動がWinError 5になり、承認された権限で再実行して成功。権限由来の失敗をアプリの不具合やテスト合格と混同しない。
- 実行例: `F:\myapp\cut\venv\Scripts\python.exe -m unittest discover -s tests`（作業ディレクトリは`F:\myapp\cut`）。
- 合成動画・隔離DBだけで検証。`data/`、`video/`、`clips/`、`exports/`、`.env`、認証情報、private transcriptを調査・アップロード・commitしない。
- ユーザーデータの削除、既存編集の破棄、認証変更、課金、commit/push、破壊的Git操作は明示承認なしに行わない。
- 公開・共有対象には動画名やASR本文等が入り得るため、共有操作は明示確認を維持する。
- モデル/エージェント運用は現在のAGENTS.mdとセッションルールに従う。Astraでは`sol-luna-router` Skillを適用しないよう変更済みです。これはAGENTS.md等による独立した委譲指示まで禁止するものではありません。

## 9. 引き継ぎ後の進め方

まず作業場所、branch/HEAD、git diff、仕様書と実装記録を確認してください。
その後、実装済み/未確認/仕様差を短く報告し、次の利用者依頼に沿って小さな変更単位で進めてください。
未確認事項を勝手に完了扱いにせず、依頼がない限りUI再設計・Python移行・データ移行・外部投稿機能等へ作業範囲を広げないでください。
報告は日本語で、変更点・検証結果・残る制限を簡潔に伝えてください。
