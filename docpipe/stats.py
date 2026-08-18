from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


LOW_GRADES = ("poor", "fair")
"""Docling QualityGrade values (as plain strings) treated as low confidence."""


@dataclass
class FileStat:
    path: Path
    size_bytes: int
    duration_s: float
    success: bool
    char_count: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    """Warning-level log messages emitted by Docling/OCR while converting this
    file (e.g. "RapidOCR returned empty result!" for an unreadable page)."""

    page_count: int = 0
    parse_score: float | None = None
    layout_score: float | None = None
    table_score: float | None = None
    ocr_score: float | None = None
    confidence_grade: str | None = None
    """Docling's own per-document quality grade ("poor"/"fair"/"good"/"excellent"),
    derived from its parse/layout/table/OCR confidence scores -- a much more
    direct answer to "did this actually convert well" than log warnings."""
    page_scores: dict[int, dict] = field(default_factory=dict)
    """page_no -> {"parse", "layout", "table", "ocr", "mean_score", "grade"}."""

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)

    @property
    def mb_per_s(self) -> float | None:
        return self.size_mb / self.duration_s if self.duration_s > 0 else None

    @property
    def warning_summary(self) -> str:
        if not self.warnings:
            return ""
        counts = Counter(self.warnings)
        return ", ".join(f"{msg} (x{n})" for msg, n in counts.items())

    def worst_pages(self, n: int = 3) -> list[tuple[int, str, float]]:
        """The n lowest-confidence pages as (page_no, grade, mean_score), worst first."""
        scored = [
            (page_no, s["grade"], s["mean_score"])
            for page_no, s in self.page_scores.items()
            if s["mean_score"] is not None
        ]
        return sorted(scored, key=lambda t: t[2])[:n]


@dataclass
class PipelineStats:
    """Accumulates per-file timing/size data and derives run-level metrics."""

    records: list[FileStat] = field(default_factory=list)
    min_chars: int = 0
    """Successful conversions with char_count <= min_chars are flagged as low content
    (e.g. OCR silently returning empty text instead of raising)."""

    def add(self, record: FileStat) -> None:
        self.records.append(record)

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def succeeded(self) -> list[FileStat]:
        return [r for r in self.records if r.success]

    @property
    def failed(self) -> list[FileStat]:
        return [r for r in self.records if not r.success]

    @property
    def failure_rate(self) -> float:
        return len(self.failed) / self.total if self.total else 0.0

    @property
    def low_content(self) -> list[FileStat]:
        """Successful conversions that produced suspiciously little text."""
        return [r for r in self.succeeded if r.char_count <= self.min_chars]

    @property
    def low_content_rate(self) -> float:
        return len(self.low_content) / self.total if self.total else 0.0

    @property
    def with_warnings(self) -> list[FileStat]:
        """Files that converted but logged warnings along the way (e.g. OCR
        failing on individual pages/images)."""
        return [r for r in self.records if r.warnings]

    @property
    def low_confidence(self) -> list[FileStat]:
        """Files Docling itself graded "poor"/"fair" overall -- a more direct
        signal than with_warnings for whether OCR/table/layout extraction
        actually went wrong, as opposed to just being noisy."""
        return [r for r in self.succeeded if r.confidence_grade in LOW_GRADES]

    def _avg_score(self, attr: str) -> float | None:
        values = [v for r in self.succeeded if (v := getattr(r, attr)) is not None]
        return sum(values) / len(values) if values else None

    @property
    def avg_ocr_score(self) -> float | None:
        return self._avg_score("ocr_score")

    @property
    def avg_table_score(self) -> float | None:
        return self._avg_score("table_score")

    @property
    def median_duration_s(self) -> float | None:
        durations = [r.duration_s for r in self.succeeded]
        return statistics.median(durations) if durations else None

    @property
    def throughput_mb_s(self) -> float | None:
        """Aggregate throughput across all successful files (total MB / total time)."""
        succeeded = self.succeeded
        if not succeeded:
            return None
        total_mb = sum(r.size_mb for r in succeeded)
        total_s = sum(r.duration_s for r in succeeded)
        return total_mb / total_s if total_s > 0 else None

    def summary(self) -> dict:
        return {
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "failure_rate": self.failure_rate,
            "low_content": len(self.low_content),
            "low_content_rate": self.low_content_rate,
            "with_warnings": len(self.with_warnings),
            "low_confidence": len(self.low_confidence),
            "avg_ocr_score": self.avg_ocr_score,
            "avg_table_score": self.avg_table_score,
            "median_duration_s": self.median_duration_s,
            "throughput_mb_s": self.throughput_mb_s,
        }

    def log_summary(self, logger) -> None:
        s = self.summary()
        median = f"{s['median_duration_s']:.2f}s" if s["median_duration_s"] is not None else "n/a"
        throughput = f"{s['throughput_mb_s']:.2f} MB/s" if s["throughput_mb_s"] is not None else "n/a"
        logger.info(
            "Processed %d files: %d succeeded, %d failed (%.1f%% failure rate), "
            "%d low-content (<=%d chars, %.1f%%). median: %s/file, throughput: %s",
            s["total"],
            s["succeeded"],
            s["failed"],
            s["failure_rate"] * 100,
            s["low_content"],
            self.min_chars,
            s["low_content_rate"] * 100,
            median,
            throughput,
        )
        for r in self.failed:
            logger.warning("Failed: %s -- %s", r.path.name, r.error)
        for r in self.low_content:
            logger.warning("Low content: %s -- %d chars", r.path.name, r.char_count)
        for r in self.with_warnings:
            logger.warning(
                "%s had %d warning(s) during conversion: %s",
                r.path.name,
                len(r.warnings),
                r.warning_summary,
            )
        for r in self.low_confidence:
            worst = ", ".join(f"page {no}: {grade} ({score:.2f})" for no, grade, score in r.worst_pages())
            logger.warning("%s has low confidence (%s) -- worst pages: %s", r.path.name, r.confidence_grade, worst)

    def report(self) -> str:
        """Multi-line, human-readable end-of-batch report -- distinct from the
        per-file log stream, meant to be the one thing you read after a run."""
        s = self.summary()
        median = f"{s['median_duration_s']:.2f}s" if s["median_duration_s"] is not None else "n/a"
        throughput = f"{s['throughput_mb_s']:.2f} MB/s" if s["throughput_mb_s"] is not None else "n/a"
        avg_ocr = f"{s['avg_ocr_score']:.2f}" if s["avg_ocr_score"] is not None else "n/a"
        avg_table = f"{s['avg_table_score']:.2f}" if s["avg_table_score"] is not None else "n/a"

        lines = [
            "=" * 60,
            "DocPipe batch summary",
            "=" * 60,
            f"Total files:      {s['total']}",
            f"Succeeded:        {s['succeeded']}",
            f"Failed:           {s['failed']} ({s['failure_rate'] * 100:.1f}%)",
            f"Low content:      {s['low_content']} (<= {self.min_chars} chars, {s['low_content_rate'] * 100:.1f}%)",
            f"With warnings:    {s['with_warnings']}",
            f"Low confidence:   {s['low_confidence']} (Docling-graded poor/fair overall)",
            f"Avg OCR score:    {avg_ocr}   Avg table score: {avg_table}",
            f"Median time/file: {median}",
            f"Throughput:       {throughput}",
        ]

        if self.failed:
            lines += ["", "Failed:"]
            lines += [f"  - {r.path.name}: {r.error}" for r in self.failed]
        if self.low_content:
            lines += ["", "Low content:"]
            lines += [f"  - {r.path.name}: {r.char_count} chars" for r in self.low_content]
        if self.with_warnings:
            lines += ["", "With warnings:"]
            lines += [f"  - {r.path.name}: {r.warning_summary}" for r in self.with_warnings]
        if self.low_confidence:
            lines += ["", "Low confidence:"]
            for r in self.low_confidence:
                worst = ", ".join(f"page {no}: {grade} ({score:.2f})" for no, grade, score in r.worst_pages())
                lines.append(f"  - {r.path.name}: {r.confidence_grade} -- worst pages: {worst}")

        lines.append("=" * 60)
        return "\n".join(lines)

    def print_report(self) -> None:
        print(self.report())
