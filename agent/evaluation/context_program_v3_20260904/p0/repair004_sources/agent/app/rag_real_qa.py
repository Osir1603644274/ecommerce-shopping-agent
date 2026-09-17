import json
from pathlib import Path
from typing import Any

from .rag import YELP_EVAL_PATH, load_retrieval_cases
from .settings import settings


RAG_DIRECTORY = Path(settings.rag_data_dir)
YELP_REAL_REVIEW_QA_ANSWERS_PATH = (
    RAG_DIRECTORY / "eval" / "yelp_real_review_qa_answers.json"
)


def load_yelp_real_review_answers(
    answers_path: Path = YELP_REAL_REVIEW_QA_ANSWERS_PATH,
) -> dict[str, dict[str, Any]]:
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    return {answer["id"]: answer for answer in answers}


def load_yelp_real_review_qa_cases(
    retrieval_cases_path: Path = YELP_EVAL_PATH,
    answers_path: Path = YELP_REAL_REVIEW_QA_ANSWERS_PATH,
) -> list[dict[str, Any]]:
    """Merge real Yelp evidence cases with generated expected answers."""

    retrieval_cases = load_retrieval_cases(retrieval_cases_path)
    answers_by_id = load_yelp_real_review_answers(answers_path)
    retrieval_ids = {case["id"] for case in retrieval_cases}
    answer_ids = set(answers_by_id)
    if retrieval_ids != answer_ids:
        missing_answers = sorted(retrieval_ids - answer_ids)
        orphan_answers = sorted(answer_ids - retrieval_ids)
        raise ValueError(
            "Yelp real review QA answers do not match retrieval cases: "
            f"missingAnswers={missing_answers}, orphanAnswers={orphan_answers}"
        )

    qa_cases: list[dict[str, Any]] = []
    for case in retrieval_cases:
        answer = answers_by_id[case["id"]]
        qa_cases.append(
            {
                **case,
                "expectedAnswer": answer["expectedAnswer"],
                "answerSource": answer.get(
                    "answerSource",
                    "generated_from_real_yelp_evidence",
                ),
            }
        )
    return qa_cases
