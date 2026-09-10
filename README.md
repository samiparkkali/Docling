# Docling

POC for a document transformation pipeline (`DocPipe`) meant to run in
Databricks. Built around [Docling](https://github.com/docling-project/docling)
so the input format isn't locked to PDF, even though PDFs are the expected
first use case.

## Layout

- `docpipe/pipeline.py` — the `DocPipe` class: discovers files in a directory,
  converts each one to markdown, and records per-file timing/size stats.
- `docpipe/stats.py` — `PipelineStats`/`FileStat`: failure rate, median
  seconds/file, and aggregate MB/s throughput for a run.
- `docpipe/logger.py` — `get_logger`: colorized console logger, level
  configurable via `LOG_LEVEL` env var.
- `run.py` — CLI entry point for running `DocPipe` standalone.
- `mock-files/` — drop sample documents here for local testing (git-ignored,
  see below).
- `scripts/download_mock_files.py` — downloads sample arXiv PDFs into
  `mock-files/`.
- `tests/` — basic smoke tests.

## Setup

> macOS ships without a bare `python`/`pip` command — use `python3`/`pip3`, or activate the venv below and use `python`/`pip` from inside it.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

If you want to try the `tesseract_cli` OCR engine (see below), install the
Tesseract binary separately -- it's a system dependency, not a pip package:

```bash
brew install tesseract          # macOS; add tesseract-lang for non-English data
```

## Getting mock files

Locally:

```bash
python3 scripts/download_mock_files.py
```

In a Databricks notebook:

```python
from scripts.download_mock_files import download

download(output_dir="/Workspace/Shared/Docling/mock-files")
```

## Running tests

```bash
pip3 install -r requirements.txt
python3 -m pytest tests/ -v
```

(Running a test file directly with `python` won't work — pytest needs to
discover and invoke the `test_*` functions.)

## Usage

### CLI

```bash
python3 run.py --input-dir mock-files --output-dir output --min-chars 20
python3 run.py --ocr-engine tesseract_cli --ocr-lang eng   # try a different OCR engine
```

### As a library

```python
from docpipe import DocPipe

# min_chars: successful conversions with <= this many characters (e.g. OCR
# silently returning nothing on a scanned page) are flagged as low content
# instead of counted as clean successes.
#
# ocr_engine/ocr_lang/ocr_options: swap OCR backends to see which works best
# on your documents without changing any other code. ocr_options is passed
# straight through to the underlying docling OCR options class (e.g.
# {"psm": 6} for tesseract_cli), so any engine-specific knob is reachable.
pipe = DocPipe(
    input_dir="mock-files",
    output_dir="output",
    min_chars=20,
    ocr_engine="tesseract_cli",   # or "auto" (default), "rapidocr", "easyocr", "mac", ...
    ocr_lang=["eng"],
)
results = pipe.run()  # logs a run summary at the end
pipe.stats.print_report()  # distinct, human-readable end-of-batch report

for r in results:
    if not r.success:
        print(r.source, "FAILED:", r.error)
    elif r.char_count <= pipe.stats.min_chars:
        print(r.source, "LOW CONTENT:", r.char_count, "chars")
    else:
        print(r.source, "->", r.output_path)

print(pipe.stats.summary())
# {'total': 38, 'succeeded': 36, 'failed': 2, 'failure_rate': 0.0526,
#  'low_content': 1, 'low_content_rate': 0.0263, 'with_warnings': 3,
#  'low_confidence': 0, 'avg_ocr_score': 0.94, 'avg_table_score': None,
#  'median_duration_s': 4.12, 'throughput_mb_s': 0.87}

pipe.stats.low_content     # list[FileStat] of the suspiciously-empty ones
pipe.stats.with_warnings   # list[FileStat] that converted but logged warnings
                           # (e.g. r.warning_summary == "RapidOCR returned empty result! (x2)")
pipe.stats.low_confidence  # list[FileStat] Docling itself graded "poor"/"fair" overall
```

`docpipe.OCR_ENGINES` lists the supported engine names
(`rapidocr`, `tesseract_cli`, `tesserocr`, `easyocr`, `mac`, `auto`) --
see [docling's OCR options](https://github.com/docling-project/docling) for
what each needs installed. `auto` (the default) lets docling pick whichever
backend is actually available in your environment. Pinning a specific engine
also pins its own defaults -- e.g. `rapidocr` defaults to the `onnxruntime`
sub-backend, so if you only have `torch` installed pass
`ocr_options={"backend": "torch"}` alongside it. `tesseract_cli` needs the
`tesseract` binary (see Setup above).

`DocPipe.run()` catches per-file conversion errors instead of aborting the
whole batch. It also attributes any WARNING-level log records raised during
a file's conversion (OCR failures, empty-page detections, etc.) to that
specific file -- `docling`/OCR libraries log these without saying which
document caused them, but since DocPipe converts one file at a time it can
correlate them itself. Everything is logged live via
`docpipe.logger.get_logger` (set `LOG_LEVEL=DEBUG` for more detail), and
`pipe.stats.report()` / `print_report()` gives a single consolidated summary
block at the end, separate from the per-file log stream.

OCR warnings specifically are further tagged with the page number they came
from, e.g. `r.warning_summary == "[page 4] RapidOCR returned empty result! (x1)"`.
Docling's own log message doesn't say which page triggered it, so DocPipe
monkeypatches the (engine-agnostic) `BaseOcrModel.get_ocr_rects` hook --
called once per page right before that page's OCR runs -- to track the
current page, then tags any warning raised while an OCR attempt is in
flight. This only tags OCR-stage warnings; it's not a general-purpose
page-attribution mechanism for every possible log message.

### Warnings vs. confidence: which one to trust

A conversion can log an OCR warning (e.g. "RapidOCR returned empty result!")
for something totally harmless -- a photo or figure with no text in it -- so
warning counts alone aren't a reliable "did this actually work" signal.
Docling separately computes its own per-page/per-document confidence report
(`parse`/`layout`/`table`/`ocr` scores + a `poor`/`fair`/`good`/`excellent`
grade), which is a much more direct answer. `DocPipe` surfaces it on every
successful `FileStat`/`DocResult`:

```python
r.confidence_grade         # "excellent", overall
r.ocr_score, r.table_score # doc-level scores (None if that stage didn't run)
r.page_scores              # {page_no: {"parse", "layout", "table", "ocr", "mean_score", "grade"}}
r.worst_pages(n=3)          # [(page_no, grade, mean_score), ...], worst first -- FileStat only
```

`pipe.stats.low_confidence` lists files Docling itself graded poor/fair
overall (distinct from `with_warnings`, which just counts log noise), and the
end-of-batch report names their worst pages by number -- e.g. "page 7: poor
(0.31)" -- so you can go straight to the page that actually has a problem
instead of guessing from a warning count.

**Caveat:** as of docling 2.120.3, `table_score` is `None` even on documents
with correctly-extracted tables -- the confidence report doesn't score the
table stage in this version, though the field is kept here in case a future
Docling release populates it. Until then, verify table extraction the way we
validated it during development: read the actual markdown output, e.g.
`grep -l '^|.*|.*|$' output/*.md` to find files with markdown tables, then
open one and check the rows/headers by eye.

## Status

Early POC. `DocPipe` discovers + converts documents to markdown with
per-file error handling and basic run statistics; batching and
Databricks-specific I/O (e.g. Volumes, DBFS) aren't wired up yet.
