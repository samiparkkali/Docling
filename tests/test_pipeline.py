from pathlib import Path

import pytest

from docpipe import DocPipe, FileStat, PipelineStats


def test_list_documents_filters_by_extension(tmp_path):
    (tmp_path / "doc_1.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "notes.txt").write_text("ignored")

    pipe = DocPipe(input_dir=tmp_path)
    docs = pipe.list_documents()

    assert docs == [tmp_path / "doc_1.pdf"]


def test_list_documents_missing_dir_raises(tmp_path):
    pipe = DocPipe(input_dir=tmp_path / "does-not-exist")

    try:
        pipe.list_documents()
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


def test_pipeline_stats_failure_rate_and_median():
    stats = PipelineStats()
    stats.add(FileStat(Path("a.pdf"), size_bytes=1024 * 1024, duration_s=1.0, success=True))
    stats.add(FileStat(Path("b.pdf"), size_bytes=2 * 1024 * 1024, duration_s=3.0, success=True))
    stats.add(FileStat(Path("c.pdf"), size_bytes=0, duration_s=0.5, success=False, error="boom"))

    assert stats.total == 3
    assert len(stats.succeeded) == 2
    assert len(stats.failed) == 1
    assert stats.failure_rate == 1 / 3
    assert stats.median_duration_s == 2.0
    # 3 MB total over 4 seconds total, across successful files only
    assert stats.throughput_mb_s == 0.75


def test_pipeline_stats_empty():
    stats = PipelineStats()

    assert stats.total == 0
    assert stats.failure_rate == 0.0
    assert stats.median_duration_s is None
    assert stats.throughput_mb_s is None


def test_pipeline_stats_low_content():
    stats = PipelineStats(min_chars=10)
    stats.add(FileStat(Path("a.pdf"), size_bytes=100, duration_s=1.0, success=True, char_count=500))
    stats.add(FileStat(Path("b.pdf"), size_bytes=100, duration_s=1.0, success=True, char_count=0))
    stats.add(FileStat(Path("c.pdf"), size_bytes=100, duration_s=1.0, success=True, char_count=10))
    # a failed conversion never has real text -- it shouldn't double-count as low content
    stats.add(FileStat(Path("d.pdf"), size_bytes=100, duration_s=1.0, success=False, error="boom"))

    assert [r.path.name for r in stats.low_content] == ["b.pdf", "c.pdf"]
    assert stats.low_content_rate == 2 / 4
    assert stats.summary()["low_content"] == 2


def test_pipeline_stats_with_warnings():
    stats = PipelineStats()
    stats.add(FileStat(Path("a.pdf"), size_bytes=100, duration_s=1.0, success=True))
    stats.add(
        FileStat(
            Path("b.pdf"),
            size_bytes=100,
            duration_s=1.0,
            success=True,
            warnings=["RapidOCR returned empty result!", "RapidOCR returned empty result!"],
        )
    )

    assert [r.path.name for r in stats.with_warnings] == ["b.pdf"]
    assert stats.summary()["with_warnings"] == 1
    assert stats.records[1].warning_summary == "RapidOCR returned empty result! (x2)"


def test_pipeline_stats_report_includes_problem_files():
    stats = PipelineStats(min_chars=5)
    stats.add(FileStat(Path("a.pdf"), size_bytes=100, duration_s=1.0, success=True, char_count=500))
    stats.add(FileStat(Path("b.pdf"), size_bytes=100, duration_s=1.0, success=False, error="boom"))
    stats.add(FileStat(Path("c.pdf"), size_bytes=100, duration_s=1.0, success=True, char_count=0))

    report = stats.report()

    assert "Total files:      3" in report
    assert "b.pdf: boom" in report
    assert "c.pdf: 0 chars" in report


def test_file_stat_worst_pages_sorts_ascending_and_skips_unscored():
    stat = FileStat(
        Path("a.pdf"),
        size_bytes=100,
        duration_s=1.0,
        success=True,
        page_scores={
            1: {"grade": "excellent", "mean_score": 0.95},
            2: {"grade": "poor", "mean_score": 0.3},
            3: {"grade": None, "mean_score": None},  # unscored page, e.g. no OCR/table ran
            4: {"grade": "fair", "mean_score": 0.65},
        },
    )

    assert stat.worst_pages(n=2) == [(2, "poor", 0.3), (4, "fair", 0.65)]


def test_pipeline_stats_low_confidence():
    stats = PipelineStats()
    stats.add(FileStat(Path("a.pdf"), size_bytes=100, duration_s=1.0, success=True, confidence_grade="excellent", ocr_score=0.95, table_score=0.9))
    stats.add(FileStat(Path("b.pdf"), size_bytes=100, duration_s=1.0, success=True, confidence_grade="poor", ocr_score=0.3, table_score=None))

    assert [r.path.name for r in stats.low_confidence] == ["b.pdf"]
    assert stats.summary()["low_confidence"] == 1
    assert stats.avg_ocr_score == (0.95 + 0.3) / 2
    assert stats.avg_table_score == 0.9  # None is excluded, not averaged as 0


def test_docpipe_rejects_unknown_ocr_engine(tmp_path):
    with pytest.raises(ValueError, match="Unknown ocr_engine"):
        DocPipe(input_dir=tmp_path, ocr_engine="not-a-real-engine")


def test_build_ocr_options_passes_through_lang_and_kwargs(tmp_path):
    pipe = DocPipe(
        input_dir=tmp_path,
        ocr_engine="tesseract_cli",
        ocr_lang=["eng"],
        ocr_options={"psm": 6},
    )

    ocr_options = pipe._build_ocr_options()

    assert ocr_options.lang == ["eng"]
    assert ocr_options.psm == 6
