"""CLI entry point for running DocPipe standalone.

    python3 run.py
    python3 run.py --input-dir mock-files --output-dir output --min-chars 20
    python3 run.py --ocr-engine tesseract_cli --ocr-lang eng
"""

import argparse

from docpipe import OCR_ENGINES, DocPipe


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert documents to markdown with DocPipe.")
    parser.add_argument("--input-dir", default="mock-files", help="Directory of source documents.")
    parser.add_argument("--output-dir", default="output", help="Directory to write converted markdown to.")
    parser.add_argument(
        "--min-chars",
        type=int,
        default=0,
        help="Flag successful conversions with <= this many characters as low content.",
    )
    parser.add_argument(
        "--ocr-engine",
        default="auto",
        choices=sorted(OCR_ENGINES),
        help="OCR engine to use, swap to compare which works best on your documents.",
    )
    parser.add_argument(
        "--ocr-lang",
        nargs="+",
        default=None,
        help="OCR language codes, format depends on engine (e.g. --ocr-lang eng deu).",
    )
    args = parser.parse_args()

    pipe = DocPipe(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        min_chars=args.min_chars,
        ocr_engine=args.ocr_engine,
        ocr_lang=args.ocr_lang,
    )
    pipe.run()
    pipe.stats.print_report()


if __name__ == "__main__":
    main()
