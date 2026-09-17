from __future__ import annotations

from evaluation.post_training_task_state_v1_20260904.build_dataset import build_split
from evaluation.post_training_task_state_v1_20260904.common import (
    canonical_json,
    parse_json_object,
    render_messages,
    semantic_effect,
    validate_arguments,
)
from evaluation.post_training_task_state_v1_20260904.compare_and_adjudicate import (
    exact_two_sided_binomial_p,
)


def test_generated_gold_passes_production_pure_validator() -> None:
    rows = build_split("test", 22)
    assert len(rows) == 22
    for row in rows:
        payload, _ = validate_arguments(row, row["targetArguments"])
        assert semantic_effect(row, payload)["status"] in {
            "ready",
            "collecting_information",
        }


def test_split_templates_and_prompts_are_disjoint() -> None:
    train = build_split("train", 33)
    dev = build_split("dev", 33)
    test = build_split("test", 33)
    prompt_sets = [
        {canonical_json(render_messages(row)) for row in rows}
        for rows in (train, dev, test)
    ]
    assert not prompt_sets[0] & prompt_sets[1]
    assert not prompt_sets[0] & prompt_sets[2]
    assert not prompt_sets[1] & prompt_sets[2]
    template_sets = [
        {row["templateFamily"] for row in rows}
        for rows in (train, dev, test)
    ]
    assert not template_sets[0] & template_sets[1]
    assert not template_sets[0] & template_sets[2]
    assert not template_sets[1] & template_sets[2]


def test_json_parser_distinguishes_strict_from_recovered_json() -> None:
    value, strict, error = parse_json_object('{"status":"ready"}')
    assert value == {"status": "ready"}
    assert strict is True
    assert error is None
    value, strict, error = parse_json_object('```json\n{"status":"ready"}\n```')
    assert value == {"status": "ready"}
    assert strict is False
    assert error is None


def test_exact_mcnemar_binomial_is_two_sided() -> None:
    assert exact_two_sided_binomial_p(0, 0) == 1.0
    assert exact_two_sided_binomial_p(10, 0) == 2 / 1024
    assert exact_two_sided_binomial_p(5, 5) == 1.0

