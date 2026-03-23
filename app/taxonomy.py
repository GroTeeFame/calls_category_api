from __future__ import annotations

"""Taxonomy loading and prompt formatting helpers."""

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from app.errors import ProcessingError

logger = logging.getLogger("calls_category_api.taxonomy")


class Category(BaseModel):
    """Single classification category definition."""

    key: str
    name: str
    definition: str
    caller_types: list[Literal["NATURAL", "JURIDICAL"]]
    examples: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    """Collection of categories and convenience methods for prompt generation."""

    version: str
    categories: list[Category]

    @property
    def keys(self) -> set[str]:
        """Return all category keys in the taxonomy."""
        return {category.key for category in self.categories}

    def keys_for_caller_type(self, caller_type: Literal["NATURAL", "JURIDICAL", "UNKNOWN"]) -> set[str]:
        """Return allowed category keys for a caller type.

        For `UNKNOWN`, the full taxonomy is considered valid.
        """
        if caller_type == "UNKNOWN":
            return self.keys
        return {
            category.key
            for category in self.categories
            if caller_type in category.caller_types
        }

    def prompt_block_for_caller_type(self, caller_type: Literal["NATURAL", "JURIDICAL"]) -> str:
        """Build prompt text listing categories for one caller type."""
        lines: list[str] = []
        for category in self.categories:
            if caller_type not in category.caller_types:
                continue
            examples = ", ".join(category.examples[:3]) if category.examples else "no examples"
            lines.append(
                f"- {category.key}: {category.definition} (name: {category.name}; examples: {examples})"
            )
        return "\n".join(lines)

    def prompt_block(self) -> str:
        """Build prompt text listing all categories."""
        lines: list[str] = []
        for category in self.categories:
            examples = ", ".join(category.examples[:3]) if category.examples else "no examples"
            lines.append(
                f"- {category.key}: {category.definition} (name: {category.name}; examples: {examples})"
            )
        return "\n".join(lines)


def load_taxonomy(path: Path) -> Taxonomy:
    """Load and validate taxonomy YAML from disk."""
    logger.info("taxonomy.load_taxonomy started path=%s", path)
    if not path.exists():
        raise ProcessingError("taxonomy_not_found", f"Taxonomy file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    try:
        taxonomy = Taxonomy.model_validate(data)
    except Exception as exc:  # pragma: no cover - pydantic-specific branch
        raise ProcessingError("taxonomy_invalid", f"Invalid taxonomy format: {exc}") from exc

    if not taxonomy.categories:
        raise ProcessingError("taxonomy_invalid", "Taxonomy must contain at least one category")

    keys = [category.key for category in taxonomy.categories]
    if len(keys) != len(set(keys)):
        raise ProcessingError("taxonomy_invalid", "Category keys in taxonomy must be unique")

    missing_types = [category.key for category in taxonomy.categories if not category.caller_types]
    if missing_types:
        raise ProcessingError(
            "taxonomy_invalid",
            "Each category must include at least one caller type",
        )

    logger.info(
        "taxonomy.load_taxonomy completed version=%s categories=%s natural=%s juridical=%s",
        taxonomy.version,
        len(taxonomy.categories),
        len(taxonomy.keys_for_caller_type("NATURAL")),
        len(taxonomy.keys_for_caller_type("JURIDICAL")),
    )
    return taxonomy
