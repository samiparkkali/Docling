"""Download sample arXiv PDFs into mock-files/ for local pipeline testing.

Run locally:
    python scripts/download_mock_files.py

Run in Databricks, point output_dir at the workspace path instead, e.g.:
    download(output_dir="/Workspace/Shared/Health CPE/Docling/mock-files")
"""

import ssl
import urllib.request
from pathlib import Path

import certifi

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "mock-files"

# macOS python.org installs don't wire up the system CA bundle by default,
# which makes urllib fail SSL verification against arxiv.org. certifi's
# bundle works regardless of how Python was installed.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def download(output_dir: str | Path = DEFAULT_OUTPUT_DIR, count: int = 40) -> int:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for i in range(1, count + 1):
        pdf_id = f"2301.{i:05d}"
        url = f"https://arxiv.org/pdf/{pdf_id}.pdf"
        file_path = output_dir / f"doc_{i}.pdf"

        try:
            with urllib.request.urlopen(url, context=SSL_CONTEXT) as response:
                file_path.write_bytes(response.read())
            downloaded += 1
        except Exception as exc:
            print(f"Skipped {pdf_id}: {exc}")

    print(f"Downloaded {downloaded}/{count} files to {output_dir}")
    return downloaded


if __name__ == "__main__":
    download()
