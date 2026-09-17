from recommendation.yelp_recommendation import build_yelp_recommendation_cases
from scripts.evaluate_yelp_strict_hybrid import split_cases


def test_builds_yelp_recommendation_cases_from_positive_history():
    businesses = [
        {
            "business_id": "biz-1",
            "name": "Alpha Cafe",
            "city": "Philadelphia",
            "is_open": 1,
            "review_count": 10,
            "categories": "Coffee & Tea, Cafes",
            "address": "A Road",
            "state": "PA",
            "longitude": -75.1,
            "latitude": 39.9,
        },
        {
            "business_id": "biz-2",
            "name": "Beta Cafe",
            "city": "Philadelphia",
            "is_open": 1,
            "review_count": 10,
            "categories": "Coffee & Tea, Cafes",
            "address": "B Road",
            "state": "PA",
            "longitude": -75.2,
            "latitude": 39.9,
        },
        {
            "business_id": "biz-3",
            "name": "Gamma Pizza",
            "city": "Philadelphia",
            "is_open": 1,
            "review_count": 10,
            "categories": "Restaurants, Pizza",
            "address": "C Road",
            "state": "PA",
            "longitude": -75.3,
            "latitude": 39.9,
        },
    ]
    reviews = [
        {
            "review_id": "r1",
            "user_id": "u1",
            "business_id": "biz-1",
            "stars": 5,
            "date": "2026-01-01 00:00:00",
        },
        {
            "review_id": "r2",
            "user_id": "u1",
            "business_id": "biz-2",
            "stars": 4,
            "date": "2026-01-02 00:00:00",
        },
        {
            "review_id": "r3",
            "user_id": "u1",
            "business_id": "biz-3",
            "stars": 5,
            "date": "2026-01-03 00:00:00",
        },
        {
            "review_id": "r4",
            "user_id": "u2",
            "business_id": "biz-1",
            "stars": 3,
            "date": "2026-01-01 00:00:00",
        },
    ]

    result = build_yelp_recommendation_cases(
        businesses,
        reviews,
        min_positive_interactions=3,
        min_history_interactions=2,
        train_ratio=0.67,
        max_cases=10,
    )

    assert result["metadata"]["candidateItemCount"] == 3
    assert result["metadata"]["caseCount"] == 1
    case = result["cases"][0]
    assert case["userId"] == "yelp-user-u1"
    assert [item["itemName"] for item in case["history"]] == ["Alpha Cafe", "Beta Cafe"]
    assert [item["itemName"] for item in case["targetInteractions"]] == ["Gamma Pizza"]
    assert case["relevantItemIds"] == [100003]


def test_split_cases_is_deterministic_and_preserves_cases():
    cases = [{"caseId": f"case-{index}", "userId": f"user-{index}"} for index in range(30)]

    first = split_cases(cases, seed=7, train_percent=60, validation_percent=20)
    second = split_cases(cases, seed=7, train_percent=60, validation_percent=20)

    assert first["counts"] == second["counts"]
    assert first["train"] == second["train"]
    assert first["validation"] == second["validation"]
    assert first["test"] == second["test"]
    assert first["counts"]["total"] == 30
    assert (
        first["counts"]["train"]
        + first["counts"]["validation"]
        + first["counts"]["test"]
    ) == 30
