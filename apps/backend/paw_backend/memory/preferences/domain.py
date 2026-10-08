"""Enums of the Inferred Preference flow that the schema repeats (PAW-044)."""

from enum import StrEnum


class Resolution(StrEnum):
    """The person's answer to a held candidate."""

    CONFIRMED = "confirmed"  # written as a confirmed memory
    REJECTED = "rejected"  # [保存しない]
