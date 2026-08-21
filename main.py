import os
import json
import time
import asyncio
import hashlib
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional

from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeFilename


# =========================================================
# CONFIG
# =========================================================

API_ID_TEXT = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
CHANNEL_ID_TEXT = os.getenv("CHANNEL_ID", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")

TEMP_DIR = Path(
    os.getenv(
        "TEMP_DIR",
        "/tmp/telegram_auto_uploader"
    )
)

TEMP_DIR.mkdir(
    parents=True,
    exist_ok=True
)

STATE_FILE = TEMP_DIR / "uploaded_files.json"

MAX_RETRIES = 5

# Only one Telegram upload at a time.
UPLOAD_LOCK = asyncio.Lock()


# =========================================================
# VALIDATE CONFIG
# =========================================================

def get_api_id() -> int:
    try:
        return int(API_ID_TEXT)
    except (TypeError, ValueError):
        raise RuntimeError(
            "API_ID is missing or invalid."
        )


def get_channel_id():
    if not CHANNEL_ID_TEXT:
        raise RuntimeError(
            "CHANNEL_ID is missing."
        )

    # Numeric channel ID
    try:
        return int(CHANNEL_ID_TEXT)
    except ValueError:
        # Username such as @mychannel
        return CHANNEL_ID_TEXT


# =========================================================
# FASTAPI
# =========================================================

app = FastAPI(
    title="Telegram Large File Auto Uploader",
    version="3.0.0"
)


# =========================================================
# TELEGRAM CLIENT
# =========================================================

telegram_client: Optional[TelegramClient] = None


async def get_telegram_client() -> TelegramClient:

    global telegram_client

    if telegram_client is not None:

        if not telegram_client.is_connected():
            await telegram_client.connect()

        return telegram_client

    if not SESSION_STRING:
        raise RuntimeError(
            "SESSION_STRING is missing."
        )

    api_id = get_api_id()

    telegram_client = TelegramClient(
        StringSession(SESSION_STRING),
        api_id,
        API_HASH
    )

    await telegram_client.connect()

    if not await telegram_client.is_user_authorized():

        await telegram_client.disconnect()

        telegram_client = None

        raise RuntimeError(
            "Telegram session is not authorized."
        )

    return telegram_client


# =========================================================
# HASH DATABASE
# =========================================================

def load_uploaded_files() -> dict:

    try:

        if not STATE_FILE.exists():
            return {}

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            data = json.load(file)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_uploaded_files(data: dict) -> None:

    temporary_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    try:

        with open(
            temporary_file,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temporary_file,
            STATE_FILE
        )

    except Exception:

        try:

            if temporary_file.exists():
                temporary_file.unlink()

        except Exception:
            pass


# =========================================================
# SHA256
# =========================================================

def calculate_sha256(
    path: Path
) -> str:

    sha256 = hashlib.sha256()

    with open(
        path,
        "rb"
    ) as file:

        while True:

            chunk = file.read(
                1024 * 1024
            )

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest()


# =========================================================
# SAFE FILENAME
# =========================================================

def safe_filename(
    filename: Optional[str]
) -> str:

    if not filename:
        return "uploaded_file"

    name = Path(
        filename
    ).name

    name = name.replace(
        "\x00",
        ""
    ).strip()

    if not name:
        return "uploaded_file"

    return name


# =========================================================
# TELEGRAM UPLOAD
# =========================================================

async def send_file_to_telegram(
    path: Path,
    filename: str
) -> dict:

    client = await get_telegram_client()

    channel = get_channel_id()

    last_error = None

    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            message = await client.send_file(
                entity=channel,
                file=str(path),
                force_document=True,
                attributes=[
                    DocumentAttributeFilename(
                        file_name=filename
                    )
                ]
            )

            return {
                "success": True,
                "attempt": attempt,
                "message_id": message.id
            }

        except FloodWaitError as error:

            last_error = (
                f"Telegram FloodWait: "
                f"{error.seconds} seconds"
            )

            await asyncio.sleep(
                error.seconds
            )

        except Exception as error:

            last_error = str(error)

            if attempt < MAX_RETRIES:

                await asyncio.sleep(
                    min(
                        attempt * 5,
                        60
                    )
                )

    return {
        "success": False,
        "attempt": MAX_RETRIES,
        "error": last_error
    }


# =========================================================
# STARTUP
# =========================================================

@app.on_event("startup")
async def startup_event():

    try:

        await get_telegram_client()

        print(
            "Telegram client connected successfully."
        )

    except Exception as error:

        print(
            "Telegram startup warning:",
            error
        )

        # Do not crash FastAPI.
        # The endpoint will report the actual error.


# =========================================================
# SHUTDOWN
# =========================================================

@app.on_event("shutdown")
async def shutdown_event():

    global telegram_client

    if telegram_client is not None:

        try:
            await telegram_client.disconnect()
        except Exception:
            pass

        telegram_client = None


# =========================================================
# HOME
# =========================================================

@app.get("/")
async def home():

    return {
        "status": "online",
        "service": "Telegram Large File Auto Uploader",
        "version": "3.0.0",
        "protocol": "MTProto",
        "large_files": True
    }


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
async def health():

    try:

        client = await get_telegram_client()

        me = await client.get_me()

        return {
            "status": "healthy",
            "telegram_connected": True,
            "telegram_user": (
                getattr(
                    me,
                    "username",
                    None
                )
                or getattr(
                    me,
                    "first_name",
                    None
                )
            )
        }

    except Exception as error:

        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "telegram_connected": False,
                "error": str(error)
            }
        )


# =========================================================
# UPLOAD
# =========================================================

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...)
):

    filename = safe_filename(
        file.filename
    )

    temporary_path: Optional[Path] = None

    async with UPLOAD_LOCK:

        try:

            # -------------------------------------------------
            # SAVE UPLOADED FILE TO DISK
            # -------------------------------------------------

            suffix = Path(
                filename
            ).suffix

            with NamedTemporaryFile(
                mode="wb",
                delete=False,
                suffix=suffix,
                dir=TEMP_DIR
            ) as temporary_file:

                temporary_path = Path(
                    temporary_file.name
                )

                while True:

                    chunk = await file.read(
                        1024 * 1024
                    )

                    if not chunk:
                        break

                    temporary_file.write(
                        chunk
                    )

            # -------------------------------------------------
            # CHECK FILE
            # -------------------------------------------------

            if not temporary_path.exists():

                return JSONResponse(
                    status_code=500,
                    content={
                        "success": False,
                        "safe_to_delete": False,
                        "error": (
                            "Temporary file "
                            "was not created."
                        )
                    }
                )

            file_size = (
                temporary_path.stat().st_size
            )

            if file_size <= 0:

                return JSONResponse(
                    status_code=400,
                    content={
                        "success": False,
                        "safe_to_delete": False,
                        "error": "Empty file."
                    }
                )

            # -------------------------------------------------
            # HASH
            # -------------------------------------------------

            file_hash = await asyncio.to_thread(
                calculate_sha256,
                temporary_path
            )

            uploaded_files = (
                load_uploaded_files()
            )

            # -------------------------------------------------
            # DUPLICATE
            # -------------------------------------------------

            if file_hash in uploaded_files:

                return {
                    "success": True,
                    "duplicate": True,
                    "filename": filename,
                    "size": file_size,
                    "sha256": file_hash,
                    "safe_to_delete": True,
                    "message": (
                        "File was already "
                        "uploaded successfully."
                    )
                }

            # -------------------------------------------------
            # TELEGRAM
            # -------------------------------------------------

            result = await send_file_to_telegram(
                temporary_path,
                filename
            )

            # -------------------------------------------------
            # SUCCESS
            # -------------------------------------------------

            if result.get("success"):

                uploaded_files[file_hash] = {
                    "filename": filename,
                    "size": file_size,
                    "message_id": result.get(
                        "message_id"
                    ),
                    "timestamp": int(
                        time.time()
                    )
                }

                save_uploaded_files(
                    uploaded_files
                )

                return {
                    "success": True,
                    "duplicate": False,
                    "filename": filename,
                    "size": file_size,
                    "sha256": file_hash,
                    "attempts": result.get(
                        "attempt"
                    ),
                    "message_id": result.get(
                        "message_id"
                    ),

                    # Android deletes the
                    # original only after this.
                    "safe_to_delete": True,

                    "message": (
                        "Telegram upload "
                        "completed successfully."
                    )
                }

            # -------------------------------------------------
            # FAILURE
            # -------------------------------------------------

            return JSONResponse(
                status_code=502,
                content={
                    "success": False,
                    "filename": filename,
                    "size": file_size,
                    "safe_to_delete": False,
                    "attempts": result.get(
                        "attempt"
                    ),
                    "error": result.get(
                        "error"
                    )
                }
            )

        except Exception as error:

            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "filename": filename,
                    "safe_to_delete": False,
                    "error": str(error)
                }
            )

        finally:

            try:
                await file.close()
            except Exception:
                pass

            # -------------------------------------------------
            # SERVER TEMP FILE CLEANUP
            # -------------------------------------------------

            if temporary_path is not None:

                try:

                    if temporary_path.exists():
                        temporary_path.unlink()

                except Exception:
                    pass


# =========================================================
# TELEGRAM TEST
# =========================================================

@app.get("/telegram-test")
async def telegram_test():

    try:

        client = await get_telegram_client()

        me = await client.get_me()

        return {
            "success": True,
            "connected": True,
            "id": me.id,
            "username": getattr(
                me,
                "username",
                None
            ),
            "name": getattr(
                me,
                "first_name",
                None
            )
        }

    except Exception as error:

        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "connected": False,
                "error": str(error)
            }
    )
