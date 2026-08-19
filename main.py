import os
import tempfile
from pathlib import Path

import requests
from fastapi import FastAPI, UploadFile, File, HTTPException

app = FastAPI()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB


@app.get("/")
def home():
    return {"status": "Telegram Auto Uploader is running"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not BOT_TOKEN or not CHANNEL_ID:
        raise HTTPException(
            status_code=500,
            detail="BOT_TOKEN or CHANNEL_ID is missing"
        )

    filename = Path(file.filename or "video").name

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix) as temp:
        temp_path = temp.name

        total_size = 0

        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break

            total_size += len(chunk)

            if total_size > MAX_FILE_SIZE:
                os.remove(temp_path)
                raise HTTPException(
                    status_code=413,
                    detail="File is larger than 2 GB"
                )

            temp.write(chunk)

    try:
        telegram_url = (
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        )

        with open(temp_path, "rb") as document:
            response = requests.post(
                telegram_url,
                data={
                    "chat_id": CHANNEL_ID
                },
                files={
                    "document": (
                        filename,
                        document,
                        "application/octet-stream"
                    )
                },
                timeout=3600
            )

        result = response.json()

        if not response.ok or not result.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=f"Telegram upload failed: {result}"
            )

        return {
            "success": True,
            "message": "Uploaded to Telegram successfully",
            "filename": filename
        }

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
