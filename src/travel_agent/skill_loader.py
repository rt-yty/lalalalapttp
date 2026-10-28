"""Load only trusted, installed skill resources; never arbitrary paths."""
from __future__ import annotations

import hashlib
import re
from importlib.resources import files
from pathlib import Path

import yaml


class SkillRegistry:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else Path(str(files("travel_agent").joinpath("skills")))
        self.root = self.root.resolve(strict=True)
        self._catalog: dict[str, dict] = {}
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir():
                continue
            path = (directory / "SKILL.md").resolve(strict=True)
            if not path.is_relative_to(self.root) or not directory.resolve().is_relative_to(self.root):
                raise ValueError("Skill resources must remain inside the trusted package directory")
            metadata, _ = self._parse(path.read_text(encoding="utf-8"))
            name, description = metadata.get("name"), metadata.get("description")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                raise ValueError("Skill name is missing or invalid")
            if name != directory.name or name in self._catalog:
                raise ValueError("Skill name must be unique and match its directory")
            if not isinstance(description, str) or not description.strip():
                raise ValueError("Skill description is missing")
            self._catalog[name] = {"name": name, "description": description, "path": path}

    @staticmethod
    def _parse(text: str) -> tuple[dict, str]:
        match = re.match(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", text, re.DOTALL)
        if not match:
            raise ValueError("Skill must have YAML frontmatter")
        metadata = yaml.safe_load(match[1])
        if not isinstance(metadata, dict) or not match[2].strip():
            raise ValueError("Skill metadata/body is invalid")
        return metadata, match[2].strip()

    def catalog(self) -> list[dict]:
        return [{"name": item["name"], "description": item["description"]} for item in self._catalog.values()]

    def load(self, name: str) -> dict:
        if name not in self._catalog:
            raise ValueError("Unknown skill name")
        path = self._catalog[name]["path"].resolve(strict=True)
        if not path.is_relative_to(self.root):
            raise ValueError("Skill path escapes trusted resources")
        text = path.read_text(encoding="utf-8")
        metadata, body = self._parse(text)
        if metadata.get("name") != name:
            raise ValueError("Skill name changed after catalog validation")
        return {"name": name, "description": metadata["description"], "body": body,
                "version": hashlib.sha256(text.encode("utf-8")).hexdigest(), "references": []}


SkillLoader = SkillRegistry
