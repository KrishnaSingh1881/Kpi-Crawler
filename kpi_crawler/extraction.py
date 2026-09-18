"""Structured artifact extraction using Docling's native PDF parser."""

from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from docling_parse.pdf_parser import DoclingPdfParser

from .models import Artifact


DOCLING_VERSION = version("docling-parse")


@dataclass(frozen=True)
class ExtractedItem:
    evidence_type: str
    claim: str
    value: str
    supporting_context: str
    page_number: int | None
    location: str | None
    structure: dict[str, Any]


@dataclass(frozen=True)
class ExtractionResult:
    artifact_id: int
    extractor: str
    extractor_version: str
    metadata: dict[str, Any]
    items: tuple[ExtractedItem, ...]


class Extractor(Protocol):
    def extract(self, artifact: Artifact) -> ExtractionResult:
        """Extract structured evidence from one stored artifact."""


def _location(box: Any) -> str:
    return f"x0={box.l:.2f},y0={box.t:.2f},x1={box.r:.2f},y1={box.b:.2f}"


def _claim_and_value(text: str) -> tuple[str, str]:
    if ":" in text:
        claim, value = text.split(":", 1)
        return claim.strip(), value.strip()
    return text, text


def _table_item(lines: list[tuple[str, Any]], page_number: int, section: str | None) -> ExtractedItem | None:
    rows: list[list[tuple[str, Any]]] = []
    for text, box in sorted(lines, key=lambda line: -((line[1].t + line[1].b) / 2)):
        center = (box.t + box.b) / 2
        if rows and abs(center - ((rows[-1][0][1].t + rows[-1][0][1].b) / 2)) <= 4:
            rows[-1].append((text, box))
        else:
            rows.append([(text, box)])
    rows = [sorted(row, key=lambda cell: cell[1].l) for row in rows if len(row) >= 2]
    if len(rows) < 2 or len({len(row) for row in rows}) != 1:
        return None
    headers = [text for text, _ in rows[0]]
    values = [[text for text, _ in row] for row in rows[1:]]
    boxes = [box for row in rows for _, box in row]
    location = (
        f"x0={min(box.l for box in boxes):.2f},y0={max(box.t for box in boxes):.2f},"
        f"x1={max(box.r for box in boxes):.2f},y1={min(box.b for box in boxes):.2f}"
    )
    context = "\n".join(" | ".join(row) for row in [headers, *values])
    return ExtractedItem(
        evidence_type="table",
        claim="Table",
        value=" | ".join(headers),
        supporting_context=context,
        page_number=page_number,
        location=location,
        structure={"headers": headers, "rows": values, "section": section},
    )


class PdfExtractor:
    """Model-free Docling extractor preserving native PDF structure."""

    name = "docling-native-pdf"
    version = DOCLING_VERSION

    def extract(self, artifact: Artifact) -> ExtractionResult:
        document = DoclingPdfParser().load(Path(artifact.raw_storage_ref), lazy=False)
        try:
            items: list[ExtractedItem] = []
            heading_count = table_count = visual_count = text_count = 0
            current_section: str | None = None

            for page_number, page in document.iterate_pages():
                lines = [(cell.text.strip(), cell.to_bounding_box()) for cell in page.textline_cells if cell.text.strip()]
                heights = [abs(box.t - box.b) for _, box in lines]
                heading_height = median(heights) * 1.5 if heights else float("inf")
                for text, box in lines:
                    is_heading = abs(box.t - box.b) >= heading_height
                    if is_heading:
                        current_section = text
                        heading_count += 1
                    claim, value = _claim_and_value(text)
                    items.append(
                        ExtractedItem(
                            evidence_type="text",
                            claim=claim,
                            value=value,
                            supporting_context=text,
                            page_number=page_number,
                            location=_location(box),
                            structure={"role": "heading" if is_heading else "text", "section": current_section},
                        )
                    )
                    text_count += 1

                table = _table_item(lines, page_number, current_section)
                if table is not None:
                    items.append(table)
                    table_count += 1

                for bitmap in page.bitmap_resources:
                    box = bitmap.rect.to_bounding_box()
                    items.append(
                        ExtractedItem(
                            evidence_type="visual",
                            claim="Embedded image",
                            value="bitmap",
                            supporting_context=f"Embedded image on page {page_number}",
                            page_number=page_number,
                            location=_location(box),
                            structure={"kind": "bitmap", "section": current_section},
                        )
                    )
                    visual_count += 1

            return ExtractionResult(
                artifact_id=artifact.id,
                extractor=self.name,
                extractor_version=self.version,
                metadata={
                    "page_count": document.number_of_pages(),
                    "text_count": text_count,
                    "heading_count": heading_count,
                    "table_count": table_count,
                    "visual_count": visual_count,
                },
                items=tuple(items),
            )
        finally:
            document.unload()
