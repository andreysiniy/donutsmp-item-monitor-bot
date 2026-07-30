import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class IconEntry:
    item_id: str
    display_name: str
    path: Path


class IconService:
    def __init__(self, manifest_path: Path, assets_dir: Path) -> None:
        self.manifest_path = manifest_path
        self.assets_dir = assets_dir
        self._entries: dict[str, IconEntry] = {}
        self._missing_path = assets_dir / "icons" / "missingno.png"

    def load(self) -> None:
        with self.manifest_path.open("r", encoding="utf-8") as stream:
            manifest: dict[str, Any] = json.load(stream)

        entries: dict[str, IconEntry] = {}
        for section in ("blocks", "items"):
            raw_entries = manifest.get(section, [])
            if not isinstance(raw_entries, list):
                continue
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                item_id = str(raw.get("id", "")).strip()
                icon = str(raw.get("icon", "")).strip()
                if not item_id or not icon:
                    continue
                path = self.assets_dir / Path(icon)
                candidate = IconEntry(
                    item_id=item_id,
                    display_name=str(raw.get("display_name") or item_id),
                    path=path,
                )
                current = entries.get(item_id)
                if path.is_file() and (current is None or not current.path.is_file()):
                    entries[item_id] = candidate
                elif current is None:
                    entries[item_id] = candidate

        self._entries = entries

    def icon_path(self, item_id: str) -> Path:
        entry = self._entries.get(item_id)
        if entry and entry.path.is_file():
            return entry.path
        return self._missing_path

    def display_name(self, item_id: str) -> str:
        entry = self._entries.get(item_id)
        return entry.display_name if entry else item_id

    def contains(self, item_id: str) -> bool:
        return item_id in self._entries

    def autocomplete(self, query: str, limit: int = 25) -> list[IconEntry]:
        needle = query.casefold().strip()
        matches = [
            entry
            for entry in self._entries.values()
            if not needle
            or needle in entry.item_id.casefold()
            or needle in entry.display_name.casefold()
        ]
        matches.sort(
            key=lambda entry: (
                not entry.item_id.casefold().startswith(needle),
                not entry.display_name.casefold().startswith(needle),
                entry.display_name.casefold(),
            )
        )
        return matches[:limit]

    @property
    def size(self) -> int:
        return len(self._entries)

