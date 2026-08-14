from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROFILE_STRING_LIMITS = {
    "title": 300,
    "author": 200,
    "genre": 300,
    "narrative_style": 2_000,
    "summary": 12_000,
}
PROFILE_ARRAY_LIMITS = {
    "main_characters": (200, 300),
    "places": (200, 300),
    "factions": (200, 300),
    "terms": (500, 300),
    "legitimate_structures": (200, 500),
    "evidence": (200, 2_000),
}
PROFILE_KEYS = frozenset(
    {*PROFILE_STRING_LIMITS, *PROFILE_ARRAY_LIMITS, "rename_verified"}
)
MAX_PROFILE_BYTES = 256 * 1024


def _reject_constant(value: str) -> None:
    raise ValueError(f"book profile contains non-finite JSON number: {value}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"book profile contains duplicate key: {key}")
        result[key] = value
    return result


def validate_book_profile(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("book profile root must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError("book profile keys must be strings")
    unknown = sorted(set(value) - PROFILE_KEYS)
    if unknown:
        raise ValueError(
            "book profile has unsupported key(s): " + ", ".join(unknown)
        )

    normalized: dict[str, Any] = {}
    for key, limit in PROFILE_STRING_LIMITS.items():
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str):
            raise ValueError(f"book profile {key} must be a string")
        if len(item) > limit:
            raise ValueError(
                f"book profile {key} exceeds the {limit}-code-point limit"
            )
        normalized[key] = item

    for key, (item_limit, text_limit) in PROFILE_ARRAY_LIMITS.items():
        if key not in value:
            continue
        items = value[key]
        if not isinstance(items, list):
            raise ValueError(f"book profile {key} must be an array of strings")
        if len(items) > item_limit:
            raise ValueError(
                f"book profile {key} exceeds the {item_limit}-item limit"
            )
        normalized_items: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, str):
                raise ValueError(
                    f"book profile {key}[{index}] must be a string"
                )
            if len(item) > text_limit:
                raise ValueError(
                    f"book profile {key}[{index}] exceeds the "
                    f"{text_limit}-code-point limit"
                )
            normalized_items.append(item)
        normalized[key] = normalized_items

    if "rename_verified" in value:
        rename_verified = value["rename_verified"]
        if not isinstance(rename_verified, bool):
            raise ValueError("book profile rename_verified must be boolean")
        normalized["rename_verified"] = rename_verified
    return normalized


def parse_book_profile_bytes(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_PROFILE_BYTES:
        raise ValueError(
            f"book profile exceeds the {MAX_PROFILE_BYTES}-byte size limit"
        )
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
        )
    except UnicodeError as exc:
        raise ValueError("book profile must be strict UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"book profile is invalid JSON: {exc}") from exc
    return validate_book_profile(value)


def load_book_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError("book profile path must be a regular file")
    return parse_book_profile_bytes(path.read_bytes())


def protection_terms(profile: dict[str, Any]) -> set[str]:
    validated = validate_book_profile(profile)
    values: list[str] = []
    for key in ("title", "author"):
        item = validated.get(key)
        if isinstance(item, str):
            values.append(item)
    for key in (
        "main_characters",
        "places",
        "factions",
        "terms",
        "legitimate_structures",
    ):
        items = validated.get(key, [])
        values.extend(items if isinstance(items, list) else [])
    return {item.strip() for item in values if item.strip()}


def verified_title(profile: dict[str, Any]) -> str:
    validated = validate_book_profile(profile)
    title = validated.get("title")
    if validated.get("rename_verified") is True and isinstance(title, str):
        return title.strip()
    return ""
