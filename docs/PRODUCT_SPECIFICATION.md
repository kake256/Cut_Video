# 動画シーン検索・切り抜きツール 製品仕様

## 0. 文書の扱い

この文書は「利用者から見て何が正しい動作か」を定義する。実装方法と移行順序は [ARCHITECTURE_IMPLEMENTATION_PLAN.md](ARCHITECTURE_IMPLEMENTATION_PLAN.md) に分ける。

- 状態: Draft v2
- 基準実装: `agent/multi-clip-checkpoint` / `d911ad1`
- 対象: Windows上で一人の利用者がローカル実行する構成
- 時刻は特記がなければ元動画先頭からの絶対時刻

本文中の「必須」は受け入れ条件、「推奨」は初期実装で可能な限り満たす条件、「保留」は需要や計測結果が出るまで実装しない事項を表す。

## 1. プロダクトの目的

このツールは汎用動画編集ソフトではない。長時間動画の発言を検索し、該当箇所を確認し、前後の境界と不要区間を決め、クリップとして保存するためのローカルツールである。

最適化する利用フローは次の一周である。

```text
動画を選ぶ → 発言を検索 → 根拠を確認 → 境界を決める → 保存する
```

主指標は次のとおり。

1. 検索開始から最初の有効候補を再生できるまでの時間
2. 候補選択から保存開始までの時間
3. 境界決定に必要な操作回数
4. 誤操作、状態不整合、保存失敗の回数

## 2. 対象範囲

### 2.1 必須機能

- ローカル動画または利用者が明示したURLから動画を登録する
- ASR文字起こしと意味検索用インデックスを作る
- 正規化文字列一致と意味検索で発言を探す
- サムネイル、動画名、長さ、文字起こしから対象を識別する
- 全体開始・終了と複数の途中除外区間を編集する
- 元動画と編集結果をプレビューする
- 高速または精密モードで動画を保存する
- インデックスを明示操作で他PCへ共有する
- WebUIとCLIが同じ検索・保存規則を使う

### 2.2 追加価値として扱う機能

- クリップに対応するSRT sidecar
- 編集計画・時刻表の機械可読出力
- 新しいWebフロントエンド
- デスクトップシェル

### 2.3 対象外

- マルチトラック、トランジション、エフェクト、カラー調整
- 字幕本文の手動編集、翻訳、詳細な字幕装飾
- 映像内容だけを対象とした検索
- 複数利用者の共同編集、クラウド同期
- 自動要約や切り抜き提案を製品の中核にすること

## 3. 用語とデータモデル

| 用語 | 意味 |
|---|---|
| Video | 利用者が登録した論理上の動画 |
| Source generation | 同じVideo IDでもファイル内容が変わったことを識別する世代 |
| Transcript revision | 一つのSource generationに対して、ASR設定と完成した文字起こしを識別する改訂 |
| Index generation | Transcript revision集合とFAISSを整合したまま公開する検索snapshot |
| ASR発話区間 | Whisperが保存した開始・終了・本文を持つ区間 |
| ASR認識単位 | `words_json`に実在する開始・終了を持つ最小単位。言語学的な単語とは限らない |
| Search evidence | 検索語または意味類似の根拠になった元動画区間 |
| Suggested range | evidenceを会話の切れ目まで広げた編集初期範囲 |
| Edit document | Video、source generation、編集履歴を結び付けた一つの作業単位 |
| Edit plan | 全体範囲と除外区間だけからなる保存計画 |
| Kept range | 全体範囲から除外区間を引いた、実際に残す区間 |
| Clean reference | documentを開いた時点、または最後に保存成功したEdit plan。dirty判定の比較対象 |
| Effective Export Plan | 外側paddingを適用した、成果物生成専用のKept range集合 |
| Source timeline | 元動画の絶対時刻 |
| Result timeline | Kept rangeを連結した編集結果内の時刻 |
| Artifact timeline | Effective Export PlanのKept rangeを連結した、padding込み成果物内の時刻 |

Search evidenceとSuggested rangeを同じ値として扱わない。検索根拠の強調表示と、切り抜きの初期値は別の概念である。

## 4. 動画ライブラリ

### 4.1 動画の識別

- すべてのVideoは永続・一意なopaque `public_video_id`をcanonical IDとして持つ。新規登録ではUUID相当を採番し、ファイル名や絶対pathを含めない。
- migration時は既存Videoにも`public_video_id`を一度だけ採番する。従来のpath由来IDはrepository内部の`legacy_alias`に隔離し、lookup以外のapplication DTO、CLI、共有package、成果物名へ出さない。
- path移動だけでは`public_video_id`を変えない。
- UIは原則としてファイル名、サムネイル、長さ、登録状態を表示し、内部IDだけを動画名として表示しない。
- source locator（現在のローカルpath）と論理Video identityを分離する。
- 同じパスでもサイズ、更新時刻、sample fingerprintが変わった場合はsource generationを更新する。generation ID自体はopaqueにし、mtimeやfingerprintを文字列へ埋め込まない。暗号学的な同一性が必要な共有・検証ではfull hashを任意に計算できる形を残す。
- 開いているEdit documentとsource generationが一致しない場合、保存を開始せず再読み込みを求める。
- 元ファイルが見つからない共有インデックスは検索可能と表示してよいが、プレビュー・保存は「動画を再関連付け」するまで無効にする。
- 再関連付けは利用者が選んだ候補のdurationとfingerprintを検査する。一致すればsource locatorだけを更新し、Video IDを変えない。内容が異なる場合は再関連付けを拒否し、明示的な「元動画を置換」または「別動画として登録」を案内する。置換を選んだ場合は別Source generationとし、既存文字起こしを黙って流用しない。

### 4.2 選択の既定値

- 編集中の動画がある場合、検索対象の既定値はその動画とする。
- 編集中の動画がない場合だけ「すべての動画」を既定にできる。
- 利用者が明示的に「すべての動画」へ変更した選択は、一覧更新だけで勝手に個別動画へ戻さない。
- 未保存編集がある状態で動画または検索候補を切り替える場合、確認は一度だけ表示する。
- サムネイル選択で動画を即時読込みし、選択panelは自動で畳む。現在の動画名、サムネイル、長さと「選び直す」は編集画面に残す。

### 4.3 保存場所とドライブ移動

- code、Library store、search generation、preview/cache、元動画、利用者成果物を別のrootとして扱う。
- 既定値はアプリ配下の相対pathでよいが、設定には絶対pathを保存でき、source codeへ個人drive/pathを埋め込まない。
- DB、FAISS、model cache、preview一時領域をSSDへ置き、元動画だけをHDDへ残す構成を許容する。アプリ全体の移動を必須にしない。
- root変更後もopaque Video IDを維持し、再関連付けでsource locatorだけを更新できる。
- 外付けdriveが未接続の場合はsource missingとして扱い、自動削除・別動画への誤関連付けを行わない。
- artifactのstagingはatomic publishのため、最終出力先と同じfilesystemへ作る。

### 4.4 登録とdownload

- ローカル登録、URL download、ASR、埋め込み、公開を別job段階として表示する。
- downloadは一時拡張子へ保存し、成功検証後だけ元動画として登録する。中断・失敗した部分ファイルをライブラリへ見せない。
- URLは利用者が明示したものだけを取得し、重複名を暗黙上書きしない。
- download停止とindex停止は別操作とし、停止後にどこから再開できるかを表示する。

## 5. インデックス作成

### 5.1 プロセス境界

- Whisperとそのnative依存はWebUIプロセスへ読み込まない。
- インデックス作成は現在と同じく専用子プロセスで実行する。
- UIは開始、音声準備、ASR、埋め込み、公開の各段階と停止結果を表示する。
- 500ms以上応答がない処理ではspinnerまたはbusy表示を出し、1秒を超える処理では現在段階と次に起きることを詳しく表示する。

### 5.2 再開、公開状態、整合性

- ASRはdraftへ途中保存でき、埋め込み失敗時に再文字起こしを要求しない。
- 同じ動画への二重インデックス作成を防ぐ。
- ASR draftは開始時のSource generationへ固定し、resume前とTranscript revision公開直前に元ファイルを再検査する。不一致ならdraftを公開せず、直前のactive revisionを維持する。
- 新しいTranscript revisionは全ASR区間の検証後にだけ`TEXT_READY`としてatomicに公開する。再ASR失敗時は直前のactive revisionを維持する。
- SQLiteのチャンクとFAISSベクトルは同じindex generationとして公開する。
- 途中失敗したgenerationは検索対象にしない。
- 文字一致は`TEXT_READY`のactive Transcript revisionだけを対象にし、途中保存中のASRを検索結果へ混ぜない。FAISS公開を待たせない。
- 意味検索は同じTranscript revisionを含むactive Index generationだけを対象にする。文字一致だけ新revisionへ切り替わった場合は、意味検索を「準備中」とし旧revisionの意味候補を混ぜない。
- 埋め込み中にactive Transcript revisionが変わった場合、そのIndex generationをcurrentへ公開せず、新しいrevisionに対して再構築する。
- force再作成は旧generationを、新generation公開後にだけ削除可能とする。
- DB migrationには明示的なschema versionを持たせ、予期しない`OperationalError`を「適用済み」として握り潰さない。

### 5.3 ASR粒度

- 新規Transcript revisionはASRモデルとrevision、言語、decode/VADの検索結果へ影響する設定、word timestamp有効率を必須保存する。既存データのmodel/languageは`unknown`、有効率は`words_json`から再計算し、再ASRは要求しない。
- 発話区間自体が`0 <= segment.start < segment.end <= source_duration`を満たすことを前提とする。`words_json`が空、または空本文recordを除いた有効認識単位が0件なら発話区間fallbackとする。有効な全認識単位が整数msへ変換可能な有限値で、`segment.start <= word.start < word.end <= segment.end`を満たし、start列とend列がそれぞれ単調非減少である発話区間だけ、認識単位で境界指定できる。
- 一つでも欠損、非有限、逆転、segment外、時系列逆行がある発話区間は、区間全体を発話区間単位へフォールバックする。境界へclampして「修復」しない。
- 欠損した単語時刻を等分や補間で作らない。

## 6. 検索仕様

### 6.1 利用者へ見せる分類

検索結果の分類は次の二つだけとする。

1. 正規化文字列一致
2. 意味検索

かな・空白吸収などの一致方法は内部diagnosticとして保持してよいが、利用者へ別の検索種別として増やさない。

### 6.2 正規化文字列一致

文字一致はSQLite上の文字起こしだけで成立し、埋め込みモデル、FAISS、意味閾値に依存してはならない。正規化後に空となるqueryはvalidation errorとし、それ以外の部分一致には類似度閾値を設けない。

一致判定は次の累積tierを順番に使う。

1. `base`: Unicode NFKC、casefold、連続空白整理後の部分一致
2. `kana`: baseの片仮名を平仮名へ統一した部分一致
3. `cjk_compact`: kanaからCJK文字間の空白だけを除去した部分一致

各tierはクエリと検索対象の双方へ適用する。この規則により「ワンチャン」と「ワンちゃん」は文字一致になる。CJK間空白の接続判定は漢字、平仮名、片仮名に加え、長音記号と踊り字を含めるため「ケ ー キ」と「ケーキ」も一致できる。ただし長音記号・踊り字そのものは削除しない。英数字とCJKの境界空白は削除しない。結合文字はNFKCの結果をそのまま使う。英語の`a b`と`ab`を無条件に同じ文字列として扱わない。漢字の言い換え、同義語、誤認識の推測は文字一致に含めず、意味検索へ任せる。

正規化処理は各normalized文字から元のASR segment、認識単位index、実在時刻へ戻れるprojectionを生成する。NFKCや空白削除で文字数が変わっても、文字位置を割合で推測しない。

検索は一つのASR発話区間内に加え、時間順に隣接し`gap = next.start - previous.end`が0〜2,000msの発話区間列をまたげる。隣接区間は一つのsynthetic spaceで接続し、CJK接続文字間だけ`cjk_compact`で除去できる。このseparatorに架空の時刻を与えない。gapが負のoverlap、より長い無音、時刻逆転、欠損区間はhard boundaryとし、その境界を越えて部分一致させない。2,000msはversion付き内部設定とし、UIの検索optionにはしない。

文字一致の根拠時刻は検索用15秒チャンクの外枠ではなく、可能な限り次を使用する。

- 該当する認識単位を特定できる場合: 最初に一致した単位の開始から最後に一致した単位の終了まで。match端が単位内部でも単位境界へ外向きに丸める
- 認識単位を特定できないfallback区間を含む場合: その区間はASR発話区間全体を使い、前後の有効単位を含む最小の連続Source区間にする
- 複数発話区間をまたぐ場合も上記を合成し、15秒検索chunk全体へ広げない
- 同じ発話内に同じ語が複数回ある場合: occurrence indexは内部に保持する。異なる認識単位列へ写る反復は別evidence、同じ認識単位列・同じtime spanへ写る反復は表示上1件にまとめてoccurrence countを持てる

文字一致は意味scoreで並べない。`match_tier`の順位はbase=0、kana=1、cjk_compact=2とし、完全な順位keyは`(match_tier, evidence_duration, public_video_id, evidence_start, hit_id)`とする。

### 6.3 意味検索

- 意味検索だけに類似度閾値を適用する。
- 動画指定時は全体FAISS上位を後から絞らず、指定動画のベクトルだけで順位を計算する。
- 完全な順位keyは`(-semantic_score, public_video_id, evidence_start, hit_id)`とする。
- 意味scoreは意味検索結果だけに表示する。文字一致は`—`とする。
- モデルまたはFAISSが利用不能でも文字一致を返し、意味検索だけが利用不能であることを構造化して通知する。
- 各検索requestは開始時のactive Transcript revision集合と、存在する場合はIndex generationを一つのSearch snapshotとしてpinする。TextMatcherとSemanticRetrieverは同じsnapshotを使い、検索中の再index公開で同じrequest内の根拠を入れ替えない。意味indexがない、壊れている、または非互換でもsnapshotは文字一致だけで成立する。

### 6.4 結果統合

- 文字一致枠と意味検索枠は別の上限を持つ。初期値は文字一致最大20件、意味検索最大5件とする。
- UIでは文字一致を先に表示し、意味検索を続けて表示する。
- 文字一致は同じTranscript revision、認識単位列、evidence spanへprojectionされたoccurrenceだけを重複統合する。内部occurrence identityは失わない。
- 意味検索はdurationが0以下の候補を先に拒否し、完全順位keyでscore同値を確定してからdeterministic NMSを行う。同じpublic_video_idかつ半開区間の`overlap / min(duration) >= 0.30`の候補を抑制する。この0.30は現行15秒chunk/5秒overlapを基準とする初期設定で、連結成分によって長い会話全体を一件へ結合しない。
- 同一シーンが両分類にあり、文字evidenceがsemantic evidence内に整数msで完全包含される場合は文字一致を代表とする。境界許容差を設けず、同値境界は包含とする。別の認識単位列へ写る文字occurrence同士は統合しない。
- 件数上限はcluster化と重複排除の後に適用する。
- 文字一致だけで意味検索枠を消費しない。
- `hit_id`は結果再描画中も安定し、表の行番号を識別子にしない。

### 6.5 検索範囲

- 範囲検索は個別動画を選んだ場合だけ利用できる。
- 区間の交差は半開区間として`candidate.end > range.start and candidate.start < range.end`を使う。文字一致ではevidence、意味検索ではsemantic chunkをcandidateとする。
- 範囲指定は詳細設定へ置き、通常検索の操作数を増やさない。

### 6.6 検索とASR失敗の区別

検索語が見つからない場合、次を混同しない。

- 文字起こしにその文字列が存在しない
- 意味検索候補が閾値を超えない
- 意味インデックスが利用不能
- 動画が未文字起こしまたは未インデックス

「該当なし」は上記状態を内部codeで区別し、UI文言を適切に変える。

## 7. 編集仕様

### 7.1 canonical time

- Edit planの境界は常にSource timelineで保持する。
- Result timelineの再生位置をSource timelineの境界として直接適用しない。
- domain内部の正規形は整数ミリ秒を目標とし、UIの秒floatはadapterで変換する。
- 区間は原則として半開区間`[start, end)`とする。
- 全体終端だけ、表示上は動画末尾を含む終端として扱える。

### 7.2 Edit plan invariant

正規化後のplanは次を必ず満たす。

1. `0 <= overall_start < overall_end <= source_duration`
2. 全体範囲は100ms以上
3. 各除外区間は全体範囲内で100ms以上
4. 除外区間は開始順で、重複しない
5. 1ms以下の隣接差は同一区間として統合する
6. 全体範囲のすべてを除外せず、100ms以上のKept rangeを残す
7. 100ms未満のKept islandを作らない。除外間または外端に生じた小gapは隣接除外へ統合し、全体を消す場合は操作を拒否する

現行実装は新規除外について1ms程度を許すため、100msへの統一は互換維持ではなく意図的な仕様変更として移行テストを付ける。

### 7.3 境界操作

| 操作 | 規則 |
|---|---|
| 全体開始・終了 | 動画範囲と最小全体幅へクランプする |
| 全体範囲縮小 | 除外を新しい全体範囲へ切り詰め、重複を統合する |
| 除外追加 | 逆方向ドラッグは並べ替え、全体範囲へクランプ、既存区間と統合する |
| 除外境界変更 | 最小幅へクランプし、重なれば統合する |
| 全範囲除外 | 操作全体を拒否し、変更前planを維持する |
| 除外削除 | 安定したexclusion IDで対象を指定する |
| 秒微調整 | 0.1 / 1 / 10 / 30 / 60 / 600秒を維持する |

統合後に元のexclusion IDが消える場合、選択位置を含む生存区間へ選択を再対応する。失敗操作でplan、履歴、選択を途中状態へ変えない。

### 7.4 境界指定の根拠

- 認識単位時刻が有効なら、その保存済み開始・終了を使う。
- 無効なら発話区間開始・終了を使う。
- プレビューの現在位置と手入力は任意時刻を指定できる。
- UIには「ASR認識単位」「発話区間」「手動時刻」を表示し、「単語精度」と「フレーム精度」を同一視しない。
- 「文単位」という名称は、文法上の文を保証できない場合「発話区間単位」へ変更する。

### 7.5 Viewportと編集範囲

- 概要タイムラインのviewportは表示範囲であり、移動や拡大だけでEdit planを変えない。
- 表示範囲を全体範囲へ適用する場合は、明示的な一回操作にする。
- 拡大タイムラインは現在のviewportを表示し、Source timeline上の境界を編集する。
- タイムライン編集モードOFFではドラッグをシークとして扱い、「シーク中」と表示する。

### 7.6 履歴とdirty

- Undo/RedoはEdit planの意味上の変更だけを対象にする。
- シーク、viewport、ツール選択、文字起こしスクロールでは履歴を増やさない。
- ドラッグgestureは開始前のplanを一つだけUndoへ積む。
- 新しい編集でRedoを消し、失敗操作では履歴を変えない。
- 履歴上限は50planとする。
- dirtyはcurrent planとclean referenceの意味上の比較から導出する。clean referenceはdocumentを開いた初期plan、または最後に成功保存されたplanである。
- exclusion IDだけの違いはdirtyにしない。
- 検索候補から開いた初期planは、それ自身をClean referenceとしてcleanで開始する。

### 7.7 動画・候補切替

- dirty状態の切替確認は一操作につき一度だけ出す。
- キャンセル時はVideo、plan、revision、表示を一切変えない。
- 確認後の切替は新しいdocument ID、nonce、revision 0を作り、古い待機commandを拒否する。

## 8. Source timelineとResult timeline

Kept rangeを`Ki=[si, ei)`とすると、元動画時刻`t`のResult timeline時刻は次とする。

```text
source_to_result(t) = sum(ej - sj, j < i) + (t - si)
```

- 除外区間内のSource時刻にはResult時刻が存在しない。
- Result→Sourceは累積Kept range長から区間を特定する。
- 連結点は次のKept range開始へ対応させ、二つのSource時刻へ同時対応させない。
- 最終終端だけはoverall endへ対応できる。
- 編集画面の完成予定時間、編集結果プレビュー、結果から元動画へ戻る操作は同じEdit-plan TimelineMapを使う。保存動画、SRT、成果物durationは同じartifact TimelineMapを使い、用途ごとに別計算を作らない。

外側paddingを使う保存では、padding適用後のEffective Export Planを作り、そのKept rangeからartifact用TimelineMapを生成する。編集画面のTimelineMapとpadding付きartifactのTimelineMapを混同しない。

境界例:

```text
Edit kept ranges: [10,20), [30,40)
result_to_source(0)  = 10
result_to_source(10) = 30   # 連結点は次のrange開始
source_to_result(15) = 5
source_to_result(25) = None # 除外内
result duration      = 20

leading padding=2, trailing padding=3 の成果物:
Effective kept ranges: [8,20), [30,43)
source_to_result(10) = 2
source_to_result(30) = 12
artifact duration    = 25
```

TimelineMap実装前は、編集結果プレビュー中の境界編集を禁止し、文字起こしを隠すか「元動画時刻・編集不可」と明示する。

## 9. プレビュー仕様

### 9.1 元動画プレビュー

- 候補選択時にSuggested rangeの前後を含む短い範囲を読み込み、Search evidence位置へシークする。
- 動画名、表示区間、Source timelineであることを常時表示する。
- プレビュー生成失敗でもEdit planを壊さず、再試行可能にする。

### 9.2 編集結果プレビュー

- Kept rangeを連結した結果を再生する。
- 表示時刻はResult timelineであることを明記する。
- 元動画へ戻るときはTimelineMapで対応するSource位置へ戻す。
- result preview生成はEdit planを変更せず、Undo履歴も増やさない。

### 9.3 精度

- プレビューは速度優先で、キーフレームによるずれを許容する。
- 保存結果の精度はffmpeg export側で保証する。
- UIは「プレビューのずれ」と「保存境界の精度」を区別して説明する。

## 10. 保存・出力仕様

### 10.1 動画保存

- WebUIの上部タブは「検索・編集・切り抜き」「LLM要約・見どころ」「動画保存」「インデックスの共有」の順に表示する。
- 切り抜き成果物とダウンロード動画は各保存画面から保存フォルダをExplorerで開ける。共有用インデックスzipは、生成後にExplorerで当該ファイルを選択表示でき、未生成時は既定のexportフォルダを開く。
- 単一区間と複数Kept rangeが同じ保存use caseを通る。
- paddingは全成果物の先頭と末尾だけに適用し、除外した中間へ食い込ませない。
- `make_effective_export_plan`はoverallを`[max(0, overall.start-pad_before), min(source_duration, overall.end+pad_after))`へ拡張し、既存exclusionはSource時刻のまま維持してKept rangeを再導出する。requested paddingとsource端でclampされたeffective paddingを両方返す。
- padding適用後のEffective Export Planを動画、SRT、artifact完成時間の唯一の入力にする。paddingで新たに含まれたSource範囲の字幕も、通常と同じ実在時刻規則を満たす場合はSRTへ含める。
- 高速モードはstream copyを許可し、キーフレームずれを明示する。
- 精密モードは入力前シークと再エンコードを使う。
- 既存ファイルへ暗黙に上書きしない。
- 出力先と同じファイルシステム上でjob固有directoryに全成果物を完成させる。複数成果物はdirectoryのatomic rename、またはcommit manifestを最後にatomic publishする方式で一組として公開し、部分公開を成功扱いしない。
- 失敗・timeout・cancelで既存成果物とEdit planを壊さない。

### 10.2 保存中の編集とClean reference

各保存jobは開始時に次を固定する。

- document ID
- source generation
- plan snapshotとplan hash
- 単調増加するsave sequence
- 出力artifact一覧

save sequenceはdocument単位で採番する。保存開始時とartifact公開直前に元ファイルのstat/fingerprintを検査する。保存成功時は、同じdocument IDとsource generationを持つ所有sessionがまだ存在し、より新しい成功save sequenceに上書きされていない場合に、そのsnapshotをclean referenceとする。保存中にcurrent planが変わっていてもcurrent/historyは変更しない。閉じたdocumentのjobはartifactだけを成功とし、現在開いているdocumentのclean referenceを変更しない。完了順が逆転した古いjobは新しいclean referenceを上書きしない。

### 10.3 SRT sidecar

- SRTは字幕焼き込みとは別機能として先に実装する。
- 字幕付き保存はplanned rangeと映像の対応を保つため精密保存を使う。
- ASR認識単位が完全なら、Effective Export Planの各Kept rangeと元ASR発話区間ごとに、完全に含まれる連続認識単位を一cueへまとめる。本文は検索正規化前の保存tokenを順番に連結し、cue境界は最初と最後の単位の実在時刻をartifact TimelineMapで写す。除外をまたいで一cueにしない。
- 途中カットが認識単位の内部を横切る場合、その単位は推測分割せず省略してwarningへ記録し、前後の完全な単位群は別cueにできる。
- 発話区間時刻しかないcueは、その区間全体が一つのKept rangeへ含まれる場合だけ採用する。先頭・末尾を含む一部切断や複数rangeへの分断では、本文を推測分割・重複表示せず省略してwarning manifestへ記録する。
- cueは`0 <= start < end <= probed output duration + tolerance`を満たす。cueを`(start,end,source_segment_id)`で安定sortし、start列をミリ秒単調非減少にする。異なるcueが重なる場合は時刻を黙ってずらさずwarningへ記録する。toleranceは実動画のframe durationとcontainer timebaseをprobeして決め、固定の厳密一致にしない。
- 最終cueを動画末尾まで無理に延ばさない。
- SRTはUTF-8、`HH:MM:SS,mmm`形式とする。

字幕の最大行数、文字幅、フォント、表示秒数は実素材評価前に公開仕様として固定しない。

### 10.4 字幕焼き込み

通常の編集保存に対する汎用字幕焼き込みは保留機能とする。SRTと同じ初期フェーズへ含めない。

ただし、見どころ候補から作るショート動画については、ローカル実験機能として次を許可する。

- 出力は9:16のMP4とし、1080x1920または720x1280を選べる。
- 既定の画面配置は、元映像全体を中央へ残し、余白を同じ映像のぼかし背景で埋める。利用者が明示した場合だけ中央cropを使う。
- 字幕はactive Transcript revisionの実在時刻を候補区間のResult timelineへ写し、電話画面で読める長さへ表示ブロックを分割してlibassで焼き込む。ASR本文、ASS中間ファイル、動画内容をログ・DB・外部サービスへ送らない。
- 字幕焼き込みと縦型化は再エンコードを必須とし、高速stream copyとして表示しない。
- Windowsのdrive-letter escapingを避けるため、字幕filterはjob固有の同一filesystem staging directoryに置いた相対名を参照する。
- 通常動画保存は従来の高速・精密選択を維持し、ショート動画の設定によって変更しない。
- 初期実装は顔追従、被写体推定、字幕本文編集、翻訳、単語karaoke強調を含まない。

### 10.5 機械可読出力

CLIと将来UIのため、保存結果は少なくとも次を構造化して返せるようにする。

- opaque `public_video_id`とsource generation（path由来legacy aliasは含めない）
- overall range、exclusions、kept ranges
- precise/fast、padding
- 成果物パスと成功/失敗
- subtitle warning

ローカル絶対パスを外部HTTP APIの公開識別子として使わない。

## 11. WebUI仕様

### 11.1 基本レイアウト

正式なメイン画面は「検索・編集・切り抜き」とし、直感編集の操作系を次の順序で一画面内に置く。従来の「検索・切り抜き」は通常起動時に表示せず、移行確認が必要な場合だけ環境変数`CUT_VIDEO_ENABLE_LEGACY_UI=1`で有効にする退避UIとする。新機能は正式メイン画面へ実装し、従来UIを二重保守しない。

1. 選択動画、表示範囲、除外数、完成予定時間、Undo/Redo、結果確認
2. 常設検索欄
3. 動画プレビュー、文字クエリー検索と候補、スクロール可能な文字起こし
4. 選択境界、時刻入力、現在位置適用、秒微調整
5. 同じEdit planを共有する「① 全体を決める」「② 詳細編集（任意）」のタイムラインタブ
6. 詳細編集タブ内の除外区間一覧
7. 固定保存アクションバー

検索入力と検索ボタンはAccordionへ隠さない。動画対象、範囲、意味閾値、件数は詳細設定へ置く。

タイムライン直前には、上部の操作と重複する大きな常設ガイドを置かない。必要な説明はボタン名、短い状態表示、tooltip、初回またはエラー時の文脈ヘルプで示す。全体タブでも全体開始・全体終了を明示的に選択し、0.1/1/10/30/60/600秒の幅で前後へ微調整できる。詳細タブと同じEdit planおよび直列コマンド経路を使う。

### 11.2 検索候補表示

候補に必要な情報は次だけとする。

- 一致方法
- 動画名
- Source時刻
- 短い根拠文
- 意味検索だけscore

候補選択で即プレビューし、別の「読み込み」ボタンを要求しない。

検索ヒットは「① 全体を決める」の元動画overviewにもマーカー表示する。文字一致は
菱形、意味検索は三角形と凡例で区別し、色だけを識別手段にしない。マーカー選択は
候補行選択と同じhit ID解決・検索結果open経路へ合流させる。

二段検索では、cleanかつ未選択の場合だけ先頭の文字一致を自動選択できる。意味検索が後から追加されても、選択中hit、preview、viewport、Edit planを変えない。queryごとにrequest IDを持ち、古いqueryの遅延応答を破棄する。意味検索処理中は文字一致0件だけを理由に「該当なし」を確定しない。候補領域は内部スクロールまたは分類ごとの折り畳みを使い、動画・文字起こし領域を押し下げない。

overviewの検索マーカーはEdit planではなくrequest ID付きの一時的なadapter viewとする。
後着の意味検索では候補とマーカーだけを追加し、editor stateを再描画・再初期化しない。
新しいqueryの送信時点で旧マーカーを無効化し、旧request IDの応答とクリックを破棄する。

全体タブは元動画全体のoverview上でviewportを移動・拡縮し、明示操作によってviewportを保存範囲へ反映する。詳細編集タブは現在のviewportを拡大し、全体開始・全体終了・除外開始・除外終了のすべてを変更できる。タブ切替は表示だけを変更し、Edit plan、再生位置、選択境界、active tool、Undo/Redo履歴、dirty状態を変更しない。

文字起こしの取得・表示範囲は現在のviewport開始・終了と一致させる。再生位置だけを理由にviewport内の別の短い文字起こし窓へ切り替えない。

基準viewportを1440×900とし、選択情報、検索、preview、文字起こし、境界操作、選択中のtimelineまでは通常のpage scrollなしで確認できることを目標にする。候補・文字起こしは内部scroll、保存はsticky barを使う。狭い画面では操作部を読めない幅へ圧縮せず、縦積みとpage scrollへ切り替える。

Phase 5の比較記録と今後のlayout回帰確認では、1440×900のscreenshotとDOM寸法を記録し、選択中timelineの下端までが初期viewport内、または一回以内の短いscrollで到達できることを確認する。従来UIとの比較が必要な場合だけ環境変数で退避UIを表示する。密度を満たすために文字サイズや操作targetを縮小しない。

### 11.3 編集操作

- 文字起こしと拡大タイムラインは同じ四ツール（全体開始、全体終了、除外開始、除外終了）を使う。
- 通常ツールは置かず、同じツールを再度押すと解除する。
- 選択中ツール、選択中境界、時刻粒度を常時表示する。
- 除外区間は斜線など色以外の表現も使い、既存除外上で新規除外を作っても正規化後に安全に統合する。
- 保存の主ボタンは一つとし、画面上部からは固定アクションバーへ到達しやすくする。

### 11.4 キーボードとアクセシビリティ

- 日本語IME変換中のEnterで検索・編集を発火しない。
- 文字起こしはroving tabindexを使い、矢印移動、Enter/Space決定を提供する。
- sliderはArrow、Home、Endと適切なARIA値を持つ。
- 色だけで保存範囲、除外、再生位置を区別しない。
- command待機、validation失敗、後続操作破棄を画面上で確認できる。
- ボタンと編集ツールはpointer downの時点で視覚的に応答し、server commandの完了表示とは分離する。
- timelineのdragは掴んだ位置とのoffsetを保って1対1で追従する。精密な時刻指定へ慣性、投射、bounceを適用しない。
- `prefers-reduced-motion`、`prefers-reduced-transparency`、`prefers-contrast`を尊重し、動きや透過を減らしても状態差が失われないようにする。

## 12. CLI仕様

- CLIはGradioと`app.py`をimportしない。
- 現在の`search_video.py`と`cut_clip.py`は当面互換wrapperとして残す。
- WebUIとCLIは同じSearchHit、Edit plan、保存use caseを使用する。
- `search`は`combined/text/semantic`を選べ、既定はcombinedとする。
- `clip`は複数除外、精密モード、SRTを表現できる。
- JSON出力は人間向けstderrと分離する。
- `batch-clip`は単一clipとmanifestの仕様が安定してから追加し、dry-run、重複統合、件数上限、出力衝突規則を必須とする。

## 13. インデックス共有仕様

- 共有zipには文字起こし本文、論理動画名、content identifier、vectorsが含まれ得ることを作成前に列挙する。論理動画名は既定で匿名名へ置換し、元名を残す場合だけ明示選択を求める。
- manifestへ元PCの絶対動画パス、ユーザー名、キャッシュパスを保存しない。
- path由来のlegacy aliasはpackage外へ出さず、canonicalな`public_video_id`だけを使う。同じIDの再importはprivacy-safe content digestが一致する場合だけ冪等mergeし、不一致ならID conflictとして拒否する。
- 匿名化済み論理動画名、duration、ASR metadata、segments、search units、vectorsだけをversion付きschemaで保存する。Source generationはpackage用opaque tokenと任意のfull-content digestで表し、local mtime、sample位置、sample fingerprintを含めない。
- vectorsにはembedding model/revision、pooling、正規化方式、次元を記録する。受入側と非互換ならvectorsだけを無効化し、共有されたTranscript revisionから再ASRなしで再埋め込みできるようにする。
- import時はschema version、必須field、件数、vector次元、展開サイズを検証する。
- importした動画はローカル動画との再関連付けが完了するまで検索専用とする。
- zipの内容を信頼して任意パスへ書き出さない。

## 14. エラー仕様

エラーは少なくとも次の分類を持つ。

- `VALIDATION_ERROR`: 入力や境界が不正
- `EDIT_CONFLICT`: revisionが古い
- `SOURCE_CHANGED`: source generation不一致
- `INDEX_UNAVAILABLE`: 意味インデックス利用不能
- `SEMANTIC_PENDING`: active Transcript revisionに対応する意味indexを作成中
- `INDEX_INCONSISTENT`: publication recordとFAISS generation不一致
- `SOURCE_MISSING`: 元動画の再関連付けが必要
- `PREVIEW_FAILED`: プレビュー生成失敗
- `EXPORT_FAILED`: 保存失敗
- `JOB_TIMEOUT` / `JOB_CANCELLED`
- `PRIVACY_CONFIRMATION_REQUIRED`: transcriptを含む共有操作の未確認

利用者向け文言にPython例外名やffmpeg stderr全文を直接表示しない。詳細ログにはoperation IDを出し、動画パス、検索語、文字起こし本文を既定で記録しない。

## 15. 非機能要件

### 15.1 プライバシー

- 文字起こし、動画、clip、共有zip、認証情報はGit管理対象にしない。
- テスト、文書、ログへ実在動画名、配信ID、個人パス、文字起こし本文を残さない。
- 外部APIへ文字起こしを送る機能は既定OFFかつ明示opt-inとする。
- Git commit前に禁止pathと動画・zip・DBなどの生成物を検査できるscriptを用意する。
- モデル取得、利用者指定URLのdownload、明示したremote provider以外の通信を行わない。

### 15.2 信頼性

- UI validation失敗でサーバーstateを部分更新しない。
- 書込みは単一writerに集約する。
- ファイル公開は一時ファイルからatomic replaceする。
- timeoutは一つのjob全体で共有し、子ffmpegごとに予算をリセットしない。
- cancel可能なffmpeg jobは`Popen`とprocess groupで管理し、停止確認後にstagingを掃除する。blocking `subprocess.run`だけでcancel対応済みとしない。
- 12時間級動画でも文字起こし全体を一度にDOMへ載せない。

### 15.3 性能予算

最初に合成fixture、動画本数・ASR segment数、CPU/GPU、HDD/SSD、warm/cold、試行回数、計測開始・終了点を記録した性能harnessでbaselineを測り、その後に次の暫定目標を評価する。p95は同一条件で最低20回測定する。

文字起こしはviewport変更、動画変更、検索候補読込など表示内容が変わる操作でだけ再取得・再描画する。境界変更、ツール選択、秒微調整などの通常編集commandでは文字起こしHTML全体を差し替えず、選択境界、保存範囲、除外範囲などの装飾だけを小さな状態projectionから更新する。

ASR segmentの範囲取得は選択動画のactive `transcript_revision`だけを対象とし、旧revisionを混在させない。範囲取得の主要経路には`(video_id, transcript_revision, start_sec)`の複合indexを用い、再文字起こしを重ねても問い合わせ量が過去revision数に比例して増えないようにする。

初期画面はキャッシュ済みサムネイルだけを読み、未キャッシュ動画全件に対するffmpegを起動処理やカード選択処理から同期実行しない。未生成分は一覧の展開、絞り込み、明示更新などの一覧操作で作る。可視カードは安定したvideo IDを直接選択commandへ渡し、同じ画像群を非表示Galleryへ二重描画しない。

| 操作 | 暫定目標 |
|---|---|
| 編集domain command | p95 50ms未満 |
| UI command ACK | p95 250ms未満 |
| 選択動画内の文字一致（warm） | p95 300ms未満 |
| 意味検索（モデルwarm） | p95 2秒未満 |
| 長処理のbusy/progress表示 | 500ms以内 |
| 候補選択からpreview request開始 | 100ms以内 |

preview完成とsave時間は媒体、codec、HDD/SSD、長さで大きく変わるため固定秒ではなく、現行比と進捗表示で評価する。

### 15.4 互換性

- 主対象はWindows、Python 3.10以上（bootstrap推奨は3.12）、ffmpeg、NVIDIA GPUとする。
- CPU fallbackを維持する。
- 既存DB/FAISSから再ASRなしで移行できることを優先する。
- `pyarrow==16.1.0`固定と`torch>=2.6`要件をセットアップ・requirements・文書で一致させる。

## 16. 受け入れシナリオ

### 16.1 検索から保存

1. サムネイルから動画を選ぶ。
2. 検索欄へ入力し、文字一致がFAISSなしでも表示される。
3. 意味検索が利用可能なら別枠で追加表示される。
4. 候補選択だけで根拠位置のpreview、文字起こし、cleanな初期planが開く。
5. 文字起こしで全体開始、タイムラインで全体終了を設定する。
6. 重なる除外区間を二つ追加し、一つへ統合される。
7. Undo/Redo、現在位置適用、0.1秒微調整を行う。
8. 編集結果を確認して元動画の対応位置へ戻る。
9. 保存成功後だけclean referenceが更新される。

### 16.2 検索異常系

- DBに文字一致がありFAISS IDが欠損しても文字一致する。
- 意味モデルロード失敗時も文字一致する。
- 再ASR公開と意味index作成の間は新Transcript revisionの文字一致だけを返し、旧revisionの意味候補を混ぜない。
- 「壊した」で「壊したんじゃね」の該当認識単位を返し、意味閾値で落とさない。
- 文字起こしに「命令」が存在すれば、指定動画内の文字一致としてFAISSなしで返す。
- 「ワンチャン」と「ワンちゃん」が文字一致する。
- CJK間のASR空白が一致を妨げない。
- overlap chunk由来の同一シーンが一行になる。
- WebUIとCLIで分類、時刻、順序が一致する。

### 16.3 編集異常系

- 検索結果を開いた直後はcurrent planとclean referenceが一致する。
- 非編集commandだけでdirtyにならない。
- 全範囲除外、NaN、古いrevisionを拒否してstateを維持する。
- 結果preview時刻をSource境界として誤適用しない。
- dirty状態の切替cancelでrevisionも進めない。

### 16.4 保存異常系

- 保存中の編集でcurrent planを巻き戻さない。
- 複数saveの完了順が逆でも古いjobがclean referenceを上書きしない。
- source変更を検出して疑わしい成果物を成功扱いしない。
- ffmpeg失敗時に既存出力を壊さない。
- SRT cueがartifact timeline（Effective Export Plan）内で単調かつ出力動画長以下になる。
- padding付き複数rangeで動画、SRT、manifestのTimelineMapとdurationが一致する。

### 16.5 プライバシー

- 共有zip manifestに絶対動画パスがない。
- 共有zip、外部API、成果物manifestにはpath由来legacy aliasを出さず、canonicalなopaque public IDだけを出す。
- commit検査が`data/`、`video/`、`clips/`、`exports/`、DB、zip、動画を拒否する。
- 合成fixtureだけで全E2Eが通る。

### 16.6 受け入れID

実装計画とtestは次のIDで本文のシナリオを参照する。

| ID | 対象 |
|---|---|
| `AC-SEARCH-CORE` | 16.1手順2〜4と16.2。FAISS非依存文字一致、正規化、evidence、二段結果、WebUI/CLI parity |
| `AC-SEARCH-SNAPSHOT` | 6.3と16.2。publication競合、Transcript revision非混在、reader lease |
| `AC-EDIT-KERNEL` | 16.1手順5〜7と16.3。境界、merge、履歴、TimelineMap |
| `AC-EDIT-ADAPTER` | 16.1手順8と16.3。Result currentTimeからSource preview位置への復帰 |
| `AC-INDEX-01` | 5.2と16.2。draft、Transcript revision公開、Index generation切替、旧世代維持 |
| `AC-SAVE-01` | 16.1手順9と16.4。保存中編集、逆順完了、source再検査、artifact transaction |
| `AC-SRT-01` | 10.3と16.4。途中カット、padding、動画/SRT duration整合 |
| `AC-PRIVACY-01` | 16.5。commit検査、opaque ID、共有metadata匿名化 |

## 17. 実験機能: ローカルLLMによる文字起こし解析

この節は `experiment/llm-transcript-analysis` ブランチだけに適用する。
自動要約を製品の中核にしないという2.3の方針は維持し、採用判断前の任意実験として扱う。

- LLM解析は既定OFFとし、利用者が動画追加時または既存動画に対して明示的に開始する。
- 初期providerは利用者PCのloopbackで動作するOllamaだけとし、文字起こしをクラウドへ送信しない。
- ASR原文、認識単位、検索chunkをLLM出力で上書きしない。
- 解析結果はactive Transcript revisionに紐づく再生成可能なderived dataとしてSQLiteへ保存する。
- 保存内容は動画全体の要約、代表タグ、ASR segment IDを根拠とする時間付き章とする。
- LLMへ時刻を生成させず、選択されたsegment IDを検証してからSource timelineの時刻へ解決する。
- 要約・章タイトル・章要約は日本語を必須とし、英語だけの出力は再生成後も不正なら解析失敗とする。英字の商品名・固有名詞タグは原文表記を許可する。
- Ollamaのcontext長を明示し、長いwindowの先頭が既定contextによって切り捨てられないようにする。
- 約5分を目安に全ASR segmentを決定的な連続groupへ分け、LLMは時刻や境界ではなく各groupのラベルだけを生成する。
- 各groupは一度ずつ全segmentを覆うことをアプリ側で検証し、未知ID、欠落、逆順、重複、厳格JSONでない応答は再生成後も不正なら解析失敗とする。
- 状態はpending/running/ready/failedを保持し、provider、model、prompt version、エラーを記録する。
- 解析失敗は文字起こし、検索generation、動画登録を失敗扱いにしない。後から再実行できる。
- 長時間文字起こしはASR segmentを分割しない文字数windowへ分ける。窓別章は文字起こしを直接見た結果として保持し、第2段階は動画全体の要約と代表タグだけを統合する。
- 結果にはwindow数、章数、文字起こしsegment網羅率を品質指標として保存する。
- WebUIは保存済み要約・タグ・時間付き章・品質指標を表示する。表示文字列は未信頼データとしてescapeする。

## 18. 実験機能: 文字起こしからの見どころ候補

この節は `experiment/llm-highlight-candidates` ブランチだけに適用する。
見どころ候補を自動保存する機能ではなく、利用者が確認して編集へ進むための任意の候補生成実験とする。

- 候補生成は既定OFFとし、17節のreadyな解析結果に対して利用者が明示的に開始する。
- 動画全体の要約だけから切り出し時刻を決めない。章タイトル・章要約・タグで候補章を選び、その章の元ASR発話区間をローカルLLMで再評価する。
- LLMは候補の核心となる30秒以内のanchor ASR segment IDを1件だけ選ぶ。時刻を生成させず、存在と同じTranscript revisionへの所属を検証してSource時刻へ解決する。導入・着地を含む完成候補全体をLLMへ直接決めさせない。
- アプリはanchorの前後から最大尺以下の許可窓を決定的に作る。LLMは許可窓内の実在ASR segment IDだけで、導入・本題・着地が収まる開始・終了を選ぶ。アプリはanchor包含、順序、最小尺、最大尺を再検証してSource時刻へ解決する。許可窓外や自由な時刻は指定できない。
- 初期値は候補6件、最小20秒、最大180秒とし、件数は3〜10件の範囲で指定できる。最大尺は候補を一律に引き延ばす目標値ではなく、前後関係の完結に必要な発話を追加できるハード上限とする。anchor自体が最大尺を超える場合は推測で分割せず、その応答を不正として一度だけ再生成する。
- 同じ内容の候補は、Source半開区間の `overlap / min(duration) >= 0.30` を重複とし、順位が後の候補を抑制する。
- 境界選択後に直前・直後の発話が文法・意味上必要かを最大2回確認する。最大尺内で必要segmentを追加できない候補は失敗で消さず、境界警告を付けてプレビューと手動調整を必須にする。
- 候補はタイトル、要約、選定理由、分類、タグ、anchor segment ID、調整後segment ID、Source開始・終了、順位を持つ。文字起こし本文を候補tableへ複製保存しない。
- 候補runと候補行はactive Transcript revisionおよび元のanalysis runに紐づく再生成可能なderived dataとする。ASR、検索、章解析を上書きしない。
- WebUIは候補一覧、時間、尺、理由、品質指標を表示し、選択候補をローカルpreviewできる。初期実験では利用者の確認なしにclipを書き出さない。
- WebUIは動画登録とは独立した「LLM要約・見どころ」タブを持ち、その内側を「① 要約を作る・確認する」「② 要約から見どころを作る・切り抜く」の2タブに分離する。対象動画の選択欄は各作業タブ内に置き、「検索・編集・切り抜き」と同じファイル名絞り込み・サムネイルカード・状態badgeを再利用し、代替Dropdownも提供する。どちらのタブから変更しても他方へ同期し、サムネイルの選択表示も同期する。active Transcript revisionにreadyな解析結果があるカードは「要約済み」、それ以外は「未要約」と明示する。未要約カードは選択可能なまま減光し、hover・focus・選択中は視認性を戻す。選択時に保存済み要約と候補状態を読み込み、ready解析があれば再要約せず候補生成を有効化し、なければ誤操作できないよう無効化する。
- 生成失敗や停止は文字起こし、検索、章解析、Edit planを変更しない。後から同じrevisionで再実行できる。
- 要約から自動選定する方式のproviderは17節と同じloopback Ollamaに限定し、クエリ方式はローカル文字一致とBGE-M3だけを使う。どちらも映像・音声の抑揚・無言の出来事・画面変化は評価できないことをUIへ明示する。
- 候補生成方式はRadioで「要約から自動選定」「自然言語クエリで検索」を明示的に切り替える。クエリ方式は選択動画内だけを対象に、共通検索サービスの正規化文字一致とBGE-M3意味検索を使い、順位付きhitのevidenceを実在ASR segmentへ解決して指定尺内へ決定的に拡張する。クエリ方式もLLMに時刻を生成させない。
- 生成候補の選択Radioは別の選択欄へ分離せず、各候補の番号・時間・タイトルの直前に配置する。説明を含む候補行全体をlabelとしてクリック可能にし、選択状態と内容を同じ場所で確認できるようにする。保存は利用者の明示操作後に「選択候補のみ」または「表示中の全候補」を個別mp4へ切り抜く。保存形式は通常動画または10.4のショート動画を選べる。保存名は通常動画を `元動画名_章タイトル.mp4`、ショート動画を `元動画名_章タイトル_short.mp4` の基本形とし、Windows禁止文字・予約名を無害化する。同名時は連番を付ける。途中ファイルから最終名への置換前にclaimを取得し、既存ファイルを上書きせず、失敗時はpartialとclaimを除去する。

品質評価では動画名や文字起こし本文を記録せず、少なくとも次をrun単位で保存する。

- requested件数と生成件数
- Source章数
- 候補尺の最小値・中央値・最大値
- segment IDへ完全に根拠付けできたか
- 重複抑制件数
- 利用者が採用した割合と、採用前に境界を変更した秒数（将来UIで計測する場合）

受け入れ条件は、全候補が実在segmentへ対応し、anchorを包含し、指定最大尺以下で、重複抑制後の順序が決定的であることとする。候補の面白さ・重要性は自動testだけで合格とせず、実データを外部送信しないローカル目視評価を別に行う。
