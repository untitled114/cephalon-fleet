"""Baro stock item-name resolution."""

import re
from collections.abc import Mapping

_STORE = "/StoreItems"
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _tail(path: str) -> str:
    return path.rsplit("/", 1)[-1]


class ItemNameIndex:
    def __init__(self, db_names: Mapping[str, str]) -> None:
        self._db = db_names
        seen: dict[str, str] = {}
        ambiguous: set[str] = set()
        for unique_name, name in db_names.items():
            tail = _tail(unique_name)
            if tail in seen:
                ambiguous.add(tail)
            seen[tail] = name
        self._by_tail = {t: n for t, n in seen.items() if t not in ambiguous}

    def _lookup(self, item_type: str, wfcd_names: Mapping[str, str]) -> tuple[int, str]:
        name = self._db.get(item_type.replace(_STORE, "", 1))
        if name is not None:
            return 1, name
        tail = _tail(item_type)
        name = self._by_tail.get(tail)
        if name is not None:
            return 2, name
        name = wfcd_names.get(item_type)
        if name is not None:
            return 3, name
        return 4, _CAMEL.sub(" ", tail)

    def resolve(self, item_type: str, wfcd_names: Mapping[str, str]) -> str:
        return self._lookup(item_type, wfcd_names)[1]

    def resolve_step(self, item_type: str, wfcd_names: Mapping[str, str]) -> int:
        return self._lookup(item_type, wfcd_names)[0]
