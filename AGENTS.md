# Codex agent routing

This repository uses model-routed subagents to keep the main thread focused and reduce expensive reasoning usage.

## Default roles

- Keep the root/coordinator on Sol for requirements, planning, decisions, integration, and final evaluation.
- Delegate bounded implementation, refactoring, repository search, test execution, formatting, and mechanical verification to `terra_worker`.
- Delegate architecture review, ambiguous requirements, difficult debugging, security/correctness review, and high-impact tradeoffs to `sol_reviewer` only when the extra depth is justified.

## Token and coordination policy

- Do not spawn a subagent for a trivial one-step task; delegation itself consumes tokens.
- Prefer one focused Terra worker over multiple parallel workers for write-heavy changes.
- Use parallel agents only for independent read-heavy work where elapsed-time savings outweigh coordination cost.
- Give each subagent a bounded task, the files in scope, constraints, and the expected summary format.
- Require concise summaries instead of raw logs or long exploration notes.
- The root agent owns the final decision, reviews every diff, and runs or verifies the relevant tests before reporting completion.
- Do not ask Sol to repeat work already proven by tests or a focused Terra result. Use Sol where judgment, ambiguity, or risk remains.

## Repository safety

- Preserve the separate-process Whisper indexing design in `index_video.py`; never load WhisperModel in a Gradio worker thread.
- Use `venv\Scripts\python.exe` for Python commands.
- Never inspect, upload, commit, or send `data/`, `video/`, `clips/`, `exports/`, `.env`, credentials, or private transcripts to external services.
- Do not commit, push, delete user data, change authentication, or make billing-related changes without explicit user authorization.
