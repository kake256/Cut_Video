"""Local-only posting suggestions derived from a highlight candidate."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping


def _clean_text(value: object, *, maximum: int, multiline: bool) -> str:
    text = str(value or "").replace("\x00", "")
    if multiline:
        text = re.sub(r"[\t ]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    return text[:maximum].rstrip()


def _clean_tags(values: Iterable[object]) -> tuple[str, ...]:
    tags: list[str] = []
    for value in values:
        tag = _clean_text(value, maximum=30, multiline=False).lstrip("#")
        if tag and tag.casefold() not in {item.casefold() for item in tags}:
            tags.append(tag)
        if len(tags) >= 20:
            break
    return tuple(tags)


@dataclass(frozen=True)
class PostingMetadata:
    title: str
    description: str
    tags: tuple[str, ...]
    generated_with_llm: bool = True
    requires_review: bool = True
    schema_version: int = 1

    def validate(self) -> "PostingMetadata":
        if not self.title:
            raise ValueError("投稿用タイトルを入力してください")
        if len(self.title) > 120 or len(self.description) > 1000:
            raise ValueError("投稿用メタデータが長すぎます")
        if len(self.tags) > 20 or any(len(tag) > 30 for tag in self.tags):
            raise ValueError("投稿用タグが多すぎるか長すぎます")
        return self

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "generated_with_llm": self.generated_with_llm,
            "requires_review": self.requires_review,
            "notice": "LLM由来の候補です。投稿前に内容を確認してください。",
        }


def posting_metadata_from_candidate(
    candidate: Mapping[str, object], *, title_override: str = "",
    description_override: str = "", tags_override: str = "",
) -> PostingMetadata:
    """Build editable suggestions without transcript text or local paths."""
    title = _clean_text(
        title_override or candidate.get("export_title") or candidate.get("title"),
        maximum=120,
        multiline=False,
    )
    description = _clean_text(
        description_override or candidate.get("summary"),
        maximum=1000,
        multiline=True,
    )
    if str(tags_override or "").strip():
        tag_values = re.split(r"[,、\n]+", str(tags_override))
    else:
        raw_tags = candidate.get("tags") or []
        tag_values = raw_tags if isinstance(raw_tags, (list, tuple)) else [raw_tags]
    return PostingMetadata(title, description, _clean_tags(tag_values)).validate()
