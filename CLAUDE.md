# Claude Code 運用方針

このプロジェクトでは，設計・方針検討・レビューはFableを優先する．
実装作業はSonnet系モデルまたはimplementerサブエージェントを優先する．

## 作業フロー

1. まず既存構成を確認する．
2. 実装前に変更方針を簡潔に整理する．
3. 実装は小さな差分に分ける．
4. 認証情報，課金設定，削除，git pushは事前確認なしに行わない．
5. 実装後はテスト，lint，型チェック，または最低限の動作確認を行う．
6. 変更したファイルと確認結果を最後に報告する．

## モデル運用

- 設計，調査，レビュー: Fable
- 実装，リファクタ，テスト追加: Sonnet (implementerサブエージェント)
- 軽い確認，単純修正，ディレクトリの検索などの雑用: SonnetまたはHaiku

## Codexとの協働レビュー

- 設計の妥当性確認と差分レビューは、まず編集を行わないレビューとして実施する。
- レビュー対象はソースコード、テスト、`git diff`に限定する。`data/`、`video/`、`clips/`、`exports/`、`.env`、認証情報は読んだり外部へ送ったりしない。
- 指摘には、対象ファイル・再現条件・影響範囲・修正案を含める。
- 実装後は、テスト結果と未解決の懸念点を添えて再レビューする。
- git commit、git push、認証情報の変更、データ削除は、明示的な依頼がある場合にだけ行う。

## プロジェクト概要

自然言語クエリによる動画シーン検索・切り抜きツール (Windows向け)．

- `app.py`: Gradio WebUI (検索・切り抜き / 動画の追加)
- `index_video.py`: インデックス構築 (faster-whisper → チャンク化 → BGE-M3 → FAISS)
- `search_video.py` / `cut_clip.py`: CLI
- `moment_retrieval/`: コアモジュール (asr, chunker, embedder, vector_index, refine, db, config, utils)
- 実行は `venv\Scripts\python.exe` を使う (システムPythonには依存パッケージがない)
- `data/` (SQLite+FAISS), `clips/`, `video/` はgit管理外
