import os
import subprocess
import tempfile
import uuid

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

app = FastAPI(title="DOCX to PDF Converter API")

ALLOWED_EXTENSIONS = {".docx", ".doc", ".odt", ".rtf"}


@app.get("/")
def health_check():
    """Simple health check endpoint (also used by Render)."""
    return {"status": "ok", "message": "DOCX to PDF converter is running"}


@app.post("/convert")
async def convert_docx_to_pdf(file: UploadFile = File(...)):
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

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, file.filename)
        with open(input_path, "wb") as f:
            f.write(await file.read())

        # LibreOffice headless conversion. Each request gets its own
        # temp user profile dir to avoid conflicts under concurrency.
        profile_dir = os.path.join(tmpdir, "lo_profile")
        os.makedirs(profile_dir, exist_ok=True)

        cmd = [
            "libreoffice",
            "--headless",
            "--norestore",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to",
            "pdf",
            "--outdir",
            tmpdir,
            input_path,
        ]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=90
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="Conversion timed out")

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"LibreOffice conversion failed: {result.stderr.strip()}",
            )

        base_name = os.path.splitext(os.path.basename(file.filename))[0]
        generated_pdf = os.path.join(tmpdir, f"{base_name}.pdf")

        if not os.path.exists(generated_pdf):
            raise HTTPException(status_code=500, detail="PDF was not generated")

        # Move the PDF outside the tempdir (which is about to be deleted)
        # so FileResponse can stream it after this function returns.
        persistent_path = os.path.join(
            tempfile.gettempdir(), f"{uuid.uuid4().hex}_{base_name}.pdf"
        )
        os.replace(generated_pdf, persistent_path)

        return FileResponse(
            persistent_path,
            media_type="application/pdf",
            filename=f"{base_name}.pdf",
            background=BackgroundTask(_cleanup_file, persistent_path),
        )


def _cleanup_file(path: str):
    try:
        os.remove(path)
    except OSError:
        pass
