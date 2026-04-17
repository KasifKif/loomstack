"""Generic JSON-backed key/value store for Weaver runtime data."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from pathlib import Path

import aiofiles
import structlog
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


class JsonStore(Generic[T]):
    """Persist a collection of Pydantic models as a single JSON file.

    The JSON file contains a mapping of ``id -> model_dict``.  Writes are
    atomic: the new content is written to a ``.tmp`` sibling, then renamed
    over the real file via ``os.replace()``.
    """

    def __init__(self, data_dir: Path, filename: str, model_type: type[T]) -> None:
        self._data_dir = data_dir
        self._path = data_dir / filename
        self._tmp_path = data_dir / f"{filename}.tmp"
        self._model_type = model_type

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _read_raw(self) -> dict[str, dict]:  # type: ignore[type-arg]
        if not self._path.exists():
            return {}
        async with aiofiles.open(self._path, encoding="utf-8") as fh:
            text = await fh.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("store_corrupt", path=str(self._path))
            return {}
        if not isinstance(data, dict):
            logger.warning("store_not_a_dict", path=str(self._path))
            return {}
        return data  # type: ignore[return-value]

    async def _write_raw(self, data: dict[str, dict]) -> None:  # type: ignore[type-arg]
        self._data_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, indent=2, ensure_ascii=False)
        async with aiofiles.open(self._tmp_path, "w", encoding="utf-8") as fh:
            await fh.write(text)
        os.replace(self._tmp_path, self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load_all(self) -> dict[str, T]:
        """Return all stored items keyed by id."""
        raw = await self._read_raw()
        result: dict[str, T] = {}
        for item_id, item_dict in raw.items():
            try:
                result[item_id] = self._model_type.model_validate(item_dict)
            except Exception:
                logger.warning("store_invalid_item", id=item_id, path=str(self._path))
        return result

    async def get(self, item_id: str) -> T | None:
        """Return a single item by id, or None if not found."""
        raw = await self._read_raw()
        item_dict = raw.get(item_id)
        if item_dict is None:
            return None
        try:
            return self._model_type.model_validate(item_dict)
        except Exception:
            logger.warning("store_invalid_item", id=item_id, path=str(self._path))
            return None

    async def upsert(self, item_id: str, item: T) -> T:
        """Create or replace an item.  Returns the stored item."""
        raw = await self._read_raw()
        raw[item_id] = item.model_dump()
        await self._write_raw(raw)
        return item

    async def save_all(self, items: dict[str, T]) -> None:
        """Atomically replace the entire store contents."""
        raw = {item_id: item.model_dump() for item_id, item in items.items()}
        await self._write_raw(raw)

    async def delete(self, item_id: str) -> bool:
        """Remove an item.  Returns True if it existed, False otherwise."""
        raw = await self._read_raw()
        if item_id not in raw:
            return False
        del raw[item_id]
        await self._write_raw(raw)
        return True
