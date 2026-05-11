from fastapi import FastAPI, UploadFile
from fastapi.responses import JSONResponse
import zipfile
import tempfile
from pathlib import Path

app = FastAPI()

@app.post("/upload")
async def upload_archive(file: UploadFile):
    if not file.filename.endswith(".zip"):
        return JSONResponse(status_code=400, 
                            content={"error": "Only .zip archives supported"})

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)

        archive_path = tmpdir / file.filename
        archive_path.write_bytes(await file.read())

        extract_dir = tmpdir / "extracted"
        extract_dir.mkdir()

        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(extract_dir)

        files = [
            str(p.relative_to(extract_dir))
            for p in extract_dir.rglob("*")
            if p.is_file()
        ]

        return {"filename": file.filename, "files": files}
