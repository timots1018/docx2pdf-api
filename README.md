# DOCX to PDF Converter API

A small FastAPI service that converts `.docx` (and `.doc`/`.odt`/`.rtf`) files
to PDF using headless LibreOffice, ready to deploy on Render.

## Why LibreOffice?

Pure-Python docx→PDF conversion isn't reliable — libraries like `docx2pdf`
depend on Microsoft Word or LibreOffice being installed anyway, and most
"pure Python" renderers don't reproduce Word formatting accurately.
Calling `libreoffice --headless --convert-to pdf` is the standard,
dependable approach on Linux servers.

## Files

- `app.py` — FastAPI app with `POST /convert`
- `Dockerfile` — installs LibreOffice + Python deps
- `requirements.txt` — Python dependencies
- `render.yaml` — optional Render Blueprint for one-click deploy

## Run locally with Docker

```bash
docker build -t docx2pdf-api .
docker run -p 10000:10000 docx2pdf-api
```

Test it:

```bash
curl -X POST http://localhost:10000/convert \
  -F "file=@/path/to/your/document.docx" \
  -o converted.pdf
```

## Run locally without Docker (needs LibreOffice installed on your machine)

```bash
# macOS: brew install --cask libreoffice
# Ubuntu: sudo apt-get install libreoffice
pip install -r requirements.txt
uvicorn app:app --reload --port 10000
```

## Deploy to Render

Render's native Python runtime can't install system packages (LibreOffice),
so this uses Render's **Docker** environment instead.

### Option A — Blueprint (render.yaml), fastest

1. Push this folder to a GitHub repo.
2. In the Render dashboard: **New +** → **Blueprint**.
3. Connect the repo. Render detects `render.yaml` and configures everything
   automatically.
4. Click **Apply** to deploy.

### Option B — Manual setup

1. Push this folder to a GitHub repo.
2. In the Render dashboard: **New +** → **Web Service**.
3. Connect your repo.
4. Set **Environment** to `Docker` (Render auto-detects the `Dockerfile`).
5. Leave build/start commands blank — they come from the `Dockerfile`.
6. Choose a plan (Free tier works, but conversion is CPU/RAM-heavy — the
   free tier's 512MB RAM can be tight for large documents; Starter plan is
   more reliable for production use).
7. Click **Create Web Service**.

Render will build the Docker image (installing LibreOffice takes a few
minutes on first deploy) and give you a public URL like
`https://docx-to-pdf-api.onrender.com`.

## Usage once deployed

```bash
curl -X POST https://YOUR-APP.onrender.com/convert \
  -F "file=@document.docx" \
  -o converted.pdf
```

Or from Python:

```python
import requests

with open("document.docx", "rb") as f:
    resp = requests.post(
        "https://YOUR-APP.onrender.com/convert",
        files={"file": f},
    )

with open("converted.pdf", "wb") as out:
    out.write(resp.content)
```

## Notes & limitations

- **Free tier cold starts**: Render's free plan spins the service down after
  inactivity; the first request after idling can take ~30-60s.
- **Concurrency**: LibreOffice headless isn't great at handling many
  simultaneous conversions in one process. Each request uses an isolated
  user profile directory to reduce conflicts, but for high traffic
  consider a task queue (e.g. Celery + Redis) so conversions run one at a
  time or in a worker pool.
- **File size / timeout**: the conversion has a 90-second subprocess
  timeout; large or complex documents may need a longer timeout or a
  bigger instance.
- **Security**: add authentication (an API key header, for example) before
  exposing this publicly — as written, anyone with the URL can use it.
