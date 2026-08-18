"""Minimal POC pipeline for turning documents into text/markdown with Docling.

Built to run standalone or inside a Databricks notebook. Input format isn't
locked to PDF on purpose -- Docling handles several formats, so DocPipe just
discovers whatever files are in the input directory and converts each one.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .logger import get_logger
from .stats import LOW_GRADES, FileStat, PipelineStats

DEFAULT_EXTENSIONS = (".pdf", ".docx", ".pptx", ".html", ".md")

# Maps a short CLI/config-friendly name to the docling OCR options class.
# rapidocr: bundled, no system deps, ONNX/torch models auto-downloaded.
# tesseract_cli: shells out to the `tesseract` binary (brew install tesseract).
# tesserocr: same engine via Python bindings (needs the tesserocr package).
# easyocr / mac / auto: see docling.datamodel.pipeline_options.
OCR_ENGINES = {
    "rapidocr": "RapidOcrOptions",
    "tesseract_cli": "TesseractCliOcrOptions",
    "tesserocr": "TesseractOcrOptions",
    "easyocr": "EasyOcrOptions",
    "mac": "OcrMacOptions",
    "auto": "OcrAutoOptions",
}


_ocr_page_tracking_patched = False
_current_ocr_page: int | None = None


def _patch_ocr_page_tracking() -> None:
    """Monkeypatch BaseOcrModel.get_ocr_rects (shared by every OCR engine) to
    record which page is currently being OCR'd, in _current_ocr_page.

    Docling's own OCR warnings (e.g. "RapidOCR returned empty result!") don't
    say which page triggered them. get_ocr_rects(page) is called exactly once
    per page, immediately before that page's OCR attempts, so intercepting it
    lets _WarningCollector tag any warning raised during those attempts with
    the right page number -- without depending on engine-specific internals,
    since every OCR engine goes through this same base-class method.
    """
    global _ocr_page_tracking_patched
    if _ocr_page_tracking_patched:
        return
    from docling.models.base_ocr_model import BaseOcrModel

    original_get_ocr_rects = BaseOcrModel.get_ocr_rects

    def _tracked_get_ocr_rects(self, page):
        global _current_ocr_page
        _current_ocr_page = getattr(page, "page_no", None)
        return original_get_ocr_rects(self, page)

    BaseOcrModel.get_ocr_rects = _tracked_get_ocr_rects
    _ocr_page_tracking_patched = True


class _WarningCollector(logging.Handler):
    """Captures WARNING+ log records emitted while converting a single file.

    Docling processes one file at a time, so any warning raised between
    attaching and detaching this handler around a convert() call can be
    attributed to that specific file -- useful since messages like "RapidOCR
    returned empty result!" don't otherwise say which document triggered them.
    Messages raised while an OCR attempt is in flight are further tagged with
    the page number, via _current_ocr_page (see _patch_ocr_page_tracking).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if _current_ocr_page is not None:
            message = f"[page {_current_ocr_page}] {message}"
        self.messages.append(message)


def _clean_score(value) -> float | None:
    """NaN (Docling's "this stage didn't run on this page") -> None."""
    if value is None:
        return None
    value = float(value)
    return None if value != value else value


def _page_scores(confidence) -> dict[int, dict]:
    return {
        page_no: {
            "parse": _clean_score(pc.parse_score),
            "layout": _clean_score(pc.layout_score),
            "table": _clean_score(pc.table_score),
            "ocr": _clean_score(pc.ocr_score),
            "mean_score": _clean_score(pc.mean_score),
            "grade": pc.mean_grade.value,
        }
        for page_no, pc in confidence.pages.items()
    }


@dataclass
class DocResult:
    source: Path
    success: bool
    duration_s: float
    size_bytes: int
    char_count: int = 0
    text: str = ""
    output_path: Path | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    page_count: int = 0
    ocr_score: float | None = None
    table_score: float | None = None
    confidence_grade: str | None = None
    page_scores: dict[int, dict] = field(default_factory=dict)


class DocPipe:
    """Discovers documents in a directory and converts them to markdown.

    Example:
        pipe = DocPipe("mock-files", output_dir="output")
        results = pipe.run()
        pipe.stats.log_summary(pipe.logger)
    """

    def __init__(
        self,
        input_dir: str | Path,
        output_dir: str | Path | None = None,
        extensions: tuple[str, ...] = DEFAULT_EXTENSIONS,
        min_chars: int = 0,
        ocr_engine: str = "auto",
        ocr_lang: list[str] | None = None,
        ocr_options: dict | None = None,
    ) -> None:
        """min_chars: successful conversions with <= this many characters (e.g. OCR
        silently returning nothing) are flagged as low content rather than counted
        as clean successes.

        ocr_engine: one of OCR_ENGINES.keys() -- swap engines to see which one
        handles your documents best. Defaults to "auto" (docling picks whatever
        backend is actually installed); pinning e.g. "rapidocr" also pins its
        default sub-backend (onnxruntime), which may not be installed -- pass
        ocr_options={"backend": "torch"} if that's what you have instead.
        ocr_lang and ocr_options (engine-specific kwargs, e.g. {"psm": 6} for
        tesseract_cli) are passed straight through to the underlying docling
        OCR options class."""
        if ocr_engine not in OCR_ENGINES:
            raise ValueError(f"Unknown ocr_engine {ocr_engine!r}. Choose from: {', '.join(OCR_ENGINES)}")
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir) if output_dir else None
        self.extensions = tuple(ext.lower() for ext in extensions)
        self.logger = get_logger("docpipe")
        self.stats = PipelineStats(min_chars=min_chars)
        self.ocr_engine = ocr_engine
        self.ocr_lang = ocr_lang
        self.ocr_options = ocr_options or {}
        self._converter = None

    def _build_ocr_options(self):
        from docling.datamodel import pipeline_options as docling_opts

        ocr_options_cls = getattr(docling_opts, OCR_ENGINES[self.ocr_engine])
        kwargs = dict(self.ocr_options)
        if self.ocr_lang is not None:
            kwargs["lang"] = self.ocr_lang
        return ocr_options_cls(**kwargs)

    @property
    def converter(self):
        if self._converter is None:
            self.logger.debug("Initializing Docling DocumentConverter (ocr_engine=%s)", self.ocr_engine)
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption

            # RapidOCR's own logger disables propagation and logs every
            # engine/device/model-file check at INFO -- silence that noise but
            # keep its WARNINGs (e.g. empty OCR results), which convert_one()
            # captures and attributes to the file being processed.
            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            _patch_ocr_page_tracking()

            pipeline_options = PdfPipelineOptions(do_ocr=True, ocr_options=self._build_ocr_options())
            self._converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
        return self._converter

    def list_documents(self) -> list[Path]:
        if not self.input_dir.exists():
            raise FileNotFoundError(f"Input directory not found: {self.input_dir}")
        return sorted(
            p
            for p in self.input_dir.iterdir()
            if p.is_file() and p.suffix.lower() in self.extensions
        )

    def convert_one(self, path: Path) -> DocResult:
        size_bytes = path.stat().st_size
        start = time.monotonic()

        global _current_ocr_page
        _current_ocr_page = None  # don't leak the previous file's page number

        # RapidOCR's logger doesn't propagate to root, so it needs its own handler
        # alongside root -- together they catch both its warnings and Docling's own.
        collector = _WarningCollector()
        root_logger = logging.getLogger()
        rapidocr_logger = logging.getLogger("RapidOCR")
        root_logger.addHandler(collector)
        rapidocr_logger.addHandler(collector)
        try:
            result = self.converter.convert(str(path))
            text = result.document.export_to_markdown()
        except Exception as exc:
            duration_s = time.monotonic() - start
            self.logger.error("Failed to convert %s: %s", path.name, exc)
            doc_result = DocResult(
                source=path,
                success=False,
                duration_s=duration_s,
                size_bytes=size_bytes,
                error=str(exc),
                warnings=collector.messages,
            )
            self.stats.add(
                FileStat(
                    path, size_bytes, duration_s, success=False, error=str(exc),
                    warnings=collector.messages,
                )
            )
            return doc_result
        finally:
            root_logger.removeHandler(collector)
            rapidocr_logger.removeHandler(collector)

        duration_s = time.monotonic() - start
        char_count = len(text)

        confidence = result.confidence
        page_count = result.document.num_pages()
        page_scores = _page_scores(confidence)
        ocr_score = _clean_score(confidence.ocr_score)
        table_score = _clean_score(confidence.table_score)
        confidence_grade = confidence.mean_grade.value

        output_path = None
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            output_path = self.output_dir / f"{path.stem}.md"
            output_path.write_text(text)

        size_mb = size_bytes / (1024 * 1024)
        self.logger.info(
            "Converted %s (%.2f MB) in %.2fs (%.2f MB/s, %d chars, %d pages, confidence=%s)",
            path.name,
            size_mb,
            duration_s,
            size_mb / duration_s if duration_s > 0 else 0.0,
            char_count,
            page_count,
            confidence_grade,
        )
        if char_count <= self.stats.min_chars:
            self.logger.warning("Low content: %s -- %d chars", path.name, char_count)

        file_stat = FileStat(
            path,
            size_bytes,
            duration_s,
            success=True,
            char_count=char_count,
            warnings=collector.messages,
            page_count=page_count,
            parse_score=_clean_score(confidence.parse_score),
            layout_score=_clean_score(confidence.layout_score),
            table_score=table_score,
            ocr_score=ocr_score,
            confidence_grade=confidence_grade,
            page_scores=page_scores,
        )
        if collector.messages:
            self.logger.warning(
                "%s had %d warning(s) during conversion: %s",
                path.name,
                len(collector.messages),
                file_stat.warning_summary,
            )
        if confidence_grade in LOW_GRADES:
            worst = ", ".join(f"page {no}: {grade} ({score:.2f})" for no, grade, score in file_stat.worst_pages())
            self.logger.warning("%s has low confidence (%s) -- worst pages: %s", path.name, confidence_grade, worst)
        self.stats.add(file_stat)

        return DocResult(
            source=path,
            success=True,
            duration_s=duration_s,
            size_bytes=size_bytes,
            char_count=char_count,
            text=text,
            output_path=output_path,
            warnings=collector.messages,
            page_count=page_count,
            ocr_score=ocr_score,
            table_score=table_score,
            confidence_grade=confidence_grade,
            page_scores=page_scores,
        )

    def run(self) -> list[DocResult]:
        documents = self.list_documents()
        self.logger.info("Found %d document(s) in %s", len(documents), self.input_dir)

        results = [self.convert_one(path) for path in documents]

        self.stats.log_summary(self.logger)
        return results
