from collections.abc import Sequence

__all__ = ["exclusion_reason"]


def exclusion_reason(title: str, keywords: Sequence[str]) -> str | None:
    folded = title.casefold()
    for keyword in keywords:
        needle = keyword.casefold()
        if needle and needle in folded:
            return keyword
    return None
