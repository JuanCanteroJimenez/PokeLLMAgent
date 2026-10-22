"""Small cross-cutting utilities shared by the V4 engine."""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("battle_state")


def configure_file_logging() -> None:
    """Preserve the original battle-state warning log without duplicate handlers."""
    logger.setLevel(logging.WARNING)
    if not logger.handlers:
        handler = logging.FileHandler('battle_state_debug.log', mode='a')
        handler.setFormatter(logging.Formatter('%(asctime)s\n%(message)s\n'))
        logger.addHandler(handler)


def normalize_name(name: str) -> str:
    """Normalize a Pokémon or move name to a case-insensitive Showdown-style ID."""
    return "".join(filter(str.isalnum, name)).lower()


def json_safe(value: Any) -> Any:
    """Convert Pydantic models, enums and trace payloads to JSON-compatible data."""
    def fallback(obj: Any) -> Any:
        """Serialize a non-native JSON object without failing battle tracing."""
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if hasattr(obj, "name"):
            return str(obj.name)
        return str(obj)

    return json.loads(json.dumps(value, ensure_ascii=False, default=fallback))
