# Phase 2–5 implementation record

This record describes what is implemented on top of Phase 1 and what still
requires real user measurements. It does not mark the candidate UI as adopted.

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
- Writer ownership uses PID plus a random process token, heartbeat and expiry.
  Reader leases protect an open generation. Stale leases and unreferenced
  generation directories can be recovered.
- Indexing checks a sampled private source fingerprint after ASR and embedding,
  before publication. Whisper remains in the existing subprocess.

Known migration boundary: the legacy `force` re-index command still starts from
the compatibility storage rows. Publication prevents mixed search requests, but
fully retaining the previous active ASR while a forced replacement is being
transcribed requires moving the remaining legacy indexing writer to revision-
specific draft rows. That writer migration is deliberately not hidden by a UI
claim and should be the next indexing hardening item.

## Phase 2A.2 — share v2 and relinking

- New exports use share schema v2 and include canonical public ID, content
  digest, embedding revision, chunk settings and an opaque package source token.
- The previous redacted package and the original legacy shape remain importable;
  new export no longer writes them.
- Same public ID plus same digest is idempotent. Different content is rejected.
- Source path, old alias, private fingerprint and original filename are absent.
- The share tab can select a local source for an unlinked import. Duration is
  validated before a new local source generation and private fingerprint are
  registered. A mismatch leaves the shared record unlinked.

## Phase 2B — save and artifact transaction

- `DocumentRepository` issues monotonic save tickets containing document,
  source generation, sequence, immutable plan snapshot and plan hash.
- Reverse completion, editing during save and closed documents cannot overwrite
  a newer clean reference.
- Effective Export Plan applies padding only to the outer source boundaries and
  owns the artifact TimelineMap.
- ffmpeg supports a cancellation event without unread-pipe deadlocks.
- Video, optional SRT and manifest are prepared in a job directory on the output
  filesystem. Source fingerprint is checked again before publish.
- The manifest is the commit marker. A publish journal allows stale staging and
  partially exposed files to be rolled back after a crash.
- The intuitive editor uses this path for real local sources; the old callback
  remains only as a compatibility fallback for synthetic/legacy adapters.

## Phase 2C — SRT sidecar

- Transcript parsing validates integer-millisecond segment/word timestamps. Any
  invalid word list falls back to the real segment span rather than interpolation.
- Complete word groups are mapped through the artifact TimelineMap. A word cut
  internally is omitted with a warning. Segment fallback is emitted only when
  the whole segment is contained in one kept range.
- Cues use UTF-8 `HH:MM:SS,mmm`, stable ordering and output-duration tolerance.
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

## Phase 5 — candidate UI experiment

- The current intuitive editor is the candidate single-backend adapter: search,
  preview, timestamped transcript, overview/zoom timelines and fixed save bar.
- An opt-in comparison panel records only scenario, UI variant, warm/cold flag,
  elapsed time, action/error counts and acceptance. Query, video ID, filename and
  path are not accepted by the recorder.
- `scripts/report_ui_experiment.py` reports medians and applies the documented
  gate: at least ten runs in every warm/cold × scenario × UI cell, main scenario
  ratio at most 0.85, all other ratios at most 1.05, and zero acceptance errors.

Candidate adoption remains **undecided** until real runs meet this gate. The
existing Gradio editor remains available and is not replaced by code alone.
