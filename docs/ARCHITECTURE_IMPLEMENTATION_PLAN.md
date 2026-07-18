# 動画シーン検索・切り抜きツール アーキテクチャと実施計画

## 0. 文書の位置づけ

この文書は [PRODUCT_SPECIFICATION.md](PRODUCT_SPECIFICATION.md) を実現するための責務境界、移行方法、フェーズ順を定義する。

- 状態: Revision 2 / 再監査版
- 現行基点: `agent/multi-clip-checkpoint` / `d911ad1`
- 現行Gradio版は、新しい編集画面が代表シナリオで明確に上回るまで動作基準として残す。
- これは一括リライト計画ではない。各段階で動くGradio版を維持する。

## 1. 再監査の結論

前版の「純粋domain」「Whisper別プロセス」「Tauri保留」は維持する。一方、次の判断を変更する。

1. **字幕より検索正確性を先に直す。** 文字一致がFAISSに依存して消える現状は製品の中核に反する。
2. **SRTと字幕焼き込みを分離する。** SRT sidecarは本線、焼き込みは需要確認後の技術spikeとする。
3. **JSON契約を早期に固定しない。** まずPython内部の型付きuse caseを安定させ、その後にversion付き表現を固定する。
4. **新UIより先に単一writerを作る。** Gradioと新Web UIが別々にSQLite/FAISSを書き換える構成は禁止する。
5. **製品仕様と実装設計を分離する。** 仕様上の意図的変更を、現行挙動の単なる抽出と混ぜない。
6. **LLM実験を本線から外す。** 検索→保存のボトルネックが計測で別にあると判明するまで優先しない。

## 2. 現行の重要所見

### 2.1 即時修正候補

| 優先 | 現状 | 影響 | 方針 |
|---|---|---|---|
| High | 検索結果から直感編集を開く際、viewportでbaseline作成後にoverallだけ上書きする | 次の非編集commandだけでdirtyになり得る | 初期plan確定後にbaselineを同じplanへ設定する |
| High | 文字一致IDもFAISSから復元して意味score順にする | FAISS欠損・モデル失敗で文字一致まで消える | 文字一致経路をSQLite/ASRだけで成立させる |
| High | DB chunkとFAISSを世代なしで別々に更新する | crash時にorphanまたは欠損vectorが残る | immutable search generationを公開する |
| High | 共有manifestに元動画の絶対pathを含める | 共有zipから個人pathが漏れる | v2 manifestでpathを廃止し再関連付けにする |
| High | `video_id`が絶対pathのhashとfile stemから作られる | 動画移動で別IDになり、IDや共有zip名にも元file名が残る | canonicalなopaque public ID、legacy alias、source locator、source generationを分離する |
| Medium | UI説明がUndo/Redo未実装のまま | 実装済み機能と表示が矛盾 | 現状説明を修正する |
| Medium | 文字一致にも意味scoreを表示する | 閾値との関係を誤認させる | 文字一致scoreは`—` |
| Medium | `requirements.txt`は`torch>=2.1`、setupは2.6 | 新規環境で要件が一致しない | `torch>=2.6`へ統一する |
| Medium | 実在由来に見える動画名がテストfixtureに残る | 個人情報保護方針と不整合 | 完全な合成名へ置換する |

### 2.2 実装と旧設計書の差

- 現在の時刻はfloatのままで、内部1ms正規化は未実装。
- 全体境界と既存除外境界は100ms最小幅だが、新規除外は約1msを許容する。
- command IDは最新ACKの識別に使うだけで、再送冪等性はない。
- FIFO経路が覆うのは主に編集commandであり、動画load、検索結果load、preview、source復帰、saveはreducer外。
- 旧編集と直感編集でKept rangeの実装が二重化し、外側範囲変更時の意味も異なる。
- browser E2Eはqueue復旧、keyboard、dirty guard、playheadに強いが、実際の除外dragから保存までの一周は未検証。

### 2.3 現在テストで守られている挙動

- nonce/revisionによる古いcommand拒否
- 失敗操作のdeep-copy非破壊性
- 除外clip、overlap/隣接統合、選択ID再対応、全範囲除外拒否
- planだけを対象にしたUndo/Redo、履歴上限50、semantic dirty
- 不完全なword timestampを発話区間へ丸ごとfallback
- viewport変更とEdit planの分離
- result preview時のSource/Result時刻混同防止
- preview cacheの衝突防止、失敗時cleanup、LRU
- multi-range保存の共有timeout、外側だけのpadding、一時連結publish

この既存保証をcharacterization testとして利用する。ただし新規除外100ms化など、製品仕様で明示した変更は差分としてテストする。

## 3. 採用するアーキテクチャ

### 3.1 Modular monolith

HTTPサービス群やmicroserviceへ分割しない。一つのPython backendの中を、純粋domain、application use case、infrastructure、adapterに分ける。

```text
Gradio / CLI / 将来Web
          |
          v
      adapters
          |
          v
  application use cases
       /          \
      v            v
editor_domain    ports/protocols
                   |
                   v
       SQLite / FAISS / ffmpeg / worker
```

依存規則:

- domainはGradio、SQLite、FAISS、ffmpeg、ファイルシステムをimportしない。
- applicationはUI型を返さず、domain objectまたは型付きDTOを返す。
- infrastructureは`app.py`をimportしない。
- adapterは例外を利用者向け表示へ変換する。
- `app.py`は最終的にもcomposition rootとして依存注入と起動だけを持ってよい。

### 3.2 プロセス構成

現行段階:

```text
Gradio backend (BGE検索、preview/save、job scheduler)
       |
       +-- index worker subprocess (Whisper、embedding計算、現行active DB/FAISSへの直接書込み)
```

将来Web UI段階:

```text
Gradio adapter ----+
CLI adapter -------+--> single local backend owner
Web adapter -------+          |
                              +-- index worker subprocess
                              +-- ffmpeg jobs
```

- Whisper/index workerの別プロセスは恒久制約とする。
- 複数adapterが同じDB/FAISSへ直接writeしない。
- CLIは当面backend moduleを同一processで呼んでよく、常駐daemonを先に作らない。ただしwrite系CLIはcross-process writer leaseを必須とし、Gradio owner稼働中はclientとして依頼するか安全に拒否する。
- HTTP/WebSocketは新Web UIの利用者が現れるまで追加しない。

## 4. 状態の所有権

### 4.1 Domain

```text
VideoRef
  public_video_id
  source_generation
  duration_ms

EditorDocument
  document_id
  video_ref
  edit_history
  revision

EditPlan
  overall: [start_ms, end_ms)
  exclusions: ordered non-overlapping intervals

TimelineMap
  kept_ranges
  source_to_result
  result_to_source

EditHistory
  current
  clean_reference
  undo
  redo
```

`EditPlan`はVideo identityを持たない。誤動画適用を防ぐidentityとdurationは`EditorDocument`が所有する。旧path由来IDは`LegacyVideoAlias`専用repository以外へ返さず、VideoRef、SearchHit、application DTOは`public_video_id`だけを使う。

### 4.2 Application session

applicationが所有するもの:

- document ID、VideoRef、EditHistory
- revision、処理済みcommand IDのbounded cache
- save sequenceと実行中save job
- source generationの検査結果
- source/result preview modeと、commandがどちらのtimeline時刻を持つか

adapterが所有するもの:

- active tool、selected boundary、pending exclusion start
- viewport、playhead、transcript focus、scroll
- accordionや詳細設定の開閉

二段クリックのpending状態をdomain commandにしない。domainへ渡す除外追加は`add_exclusion(start,end)`という原子的操作にする。移行中はselected boundaryとpending startを現行server sessionに残し、`nudge(boundary,delta)`と原子的除外commandへ置換できた後だけadapterへ移す。

### 4.3 Save完了とClean reference

各save開始時に実ファイルのstat/fingerprintを検査し、document ID、plan snapshot、source generation、document単位のsave sequenceを固定する。完了callbackは`CompleteSave(document_id, source_generation, sequence, snapshot, artifact_commit_id)`というapplication use caseを必ず通す。

Phase 2Bで最小のserver-side `DocumentSaveRegistry`を導入する。これはsession/document lease、source generation、次save sequence、最新成功sequence、clean-reference候補、job状態だけをlock下で所有し、Edit plan current/undo/redoはまだ現行adapterに残す。Gradio callbackはregistryを直接画面stateへpushできないため、pollまたは次command応答でcompletion eventを取得し、同じdocument/nonceの場合だけclean referenceを同期する。document切替は旧leaseをcloseし、browser切断はTTLで回収する。

完了処理:

1. `PrepareArtifacts`成功後、commit直前のsource再検査を通す。
2. `CommitArtifacts`成功のcommit IDを確認する。
3. registryで同じdocument ID/source generationを所有する有効leaseがあるか確認する。
4. 完了済みのより新しいsave sequenceがないか確認する。
5. 条件を満たせばjobのsnapshotをclean-reference候補としてcompletion eventへ設定する。
6. adapter同期時もcurrent、undo、redoは変更しない。
7. dirtyをadapterのcurrentと新clean referenceから再計算する。

閉じたdocumentのcallbackはartifact成功だけを記録し、active documentへ反映しない。commit後にprocessが落ちた場合もartifactは成功、clean referenceは未更新という安全側で復旧する。これにより、保存中の編集、document切替、保存完了順の逆転を処理できる。

## 5. 時刻モデル

### 5.1 正規形

- domainの正規形は整数ミリ秒。
- adapterだけが動画elementやGradioのfloat秒と変換する。
- 区間は半開区間`[start_ms,end_ms)`。
- 最小全体幅・最小除外幅は100ms。
- 1ms以下の隣接差は統合する。
- 正規化後に100ms未満のKept islandが残る場合は隣接除外へ吸収し、全体を消すなら操作を拒否する。

現行float挙動から一度に変更しない。移行は次の二段階とする。

1. 現行floatの境界表をcharacterizationする。
2. TimeMs adapterを入れ、通常ケースは1ms以内一致、1〜99ms除外だけ意図的変更として明示する。

### 5.2 TimelineMap

TimelineMapは正規化済みKept rangeから作り、次の唯一の写像実装になる。

- 完成予定時間
- result preview
- resultからsourceへの復帰
- SRT cue写像
- export plan
- CLI manifest

旧`_clip_plan_ranges`と直感`intuitive_state_to_clip_plan`が独自計算を続ける状態を終了させる。

通常編集はEditPlanのKept range、padding付き保存はEffective Export PlanのKept rangeから別instanceを作る。SRTと動画は必ず同じartifact用instanceを使い、leading padding分の字幕offsetを落とさない。

`make_effective_export_plan(plan, source_duration, pad_before, pad_after)`はoverall外端だけをsource端まで拡張し、既存exclusionをSource時刻のまま維持してKept rangeを再導出する。requested/effective paddingを別fieldで返す。例えば`[10,20),[30,40)`はpadding 2/3で`[8,20),[30,43)`となり、artifact mapではSource 10秒がResult 2秒、Source 30秒がResult 12秒になる。

## 6. 検索設計

### 6.1 SearchHit

```text
SearchHit
  hit_id
  video_ref
  transcript_revision
  publication_id (internal diagnostic)
  kind: text | semantic
  evidence_span
  suggested_span
  transcript_granularity
  snippet
  semantic_score: optional
  internal_match_tier: optional
  evidence_id / ASR unit IDs
  semantic chunk IDs: optional
```

文字一致は`semantic_score=None`とする。`hit_id`はpublic VideoRef、Transcript revision、kind、evidence identityから決定的に作る。evidenceとsuggested rangeを分け、検索語の場所と編集初期範囲を混同しない。

### 6.2 Search pipeline

```text
query
  +--> SearchSnapshotResolver
  |       publication ID / transcript revisions / reader lease
  |
  +--> TextMatcher (同snapshotのASR/SQLiteのみ)
  |       +--> evidence locator
  |       +--> occurrence dedup
  |
  +--> SemanticRetriever (同snapshotのBGE/FAISS generation)
          +--> threshold
          +--> deterministic NMS

       MergePolicy
          text wins same scene
          independent limits
          stable order
```

TextMatcherは意味indexなしでも動く。Search snapshotでpinされた`TEXT_READY`のactive Transcript revisionだけを読み、元segment/認識単位/時刻へのoffset projection付きで正規化する。同じsegment内の反復語はoccurrenceを保持するが、同じ単位列・spanへのprojectionは表示時にまとめる。隣接発話は`next.start - previous.end`が0〜2,000msだけsynthetic spaceで連結し、負のoverlap、長い無音、欠損、時刻逆転をhard boundaryにする。長時間分を一文字列へ載せず、normalized query長と最大segment長から上限を持つrolling bufferで探索する。全動画規模の計測で不足した場合だけ、再ASR不要のderived text-search tableを追加する。

重複除去は文字一致と意味検索で分ける。文字一致は同じrevision・認識単位列・evidence spanへ写るoccurrenceだけを統合する。意味検索は非正durationを拒否し、完全順位keyで同scoreを確定した後のNMSとし、半開区間で`overlap/min(duration) >= 0.30`を初期閾値とする。分類間は整数msで文字evidenceを完全包含するsemantic hitだけを抑制する。

### 6.3 検索結果の段階表示

文字一致を先に返し、意味検索を後から追加できるuse case形にする。

- Gradioはgeneratorで同じ結果領域を更新できる。
- CLIは最終統合結果を既定とし、`--mode text`ならBGEをloadしない。
- 新Web UIでは同じ二段階DTOを利用する。
- search request IDを持ち、古いqueryの遅延semantic結果を破棄する。
- semantic後着で選択hit、preview、viewport、Edit planを再初期化しない。
- request IDはUIのstale応答を捨てる識別子、publication IDはデータsnapshotの識別子であり、同じものとして扱わない。
- completion、query cancel、timeout、client切断、generator例外のすべてでSQLite read transactionとpublication/generation reader leaseを`finally`から解放する。

### 6.4 WebUI/CLI共通化

`search.py`を直接巨大化させず、connection、TextMatcher、SemanticRetrieverを注入できる`SearchService`を置く。appとCLIは同じrequest/responseを使い、表示変換だけadapterで行う。

## 7. Transcript設計

`chunker.py`が`asr.py`をimportするとfaster-whisperへ到達するため、軽量DTOを別moduleへ置く。

```text
transcript_types.py
  TranscriptRevisionRef
  TranscriptWord
  TranscriptSegment
  TimestampGranularity
  parse_words_json
```

- `asr.py`は生成側としてDTOを使う。
- editor、search、SRTはfaster-whisperをimportせずDTOを使う。
- `parse_words_json`はsegment自体がsource duration内の正区間であることを検査する。空本文recordを除外した後、全unitが整数msへ変換可能な有限値、segment内の`start < end`、start/end各列の単調非減少を満たす場合だけword modeを返す。一件でも違反する、`words_json`が空、または有効unitが0件ならsegment全体を発話区間fallbackへ落とし、clampや補間で修復しない。segment自体が不正なら検索・編集・SRT対象から隔離してdiagnosticへ出す。
- UI表示用HTMLとDTO解析を同じmoduleへ置かない。

TextNormalizerは認識単位の本文を単位ごとにNFKCし、出力した各normalized文字へその単位IDと実在time spanを付ける。発話区間fallbackでは区間全体を一単位として扱う。空白collapseは元単位集合のspanを保持し、kana変換はmappingを継承し、CJK接続文字間のspace削除はその文字だけを落とす。CJK接続文字には漢字・平仮名・片仮名・長音記号・踊り字を含めるが、記号自体は保持する。隣接segmentをまたぐsynthetic separatorはboundary markerを持つが時刻を持たない。match端は最初/最後の認識単位外端へ広げ、正規化後offsetを文字数比で時刻へ戻さない。

## 8. データとindex generation

### 8.1 Source of truthとderived data

概念上、次を分ける。

- Library store: public Video identity、legacy alias、source generation、Transcript revision、ASR segments、publication、job metadata
- Search generation: optional text-search derived data、semantic chunks、FAISS
- Preview/cache: 削除して再生成可能
- User artifacts: clip、SRT、共有zip

既存`index.db`からの移行は再ASRを要求しない。

新規Videoはopaque `public_video_id`を採番する。既存Videoにもmigrationでpublic IDを一度だけ採番し、path由来IDは`legacy_alias` tableへ移す。source locator更新とlegacy lookup以外でaliasを使わない。source generationはopaque IDとし、内部判定材料に`size + mtime_ns + sampled content fingerprint`を使う。full content hashは共有検証など必要な操作だけで計算する。

Phase 1Bのtransitional adapter導入後からPhase 2A.1移行完了までは、既存DBを`legacy generation`として扱う。元動画が存在すれば一時fingerprintを計算し、元動画不在の共有indexはgeneration=`unknown`とする。unknownでも検索はできるがpreview/saveは禁止し、migration後に永続generationへ置換する。public ID migration完了後はshare、API、artifact manifest、既定出力名で同じcanonical public IDを使い、その場限りのremapを作らない。

設定は`code_root`、`library_root`、`search_root`、`cache_root`、`source_roots`、`artifact_root`を分離する。DB/FAISS/cacheだけSSD、元動画はHDDという構成を第一級に扱う。設定値以外へdrive letterを埋め込まず、artifact stagingだけは最終出力と同じfilesystemに作る。

### 8.2 Generation publish protocol

検索generationはgeneration固有のimmutable fileとして作る。

```text
data/search/generations/<generation>/chunks.sqlite
data/search/generations/<generation>/vectors.faiss
data/search/generations/<generation>/manifest.json

Library DB:
  library_state(current_publication_id)
  active_transcripts
  search_publications(publication_id, generation_id nullable, manifest_checksum nullable)
  search_publication_members(publication_id, public_video_id, source_generation,
                             transcript_revision, semantic_covered)
```

各generation manifestはschema version、embedding model/revision、pooling/正規化方式、chunk設定、対象`(public_video_id, source_generation, transcript_revision)`集合、chunk IDとvector rowの対応、件数、次元、checksumを持つ。

publish手順:

1. backend schedulerがcross-process単一writer leaseを取得する。leaseはowner PIDだけでなく起動token、取得時刻、heartbeat、timeoutを持ち、PID再利用だけで所有者判定しない。
2. workerは開始時にsource generationを固定し、ASRをrevision固有draftへ書く。resume前とTEXT_READY公開直前に実ファイルのstat/fingerprintを再検査し、不一致ならdraftを失敗扱いにして旧active revisionを維持する。完成検証後はcurrent publication IDへのCASを含むLibrary DB transactionで、active Transcript revisionとimmutableな新publication row/member集合を作る。意味generation未完成なら新memberをsemantic pendingとし、旧revisionのsemantic hitをcoveredにしない。
3. workerはexpected publication IDとmember集合を固定し、新generationを一時directoryへ作ってchunk件数、vector件数、dimension、checksumを検証する。
4. generation directoryをatomic renameする。
5. Library DB transactionでexpected current publication IDをCASし、同じmember集合、新generation ID、manifest checksum、semantic coverageを持つ別のimmutable publicationを追加して`library_state.current_publication_id`を切り替える。CAS失敗なら別Transcript revisionが先に公開されたためgenerationを公開しない。directoryは先に存在するので、commit前crash/CAS失敗はorphan cleanup、commit後は常に完全generationを参照する。同じpublication rowを後から更新しない。
6. request開始時にcurrent publication IDとそのimmutable membersを一つのSQLite read transactionで読み、`SearchSnapshot(publication_id, transcript_refs, generation_id?, covered_transcript_refs, reader_lease?)`を一度だけ解決する。TextMatcherはそのrevision集合、SemanticRetrieverは存在する場合だけ同じimmutable generationを使う。generation欠損・破損時もtext-only snapshotを返す。
7. readerは検索完了までpublicationとgenerationのleaseを保持する。旧Transcript revision/generationはleaseがなくgrace期間を過ぎた後だけ削除し、Windowsでopen中のfile削除を要求しない。
8. crash後はpublicationから参照されないstaging/prepared/orphan generationを掃除する。stale writer/reader leaseは起動token、heartbeat、timeoutで回収する。

このprotocolではTranscript revision公開と意味generation公開を別publicationとして分離できる。TextMatcherは新revisionをすぐ検索でき、SemanticRetrieverはsnapshot memberとmanifestのcovered tupleが一致する動画だけを返すため、旧文字起こしの意味候補を混ぜない。

初期実装は理解しやすいfull generation snapshotでよい。旧世代を含むpeak diskがactive世代の2.2倍を超える、またはpublish時間が計測済みbudgetを破る場合だけ、同じmanifest契約の内側でcontent-addressedなper-video shard共有を検討する。

### 8.3 SQLite

目標schemaの責務は次のように分ける。実table名はmigration実装時に固定する。

```text
videos(public_video_id, display_name, duration_ms, ...)
legacy_video_aliases(alias, public_video_id)
sources(source_generation, public_video_id, locator, private_fingerprint, status, ...)
transcript_revisions(transcript_revision, source_generation, status, asr_config, ...)
asr_segments(transcript_revision, segment_id, start_ms, end_ms, text, words_json, ...)
active_transcripts(public_video_id, source_generation, transcript_revision)
search_generations(generation_id, manifest_checksum, status, ...)
search_publications(publication_id, generation_id nullable, created_at, ...)
search_publication_members(publication_id, public_video_id, source_generation,
                           transcript_revision, semantic_covered)
job_records(job_id, kind, owner_token, state, heartbeat, ...)
```

ASR draftは開始時のsource generationを外部keyとして固定し、新しいTranscript revisionの行だけを更新してactive rowを途中変更しない。publication row/memberはinsert後immutableとし、current pointerだけをCASで切り替える。publication membersを読む一つのSQLite transactionがTextMatcherの対象集合を固定する。source locatorとprivate fingerprintはlocal-only列で、share serializerが参照できるDTOへ含めない。

- schema metadata tableとversion付きmigrationを導入する。
- connectionごとに`PRAGMA foreign_keys=ON`、適切なbusy timeoutを設定する。
- nullableでよいfieldと必須fieldをmigration時に明文化する。
- migration失敗は起動を止め、元DBを変更前backupから復元可能にする。
- `get_indexed_video_ids`は互換wrapperにし、target APIはpublication memberごとの`TEXT_READY / SEMANTIC_PENDING / SEMANTIC_READY / SOURCE_MISSING`を返す。

### 8.4 Share package v2

- manifest schema versionを持つ。
- 絶対pathを保存しない。
- path由来legacy aliasをpackage外へ出さず、canonical public IDだけを使う。同一ID・同一content digestの再importは冪等、同一ID・異contentはconflictにする。
- transcriptを含む機密artifactとして扱う。
- 既定で論理動画名を匿名化し、transcript、元名、content identifier、vectorsを確認画面に列挙する。local mtime、sample位置、sample fingerprintは含めない。
- embedding model/revision、pooling/正規化、chunk設定、package用source token、任意full-content digestを含め、zip entry、展開サイズ、vector shapeを検証する。非互換vectorは捨て、Transcript revisionから再埋め込みする。
- importはstaging後、Library storeとSearch generationを一つのpublish jobとして登録する。
- v1 importは互換readerで受け、pathを捨てる。v1 exportは停止する。

## 9. Export設計

### 9.1 責務を一つのgod serviceへ集めない

```text
ExportClip use case (application)
  |- TimelineMap / plan snapshot (domain)
  |- MediaCutter (ffmpeg infrastructure)
  |- SubtitleMapper (pure)
  |- ArtifactPublisher (filesystem infrastructure)
  `- CompleteSave / clean-reference completion
```

`cut_clip.py`は当面低水準ffmpeg runnerとして残す。applicationが動画、SRT、manifestのtransactionを統括する。

### 9.2 Artifact transaction

1. save開始時にsource generationと出力衝突を検査する。
2. 出力先と同じfilesystemにjob staging directoryを作る。
3. cancel tokenを受け取る`Popen`/process groupで動画をstagingへ生成する。
4. 必要なら実出力をffprobeする。
5. SRTとwarning manifestを生成し、全artifactを検証する。ここまでが`PrepareArtifacts`で、まだ公開しない。
6. commit直前に元ファイルのstat/fingerprintを再検査し、不一致ならstagingを破棄する。
7. 成果物directoryのatomic rename、またはcommit manifestの最後のatomic replaceで一組として`CommitArtifacts`し、commit IDを返す。
8. commit ID付きで`CompleteSave`を呼ぶ。crash時はpublish journalから完了またはrollbackを判定し、mp4だけを成功扱いしない。
9. prepare中の失敗、source変更、timeout、cancel時はprocess停止確認後にstagingを削除し、既存artifactを維持する。

単一`cut_clip()`も最終出力へ直接`-y`せず、このpublisher経由へ移行する。

外側paddingがある場合は、先にEffective Export Planを作り、そのKept rangeから動画、artifact用TimelineMap、SRT、完成時間を導出する。

### 9.3 SRT

- SRT sidecarを最初の字幕機能とする。
- 字幕付き保存はprecise pathを使う。
- cueはplanned mappingだけでなくprobed output durationと、probeしたframe duration/container timebaseから求めたtolerance内にあることを確認する。
- 最終cueが動画末尾に一致する必要はない。
- 認識単位内部をcutが横切る場合、その単位を推測分割せず省略+warningとする。
- 発話区間時刻しかないcueは区間全体が一つのEffective Export Plan rangeへ含まれる場合だけ採用し、一部切断または分断では省略+warningとする。paddingで新たに含まれた区間も同じ規則で採用する。
- 行幅、最大秒数などは実素材評価後に決める。

### 9.4 Burn-in

本線から外す。SRT利用実績があり、焼き込み需要が確認された場合だけ次をspikeする。

- libass/filter availability
- 日本語font discoveryまたは同梱
- Windows filter path escaping
- 複数range precise export後の追加再encode時間
- 配布環境差

## 10. Application commandと外部契約

### 10.1 Python内部を先に安定させる

P1〜P3ではdataclass/Protocolによる型付きcommandを使用し、JSON schemaを公開契約として固定しない。現行reducerと同じtraceを流すdifferential testで意味を確定する。

### 10.2 外部契約

CLIとWebの二つの利用者ができた時点でversion 1を固定する。

editor command envelopeに必要なもの:

- schema version
- session/document ID
- command ID
- expected revision
- typeとpayload

responseに必要なもの:

- applied command ID
- new revision
- current plan
- dirty、can undo、can redo
- 構造化error

履歴stack、clean reference内部、adapter view stateは返さない。command ID cacheはsessionごとのbounded LRU/TTLとし、無制限に保持しない。

`search/open/save/export/index`のI/O use caseと、pure editor commandを一つの巨大なcommand unionにしない。

## 11. Adapter設計

### 11.1 Gradio

- `gr.update`、HTML、JS bridge、Gradio event wiringだけをadapter責務に寄せる。
- reducerの一括移動はしない。plan、history、sessionの順に薄くする。
- validation errorはdomain/application codeから`gr.Error`へ最外周で変換する。
- 既存のcommand FIFO、revision ACK、read-only syncを移行中も維持する。

### 11.2 CLI

- `search_video.py`と`cut_clip.py`は互換wrapperとして残す。
- 共通application use caseへ順次差し替える。
- CLI専用のargument、exit code、stdout JSON、stderrテストを追加する。
- multi-rangeとSRTが安定するまでbatch clipを追加しない。

### 11.3 新Web UI

着手条件:

1. search、EditPlan、TimelineMap、save use caseがGradio非依存
2. single writer/generation publishが実装済み
3. 現行ループの時間・操作数baselineが記録済み
4. 代表シナリオをAPIだけで完走できる

新UIは直感編集画面だけを作り、動画追加・共有は当初Gradio adapterを利用してよい。ただし両者は同じbackend ownerを経由する。

frameworkは小さなspikeでSvelte/Reactを比較し、先に決めない。新UIが中央値で明確に勝たなければGradioを継続する。

## 12. テスト戦略

### 12.1 次に追加するcharacterization test

1. plan境界表: 0.0009/0.001/0.099/0.1秒、隣接、overlap、clip、merge ID
2. 代表command trace: word/timeline/current/direct→merge→shrink→undo/redo→preview/source
3. 検索結果load直後のcurrent==clean referenceと非編集commandのclean維持
4. DB exactかつFAISS ID欠損
5. exactが上限を埋めた場合のsemantic独立枠
6. overlap chunk cluster、同score安定順、半開範囲境界
7. WebUI/CLI search parity

### 12.2 Data/index test

- fresh schema、旧schema migration、再実行
- foreign keyと必須field
- ASR resumeと重複防止
- generation stagingの各失敗点
- Transcript revision/publication切替前後のcrash recovery
- 一つの検索中にpublicationが切り替わる競合とSearchSnapshotの世代非混在
- writer crash、stale heartbeat、PID再利用、reader lease中の旧generation維持
- force再作成失敗時の旧generation維持
- share v1→v2 import、path/legacy alias redaction、public ID conflict

### 12.3 Media/export test

- synthetic videoによるfast/precise実duration
- single/multi-range、外側padding、隣接range
- source change、timeout、cancel
- 既存出力保護とstaging cleanup
- multi-artifact publish各段階のcrash recoveryと部分公開拒否
- TimelineMap anchor、requested/effective padding、SRT cue誤差

### 12.4 Browser E2E

合成動画だけで次を一周する。browser runtimeがないことによるskipは合格扱いにせず、CIまたは専用ローカル環境の少なくとも一方で実行する。

```text
動画選択 → 検索 → 候補選択 → 全体変更
→ 重複除外2回 → merge → undo/redo
→ result preview → source復帰 → 保存
```

保存job完了まで待ち、artifact実在、current/clean reference、source復帰時刻、途中カット後のdurationをassertする。既存のFIFO、keyboard、IME、dirty confirmation、playhead試験も残す。

## 13. 改訂ロードマップ

### Phase 0: 仕様固定・現在の矛盾修正

目的: 新機能前に、現行の誤表示・既知state不整合・privacy差を取り除く。

実施内容、既知制限、再現可能な性能値は[Phase 0 実施記録](PHASE0_BASELINE.md)に残す。

- 製品仕様とarchitectureを確定
- 検索結果loadのbaseline修正と回帰test
- Undo/Redo説明修正
- 実在由来fixtureの合成名化
- torch要件の統一
- 現行v1 shareのraw path/path由来ID exportを停止し、暫定redacted exportまたはv2までの安全な無効化
- transcriptを含むshare確認
- `.gitignore`で`data/`、`video/`、`clips/`、`exports/`、DB、zip、動画を継続拒否する回帰test
- 禁止path・生成物・実在由来fixtureを検査するcommit前privacy script
- 条件を固定した性能harnessによるHDD/SSD baseline記録
- URL download、index停止/再開を現行characterizationへ追加

終了条件: full test、browser E2E、privacy diff検査が通り、既知仕様差が一覧化される。

### Phase 1A: Identityとschema foundation

- 全Videoへのcanonical opaque public ID採番
- path由来IDのlegacy alias table隔離
- schema version/migrationとbackup/rollback
- storage root設定の分離
- 既存ASR/chunk/FAISS keyはcompatibility repositoryでlegacy aliasからpublic IDへ写し、Phase 2A.1の新generation公開までは再ASR・再埋め込みを要求しない

終了gate: 再ASRなしmigration、旧ID lookup、public DTOのlegacy alias非露出、rollback試験が通る。検索DTOはこのpublic IDだけを使うため、ここを通すまで1Bへ進まない。

### Phase 1B: 検索正確性と共通SearchService

目的: 発言検索エンジンとしての正しさを先に確保する。

- Transcript DTO/parserとTextNormalizer
- FAISS非依存の文字一致
- CJK空白正規化とevidence locator
- text/semantic独立上限
- 文字occurrence dedup、semantic deterministic NMS、安定順序
- 文字一致score非表示
- WebUI/CLI parity
- Phase 2A.1までのtransitional adapterでは`LegacyTranscriptRef(public_video_id, temporary_source_token, legacy_revision_token, lock_epoch)`を使い、`asr_complete=1`だけを公開対象にする。元動画なしならsource tokenは`unknown`とする
- index更新と検索を暫定共有lockで直列化し、`lock_epoch`をdiagnostic publication IDとして返す。最終的なimmutable publication snapshotを実装済みと偽らない

終了gate: `AC-SEARCH-CORE`。スコープ外: immutable publication、新UI、映像検索、LLM。

### Phase 1C: EditPlanとTimelineMap kernel

Phase 1A/1Bと独立にPhase 0後から進められる。

- 現行plan normalizationのcharacterization
- typed EditPlan、Kept range、semantic signature
- TimeMs migrationと100ms統一
- bidirectional TimelineMap
- 旧/直感adapterから同じkernelを利用

終了gate: `AC-EDIT-KERNEL`。スコープ外: history/session全移動、result video JS接続、UI改変。

### Phase 2A.1: Source・Transcript・Search publication

- source generationとTranscript revision
- immutable publication row/member、active pointer CAS
- immutable search generationとSearchSnapshot
- cross-process single writer/reader lease
- orphan cleanupとcrash recovery

終了gate: `AC-INDEX-01`と`AC-SEARCH-SNAPSHOT`が通り、一request内で文字/意味世代が混ざらない。ここを通すまで2A.2へ進まない。

### Phase 2A.2: 共有privacyと再関連付け

- share package v2、匿名名、privacy-safe metadata
- v1互換importとv1 export停止
- public ID conflict/idempotent import
- 動画再関連付けuse case

終了gate: `AC-PRIVACY-01`、zip traversal/size制限、再関連付け不一致拒否が通る。

### Phase 2B: Save jobとartifact transaction

- `CompleteSave(document_id, source_generation, sequence, snapshot, artifact_commit_id)` seam
- 最小server-side `DocumentSaveRegistry`、document lease、save sequence、completion polling
- 現行Gradio state adapterへのclean reference同期とsource再検査
- Effective Export Planとpadding-aware artifact TimelineMap
- cancel可能なffmpeg job
- multi-artifact commit/rollback journal
- single/multi-rangeのatomic publish

Phase 2Bではcurrent/undo/redoの所有権を移さず、保存完了に必要な最小registryだけをapplicationへ置く。終了gateは`AC-SAVE-01`と、保存中編集・逆順完了・閉じたdocument・poll遅延の競合testとする。

### Phase 2C: SRT sidecar

- Phase 1BのTranscript DTO/parser再利用
- artifact TimelineMapによるcue写像
- precise clip + SRT staging/publish
- warning manifest
- synthetic ffmpeg integration testとtimebase許容差

字幕焼き込みは含めない。終了gateは`AC-SRT-01`とする。

### Phase 3: Domain historyとapplication session分離

- plan-only History
- boundary command
- `DocumentSaveRegistry`をEditorDocument/EditHistory repositoryへ拡張し、current/undo/redoも同じownerへ移す。`CompleteSave`の規則とevent契約は変更しない
- revision/idempotency cache
- result video currentTime→result_to_source→source preview seekのapplication/adapter接続
- Gradio adapter薄化
- 旧reducerとのdifferential trace

終了条件: `app.py`からplan正規化、history push/pop、dirty計算が消え、`AC-EDIT-ADAPTER`を含むGradio挙動が維持される。

### Phase 4: CLIとversion付き契約

- internal typed use caseの整理
- search/clip/SRT CLI統合
- JSON v1 schema
- structured error
- CLI専用test
- 必要性確認後にbatch clip

HTTP serverは含めない。

### Phase 5: 直感編集Web UI比較実験

- single backend adapter
- 常設検索、preview、transcript、二段timeline、固定save bar
- 文字一致、意味検索、検索なし手動の3シナリオをwarm/coldで事前定義し、各UI最低10回の中央値を比較
- 操作時間、操作数、エラー数を匿名local計測
- 主シナリオの中央値がGradio比85%以下、他シナリオが105%を超えず、受入エラー0を採用gateとする

勝たなければ置換しない。

### Phase 6: Desktop shell評価

Phase 5合格時だけ、pywebviewとTauriを比較する。sidecar、installer、GPU runtime同梱は配布要件が確定するまで行わない。

### 13.1 受け入れ条件の追跡

| 受け入れID | 最初に完了させるPhase | 必須test |
|---|---|---|
| `AC-SEARCH-CORE` | 1B | SearchService unit、正規化/evidence表、WebUI/CLI parity、semantic後着browser E2E |
| `AC-SEARCH-SNAPSHOT` | 2A.1 | text/semantic publication競合、cancel/timeout lease解放、revision非混在 |
| `AC-EDIT-KERNEL` | 1C | pure trace、旧/直感adapter differential、drag→merge→Undo/Redo |
| `AC-EDIT-ADAPTER` | 3 | result currentTime→Source復帰browser E2E |
| `AC-INDEX-01` | 2A.1 | draft resume/source変更、publish failure injection、lease/crash、search snapshot |
| `AC-SAVE-01` | 2B | synthetic ffmpeg、save競合、artifact crash recovery、browser保存完走 |
| `AC-SRT-01` | 2C | cue mapping、partial unit warning、padding付き動画/SRT integration |
| `AC-PRIVACY-01` | Phase 0で漏出停止、2A.2で完了 | tracked-file scan、share manifest検査、v1 import、public ID conflict |

## 14. 本線から外す事項

需要または計測根拠が出るまで保留する。

- 字幕焼き込み
- 字幕style UI
- batch clipの高度化
- LLM要約、章分け、自動候補
- 映像埋め込み検索
- WebSocket進捗
- Tauri/pywebview/installer
- 汎用タイムラインeditor

## 15. 各フェーズ共通の完了条件

- `data/`、`video/`、`clips/`、`exports/`、private transcriptを読まない・commitしない。
- 新規domainは型付き、pure、UI非依存。
- Whisper/native依存をUI processへimportしない。
- Pythonは`venv\Scripts\python.exe`を使う。
- `git diff --check`、py_compile、full pytest/unittestを通す。
- search/edit/saveの契約またはUI eventを変更した場合は代表browser E2Eを更新し、browser有効環境で実行する。skipだけで完了扱いにしない。
- ffmpeg変更時は実合成動画のintegration testを行う。
- migrationにはrollback/recovery試験を付ける。
- 変更した仕様、互換性、保留事項を文書化する。
- 一つの変更で機能追加、全面UI刷新、storage migration、配布方式変更を同時に行わない。
