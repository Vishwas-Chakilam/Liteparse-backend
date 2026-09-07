from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path
from typing import Literal

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

app = FastAPI(title="LiteParse extraction API", version="0.1.0")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PDF_CONTENT_TYPES = {"application/pdf"}
IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp"}


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
    try:
        process = await asyncio.create_subprocess_exec(
            binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail=f"Could not start LiteParse: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(status_code=504, detail="LiteParse timed out after 120 seconds.")
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


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
