"""V2 preregistration binding for the frozen V1 semantic scorer."""

from __future__ import annotations

from pathlib import Path

from agent.evaluation import shopping_mission_baseline_scorer_v1 as v1
from agent.evaluation.shopping_mission_baseline_runner_v2 import PREREGISTRATION_PATH


def score_run(run_dir: Path):
    original = v1.PREREGISTRATION_PATH
    v1.PREREGISTRATION_PATH = PREREGISTRATION_PATH
    try:
        return v1.score_run(run_dir)
    finally:
        v1.PREREGISTRATION_PATH = original


def write_score_new(run_dir: Path, report):
    return v1.write_score_new(run_dir, report)
