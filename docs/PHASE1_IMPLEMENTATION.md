# Phase 1 implementation record

Implemented on the Phase 0 working tree. This document records the boundary of
Phase 1 and intentionally does not treat later immutable-generation work as done.

## Phase 1A — identity and schema foundation

- SQLite schema version is explicit (`PRAGMA user_version`, current version 3).
- A file database is backed up under `migration-backups/` before a legacy schema
  is changed. DDL runs in one transaction and failure rolls back.
- Existing ASR segments, chunks and FAISS row IDs are not rewritten.
- Every migrated video receives one opaque `vid_<uuid>` canonical ID. The old
  path-derived ID remains only in `legacy_video_aliases` and repository lookup.
- New indexing uses an opaque ID; reopening the same registered path resumes the
  existing entry.
- Public repository records omit the legacy alias. Source path, display name,
  source generation/state and canonical identity are separate fields.
- Library DB, search index, cache, source and artifact roots can be configured
  independently while their defaults preserve the current layout.
- Share packages now carry the canonical public ID and a privacy-safe content
  digest. Same-ID/same-digest import is idempotent; same-ID/different-content is
  rejected. Source path and legacy alias remain excluded.

## Phase 1B — search correctness and shared service

- `SearchService` is used by WebUI and CLI.
- Exact text matching reads completed SQLite transcripts and works without
  FAISS, BGE-M3 or an embedding model.
- Normalization tiers are `base`, `kana`, and `cjk_compact`. They cover NFKC,
  case folding, Katakana/Hiragana equivalence and CJK-only whitespace removal.
- A normalized match is projected back to transcript character/time evidence.
  Matches may cross adjacent ASR segments when the gap is 0–2000 ms.
- Text and semantic result limits are independent. Text scores are absent;
  semantic thresholds apply only to semantic results.
- Semantic ordering and overlap suppression are deterministic. Semantic failure
  does not discard already-found text results.
- The transitional legacy snapshot exposes only completed ASR revisions and
  records an epoch plus `unknown` source token when no durable generation exists.

## Phase 1C — edit kernel

- `EditPlan`, `TimeRange`/`KeptRange`, `EditHistory`, and `TimelineMap` are pure
  domain types using integer milliseconds and half-open ranges.
- Overall and kept ranges are at least 100 ms. Adjacent exclusions within 1 ms
  merge, tiny kept islands are absorbed, and removing the entire plan is rejected.
- `semantic_signature` is stable and independent of UI-only state.
- `TimelineMap` maps both source-to-result and result-to-source across cuts.
- Legacy range plans and intuitive-editor state have compatibility adapters into
  the same kernel. Existing UI history remains in the adapter for Phase 2B, while
  the pure history behavior is characterized here.

## Verification mapping

- `tests/test_phase1_identity.py`: fresh/legacy migration, backup, rollback,
  stable public identity, legacy alias isolation.
- `tests/test_search_service.py`: FAISS-free exact matching, 命令, kana/CJK
  variants, cross-segment 壊した, evidence projection, deterministic semantic
  NMS and semantic degradation.
- `tests/test_edit_domain.py`: 100 ms invariants, merge/absorption, adapter
  differential behavior, Undo/Redo and bidirectional TimelineMap.
- Existing Phase 0 and adapter tests continue to run unchanged except for the
  expected Phase 1 canonical ID assertion in share import.

## Explicitly deferred

- Immutable transcript/index generations and atomic publication are Phase 2A.
- Moving the entire Gradio session/history owner into the application layer is
  Phase 2B.
- New desktop/web UI work, result-preview protocol replacement, SRT and export
  transaction redesign are later phases.
