# ショート動画仕上げ実験の復帰・確認手順

## 状態

- 実験branch: `experiment/short-video-finishing`
- 実験前branch: `checkpoint/pre-finishing-20260730`
- 実験前commit: `c752471`

実験前へ戻す場合は、未保存の作業がないことを`git status`で確認してから次を実行する。

```powershell
git switch checkpoint/pre-finishing-20260730
```

実験へ戻る場合は次を実行する。

```powershell
git switch experiment/short-video-finishing
```

## 実装した範囲

1. 型付きOutputProfileとpreview/save共通化
2. 字幕preset、safe area、Windows font glyph検査
3. ExportJobの進捗、停止、構造化失敗
4. 見どころbatchの部分成功と、同一snapshotの結合済み中間動画再利用
5. 任意の音量正規化、ローカルBGM、音声成果物検証
6. 1:1 canvas、ローカル投稿補助metadata JSON

## 統合前の手動確認

- 短い非公開fixtureで、元比率、9:16 blur/crop、1:1の構図を比較する。
- 字幕standard/large/boxedと上/中央/下をpreview後に保存し、改行位置が一致することを確認する。
- 音声正規化OFF/ONを同じ発話で聴き比べ、過度な音量変動や歪みがないことを確認する。
- 権利を確認したローカルBGMだけを使い、既定-24 dB、fade 0.5/1.0秒から調整する。
- 停止後にpartial、claim、未完成成果物が残らないことを確認する。
- 投稿補助JSONは外部送信されず、投稿前確認が必要であることを確認する。
