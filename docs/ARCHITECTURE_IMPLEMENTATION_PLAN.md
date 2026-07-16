# 動画シーン検索・切り抜きツール 実装設計

## 0. この文書の位置づけ

この文書は、動画シーン検索・切り抜きツールを「動画編集ソフト」ではなく、長時間動画から発言を探し、必要な区間を短い操作で保存するためのツールとして発展させる実装設計である。

- 設計基準は「検索 → 確認 → 境界決定 → 保存」の1周を短くすること。
- 現行Gradio版は、新UIが代表シナリオで明確に上回るまで動作基準として残す。
- Whisper、埋め込み、インデックス作成はUIプロセスへ戻さず、現在の別プロセス設計を維持する。
- 精密編集、エフェクト、マルチトラック、字幕の手動編集は対象外とする。
- 設計開始時のバックアップ基点は `agent/multi-clip-checkpoint` の `42f299b`。

## 1. 現状と目標の差分

現状は、検索の中核が `moment_retrieval/search.py`、動画保存が `cut_clip.py` に分かれている一方、直感編集の状態、履歴、検証、HTML描画、Gradioイベント、プレビュー生成、保存オーケストレーションが `app.py` に集中している。

目標の依存方向は次の通りとする。

```text
Gradio / CLI / 新Web UI
          |
          v
UI adapter / command gateway
          |
          v
application services --------> editor_domain (pure)
      |          |                     |
      v          v                     v
 media       search/index          EditPlan/history
      |
      v
 export
```

禁止する依存は次の通り。

- `editor_domain` からGradio、SQLite、FAISS、ffmpeg、ファイルシステムを呼ばない。
- CLIから `app.py` またはGradioをimportしない。
- サービス層から `gr.update` を返さない。
- UI表示用の選択状態やHTMLを、保存対象となる編集計画へ混ぜない。

## 2. 状態の所有権

「編集状態」を一つの巨大なオブジェクトにせず、意味の異なる三層へ分ける。

### 2.1 ドメイン状態

`EditPlan` は保存結果を決める唯一の状態とする。

```python
@dataclass(frozen=True)
class Exclusion:
    id: str
    start: float
    end: float

@dataclass(frozen=True)
class EditPlan:
    duration: float
    overall_start: float
    overall_end: float
    exclusions: tuple[Exclusion, ...]
```

`EditorHistory` は現在値、保存時点、Undo、Redoを所有する。

```python
@dataclass(frozen=True)
class EditorHistory:
    current: EditPlan
    baseline: EditPlan
    undo: tuple[EditPlan, ...]
    redo: tuple[EditPlan, ...]

    @property
    def dirty(self) -> bool: ...
```

`dirty` は可変フラグとして保存せず、`current` と `baseline` の意味上の比較から常に導出する。除外区間のIDはUI選択用なので、dirty比較から除外する。

### 2.2 アプリケーションセッション

次は保存結果そのものではなく、複数イベントを安全に処理するための状態である。ドメインへ入れず、アプリケーション層またはadapterが所有する。

- `nonce`、`revision`、処理済みcommand ID
- 選択中の境界、選択中の編集ツール
- 除外開始だけを選んだ一時状態
- viewport、playhead、文字起こし表示中心
- 元動画/編集結果のプレビューモード

Undo/Redoは`EditPlan`だけを対象にする。シーク、viewport移動、ツール選択では履歴を増やさない。

### 2.3 永続・外部状態

- 動画、文字起こし、検索チャンク: SQLite
- ベクトル: FAISS
- プレビュー: 再生成可能なキャッシュ
- インデックス作成ジョブ: 別プロセスとPID/進捗ファイル
- 保存済み動画、SRT: 出力成果物

Gradioと将来UIが並行する場合も、編集セッションの共有は行わない。共有するのは動画ライブラリとインデックスだけとし、各UIは独立した編集セッションを持つ。

## 3. editor_domain の規則

### 3.1 時刻表現

- 公開境界は秒の`float`を受け取る。
- 入口で有限値を検証し、内部の比較とJSON出力は1ミリ秒へ正規化する。
- 最小保持区間・最小除外区間は現行挙動に合わせて0.1秒とする。
- 単語時刻がなければセグメント時刻を使い、推測した単語時刻は作らない。

### 3.2 invariant

正規化後の`EditPlan`は必ず次を満たす。

1. `0 <= overall_start < overall_end <= duration`
2. 全体範囲は0.1秒以上
3. 各除外区間は全体範囲内で0.1秒以上
4. 除外区間は開始順で、正規化後は重複しない
5. 除外区間だけで全体範囲を覆わず、保持区間を0.1秒以上残す

現行UIは重複する除外指定を統合しているため、P1でもこの挙動を維持する。「重複を拒否」へ変えると既存操作との互換性を壊すため採用しない。

### 3.3 コマンドごとの境界方針

| 操作 | 範囲外 | 他区間との重複 | 保存範囲が消える場合 |
|---|---|---|---|
| 全体開始/終了の設定・微調整 | 動画長と最小幅へクランプ | 除外を新全体範囲へ切り詰めて統合 | 拒否し元状態を維持 |
| 除外追加 | 全体範囲へクランプ | 重複・隣接区間を統合 | 拒否し元状態を維持 |
| 除外開始/終了の設定・微調整 | 全体範囲と最小幅へクランプ | 統合し、生存区間へ選択IDを再対応 | 拒否し元状態を維持 |
| 除外削除 | 該当IDがなければ構造化エラー | 対象外 | 対象外 |
| Undo/Redo | 対象履歴がなければ構造化エラー | 正規化済み状態を復元 | 対象外 |
| 保存確定 | 編集計画は変更しない | 対象外 | 外部保存成功後だけbaseline更新 |

コマンドは例外途中の部分更新を残さない。変更候補を作り、正規化・検証が成功した場合だけ新しい不変オブジェクトを返す。

## 4. モジュール構成

最終形を一度に作らず、利用者が現れた境界だけを切り出す。

```text
moment_retrieval/
  editor_domain/
    model.py          EditPlan / Exclusion / kept_ranges / SourceTimelineMap
    commands.py       set_boundary / nudge / add-remove exclusion
    history.py        Undo / Redo / baseline / dirty
    errors.py         UI非依存のエラーコード
  export_service/
    transcript.py     UI非依存のSegment/Word DTO、words_json解析
    subtitles.py      ASR cue抽出、保持区間との交差、SRT生成
    ffmpeg_export.py  通常保存と字幕焼き込みのオーケストレーション
  application/
    editor_session.py nonce/revision、UI一時状態、保存成功時の確定
    command_gateway.py JSON表現可能なcommand/responseへの変換
  adapters/
    gradio_editor.py  gr.update、HTML、Gradioイベントの変換
```

既存モジュールは当面次のように扱う。

- `moment_retrieval/search.py`: `search_service`の核として維持し、DB/FAISSの準備だけを後からfacadeへ移す。
- `cut_clip.py`: `media/export`が利用するffmpeg実行部として維持する。CLIの互換入口も残す。
- `index_video.py` と現在の `subprocess.Popen`: `index_job_service`を作るまで設計を崩さない。
- `moment_retrieval/preview_cache.py`: `media_service`抽出時に移動せず、そのまま再利用する。
- `app.py`: 各フェーズで呼び出し先だけを順に置き換え、一括書き換えをしない。

ここでいう`export_service`はクリップ、SRT、字幕焼き込みの出力を指す。既存の`moment_retrieval/share.py`が行う「インデックス共有zipのexport/import」とは別責務であり、同じserviceへ統合しない。

### 4.1 現行コードからの移行対応表

| 目標責務 | 現在の主な場所 | 最初に移す対象 | 当面残す対象 |
|---|---|---|---|
| editor domain | `app.py`の`_intuitive_plan_snapshot`、`_clip_intuitive_exclusions`、`_set_intuitive_boundary`、`dispatch_intuitive_command` | plan正規化、境界操作、履歴、dirty | nonce/revision、tool、viewport、playhead |
| Gradio adapter | `app.py`の`render_intuitive_*`、`handle_intuitive_command`、イベント配線 | domain結果から表示値への変換 | Gradio component生成とJS連携 |
| media service | `app.py`の`make_preview`/`make_intuitive_preview`、`cut_clip.py`、`preview_cache.py` | P0では保存呼出しのinterfaceだけ | preview抽出とcache再編はP4まで延期可能 |
| search service | `moment_retrieval/search.py`、`app.py`の`do_search`/`do_intuitive_search`、`search_video.py` | 検索準備と結果DTOを共通facade化 | embedder/FAISSの実装詳細 |
| index job service | `index_video.py:run_indexing`、`app.py`の`subprocess.Popen`/停止処理、`downloader.py` | P4前にlock/generation境界を追加 | 重処理の別プロセス実行 |
| export service | `cut_clip.py:cut_clip/cut_clips`、`app.py:save_intuitive_editor` | P0のtimeline map、SRT、保存orchestration | ffmpeg低水準実行関数 |

最初の抽出で`app.py`と`cut_clip.py`を同時に全面再編しない。P0は新しい純粋moduleと既存保存入口の接続、P1はdomainとadapterの接続に限定し、検索とindex jobはそれぞれP2/P4で利用者が現れた時点で切り出す。

`index_job_service`はsubprocessのコマンド、PID、進捗、停止だけを管理し、`index_video.py`、`chunker.py`、`asr.py`をimportしない。`chunker.py`からWhisper系のnative依存へ到達するため、importだけでもUIプロセス分離を壊す可能性がある。`app.py`は最終的にもcomposition rootとして起動とadapter配線だけを残してよい。

## 5. 実施順序

### P0: SRT出力と字幕焼き込み

P0とP1は完全には独立していない。SRT時刻変換と動画連結が同じ保持区間を使う必要があるため、P0の最初に`editor_domain.model.EditPlan`と`kept_ranges()`だけを小さく導入する。履歴やGradio reducerの移動はP1で行う。

#### P0-1 純粋な保持区間計算

- 現在の`overall_start/end/exclusions` dictを`EditPlan`へ変換するadapterを作る。
- `kept_ranges(plan)`を動画保存と字幕生成の双方で使用する。
- 既存の`intuitive_state_to_clip_plan()`出力との一致をテストする。

#### P0-2 字幕時刻の再マップ

保持区間を `K0, K1, ...` とし、元動画時刻`t`が区間`Ki`内にあるとき、クリップ時刻は次で求める。

```text
clip_time(t) = sum(duration(Kj), j < i) + (t - start(Ki))
```

`SourceTimelineMap`が上式を所有し、動画保存と字幕が同じ写像を利用する。保持区間は半開区間`[start, end)`として扱う。ASR cueは各保持区間との交差へ分割してから、交差部分の開始・終了を上式で変換する。これにより、除外区間をまたぐcueが削除時間を橋渡しせず、境界上のcueも二重に出ない。

- 単語時刻が揃っているセグメント: 実在する単語境界だけでcueを組み立てる。
- 一部の単語に時刻がないセグメント: 単語精度へ格上げせず、セグメント単位へフォールバックする。
- セグメント単位のcueが除外をまたぐ場合: 最も重なり時間が長い一つの保持区間へだけ割り当て、時刻をその交差へクランプする。同じ全文を除外の前後へ重複表示せず、文字列も推測分割しない。この制限はSRT生成結果へ警告として残す。
- 空文字、0ミリ秒cue、完全に除外されたcueは出力しない。
- SRTはUTF-8、連番、`HH:MM:SS,mmm`形式とする。

cueの自動整形は固定設定とし、P0ではUI設定を作らない。初期値は最大2行、1行の表示幅42、最大6秒、単語間の無音0.7秒を分割候補とする。日本語の表示幅は文字数ではなくUnicode表示幅として扱う。

#### P0-3 保存パイプライン

- 「SRTを同時出力」と「字幕を焼き込む」を独立指定できる。
- 焼き込みONならSRT生成は内部的に必須。利用者がSRT出力OFFなら一時SRTだけを使う。
- SRT出力または焼き込みを指定した保存は、要求rangeと実際の映像時間を一致させるため`precise=True`を必須にする。stream copyは区間ごとのキーフレームずれが累積し得るため字幕付き保存には使わない。UIには「字幕付き保存では精密保存となり時間がかかる」と明示する。
- SRTも焼き込みもOFFなら既存の`cut_clips()`経路をそのまま通し、DB照会、字幕parse、追加ffmpegを一切行わず速度退行を起こさない。
- 焼き込みONは連結済みの一時動画へ`subtitles`フィルタを1回適用する。字幕フィルタのため映像は再エンコードし、音声は可能ならコピーする。
- ffmpegの`subtitles`フィルタへWindows絶対パスを直接渡さない。ASCII名の一時SRTを作り、ffmpegの作業ディレクトリを一時ディレクトリへ設定して相対パスで参照する。escapingは一つの関数へ集約し、ドライブ文字、空白、日本語、シングルクォートを含む出力先でテストする。起動前にlibass/subtitles filterの利用可否を検査し、日本語フォントを明示できない環境では構造化エラーを返す。
- 動画とSRTは出力先と同じファイルシステム上の一時ディレクトリで完成させ、成功後に置換する。失敗時は既存成果物を壊さず一時物を掃除する。
- timeoutは複数ffmpeg呼び出し全体で共有する。将来のcancel tokenを渡せる関数形にする。

#### P0受け入れ条件

- 途中カット0、1、2箇所で、動画長と最終SRT終了時刻が一致範囲内にある。
- cueが二つの除外区間をまたいでも、後続字幕が除外合計分だけ正しく前へ詰まる。
- 単語時刻なし・一部欠損・不正JSONでもセグメント単位で完走する。
- 焼き込みONの動画に字幕ストリームではなく画面上の字幕が存在する。
- SRT/焼き込みともOFFのコマンド列と所要時間に実質的な変更がない。

### P1: editor_domain完全分離

1. 現在の`_clip_intuitive_exclusions`、境界検証、編集signatureを純粋関数へ移す。
2. `EditPlan`操作を新旧adapterから同時に呼ぶcharacterization testを作る。
3. Undo/Redo/baselineを`EditorHistory`へ移す。
4. `nonce/revision`、ツール選択、viewportなどを`EditorSession`へ残す。
5. `dispatch_intuitive_command()`を薄い変換層へし、`gr.Error`への変換は最外周だけにする。
6. 保存開始時のrevisionとplan hashを記録し、保存成功時にも同じplanがcurrentである場合だけ`confirm_saved()`を呼ぶ。保存中に別編集が入った場合、保存したsnapshotだけをbaseline候補として扱い、現在の編集はdirtyのままにする。ffmpeg失敗時もdirtyのままにする。
7. 既存ブラウザE2Eを通した後、`app.py`内の旧reducerを削除する。

履歴上限は現行値を維持する。ドラッグ中の連続値はgesture開始時の1状態だけをUndoへ積み、mousemoveごとに履歴を増やさない。

#### P1受け入れ条件

- ドメインテストはGradioをimportせずに実行できる。
- 現行の文字起こし、タイムライン、秒調整、現在位置適用が同じ`EditPlan`結果になる。
- 重複除外の統合、全範囲除外の拒否、境界変更後の選択再対応が維持される。
- 保存後Undoでdirty、Redoで保存時点へ戻るとcleanになる。
- `app.py`に編集計画の正規化、履歴push/pop、dirty計算が残っていない。

### P2: version付きコマンド契約とCLI統合

既存の`search_video.py`と`cut_clip.py`を捨てて別CLIを増やさず、共通サービスを呼ぶサブコマンドへ段階的に統合する。

#### 共通envelope

```json
{
  "schema_version": 1,
  "session_id": "server-issued-session-id",
  "command_id": "client-generated-id",
  "type": "set_boundary",
  "expected_revision": 12,
  "payload": {}
}
```

成功:

```json
{
  "schema_version": 1,
  "command_id": "client-generated-id",
  "ok": true,
  "revision": 13,
  "applied_command_id": "client-generated-id",
  "result": {
    "plan": {},
    "dirty": true,
    "can_undo": true,
    "can_redo": false
  }
}
```

失敗:

```json
{
  "schema_version": 1,
  "command_id": "client-generated-id",
  "ok": false,
  "error": {
    "code": "EDIT_CONFLICT",
    "message": "表示が古いため操作を適用できません",
    "details": {"current_revision": 13},
    "retryable": true
  }
}
```

外部へ公開するエラーコードは固定し、Python例外名やffmpeg stderr全文を契約にしない。

`command_id`はsession内で冪等とし、同じIDの再送には同じ結果を返す。frontendへはplan、dirty、Undo/Redo可否だけを返し、baselineや履歴stackは返さない。純粋なeditor commandと、`search/open/save/export`のようなI/Oを伴うuse-case/job APIは別のcommand unionとして文書化する。

検索modeは現在の仕様と揃え、`combined`を既定とする。`exact`は正規化文字列一致だけ、`semantic`は意味検索だけとする。文字一致結果にはscoreを表示せず、意味検索結果だけにscoreを含める。動画指定は曖昧なファイル名だけに依存せず、`video_id`を正規形とする。

`batch-clip`には次を必須にする。

- `--dry-run`
- 同じ/重なるヒットの統合規則
- 出力名衝突時の一意化
- 件数上限または明示的な`--all-hits`
- 各成果物の成功/失敗を返すmanifest

#### P2受け入れ条件

- CLI起動時の`sys.modules`にGradioがない。
- JSON schemaの正常系・未知フィールド・version不一致・revision競合をテストする。
- command再送の冪等性と、保存中にrevisionが進んだ場合のbaseline競合をテストする。
- `search`のWebUI/CLI結果順が同じ入力で一致する。
- `clip`と`batch-clip`がP0のSRT出力を同じサービス経由で使う。

### P3: 文字起こし活用のCLI実験

要約、章分け、切り抜き候補はUIへ入れる前にCLIで価値を測る。LLM providerは差し替え可能にし、文字起こしを外部へ送る処理は既定OFFとする。

- ローカルproviderとリモートproviderを同一interfaceにする。
- リモート送信時は動画ID、送信文字数、送信先を表示し、明示的なopt-inを要求する。
- 認証情報と文字起こし本文をログ、テストfixture、Gitへ残さない。
- 長時間動画は重なり付きチャンク→部分結果→時系列集約とし、チャンク境界の候補を重複排除する。
- `suggest-clips --json`はP2の`batch-clip`へそのまま渡せるが、実行前にdry-run manifestを確認できるようにする。

価値判定は、候補採用率、手動境界修正秒数、検索から保存までの総時間で行う。

### P4: 新Web UI試作

P2契約の上に直感編集画面だけを実装する。Svelte/Reactの選定は小さな技術スパイクで行い、Tauriはまだ選ばない。

- HTTPサーバーはloopbackだけで待受け、任意ファイルパスをAPIで受け取らない。
- 動画は`video_id`で解決し、ブラウザへローカル絶対パスを返さない。
- 編集セッションは新UI内で完結し、Gradioと共有しない。
- 編集セッションは`session_id`ごとの単一writerとする。
- Gradioと新UIを別プロセスのまま並行させる場合、現在のprocess-local lockだけに依存しない。P4開始前に、推奨案である単一backend processを両adapterから利用するか、最低限cross-process lock、FAISSの一時ファイルからのatomic publish、SQLiteとFAISSを対応づけるindex generationを導入する。
- 動画一覧の変更は起動時読込+index generationの低頻度pollingを初期案とする。インデックス作成中の進捗共有が必要になった時だけWebSocketを追加する。
- 概要タイムラインはviewport、拡大タイムラインは編集計画を操作する。二つの意味を混ぜない。
- dirty、Undo、Redo、revisionはサーバー応答だけを表示し、クライアント側で別計算しない。
- 編集モードOFFのドラッグはシークに限定し、視覚フィードバックを出す。

比較計測は同じ動画・同じ検索語・同じ2除外区間で行う。計測値は総秒数、クリック数、キーボード入力数、操作エラー数とし、最低5回の中央値を記録する。新UIが明確に上回らなければGradio置換へ進まない。

### P5: デスクトップシェル評価

P4合格後だけ実施する。最初は外部起動したPythonサーバーへ接続するpywebviewとTauriを比較し、sidecar化、PyInstaller、単一インストーラーは配布要件が確定するまで行わない。

評価軸は起動時間、動画再生安定性、キーボード操作、アップデート方法、ウイルス対策ソフトの誤検知、GPU環境構築、障害時にCLIへ戻れるかとする。

## 6. 手動回帰シナリオ

各フェーズで次を同じ順番で確認する。

1. サムネイルから動画を選び、自動プレビューと選択動画内検索対象を確認する。
2. 文字一致と意味検索を行い、候補から編集を開始する。
3. 文字起こしで全体開始、タイムラインで全体終了を設定する。
4. 現在位置適用と0.1秒/600秒の微調整を行う。
5. 重なる途中カットを2回追加し、統合後もUIが操作可能なことを確認する。
6. 全体範囲を縮め、除外が切り詰められ、保存範囲が残ることを確認する。
7. Undo/Redo、編集結果プレビュー、元動画復帰を行う。
8. 通常保存、SRT同時出力、字幕焼き込み保存を行う。
9. 保存成功後はclean、編集後はdirty、保存失敗後はdirtyのままであることを確認する。

P1移行時は手動確認だけでなく、旧reducerと新domainへ同じcommand traceを与え、plan、dirty、Undo/Redo可否、error codeを比較するdifferential testを必須とする。P0ではcueが除外開始/終了と一致する場合、一つのcueが二つの除外をまたぐ場合、全単語が除外される場合、ミリ秒丸めで0長になる場合も自動テストする。

## 7. フェーズ開始・終了条件

各フェーズは、次を満たす小さなPR/コミット列として進める。

- 開始前に既存テストと`git status`を記録する。
- 個人データを含む`data/`、`video/`、`clips/`、`exports/`を読まず、commit対象にしない。
- 新規純粋ロジックには型ヒントとUI非依存テストを付ける。
- UIイベントを変更した場合はブラウザE2Eを追加または更新する。
- ffmpeg処理を変更した場合は引数列、timeout、一時ファイル掃除、失敗時の既存出力保護をテストする。
- `git diff --check`、py_compile、pytestを通す。
- 既知の制限と次フェーズへ持ち越す事項を文書化する。

一つのフェーズ中にプロダクト機能、全面UI刷新、配布方式変更を同時に行わない。
