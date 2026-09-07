# LiteParse extraction API

FastAPI backend that extracts text from PDFs and images. It takes a cost-aware route:

- Digital / already-OCR'd PDFs with a usable text layer use **PyMuPDF** only.
- Scanned PDFs, PDFs LiteParse flags as needing OCR, and all images use the official **LlamaIndex LiteParse** Python distribution (`lit` CLI), returning Markdown.

LiteParse's `is-complex` command is used as the routing signal for scanned PDFs. It reports per-page `needs_ocr` flags; its non-zero status for OCR-needed files is expected.

## Storage and retention

This service has no database and does not retain uploads. The request body is held in RAM. LiteParse's official CLI accepts a filesystem path, so the API creates one uniquely named, request-scoped file only while it is parsing and deletes it in a `finally` block. No input file, extracted text, or parsing result is cached after the request. This is a shorter lifetime than a "few minutes" retention window.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

The `liteparse` package installs `lit`. Confirm it is available:

```powershell
lit --help
```

## Use

```powershell
curl.exe -X POST http://127.0.0.1:8000/extract -F "file=@C:\docs\receipt.jpg"
curl.exe -X POST http://127.0.0.1:8000/extract -F "file=@C:\docs\report.pdf"
```

`POST /extract` returns the selected `parser`, `needs_ocr`, extracted `text`, and for PDFs LiteParse's per-page complexity metadata. `GET /health` shows whether `lit` is available.

LiteParse runs locally and includes Tesseract OCR by default. For other languages or a remote OCR backend, configure LiteParse/Tesseract in the runtime environment; this minimal API currently uses LiteParse's default `eng` language.
