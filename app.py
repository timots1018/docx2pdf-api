import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from contextlib import asynccontextmanager

import fitz  # PyMuPDF
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("docx2pdf")

ALLOWED_EXTENSIONS = {".docx", ".doc", ".odt", ".rtf"}

# --- Concurrency tuning -----------------------------------------------------
# Each LibreOffice conversion spawns a full soffice process (CPU + memory
# heavy). Running unlimited conversions in parallel is what causes crashes /
# profile-lock errors under load. Cap it and queue the rest.
MAX_CONCURRENT_CONVERSIONS = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", "2"))
CONVERSION_TIMEOUT_SECONDS = int(os.environ.get("CONVERSION_TIMEOUT_SECONDS", "90"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

_conversion_semaphore: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conversion_semaphore
    _conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)
    logger.info(
        "Startup: max_concurrent_conversions=%s timeout=%ss max_upload=%sMB",
        MAX_CONCURRENT_CONVERSIONS,
        CONVERSION_TIMEOUT_SECONDS,
        MAX_UPLOAD_BYTES // (1024 * 1024),
    )
    yield


app = FastAPI(title="DOCX to PDF Converter API", lifespan=lifespan)


@app.get("/")
def health_check():
    """Simple health check endpoint (also used by Render)."""
    return {
        "status": "ok",
        "message": "DOCX to PDF converter is running",
        "max_concurrent_conversions": MAX_CONCURRENT_CONVERSIONS,
    }


def _safe_stem(filename: str) -> str:
    """Strip directories and unsafe chars, keep it short and predictable."""
    base = os.path.basename(filename or "document")
    stem = os.path.splitext(base)[0]
    stem = "".join(c for c in stem if c.isalnum() or c in (" ", "-", "_")).strip()
    return stem or "document"


async def _run_libreoffice(cmd: list[str], cwd: str) -> tuple[int, str]:
    """Run LibreOffice asynchronously so it never blocks the event loop."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CONVERSION_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise

    return proc.returncode, stderr.decode(errors="replace")


async def _convert_with_retry(input_path: str, workdir: str, attempts: int = 2) -> str:
    """
    Runs the actual conversion, retrying once on transient LibreOffice
    failures (e.g. a fresh profile occasionally throws a first-launch error
    under concurrent load).
    """
    last_error = "unknown error"

    for attempt in range(1, attempts + 1):
        profile_dir = os.path.join(workdir, f"lo_profile_{attempt}")
        os.makedirs(profile_dir, exist_ok=True)

        cmd = [
            "libreoffice",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nofirststartwizard",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            "pdf",
            "--outdir",
            workdir,
            input_path,
        ]

        try:
            returncode, stderr = await _run_libreoffice(cmd, workdir)
        except asyncio.TimeoutError:
            last_error = "conversion timed out"
            logger.warning("Attempt %s timed out for %s", attempt, input_path)
            continue

        if returncode == 0:
            base_name = os.path.splitext(os.path.basename(input_path))[0]
            generated_pdf = os.path.join(workdir, f"{base_name}.pdf")
            if os.path.exists(generated_pdf):
                return generated_pdf
            last_error = "LibreOffice reported success but no PDF was produced"
        else:
            last_error = stderr.strip() or f"exit code {returncode}"
            logger.warning("Attempt %s failed for %s: %s", attempt, input_path, last_error)

    raise RuntimeError(last_error)


def _strip_blank_pages(pdf_path: str) -> int:
    """
    Removes pages that are genuinely empty (no text, no images, no vector
    drawings). This targets the common LibreOffice artifact of inserting
    filler pages (e.g. from odd/even section breaks or trailing empty
    paragraphs) that were not visually present in the source document.

    Returns the number of pages removed. Leaves the file untouched (and
    returns 0) if removal would empty the document entirely, or if there's
    nothing to remove.
    """
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count <= 1:
            return 0

        blank_indices = []
        for i, page in enumerate(doc):
            has_text = bool(page.get_text("text").strip())
            has_images = len(page.get_images(full=True)) > 0
            has_drawings = len(page.get_drawings()) > 0
            if not has_text and not has_images and not has_drawings:
                blank_indices.append(i)

        if not blank_indices or len(blank_indices) == doc.page_count:
            # Nothing to do, or removing would delete the whole document.
            return 0

        for i in reversed(blank_indices):
            doc.delete_page(i)

        doc.saveIncremental() if doc.can_save_incrementally() else doc.save(
            pdf_path + ".tmp"
        )
        if not doc.can_save_incrementally():
            os.replace(pdf_path + ".tmp", pdf_path)

        return len(blank_indices)
    finally:
        doc.close()


def _cleanup_paths(*paths: str):
    for path in paths:
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


@app.post("/convert")
async def convert_docx_to_pdf(
    file: UploadFile = File(...),
    strip_blank_pages: bool = Query(
        True, description="Remove pages LibreOffice inserted with no text/images/drawings."
    ),
):
    """
    Accepts a .docx (or .doc/.odt/.rtf) file upload and returns the
    converted PDF as a file download.
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    assert _conversion_semaphore is not None

    # Each request gets its own directory (input, output, LO profiles all
    # isolated) so concurrent requests can never collide.
    workdir = tempfile.mkdtemp(prefix="docx2pdf_")
    persistent_path = None

    try:
        # Stream the upload to disk in chunks instead of loading it fully
        # into memory, and enforce a size cap.
        input_filename = f"{uuid.uuid4().hex}{ext}"
        input_path = os.path.join(workdir, input_filename)
        size = 0
        with open(input_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds max upload size of {MAX_UPLOAD_BYTES // (1024*1024)}MB",
                    )
                f.write(chunk)

        async with _conversion_semaphore:
            try:
                generated_pdf = await _convert_with_retry(input_path, workdir)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="Conversion timed out")
            except RuntimeError as e:
                raise HTTPException(
                    status_code=500, detail=f"LibreOffice conversion failed: {e}"
                )

        if strip_blank_pages:
            try:
                removed = _strip_blank_pages(generated_pdf)
                if removed:
                    logger.info("Removed %s blank page(s) from %s", removed, file.filename)
            except Exception as e:
                # Never fail the whole request over blank-page cleanup;
                # just serve the unmodified PDF.
                logger.warning("Blank-page stripping failed, serving as-is: %s", e)

        display_name = _safe_stem(file.filename)
        persistent_path = os.path.join(
            tempfile.gettempdir(), f"{uuid.uuid4().hex}_{display_name}.pdf"
        )
        shutil.move(generated_pdf, persistent_path)

        return FileResponse(
            persistent_path,
            media_type="application/pdf",
            filename=f"{display_name}.pdf",
            background=BackgroundTask(_cleanup_paths, workdir, persistent_path),
        )
    except HTTPException:
        _cleanup_paths(workdir)
        if persistent_path:
            _cleanup_paths(persistent_path)
        raise
    except Exception:
        _cleanup_paths(workdir)
        if persistent_path:
            _cleanup_paths(persistent_path)
        raise