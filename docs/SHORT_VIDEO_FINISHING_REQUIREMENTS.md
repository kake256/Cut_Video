# CutVideo ショート動画仕上げ機能 要件定義

## 0. 文書の扱い

- 状態: **提案 Draft 1**
- 調査日: 2026-07-30
- 調査対象: [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) の公式README、主要schema・video/task service、MIT LICENSE、v1.2.7 release notes
- CutVideoの基準: [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) と [ARCHITECTURE_IMPLEMENTATION_PLAN.md](ARCHITECTURE_IMPLEMENTATION_PLAN.md)
- 目的: MoneyPrinterTurboを組み込むことではなく、CutVideoの「既存の長時間動画から発言を探し、確認して切り抜く」流れに適合する機能を抽出し、CutVideoの責務として再定義する

この文書は採用前の要件提案であり、現行の製品仕様を自動的に変更しない。採用判断後に、製品仕様の対象範囲、字幕仕様、保存仕様とarchitecture planを更新する。

MoneyPrinterTurboのコードは本調査で複製していない。同repositoryはMIT Licenseだが、将来コードを利用する場合は著作権表示と許諾表示を同梱し、依存package、font、楽曲、素材それぞれのlicenseを別に確認する。README自身も同梱楽曲に著作権上の注意を記載しているため、CutVideoへ楽曲を転載しない。

## 1. 結論

MoneyPrinterTurboから最も参考にすべきなのは、AI台本生成や素材収集ではなく、**出力条件を一つの型付き設定へまとめ、字幕・画面形式・音声・複数出力を同じ生成jobで扱う考え方**である。

CutVideoへ採用する優先順位は次のとおりとする。

1. **P0: 出力profileとpreview/saveの一貫性**
2. **P0: 字幕preset、safe area、font適合検査**
3. **P0: render jobの進捗・停止・構造化失敗・成果物検証**
4. **P1: 複数候補の安全なbatch出力と中間生成物のjob内再利用**
5. **P2: 音量正規化と、利用者所有のlocal BGMを使う任意機能**
6. **P2: 正方形1:1など追加canvasと、投稿用metadata sidecar**

台本生成、TTS、online素材検索、SNS自動投稿、汎用transition、素材差し替えは採用しない。これらはCutVideoを「既存発言の検索・根拠確認・切り抜き」から「生成型動画制作・投稿ツール」へ変え、privacy、認証、著作権、失敗回復、UIの責務を大きく増やすためである。

## 2. 調査した機能と採否

| MoneyPrinterTurboの機能・設計 | CutVideoの現状 | 適合度 | 判断 | CutVideoでの扱い |
|---|---|---:|---|---|
| 9:16、16:9、1:1のcanvas | 元比率、9:16、blur/cropを実装済み | 高 | 一部採用 | profileへ一般化。1:1はP2 |
| subtitleのfont、位置、色、size、outline、背景 | 自動分割・改行・固定style・手動本文/時刻編集あり | 高 | 制限付き採用 | 自由な装飾panelではなく3個程度のpresetと位置指定 |
| subtitle fontのglyph確認 | 未実装 | 高 | 採用 | 日本語を描画できないfontは保存前にfallbackまたは警告 |
| batchで複数動画を生成 | 見どころ全候補の通常保存あり | 高 | 採用 | candidate batchを共通ExportJobへ寄せる。複数styleの総当たりはしない |
| task progress、failed stage、error保持 | index jobはあるが出力側は統一されていない | 高 | 採用 | render stage、cancel、retry、partial batch summaryを定義 |
| 重複再encodeの削減 | multi-range結合後にpostprocessする経路あり | 高 | 採用 | 同一job内で結合済み中間videoをprofile間で再利用 |
| BGM指定とvolume | 未実装 | 中 | P2実験 | local fileのみ、既定OFF、source音声を主役にする |
| 動画ごとの複数案生成 | LLM見どころ候補あり | 中 | 一部採用 | 候補選択とbatch保存へ限定。ランダム素材差し替えはしない |
| clip speed変更 | 未実装 | 低 | 保留 | 発話の自然さとASR時刻を壊すため、初期対象外 |
| fade/slide/zoom transition | 製品仕様で対象外 | 低 | 非採用 | Result timelineと字幕同期が複雑化するため汎用化しない |
| AI台本生成・多LLM provider | local Ollama要約は実験機能 | 低 | 非採用 | LLMは候補説明等のderived dataに限定 |
| TTS・voice preview | 元動画音声を使う | 低 | 非採用 | 発言切り抜きという目的と不一致 |
| Pexels/Pixabay/Coverr/local素材の組合せ | 単一source中心 | 低 | 非採用 | B-roll、素材置換、multi-track化は行わない |
| SNSへのone-click公開 | local保存のみ | 低 | 非採用 | 外部送信・認証・誤公開リスクが大きい |
| WebUI/API/CLIの複数入口 | WebUI/CLI共通化を進行中 | 中 | 原則だけ採用 | 同じapplication use caseを使う。HTTP serverは追加しない |

### 2.1 統合方式の判断

MoneyPrinterTurboをpackageまたはsubmoduleとしてCutVideoのruntimeへ組み込む方式は採用しない。

- MoneyPrinterTurboはStreamlit WebUI、MoviePy中心のvideo処理、多数のLLM/TTS/素材providerを一つの生成pipelineとして持つ。一方、CutVideoはGradio adapter、ffmpeg中心の保存、既存ASR時刻とEdit planを正とする。
- 直接統合すると、ASR、subtitle、download、job、WebUIの責務が重複し、CutVideoで解決済みのWhisper process分離、TimelineMap、atomic artifact publishを迂回する危険がある。
- 採用するのはparameter設計、task state、font validation、batch生成、重複encode削減という設計上の知見であり、実装はCutVideoのdomain/application/infrastructure境界へ合わせて行う。
- 後から限定的にコードを参照・移植する場合も、機能単位でprovenanceを記録し、MIT表示と依存licenseを確認する。repository全体をコピーしない。

### 2.2 実現性と優先度

| 採用候補 | 利用価値 | 実現性 | 主な既存資産 | 主な難所 | 優先度 |
|---|---:|---:|---|---|---:|
| OutputProfile | 高 | 高 | `ShortVideoOptions`、save postprocessor、manifest | 旧保存との互換adapter | P0 |
| 制約付き字幕preset | 高 | 高 | libass、canvas幅改行、session字幕編集 | preview/save一致、safe area | P0 |
| font glyph validation | 高 | 中 | ASS生成とWindows font | font探索、fallbackの再現性 | P0 |
| ExportJobの進捗・停止 | 高 | 中 | index job、ArtifactTransaction、claim | ffmpeg process停止と逆順完了 | P0 |
| candidate batch共通化 | 高 | 中 | 見どころ全候補保存、atomic publish | partial failureとretry | P1 |
| job内の中間video再利用 | 中〜高 | 中 | multi-range結合後postprocess | lifecycle、容量、cancel | P1 |
| 音量正規化 | 中 | 中 | ffmpeg audio map | 聴感評価、音声なし素材 | P2 |
| local BGM | 中 | 中 | ffmpeg再encode経路 | 著作権、mix失敗、音声明瞭度 | P2実験 |
| 1:1 canvas | 低〜中 | 高 | 9:16 scale/layout | UI選択肢増加の妥当性 | P2 |
| 投稿用metadata sidecar | 中 | 高 | 見どころtitle/summary/tag | LLM値の誤りとprivacy表示 | P2 |

## 3. 対象利用フロー

主利用フローは現在のCutVideoから変えない。

```text
動画を選ぶ
  → 文字一致・意味検索または見どころ候補を開く
  → 全体範囲と途中除外を編集する
  → ③ 出力・字幕で仕上げprofileを選ぶ
  → 短いpreviewで見た目と字幕を確認する
  → 保存jobを開始する
  → 検証済み成果物とmanifestを受け取る
```

LLM要約・見どころ画面は候補選択に集中させる。個別候補の字幕調整、縦型化、crop確認は「検索・編集・切り抜き」の第3段階で行う。「表示中の全候補を字幕なしで通常保存」は簡易batchとして残す。

## 4. 機能要件

### 4.1 出力profile

#### `FR-PROFILE-01` 型付きprofile

必須。画面形式、解像度、字幕、音声仕上げ、encode条件をUI固有の値ではなく、一つの型付き`OutputProfile`としてapplication層へ渡す。

最低限、次の情報を持つ。

```text
OutputProfile
  profile_version
  profile_id
  canvas_mode: source | portrait_blur | portrait_crop | square_fit（P2）
  width / height: source canvasの場合はnull
  caption_profile
  audio_profile
  video_codec / pixel_format / quality_preset
```

- `profile_id`は表示名ではなく安定した内部IDとする。
- profileはEdit planに含めない。境界編集のUndo/Redoと出力styleの変更を混ぜない。
- 保存開始時にprofile、Edit plan、source generation、Transcript revisionをimmutable snapshotとして固定する。
- manifestにはprofile ID、version、実効値を保存する。private transcript本文やlocal BGMの絶対pathは保存しない。

#### `FR-PROFILE-02` 初期profile

P0で次を提供する。

- 元動画の縦横比・字幕なし
- 元動画の縦横比・字幕あり
- 縦型9:16 1080x1920・blur背景
- 縦型9:16 1080x1920・中央crop
- 縦型9:16 720x1280・blur背景
- 縦型9:16 720x1280・中央crop

UIでは画面形式、配置、解像度、字幕ON/OFFを個別に変更できるが、applicationへは解決済みprofileを一つだけ渡す。

#### `FR-PROFILE-03` preview/save parity

必須。previewと最終保存は同じprofile resolver、字幕分割、字幕style、canvas layoutを使う。previewだけ低解像度・短時間にしてよいが、crop位置、safe area、改行、字幕位置を変えてはならない。

profileまたは字幕を変更した後、古いpreviewを新設定の結果として表示しない。UIは「未反映」「生成中」「最新」の状態を明示する。

### 4.2 字幕仕上げ

#### `FR-CAPTION-01` 制約付き字幕preset

P0。自由なfont/color/outline編集を最初から全面公開せず、次のpresetを提供する。

- `standard`: 白太字、黒outline、下部
- `large`: standardより大きい文字、1行の安全幅を狭くする
- `boxed`: 高contrast背景付き、下部

位置は「上・中央・下」から選べる。ただし縦型の既定は下とし、platform UIと重なりやすいcanvas端を避けるsafe marginを常に適用する。

presetの実効値はcanvas比率から解決し、固定pixelだけに依存しない。詳細な色picker、文字ごとの装飾、karaoke強調、animationは対象外とする。

#### `FR-CAPTION-02` 読みやすい分割と改行

必須。現行のASR cue分割・canvas幅による明示改行を維持し、次を満たす。

- 実在するASR時刻または利用者がセッション内で編集した時刻だけを使う。
- 長いcueは句読点を優先して連続blockへ分割する。
- blockは原cueの範囲を越えず、重複せず、時刻順を保つ。
- 既定の目安は10文字、最低表示時間500msとするが、`CaptionProfile`のversion付き実効値として扱う。
- 全角・半角の表示幅、canvas横幅、font size、左右marginから1行の上限を計算する。
- 改行後も1行がsafe widthを越える場合は保存前validation errorにする。

#### `FR-CAPTION-03` font適合性

P0。保存開始前に、選択fontが字幕に含まれる代表的な文字を描画できるか確認する。

- 適合する場合はそのfontを使う。
- 不適合で既定fontが適合する場合は既定fontへfallbackし、previewと保存logへ本文を含まないwarningを表示する。
- 適合fontがない場合は保存を開始せず、font設定の修正方法を表示する。
- font pathは許可されたfont directoryまたはOS font解決結果に限定し、任意pathをfilter文字列へ直接埋め込まない。

#### `FR-CAPTION-04` 編集状態の所有

現行仕様を維持する。手動字幕は現在のUI sessionだけに保持し、元ASR、DB、LLM候補へ書き戻さない。動画、Transcript revisionまたはEdit plan semantic signatureが変わった場合は自動字幕へ戻し、古い手動字幕を暗黙に再利用しない。

### 4.3 画面構成

#### `FR-CANVAS-01` 9:16変換

現行のblur背景と中央cropを維持する。

- 既定はblur背景とし、元映像全体を中央に残す。
- cropは利用者が明示した場合だけ使う。
- 初期実装で顔追従、被写体追従、shot単位のcrop移動は行わない。
- source audioの長さ、字幕時刻、Result timelineをcanvas変換で変更しない。

#### `FR-CANVAS-02` 追加canvas

P2。profile構造と検証が安定した後に1:1 1080x1080を追加可能にする。1:1をP0へ含めない理由は、現在の主要利用が横長保存と縦型shortであり、選択肢増加に対する利用価値が未計測だからである。

### 4.4 出力jobとbatch

#### `FR-JOB-01` 段階的な進捗

P0。保存を次のstageとして報告する。

```text
queued → validating → joining → rendering → probing → publishing → completed
                                                ↘ failed / cancelled
```

- failureは`failed_stage`、機械可読error code、利用者向けmessageを持つ。
- messageやlogへ動画内容、字幕本文、private path、URL credentialを出さない。
- 既知の失敗例はsource変更、出力衝突、font不適合、ffmpeg失敗、duration不一致、停止とする。

#### `FR-JOB-02` 停止・再試行

P0。停止は実行中ffmpeg processへ伝播し、staging、claim、未公開成果物を除去する。再試行は保存開始時のimmutable snapshotを使うか、設定変更後に新しいsnapshotとして開始するかを明示する。失敗したjobを成功扱いにせず、既存成果物を上書きしない。

#### `FR-BATCH-01` 候補batch

P1。「選択候補のみ」または「表示中の全候補」を一つのbatch jobとして扱う。

- 各候補は独立したatomic artifact transactionにする。
- 一件の失敗で、既に検証・公開済みの別成果物を削除しない。
- batch summaryに成功、失敗、停止、skip、出力先を一覧化する。
- 同名時は既存規則どおり連番を使い、上書きしない。
- 初期UIでは一batchにつき一つのOutputProfileだけを選ぶ。候補数×複数profileの総当たり生成はP2まで行わない。
- 保存開始前に成果物件数と再encodeが必要かを表示する。

#### `FR-RENDER-01` 中間生成物の再利用

P1。同一snapshotから複数成果物を作る場合、multi-rangeの結合済みResult timeline videoを同じjob内で一度だけ生成し、profile別postprocessへ渡す。

- 再利用keyは少なくともsource generation、Edit plan semantic signature、Effective Export Plan、precise modeを含む。
- P1ではjob終了後に中間videoを削除し、永続cacheにしない。
- source変更、停止、失敗では再利用しない。
- instrumentation testで同一snapshotのjoin処理回数が一回であることを検証する。

### 4.5 音声仕上げ

#### `FR-AUDIO-01` source音量正規化

P2。batchで候補ごとの聴感音量が大きく変わらないよう、任意の音量正規化を設ける。既定はOFFとし、採用前に実動画を外部送信しないlocal聴取評価を行う。

- source音声を置換しない。
- 音声streamがない動画ではwarningとしてskipする。
- 実効parameterと適用有無をmanifestへ保存する。
- clippingを検出し、出力音声が存在することをprobeする。

#### `FR-BGM-01` local BGM

P2実験。利用者が明示的に選んだlocal音声fileだけをBGMとしてmixできる。

- 既定OFF。CutVideoへ楽曲を同梱しない。
- 元発言を主音声とし、BGM音量、fade-in、fade-outを設定できる。
- 初期実験では自動選曲、online取得、生成BGM、multi-track timeline、区間ごとの音量keyframeを含めない。
- BGM fileの絶対pathは共有index、公開ID、外部送信、logへ含めない。local manifestへ記録する場合もbasenameとcontent fingerprintに限定する。
- BGM decode/mixに失敗した場合、無断で「BGMなし成功」へ降格しない。利用者がBGMなしで再試行するか選べる失敗とする。

### 4.6 投稿補助metadata

#### `FR-META-01` local sidecar

P2。既存の見どころtitle、summary、tagから、成果物と同名の投稿補助JSONまたはTXTを任意保存できる。これはSNSへ送信せず、title・説明・tagの候補だけを保持する。

- LLM生成値であることを明示し、保存前に編集可能とする。
- 元文字起こし全文、private path、credentialを含めない。
- SNS自動投稿、account連携、公開範囲の操作は対象外とする。

## 5. UI要件

### 5.1 第3段階「出力・字幕」

現行の編集画面にある第3段階を拡張し、別の大規模editorを追加しない。

```text
┌ preview ───────────────────┬ 仕上げ設定 ──────────────┐
│ 元/編集結果、現在時刻       │ 画面: 元比率 / 縦型       │
│ 最新profileの短いpreview    │ 配置: blur / crop          │
│                             │ 字幕: OFF / standard/...   │
│                             │ 位置: 上 / 中央 / 下        │
├────────────────────────────┴─────────────────────────┤
│ 字幕一覧と、選択行の本文・開始・終了編集              │
├──────────────────────────────────────────────────────┤
│ 出力名 / 保存先 / profile要約 / preview状態 / 保存     │
└──────────────────────────────────────────────────────┘
```

- 基本設定には画面形式、字幕ON/OFF、字幕presetだけを置く。
- 解像度、位置、音声仕上げは「詳細設定」へ置く。ただし現在選択値の要約は常に見えるようにする。
- subtitleのfont/color/outlineを多数並べず、presetを主操作にする。
- 設定変更のたびに全clipを自動renderしない。変更をdirty表示し、「preview更新」で現在位置付近の短いproxyを生成する。
- preview生成中に設定を変更した場合は古いjobを停止または破棄し、遅れて完了した結果で新しい状態を上書きしない。
- 最終保存buttonの直前に、画面サイズ、字幕状態、再encodeの有無、出力予定数を一行で表示する。

### 5.2 見どころ画面

- 現行どおり、candidateのRadio、title、時間、理由を同じ行で表示する。
- 個別の字幕・crop調整は「編集画面で開く」へ遷移させる。
- 「候補を字幕なしでまとめて保存」は残す。
- P1でbatch profileを追加する場合も、字幕を一件ずつ修正するUIを見どころ画面へ複製しない。

### 5.3 エラーと回復

- UIは「生成失敗」だけでなく、どのstageで失敗したかを表示する。
- 保存済み成果物があるpartial batchでは、成功分のfile locationを開ける。
- retryは「失敗分だけ再試行」と「全件を新しい名前で再実行」を区別する。
- stop後にbuttonやprofile入力が永久にdisabledにならない。

## 6. 非機能要件

### 6.1 Privacyと安全性

- 仕上げ処理はlocalだけで完結し、動画frame、音声、字幕、要約を外部serviceへ送らない。
- online素材取得、cloud TTS、cloud LLM、SNS投稿を仕上げjobから呼ばない。
- source、BGM、font、出力先は正規化済みpathとして検証し、ffmpeg commandへshell文字列連結しない。
- `data/`、`video/`、`clips/`、`exports/`、字幕中間fileをgit追跡しない。
- ASS、proxy、中間結合videoはjob固有stagingに置き、完了・失敗・停止でcleanupする。

### 6.2 正確性

- 動画、焼き込み字幕、SRT、manifestは同じEffective Export PlanとTimelineMapを使う。
- canvas変換、BGM、subtitle styleは成果物durationを変えない。
- probeでwidth、height、duration、video/audio streamを確認してからpublishする。
- precise保存のduration差はframe/timebaseから算出した許容差内とする。
- profile変更はEdit planの境界、除外、Undo/Redoを変更しない。

### 6.3 性能

- 単一profileの保存で、profile抽象化だけを理由に追加のsource全体decodeを行わない。
- 同一batch内では同じ編集結果のjoinを重複実行しない。
- previewは全clipの本番品質renderを前提にせず、短時間・低解像度proxyを使える。
- HDDでも停止操作と進捗更新が応答し、UI threadでffmpeg完了をblocking waitしない。

### 6.4 互換性

- Windowsを主対象とし、`ffmpeg`/`ffprobe`と既存venvを使う。
- WhisperModelをGradio workerへloadしない既存制約を崩さない。
- 元比率・字幕OFFの従来高速/精密保存を維持する。
- 現行の`ShortVideoOptions`はadapterで`OutputProfile`へ移行し、保存use caseの外にportrait固有分岐を増やさない。

## 7. Domain/API案

実装時の責務境界は次を推奨する。名称は要件を表す仮称であり、現時点でpublic APIとして固定しない。

```text
EditPlan + EffectiveExportPlan + TranscriptSnapshot
                         │
                         v
                  ExportRequest
                  ├─ OutputProfile
                  ├─ CaptionTrackSnapshot
                  └─ ArtifactNaming
                         │
                         v
                    ExportJob
       validate → join → render → probe → publish
                         │
                         v
             Video + SRT? + metadata? + manifest
```

- `OutputProfile`: canvas、caption、audio、encodeの値object
- `CaptionTrackSnapshot`: active ASRまたはsession編集字幕。DBへ永続化しない
- `ExportRequest`: source generationとEdit plan signatureを含むimmutable request
- `ExportJob`: progress、cancel、retry、各artifactの結果を管理
- `RenderBackend`: ffmpeg commandを構築・実行するinfrastructure interface
- `ArtifactPublisher`: 現行のclaim、staging、probe、atomic publishを再利用

UI、CLI、LLM見どころ画面はffmpeg commandを直接構築せず、同じExportRequestを作る。

## 8. 受け入れ条件

### `AC-FINISH-PROFILE`

- 元比率字幕OFF、元比率字幕ON、9:16 blur/cropの各profileが同じfixtureで生成できる。
- previewと保存のcanvas mode、字幕preset、改行位置が一致する。
- manifestのprofile実効値から同じ設定を説明できる。
- profile変更でEdit plan/historyが変化しない。

### `AC-FINISH-CAPTION`

- 日本語、英数字、全角半角混在、長文、改行、ASS予約文字を含むsynthetic cueがsafe area内に収まる。
- multi-range除外後も字幕がResult timelineへ正しく詰め直される。
- 未対応fontはfallback warningまたは保存前errorになり、豆腐文字の成果物を成功扱いにしない。
- session字幕編集が元ASR/DBを変更しない。

### `AC-FINISH-JOB`

- stop、timeout、ffmpeg失敗、source変更、出力衝突、probe失敗ごとに構造化errorを返す。
- 失敗・停止後にstagingとclaimが残らず、再試行できる。
- 完了前のfileを保存済み一覧へ表示しない。
- 動画内容・字幕本文・private pathがlogとtest fixtureへ漏れない。

### `AC-FINISH-BATCH`

- 複数候補の一部失敗でも、成功済み成果物は検証済みのまま残る。
- summaryの成功・失敗・停止件数と実fileが一致する。
- 同一snapshotを複数profileへ出す将来testでjoin処理が一回だけになる。
- 同名・同時保存で既存fileを上書きしない。

### `AC-FINISH-AUDIO`（P2）

- 音声あり、音声なし、短い音声、BGMが元動画より短い/長い場合を検証する。
- BGM OFFの既定出力は現在の音声と変わらない。
- mix後もdurationがEffective Export Planと一致し、audio streamがprobeできる。
- local BGMの絶対pathをshare、公開ID、logへ出さない。

## 9. 実装フェーズ案

### Phase F0: 現行機能のprofile化

- `ShortVideoOptions`と元比率字幕経路を`OutputProfile`の下へ統合
- preview/save共通resolver
- manifestへprofile実効値追加
- current behaviorのcharacterization test
- `AC-FINISH-PROFILE`の元比率/9:16部分

このphaseではUI選択肢を増やさない。

### Phase F1: 字幕presetとfont validation

- 3 preset、上/中央/下、safe margin
- font discoveryとglyph validation
- proxy previewへの同一style反映
- `AC-FINISH-CAPTION`

### Phase F2: ExportJob統一

- progress stage、cancel、structured error
- current ArtifactTransactionとの統合
- browser E2Eで保存、停止、再試行
- `AC-FINISH-JOB`

### Phase F3: candidate batchとjob内再利用

- batch summaryと失敗分retry
- join済み中間videoのjob内fan-out
- 見どころの字幕なし一括保存を同じjobへ移行
- `AC-FINISH-BATCH`

### Phase F4: 音声仕上げ実験

- source loudness normalization spike
- local BGM、volume、fade
- local聴取評価後に採否決定
- `AC-FINISH-AUDIO`

### Phase F5: 追加canvasとmetadata

- 利用要求が確認できた場合だけ1:1 profile
- title/summary/tag sidecar
- SNS送信は含めない

## 10. 明示的に実装しない事項

次は本要件の完了条件に含めない。

- AI台本生成、脚本からの検索語生成
- TTS、voice clone、voice preview
- online stock footageの検索・download・自動挿入
- source映像を別素材へ置換する編集
- 汎用transition、filter、color、animation editor
- 再生速度変更
- 顔追従・人物追従crop
- 翻訳、karaoke字幕、文字単位animation
- cloud LLM/TTS/music providerの追加
- TikTok、Instagram、YouTubeへの投稿と認証情報管理
- HTTP API server、multi-user、cloud job queue

## 11. 採用時に必要な既存文書の整合

本提案を採用する場合、実装前に次を更新する。

1. `PRODUCT_SPECIFICATION.md` 2.2へ、制約付き出力profile、字幕preset、batch exportを追加する。
2. 同2.3の「詳細な字幕装飾」は対象外のままとし、presetと自由装飾の境界を明記する。
3. 同10.4を`OutputProfile`とfont validationに合わせて更新する。
4. `ARCHITECTURE_IMPLEMENTATION_PLAN.md` 9.4、14節の「字幕焼き込み・style UI・batchを保留」という古い記述を、既に実装済みの現状と本提案のphaseへ整合させる。
5. 既存のLLM見どころ機能は引き続き任意のderived-data機能とし、仕上げ機能をLLM成功へ依存させない。

## 12. 参照した一次資料

- [MoneyPrinterTurbo README-en](https://github.com/harry0703/MoneyPrinterTurbo/blob/main/README-en.md)
- [MoneyPrinterTurbo VideoParams / VideoAspect](https://github.com/harry0703/MoneyPrinterTurbo/blob/main/app/models/schema.py)
- [MoneyPrinterTurbo video service](https://github.com/harry0703/MoneyPrinterTurbo/blob/main/app/services/video.py)
- [MoneyPrinterTurbo task service](https://github.com/harry0703/MoneyPrinterTurbo/blob/main/app/services/task.py)
- [MoneyPrinterTurbo v1.2.7 release notes](https://github.com/harry0703/MoneyPrinterTurbo/releases/tag/v1.2.7)
- [MoneyPrinterTurbo MIT License](https://github.com/harry0703/MoneyPrinterTurbo/blob/main/LICENSE)
