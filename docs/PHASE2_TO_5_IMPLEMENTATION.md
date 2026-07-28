# Phase 2–5 implementation record

This record describes what is implemented on top of Phase 1 and what still
requires real user measurements. The intuitive editor has now been adopted as
the primary UI under the label `検索・編集・切り抜き`.

## Phase 2A.1 — publication and recovery

- Schema v4 adds sources, transcript revisions, active transcript pointers,
  immutable publications/members, immutable search generations, job records and
  reader leases. Legacy rows are described without re-ASR or FAISS ID rewriting.
- Index publication uses an expected-publication compare-and-swap. Completed
  text can be published as text-only before semantic vectors are ready.
- Full generations contain `chunks.sqlite`, `vectors.faiss` and a checksummed
  manifest in a generation-specific directory. Staging is atomically renamed.
- Search pins one publication and its member revisions for the complete request.
  Semantic retrieval uses the same covered revisions and immutable vector file.
- Every indexing, import and relink writer acquires the same cross-process writer
  lease. Ownership uses a job ID, PID, random process token, heartbeat and
  expiry; a live writer of any type blocks every other writer type.
- Forced re-indexing writes a draft transcript revision, draft segments and
  draft chunks without changing the active revision. It builds a prospective
  immutable FAISS generation containing the exact expected chunk IDs and
  verifies count, dimension and every reconstructable vector before publication.
- Publication renames the verified generation and then performs the writer-owner
  check, source-fingerprint check and expected-publication CAS in one
  `BEGIN IMMEDIATE` transaction. Only that transaction activates the replacement
  revision and generation. A CAS failure leaves the prior publication, active
  rows and compatibility index untouched.
- The legacy compatibility `text.index` copy is refreshed only after the
  immutable publication commits. Failure to refresh that copy is reported as a
  warning and does not invalidate the committed generation.
- A recorded private source fingerprint is checked before ASR, after embedding,
  after the exact draft is built and immediately before publication. A source
  first seen by an older schema is backfilled conditionally during the same CAS.
  Whisper remains in the existing subprocess.

Reader-lease rows are issued and released, but physical garbage collection of
superseded generations remains **deferred**. Reader heartbeat and atomic
lease-acquisition hardening must be completed before that GC is enabled; current
publication correctness does not depend on deleting an old generation after a
lease expires. Cleanup is limited to safe orphan/staging recovery.

## Phase 2A.2 — share v2 and relinking

- New exports use share schema v2 and include canonical public ID, content
  digest, embedding revision, chunk settings and an opaque package source token.
- The previous redacted package and the original legacy shape remain importable;
  new export no longer writes them.
- Same public ID plus same digest is idempotent. Different content is rejected.
- Source path, old alias, private fingerprint and original filename are absent.
- The share tab can select a local source for an unlinked import. Duration is
  validated before the local locator, private fingerprint and linked status are
  updated. Relinking preserves the imported source generation, transcript
  revisions and publication identity; it does not rewrite historical revision
  ownership. A mismatch leaves the shared record unlinked.

## Phase 2B — save and artifact transaction

- `DocumentRepository` issues monotonic save tickets containing document,
  source generation, sequence, immutable plan snapshot and plan hash.
- Reverse completion, editing during save and closed documents cannot overwrite
  a newer clean reference.
- Effective Export Plan applies padding only to the outer source boundaries and
  owns the artifact TimelineMap.
- ffmpeg supports a cancellation event without unread-pipe deadlocks.
- Video, optional SRT and manifest are prepared in a job directory on the output
  filesystem. The expected source fingerprint is carried by the editor document
  and save ticket, checked before staging starts, and checked again immediately
  before any public output is replaced.
- Staged video validation uses ffprobe's video-stream duration (with format
  duration as a fallback), stream time base and frame-rate-aware tolerances.
  Precise multi-range output allows one container/frame tolerance per kept range;
  fast mode records duration drift as a manifest warning.
- The manifest is the commit marker. A publish journal allows stale staging and
  partially exposed files to be rolled back after a crash.
- Manifest duration fields describe the plan and measured artifact without
  exposing a source path or private fingerprint.
- The intuitive editor uses this path for real local sources; the old callback
  remains only as a compatibility fallback for synthetic/legacy adapters.

## Phase 2C — SRT sidecar

- Transcript parsing validates integer-millisecond segment/word timestamps. Any
  invalid word list falls back to the real segment span rather than interpolation.
- Complete word groups are mapped through the artifact TimelineMap. A word cut
  internally is omitted with a warning. Segment fallback is emitted only when
  the whole segment is contained in one kept range.
- Cues use UTF-8 `HH:MM:SS,mmm`, stable ordering and one-frame output-duration
  tolerance. Requesting an SRT forces precise video encoding so that cue timing
  and the committed video share the same precise TimelineMap.
- The intuitive save bar and versioned CLI can request SRT. Video, SRT, warnings
  and commit manifest share one artifact transaction.

## Phase 3 — application session migration

- `EditorDocument`, plan-only `EditHistory`, revision checks, bounded command ID
  idempotency, Undo/Redo, dirty/clean ownership and save sequencing now exist in
  the application layer.
- The intuitive adapter opens one document and synchronizes semantic plan changes
  to it. Saves use the application-owned snapshot.
- Returning from result preview maps `currentTime` through `TimelineMap` to a
  Source position instead of treating result seconds as source seconds.

The Gradio state still serializes view state and compatibility undo stacks during
the migration. Removing those final mirrored fields from `app.py` is a cleanup,
not a prerequisite for the new save/session correctness path, but the strict
roadmap exit condition (“no history push/pop in app.py”) is not claimed yet.

## Phase 4 — versioned CLI contract

- `video_tool.py search` and `video_tool.py clip` expose JSON schema v1.
- Search returns separate text/semantic arrays, publication ID and structured
  semantic-unavailable warnings.
- Clip consumes an integer-millisecond EditPlan JSON, supports multi-range,
  precise export, optional SRT and the same artifact transaction as WebUI.
- Success and failure envelopes are versioned and machine-readable. Existing
  `search_video.py` and `cut_clip.py` remain compatibility wrappers.

## Phase 5 — adopted primary Web UI

- The intuitive editor is the primary single-backend adapter: search,
  preview, timestamped transcript, overview/zoom timelines and fixed save bar.
- The former `検索・切り抜き` screen is not shown in the normal startup
  navigation. It remains an opt-in migration fallback only, enabled with
  `CUT_VIDEO_ENABLE_LEGACY_UI=1`; new product work is not duplicated there.
- The redundant persistent guide above the detail-edit timeline is removed.
  Controls, compact status text and contextual help carry that instruction
  without pushing the zoom timeline below the initial viewport.
- The workspace follows Preview → Search → Transcript. At widths from 761 to
  1180 px, preview and search remain together and transcript moves to the next
  row; at 760 px and below the workspace, header, boundary controls, timelines
  and save controls stack vertically in the same semantic order.
- Transcript retrieval and its visible range use the exact zoom viewport bounds
  (up to the existing 600-second viewport limit); there is no independent
  90-second transcript window that can drift away from the timeline display.
- A normal boundary/tool/nudge command no longer re-queries and replaces the
  complete transcript HTML. A compact projection updates transcript decorations;
  full transcript rendering remains limited to viewport/source/result changes.
- Range queries read only the video's active transcript revision. Schema v5 adds
  `(video_id, transcript_revision, start_sec)` so repeated ASR revisions do not
  make the visible-range query scan superseded transcript rows.
- Startup uses cached thumbnails only; missing thumbnails are generated by
  explicit picker-list actions instead of one synchronous ffmpeg process per
  video. Visible cards send stable video IDs directly, replacing the duplicated
  hidden Gallery and its fragile index-order proxy.
- Preview state is labelled explicitly as Source or Result. Only the action that
  is valid for the displayed state is visible, reducing accidental edits against
  result-relative time.
- Search is staged: normalized exact/text matches are yielded immediately, then
  semantic results arrive on a separate concurrency lane. Each request carries a
  session token and pinned publication; superseded or stale semantic completion
  cannot overwrite a newer result. Selecting a result row is the explicit action
  that opens it in the editor.
- The large CSS and editor JavaScript payloads have moved from `app.py` to
  required UTF-8 assets under `assets/`, loaded through
  `moment_retrieval.ui_assets`. Missing or invalid assets fail at startup instead
  of silently degrading the editor. Search staging is likewise isolated in
  `moment_retrieval.staged_search`.
- Gradio is pinned to `6.19.0` in `requirements.txt` so the tested component and
  browser-event behavior does not drift with an unconstrained dependency update.
- An opt-in comparison panel records only scenario, UI variant, warm/cold flag,
  elapsed time, action/error counts and acceptance. Query, video ID, filename and
  path are not accepted by the recorder.
- `scripts/report_ui_experiment.py` reports medians and applies the documented
  gate: at least ten runs in every warm/cold × scenario × UI cell, main scenario
  ratio at most 0.85, all other ratios at most 1.05, and zero acceptance errors.

The primary-UI decision is complete. Experiment recording remains useful for
regression measurement and future framework comparisons, but it no longer keeps
the legacy screen in the default navigation. The legacy screen is a temporary,
explicitly enabled fallback rather than a second product surface.
