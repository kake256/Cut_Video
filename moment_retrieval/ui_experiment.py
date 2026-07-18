from __future__ import annotations

import json
import statistics
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


SCENARIOS = ("text_search", "semantic_search", "manual_clip")
UI_VARIANTS = ("gradio", "candidate")


@dataclass(frozen=True)
class ExperimentRun:
    run_id: str
    scenario: str
    ui_variant: str
    cold: bool
    duration_ms: int
    action_count: int
    error_count: int
    accepted: bool
    recorded_at: int


class UIExperimentRecorder:
    """Anonymous local-only metrics; queries, paths and video IDs are forbidden."""
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def record(
        self, scenario: str, ui_variant: str, cold: bool, duration_ms: int,
        action_count: int, error_count: int, accepted: bool,
    ) -> ExperimentRun:
        if scenario not in SCENARIOS or ui_variant not in UI_VARIANTS:
            raise ValueError("unknown experiment scenario or UI")
        values = (duration_ms, action_count, error_count)
        if any(int(value) < 0 for value in values):
            raise ValueError("experiment metrics must be non-negative")
        run = ExperimentRun(
            f"run_{uuid.uuid4().hex}", scenario, ui_variant, bool(cold),
            int(duration_ms), int(action_count), int(error_count), bool(accepted),
            int(time.time()),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(asdict(run), separators=(",", ":")) + "\n")
        return run

    def read(self) -> list[ExperimentRun]:
        if not self.path.exists():
            return []
        runs = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                payload = json.loads(line)
                runs.append(ExperimentRun(**payload))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return runs


def compare_ui_runs(runs: list[ExperimentRun], minimum_runs: int = 10) -> dict:
    cells = {}
    for scenario in SCENARIOS:
        for variant in UI_VARIANTS:
            for cold in (False, True):
                selected = [run for run in runs if run.scenario == scenario
                            and run.ui_variant == variant and run.cold == cold]
                cells[(scenario, variant, cold)] = selected
    insufficient = [
        {"scenario": scenario, "ui_variant": variant, "cold": cold, "count": len(items)}
        for (scenario, variant, cold), items in cells.items() if len(items) < minimum_runs
    ]
    summary = {}
    for scenario in SCENARIOS:
        summary[scenario] = {}
        for variant in UI_VARIANTS:
            summary[scenario][variant] = {}
            for cold in (False, True):
                items = cells[(scenario, variant, cold)]
                summary[scenario][variant]["cold" if cold else "warm"] = {
                    "count": len(items),
                    "median_duration_ms": statistics.median(run.duration_ms for run in items) if items else None,
                    "median_actions": statistics.median(run.action_count for run in items) if items else None,
                    "errors": sum(run.error_count for run in items),
                    "acceptance_failures": sum(not run.accepted for run in items),
                }
    adopted = False
    ratios = {}
    if not insufficient:
        for scenario in SCENARIOS:
            for condition in ("warm", "cold"):
                baseline = summary[scenario]["gradio"][condition]["median_duration_ms"]
                candidate = summary[scenario]["candidate"][condition]["median_duration_ms"]
                ratios[f"{scenario}:{condition}"] = candidate / baseline if baseline else float("inf")
        adopted = (
            ratios["text_search:warm"] <= 0.85
            and ratios["text_search:cold"] <= 0.85
            and all(ratio <= 1.05 for ratio in ratios.values())
            and all(summary[s][v][c]["errors"] == 0
                    and summary[s][v][c]["acceptance_failures"] == 0
                    for s in SCENARIOS for v in UI_VARIANTS for c in ("warm", "cold"))
        )
    return {
        "ready": not insufficient,
        "adopt_candidate": adopted,
        "insufficient": insufficient,
        "duration_ratios": ratios,
        "summary": summary,
    }
