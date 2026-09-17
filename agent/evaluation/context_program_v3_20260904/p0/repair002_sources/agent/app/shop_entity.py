from typing import Any


def resolve_unique_shop(
    shops: list[dict[str, Any]],
    requested_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve one merchant without guessing between ambiguous candidates."""

    normalized_name = requested_name.strip().casefold()
    exact_matches = [
        shop
        for shop in shops
        if str(shop.get("name", "")).strip().casefold() == normalized_name
    ]
    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(exact_matches) > 1:
        return None, "multiple_exact_matches"
    if len(shops) == 1:
        return shops[0], None
    if not shops:
        return None, "not_found"
    return None, "ambiguous_partial_matches"
