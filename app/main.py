from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Literal

import pymupdf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from io import BytesIO

app = FastAPI(title="LiteParse extraction API", version="0.1.0")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_MERGE_BYTES = 50 * 1024 * 1024
MAX_MERGE_FILES = 20
PDF_CONTENT_TYPES = {"application/pdf"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}
LITPARSE_CONCURRENCY = asyncio.Semaphore(2)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> str:
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>LiteParse Tools</title><style>
body{font:16px system-ui;margin:0;background:#f5f7fb;color:#172033}main{max-width:900px;margin:40px auto;padding:0 20px}h1{font-size:34px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}.card{background:white;border:1px solid #dde3ee;border-radius:14px;padding:20px;box-shadow:0 2px 8px #17203310}input{width:100%;margin:10px 0;padding:9px;box-sizing:border-box}button{background:#3157d5;color:white;border:0;border-radius:8px;padding:10px 14px;cursor:pointer}small{color:#5c6680}#status{margin-top:20px;white-space:pre-wrap}</style></head>
<body><main><h1>LiteParse Tools</h1><p>Private, request-scoped PDF and image tools. Files are not stored.</p><div class='grid'>
<section class='card'><h2>Extract information</h2><small>PDF or image</small><form data-kind='json' action='/extract'><input name='file' type='file' accept='.pdf,image/*' required><button>Extract text</button></form></section>
<section class='card'><h2>Merge PDFs</h2><small>Combine files in upload order</small><form data-kind='download' action='/tools/merge'><input name='files' type='file' accept='.pdf' multiple required><button>Merge PDFs</button></form></section>
<section class='card'><h2>Split PDF</h2><small>Pages like 1,3-5</small><form data-kind='download' action='/tools/split'><input name='file' type='file' accept='.pdf' required><input name='pages' placeholder='1,3-5' required><button>Extract pages</button></form></section>
<section class='card'><h2>Image to PDF</h2><small>Convert one image</small><form data-kind='download' action='/tools/image-to-pdf'><input name='file' type='file' accept='image/*' required><button>Convert</button></form></section>
</div><pre id='status'></pre><p><a href='/docs'>API documentation</a></p></main><script>
for(const form of document.querySelectorAll('form'))form.onsubmit=async e=>{e.preventDefault();const s=document.querySelector('#status');s.textContent='Processing...';const r=await fetch(form.action,{method:'POST',body:new FormData(form)});if(!r.ok){s.textContent=await r.text();return}if(form.dataset.kind==='json'){s.textContent=JSON.stringify(await r.json(),null,2);return}const b=await r.blob(),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=(r.headers.get('content-disposition')||'download').split('filename=')[1]?.replaceAll('"','')||'download';a.click();s.textContent='Download ready.'};</script></body></html>"""


async def _upload_bytes(upload: UploadFile, *, pdf_only: bool = False) -> bytes:
    suffix = Path(upload.filename or "").suffix.lower()
    if pdf_only and upload.content_type != "application/pdf" and suffix != ".pdf":
        raise HTTPException(status_code=415, detail=f"{upload.filename or 'File'} must be a PDF.")
    payload = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{upload.filename or 'File'} exceeds the 25 MiB limit.")
    if not payload:
        raise HTTPException(status_code=422, detail=f"{upload.filename or 'File'} is empty.")
    return payload


def _pdf_download(data: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_page_selection(value: str, page_count: int) -> list[int]:
    """Parse 1-based page expressions such as `1,3-5` into zero-based indexes."""
    selected: set[int] = set()
    try:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = (int(piece.strip()) for piece in part.split("-", 1))
                if start > end:
                    raise ValueError
                selected.update(range(start, end + 1))
            else:
                selected.add(int(part))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Pages must use a format like 1,3-5.") from exc
    if not selected or min(selected) < 1 or max(selected) > page_count:
        raise HTTPException(status_code=422, detail=f"Pages must be between 1 and {page_count}.")
    return sorted(page - 1 for page in selected)


class ExtractionResult(BaseModel):
    filename: str
    parser: Literal["pymupdf", "liteparse"]
    needs_ocr: bool
    text: str
    page_count: int | None = None
    complexity: list[dict] | None = None


def _native_pdf_text(path: Path) -> tuple[str, int]:
    """Extract an existing PDF text layer without invoking OCR."""
    try:
        with pymupdf.open(path) as document:
            pages = [page.get_text("text") for page in document]
            return "\n\f\n".join(pages).strip(), document.page_count
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid or protected PDF: {exc}") from exc


def _native_pdf_text_bytes(payload: bytes) -> tuple[str, int]:
    """Read a PDF directly from RAM, avoiding a temp file for digital PDFs."""
    try:
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            pages = [page.get_text("text") for page in document]
            return "\n\f\n".join(pages).strip(), document.page_count
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid or protected PDF: {exc}") from exc


async def _run_liteparse(*args: str) -> tuple[int, str, str]:
    """Call the CLI installed by the official Python `liteparse` package."""
    binary = shutil.which("lit")
    if not binary:
        raise HTTPException(
            status_code=503,
            detail="LiteParse is not installed or `lit` is not on PATH. Run `pip install -r requirements.txt`.",
        )
    async with LITPARSE_CONCURRENCY:
        try:
            # subprocess.run is executed off the event loop. This is important on
            # Windows where Uvicorn may select a SelectorEventLoop that does not
            # implement asyncio.create_subprocess_exec.
            completed = await asyncio.to_thread(
                subprocess.run,
                [binary, *args],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="LiteParse timed out after 120 seconds.")
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"Could not start LiteParse: {exc}") from exc
    return completed.returncode, completed.stdout.decode(errors="replace"), completed.stderr.decode(errors="replace")


async def _complexity(path: Path) -> list[dict]:
    # LiteParse intentionally returns non-zero when at least one page needs OCR.
    _, stdout, stderr = await _run_liteparse("is-complex", "--compact", "--quiet", str(path))
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"LiteParse complexity check failed: {stderr.strip()}") from exc
    return result if isinstance(result, list) else []


async def _liteparse_markdown(path: Path) -> str:
    code, stdout, stderr = await _run_liteparse("parse", "--format", "markdown", "--quiet", str(path))
    if code != 0:
        raise HTTPException(status_code=502, detail=f"LiteParse failed: {stderr.strip() or stdout.strip()}")
    return stdout


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True, "liteparse_available": shutil.which("lit") is not None}


@app.post("/tools/merge", summary="Merge multiple PDFs")
async def merge_pdfs(files: list[UploadFile] = File(...)) -> StreamingResponse:
    if len(files) < 2 or len(files) > MAX_MERGE_FILES:
        raise HTTPException(status_code=422, detail=f"Upload between 2 and {MAX_MERGE_FILES} PDF files.")
    output = pymupdf.open()
    total_bytes = 0
    try:
        for upload in files:
            payload = await _upload_bytes(upload, pdf_only=True)
            total_bytes += len(payload)
            if total_bytes > MAX_MERGE_BYTES:
                raise HTTPException(status_code=413, detail="Combined merge upload exceeds the 50 MiB limit.")
            try:
                source = pymupdf.open(stream=payload, filetype="pdf")
            except (pymupdf.FileDataError, RuntimeError) as exc:
                raise HTTPException(status_code=422, detail=f"Invalid PDF: {upload.filename}") from exc
            try:
                output.insert_pdf(source)
            finally:
                source.close()
        if output.page_count == 0:
            raise HTTPException(status_code=422, detail="The uploaded PDFs contain no pages.")
        result = output.tobytes(garbage=4, deflate=True)
    finally:
        output.close()
    return _pdf_download(result, "merged.pdf")


@app.post("/tools/split", summary="Extract selected PDF pages")
async def split_pdf(file: UploadFile = File(...), pages: str = Form(...)) -> StreamingResponse:
    payload = await _upload_bytes(file, pdf_only=True)
    try:
        source = pymupdf.open(stream=payload, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid PDF.") from exc
    try:
        indexes = _parse_page_selection(pages, source.page_count)
        output = pymupdf.open()
        try:
            for index in indexes:
                output.insert_pdf(source, from_page=index, to_page=index)
            result = output.tobytes(garbage=4, deflate=True)
        finally:
            output.close()
    finally:
        source.close()
    return _pdf_download(result, "split.pdf")


@app.post("/tools/image-to-pdf", summary="Convert an image to PDF")
async def image_to_pdf(file: UploadFile = File(...)) -> StreamingResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in IMAGE_SUFFIXES and not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=415, detail="Upload a supported image file.")
    payload = await _upload_bytes(file)
    try:
        image = pymupdf.open(stream=payload, filetype=suffix.removeprefix(".") or None)
        result = image.convert_to_pdf()
        image.close()
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail="The image could not be converted to PDF.") from exc
    return _pdf_download(result, "converted.pdf")


@app.post("/tools/pdf-to-text", summary="Extract native PDF text")
async def pdf_to_text(file: UploadFile = File(...)) -> PlainTextResponse:
    payload = await _upload_bytes(file, pdf_only=True)
    text, _ = _native_pdf_text_bytes(payload)
    return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="extracted.txt"'})


@app.post("/extract", response_model=ExtractionResult)
async def extract(file: UploadFile = File(...)) -> ExtractionResult:
    """Extract PDF/image content using the cheapest parser that preserves quality."""
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if file.content_type not in PDF_CONTENT_TYPES and suffix != ".pdf" and suffix not in IMAGE_SUFFIXES:
        raise HTTPException(status_code=415, detail="Only PDF and supported image files are accepted.")

    payload = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds the 25 MiB limit.")
    if not payload:
        raise HTTPException(status_code=422, detail="Upload is empty.")

    is_pdf = suffix == ".pdf" or file.content_type in PDF_CONTENT_TYPES
    if is_pdf:
        # PDFs with a useful native text layer are already OCR'd/digital: use
        # PyMuPDF directly from RAM. A short one-line PDF may be classified as
        # "sparse" by LiteParse even though no OCR is needed.
        native_text, page_count = _native_pdf_text_bytes(payload)
        if native_text:
            return ExtractionResult(filename=filename, parser="pymupdf", needs_ocr=False, text=native_text, page_count=page_count)

    # LiteParse requires a path. Keep this file request-scoped and outside the
    # system temp tree, which avoids Windows antivirus/ACL locks on OCR files.
    # A single uniquely named file avoids TemporaryDirectory cleanup races on
    # Windows when the OCR subprocess briefly owns a handle.
    runtime_root = Path.cwd()
    path = runtime_root / f"input-{uuid.uuid4().hex}{suffix or '.pdf'}"
    path.write_bytes(payload)
    try:
        if is_pdf:
            complexity = await _complexity(path)
            return ExtractionResult(filename=filename, parser="liteparse", needs_ocr=True, text=await _liteparse_markdown(path), page_count=page_count, complexity=complexity)
        return ExtractionResult(filename=filename, parser="liteparse", needs_ocr=True, text=await _liteparse_markdown(path))
    finally:
        # OCR engines can briefly hold the file on Windows. Do not let cleanup
        # failure mask the extraction response; a later startup cleanup can
        # remove any remaining stale request files.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
