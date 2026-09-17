from evaluation.used_phone_react_blind_model_reviewer_v1 import _validate_review


def test_blind_model_review_payload_is_strict() -> None:
    scores = {
        "constraintFidelity": 5, "evidenceDiscipline": 4,
        "taskProgression": 3, "usefulness": 2,
    }
    review = _validate_review({
        "candidateA": scores, "candidateB": scores,
        "overallPreference": "tie", "notes": "bounded",
    })
    assert review["overallPreference"] == "tie"
