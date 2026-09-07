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

## Deploy on Render

This repository includes `render.yaml` for a Render web service. Create a new Render service from the repository (or use **Blueprints**); Render will install `requirements.txt`, bind Uvicorn to Render's `$PORT`, and use `/health` for health checks. Keep one worker because OCR is CPU/memory intensive and the service is intentionally stateless.

The included `Dockerfile` is an alternative for Render's Docker runtime. It uses Python 3.12 and starts the same production server. No database, disk, or secret configuration is required.

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

## PDF utility tools

The API also includes stateless iLovePDF-style operations. They keep uploads in memory and return the generated file directly:

- `POST /tools/merge` — multiple PDF files
- `POST /tools/split` — one PDF plus `pages=1,3-5`
- `POST /tools/image-to-pdf` — PNG/JPEG/TIFF and other supported images
- `POST /tools/pdf-to-text` — native text-layer extraction as `.txt`

All endpoints are also available interactively in `/docs`.

LiteParse runs locally and includes Tesseract OCR by default. For other languages or a remote OCR backend, configure LiteParse/Tesseract in the runtime environment; this minimal API currently uses LiteParse's default `eng` language.
