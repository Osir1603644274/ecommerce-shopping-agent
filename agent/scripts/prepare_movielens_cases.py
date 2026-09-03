"""Prepare MovieLens small recommendation cases for the FunRec milestone."""

from recommendation.movielens import convert_movielens


if __name__ == "__main__":
    cases = convert_movielens()
    print(f"Generated {len(cases)} MovieLens recommendation cases.")