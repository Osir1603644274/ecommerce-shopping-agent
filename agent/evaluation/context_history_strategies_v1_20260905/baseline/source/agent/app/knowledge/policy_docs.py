import re
from pathlib import Path
from typing import Any

from .models import KnowledgeChunk


POLICY_DOC_SOURCE_NAME = "policy_docs"
POLICY_DOC_SOURCE_TYPE = "policy_doc"
SECTION_HEADING_PATTERN = re.compile(
    r"^##\s+(?P<title>.+?)\s+\{#(?P<key>[a-z0-9][a-z0-9-]{1,63})\}\s*$"
)


def _parse_metadata_value(raw_value: str) -> Any:
    value = raw_value.strip()
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",")
        return [item.strip().strip('"').strip("'") for item in items if item.strip()]
    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        return value[1:-1]
    return value


def parse_markdown_front_matter(markdown: str) -> tuple[dict[str, Any], str]:
    """Parse a small YAML-like front matter block without adding dependencies."""

    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, markdown.strip()

    metadata: dict[str, Any] = {}
    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            continue
        metadata[key.strip()] = _parse_metadata_value(value)

    if end_index is None:
        return {}, markdown.strip()
    return metadata, "\n".join(lines[end_index + 1 :]).strip()


def _extract_document_title(body: str, metadata: dict[str, Any]) -> str:
    title = str(metadata.get("title", "")).strip()
    if title:
        return title
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "未命名知识文档"


def _split_markdown_sections(body: str) -> list[tuple[str, str, str]]:
    sections: list[tuple[str, str, list[str]]] = []
    current_key: str | None = None
    current_title: str | None = None
    current_lines: list[str] = []
    seen_keys: set[str] = set()

    for line in body.splitlines():
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            match = SECTION_HEADING_PATTERN.match(line)
            if match is None:
                raise ValueError(
                    "policy section headings require a stable key, "
                    "for example: ## 退款规则 {#refund-rules}"
                )
            if current_title is not None:
                sections.append((current_key or "full", current_title, current_lines))
            current_key = match.group("key")
            if current_key in seen_keys:
                raise ValueError(f"duplicate policy section key: {current_key}")
            seen_keys.add(current_key)
            current_title = match.group("title").strip()
            current_lines = []
            continue
        if current_title is not None:
            current_lines.append(line)

    if current_title is not None:
        sections.append((current_key or "full", current_title, current_lines))

    if not sections:
        stripped_body = body.strip()
        return [("full", "全文", stripped_body)] if stripped_body else []

    return [
        (key, title, "\n".join(lines).strip())
        for key, title, lines in sections
        if "\n".join(lines).strip()
    ]


def policy_markdown_to_chunks(markdown: str) -> list[KnowledgeChunk]:
    """Convert one policy markdown document into searchable knowledge chunks."""

    metadata, body = parse_markdown_front_matter(markdown)
    source_id = str(metadata.get("sourceId", "")).strip()
    if not source_id:
        raise ValueError("policy markdown requires sourceId in front matter")

    source_type = str(metadata.get("sourceType", POLICY_DOC_SOURCE_TYPE)).strip()
    if source_type != POLICY_DOC_SOURCE_TYPE:
        raise ValueError("policy markdown sourceType must be policy_doc")

    document_title = _extract_document_title(body, metadata)
    tags = metadata.get("tags", [])
    if not isinstance(tags, list):
        tags = []

    common_metadata = {
        "documentTitle": document_title,
        "sourceTitle": metadata.get("sourceTitle"),
        "sourceOrg": metadata.get("sourceOrg"),
        "sourceUrl": metadata.get("sourceUrl"),
        "publishDate": metadata.get("publishDate"),
        "effectiveDate": metadata.get("effectiveDate"),
    }
    common_metadata = {
        key: value for key, value in common_metadata.items() if value not in (None, "")
    }

    chunks: list[KnowledgeChunk] = []
    for index, (section_key, section_title, section_body) in enumerate(
        _split_markdown_sections(body),
        start=1,
    ):
        content = "\n".join(
            part
            for part in (
                document_title,
                section_title,
                section_body,
            )
            if part
        ).strip()
        chunks.append(
            KnowledgeChunk(
                chunk_id=f"{POLICY_DOC_SOURCE_TYPE}:{source_id}:{section_key}",
                source_type=POLICY_DOC_SOURCE_TYPE,
                source_id=source_id,
                chunk_index=index,
                source_version=str(
                    metadata.get("sourceVersion")
                    or metadata.get("version")
                    or metadata.get("effectiveDate")
                    or "1"
                ),
                title=f"{document_title} - {section_title}",
                content=content,
                metadata={
                    **common_metadata,
                    "sectionKey": section_key,
                    "sectionTitle": section_title,
                    "sectionIndex": index,
                    "legacyChunkId": (
                        f"{POLICY_DOC_SOURCE_TYPE}:{source_id}:{index:03d}"
                    ),
                },
                visibility=str(metadata.get("visibility", "public")),
                language=str(metadata.get("language", "zh")),
                tags=[str(tag) for tag in tags],
                updated_at=metadata.get("updatedAt"),
            )
        )
    return chunks


def load_policy_markdown_chunks(directory: Path) -> list[KnowledgeChunk]:
    """Load all policy markdown files under a directory into knowledge chunks."""

    chunks: list[KnowledgeChunk] = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(policy_markdown_to_chunks(path.read_text(encoding="utf-8")))
    return chunks
