# Phase 0 実施記録・性能基準

## 1. 目的と安全条件

Phase 0は、新機能を増やす前に現行実装の矛盾、privacy上の危険、既知挙動を固定する段階である。望ましい最終仕様は`PRODUCT_SPECIFICATION.md`、移行順序は`ARCHITECTURE_IMPLEMENTATION_PLAN.md`を正とする。

テストと性能計測はすべて合成データだけを使う。既存の`data/`、`video/`、`clips/`、`exports/`、利用者の文字起こし、認証情報、実動画を読まない。network、ASR/embedding model、CUDAも起動しない。

## 2. Phase 0で追加した保証

- 検索候補から開いた編集planを、その候補範囲と同じclean baselineにする。
- Undo/RedoのUI説明と実装を一致させる。
- 実在由来に見えるfixture名を合成名へ置換する。
- PyTorch要件を`torch>=2.6`へ統一する。
- 共有exportから送信元path、元ファイル名、path由来旧ID、DB内部IDを除く。
- 共有前に、全文文字起こし、単語時刻、検索チャンク、埋め込みを含むことへの明示確認を必須にする。
- 旧共有形式はimportだけ受け付け、送信元pathと旧IDを破棄してlocal opaque IDへ変換する。
- 共有zipの本体・圧縮・展開サイズ、entry、manifest、時刻、単語時刻、`.npy` header/payload、embedding model・次元・正規化を検証する。
- WebUIの動画追加、共有export、共有importを同じsingle-writer laneで直列化する。stop操作はこのlaneの外に残す。
- `*.vindex.zip`などの生成物をignoreし、commit前privacy guardで危険なpath・生成物・credential・実在由来fixtureを拒否する。
- URL download、index child process、ASR途中保存・再開を合成characterization testへ追加する。

対象test:

```powershell
venv\Scripts\python.exe -m unittest `
  tests.test_share tests.test_privacy_guard tests.test_downloader `
  tests.test_index_job_control tests.test_index_resume `
  tests.test_phase0_benchmark
```

privacy guard:

```powershell
venv\Scripts\python.exe scripts\check_privacy.py
venv\Scripts\python.exe scripts\check_privacy.py --working-tree
```

## 3. 現行挙動として記録する既知制限

以下は恒久仕様ではなく、後続Phaseで反転または置換する。

1. URL downloadはindex用`Popen`登録より前に動くため、download中は停止ボタンから中断できない。cancellable downloader導入時に、停止要求とpartial file cleanupのtestへ置換する。
2. ASRはsource audio 300秒ごとにcommitする。最初のcommit前に停止すると、次回は0秒から再開する。
3. force再indexは新index成功前に旧DB/FAISS entryを削除する。immutable generation導入後は、失敗時に旧世代を維持する保証へ置換する。
4. redacted共有形式はPhase 0の暫定形式である。canonical public Video ID、同一packageの冪等merge、動画再関連付けUI、embedding非互換時の再埋め込みはPhase 1A/2Aで実装する。
5. import時はDB commit後にFAISS fileをatomic replaceし、通常のreplace失敗はDB補償削除する。ただしprocess crashを跨ぐ完全なatomicityはなく、Phase 2のgeneration publishで解消する。
6. WindowsのOS cacheを安全にflushしていないため、性能結果をcold cacheとは呼ばない。
7. single-writer laneは現行WebUI内の競合を防ぐ暫定策であり、別CLI processとの同時書込みまでは防がない。Phase 2でbackend全体のwriter ownershipへ移す。

## 4. 合成HDD/SSD性能harness

実装は`scripts/benchmark_phase0.py`。利用者が明示した既存root直下に、UUID付き・marker付きの一時directoryだけを作り、marker、prefix、親rootの三条件を確認してから削除する。結果JSONにroot path、username、hostnameは含めない。

```powershell
venv\Scripts\python.exe scripts\benchmark_phase0.py `
  --root "HDD=$env:CUT_PERF_HDD_ROOT" `
  --root "SSD=$env:CUT_PERF_SSD_ROOT" `
  --profile baseline `
  --output benchmark-results\phase0-baseline.json
```

`baseline`条件:

- 固定seed
- 合成動画32本、合成文字列100,000行
- NFKC、casefold、空白正規化を含むselected/all text scan
- 各case 20試行、p50/p95を保存
- 64MiB sequential write + file `fsync`、64MiB sequential read
- root間でSQLite fixtureのSHA-256一致を検証
- fixture生成時間は計測外

### 2026-07-17 実測baseline

同一PC上の`HDD-D`と`SSD-F`で採取した。raw JSONはgitignore対象の`benchmark-results/`に保存する。

| case | HDD-D p50 | HDD-D p95 | SSD-F p50 | SSD-F p95 |
|---|---:|---:|---:|---:|
| 選択動画内の文字scan | 11.11 ms | 11.48 ms | 15.64 ms | 15.98 ms |
| 全動画の文字scan | 156.95 ms | 159.39 ms | 159.19 ms | 165.44 ms |
| 64MiB write + fsync | 620.91 ms / 103.08 MiB/s | 692.46 ms | 185.06 ms / 345.84 MiB/s | 243.29 ms |

文字scanはwarmなOS cacheとCPU正規化の比率が大きいため、この計測ではSSD優位を示していない。一方、fsyncを含む連続書込みはSSD-Fが中央値で約3.35倍速い。sequential readはOS cacheの影響で8GiB/s超となったため、媒体速度として解釈しない。

## 5. Browser E2E gate

合成3秒動画、isolated SQLite/FAISS、決定論fake embedderを使い、次を一周する。

```text
動画選択 → 実検索 → 候補のclean load
→ 重複する途中カット2件をmerge → Undo/Redo
→ 編集結果preview → 元動画復帰 → 実ffmpeg保存
```

保存artifactの存在・尺、保存後clean、Undo後dirty、Redo後clean、server生存、Traceback/access violation不在、一時file不在まで検査する。通常suiteでbrowser runtime不足を明示skipすることは許すが、Phase 0 gateでは次をskipなしで通す。

```powershell
venv\Scripts\python.exe -m unittest tests.test_intuitive_editor_browser -v
```

2026-07-17の指定local環境では1 testをskipなしで完走した。
