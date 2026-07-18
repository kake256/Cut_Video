#!/usr/bin/env python
import json

from moment_retrieval import config
from moment_retrieval.ui_experiment import UIExperimentRecorder, compare_ui_runs


def main() -> int:
    recorder = UIExperimentRecorder(config.CACHE_ROOT / "ui-experiment-v1.jsonl")
    print(json.dumps(compare_ui_runs(recorder.read()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
