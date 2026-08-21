import os
import asyncio
import logging
import mimetypes
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    RPCError,
)
from telethon.tl.types import DocumentAttributeVideo


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("telegram-auto-uploader")


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

def env_required(name: str) -> str:
    value = os.getenv(name)

    if value is None:
        raise RuntimeError(f"Missing Railway variable: {name}")

    value = value.strip()

    if not value:
        raise RuntimeError(f"Empty Railway variable: {name}")

    return value


def load_api_id() -> int:
    raw = env_required("TELEGRAM_API_ID")

    # Accept accidental spaces/newlines.
    raw = raw.strip()

    try:
        api_id = int(raw)
    except ValueError:
        raise RuntimeError(
            "TELEGRAM_API_ID must contain numbers only."
        )

    if api_id <= 0:
        raise RuntimeError(
            "TELEGRAM_API_ID must be a positive integer."
        )

    return api_id


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID = env_required("CHANNEL_ID")
SESSION_STRING = env_required("SESSION_STRING")
TELEGRAM_API_HASH = env_required("TELEGRAM_API_HASH")
TELEGRAM_API_ID = load_api_id()

# Optional. Kept because you already have this Railway variable.
TELEGRAM_LOCAL = os.getenv("TELEGRAM_LOCAL", "").strip()


# ============================================================
# LIMITS / SETTINGS
# ============================================================

# This is an application safety limit.
# Telegram itself may impose its own limits.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024  # 100 GB

CHUNK_SIZE = 1024 * 1024  # 1 MB

UPLOAD_TIMEOUT = 60 * 60 * 12  # 12 hours

MAX_RETRIES = 5

RETRY_BASE_SECONDS = 5


# ============================================================
# GLOBAL TELEGRAM CLIENT
# ============================================================

telegram_client: TelegramClient | None = None
telegram_entity = None

upload_lock = asyncio.Lock()


# ============================================================
# FILE HELPERS
# ============================================================

def safe_filename(filename: str | None) -> str:
    if not filename:
        return "uploaded_file"

    name = Path(filename).name

    # Remove dangerous/null characters.
    name = name.replace("\x00", "")

    if not name:
        return "uploaded_file"

    return name


def get_mime(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)

    if mime:
        return mime

    return "application/octet-stream"


def is_image(filename: str) -> bool:
    return get_mime(filename).startswith("image/")


def is_video(filename: str) -> bool:
    return get_mime(filename).startswith("video/")


def is_audio(filename: str) -> bool:
    return get_mime(filename).startswith("audio/")


def human_size(size: int) -> str:
    value = float(size)

    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PB"


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def start_telegram():
    global telegram_client
    global telegram_entity

    log.info("Starting Telegram client...")

    telegram_client = TelegramClient(
        SESSION_STRING,
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        device_model="Telegram Auto Uploader",
        system_version="Android",
        app_version="1.0",
        lang_code="en",
        system_lang_code="en",
        auto_reconnect=True,
        connection_retries=10,
        retry_delay=5,
    )

    try:
        await telegram_client.connect()

        if not await telegram_client.is_user_authorized():
            raise RuntimeError(
                "SESSION_STRING is not authorized. "
                "Generate a valid Telethon session string."
            )

        me = await telegram_client.get_me()

        if me:
            username = getattr(me, "username", None)
            first_name = getattr(me, "first_name", "")

            log.info(
                "Telegram connected as: %s %s",
                first_name or "",
                f"@{username}" if username else "",
            )

        telegram_entity = await telegram_client.get_entity(
            CHANNEL_ID
        )

        log.info("Telegram target channel resolved successfully.")

        # Test whether the account can access the target.
        await telegram_client.get_permissions(
            telegram_entity,
            me,
        )

        log.info("Telegram channel access verified.")

    except Exception:
        log.exception("Telegram startup failed.")

        if telegram_client:
            try:
                await telegram_client.disconnect()
            except Exception:
                pass

        telegram_client = None
        telegram_entity = None

        raise


# ============================================================
# TELEGRAM UPLOAD
# ============================================================

async def upload_to_telegram(
    file_path: str,
    filename: str,
    file_size: int,
):
    if telegram_client is None:
        raise RuntimeError("Telegram client is not running.")

    if telegram_entity is None:
        raise RuntimeError("Telegram channel is not available.")

    log.info(
        "Telegram upload starting: %s (%s)",
        filename,
        human_size(file_size),
    )

    last_progress = -1

    def progress_callback(current: int, total: int):
        nonlocal last_progress

        if total <= 0:
            return

        percent = int((current / total) * 100)

        # Log every 5%.
        rounded = (percent // 5) * 5

        if rounded != last_progress:
            last_progress = rounded

            log.info(
                "Uploading %s: %d%%",
                filename,
                percent,
            )

    mime = get_mime(filename)

    # Preserve normal Telegram media behaviour:
    # images -> photo where supported
    # videos -> video where supported
    # everything else -> document
    force_document = not (
        is_image(filename)
        or is_video(filename)
        or is_audio(filename)
    )

    # Video attributes are optional. We only use normal send_file
    # for maximum compatibility.
    kwargs = {
        "caption": filename,
        "force_document": force_document,
        "supports_streaming": is_video(filename),
        "progress_callback": progress_callback,
    }

    # Retry temporary Telegram/network failures.
    for attempt in range(1, MAX_RETRIES + 1):

        try:
            message = await telegram_client.send_file(
                telegram_entity,
                file_path,
                **kwargs,
            )

            log.info(
                "Telegram upload completed: %s",
                filename,
            )

            return message

        except FloodWaitError as exc:
            wait_seconds = int(exc.seconds)

            log.warning(
                "Telegram FloodWait: waiting %s seconds.",
                wait_seconds,
            )

            await asyncio.sleep(wait_seconds)

        except (ConnectionError, TimeoutError) as exc:
            if attempt >= MAX_RETRIES:
                raise

            delay = RETRY_BASE_SECONDS * attempt

            log.warning(
                "Temporary network error on attempt %d/%d: %s. "
                "Retrying in %d seconds.",
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )

            await asyncio.sleep(delay)

        except RPCError as exc:
            # Retry a few times for transient Telegram RPC errors.
            if attempt >= MAX_RETRIES:
                raise

            delay = RETRY_BASE_SECONDS * attempt

            log.warning(
                "Telegram RPC error on attempt %d/%d: %s. "
                "Retrying in %d seconds.",
                attempt,
                MAX_RETRIES,
                exc,
                delay,
            )

            await asyncio.sleep(delay)

    raise RuntimeError(
        "Telegram upload failed after all retries."
    )


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    log.info("Application startup.")

    try:
        await start_telegram()

        log.info(
            "Telegram Auto Uploader is ready."
        )

        yield

    except Exception:
        log.exception(
            "Application startup failed."
        )

        # Keep FastAPI alive so Railway can show logs,
        # but Telegram upload endpoints will report the issue.
        yield

    finally:
        log.info("Application shutting down.")

        if telegram_client:
            try:
                await telegram_client.disconnect()
                log.info("Telegram client disconnected.")
            except Exception:
                log.exception(
                    "Error while disconnecting Telegram."
                )


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Telegram Auto Uploader",
    version="2.0.0",
    lifespan=lifespan,
)


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
async def home():

    telegram_ok = (
        telegram_client is not None
        and telegram_client.is_connected()
    )

    return {
        "status": "running",
        "telegram_connected": telegram_ok,
        "service": "Telegram Auto Uploader",
    }


@app.get("/health")
async def health():

    telegram_ok = (
        telegram_client is not None
        and telegram_client.is_connected()
    )

    return {
        "ok": telegram_ok,
        "telegram_connected": telegram_ok,
    }


# ============================================================
# UPLOAD ENDPOINT
# ============================================================

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):

    filename = safe_filename(file.filename)

    log.info(
        "Incoming upload: %s",
        filename,
    )

    if telegram_client is None:
        raise HTTPException(
            status_code=503,
            detail="Telegram client is not connected.",
        )

    # Only one large upload at a time.
    # This prevents RAM/disk/network overload.
    async with upload_lock:

        temp_path = None
        total_size = 0

        try:

            # ------------------------------------------------
            # Create temporary file.
            # ------------------------------------------------

            suffix = Path(filename).suffix

            temp_file = tempfile.NamedTemporaryFile(
                mode="wb",
                delete=False,
                suffix=suffix,
                prefix="telegram_upload_",
            )

            temp_path = temp_file.name

            log.info(
                "Temporary file: %s",
                temp_path,
            )

            # ------------------------------------------------
            # Stream incoming file to disk.
            # ------------------------------------------------

            while True:

                chunk = await file.read(CHUNK_SIZE)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "File exceeds application "
                            "size limit."
                        ),
                    )

                temp_file.write(chunk)

            temp_file.flush()
            temp_file.close()

            if total_size <= 0:
                raise HTTPException(
                    status_code=400,
                    detail="Empty file.",
                )

            log.info(
                "Download/receive completed: %s (%s)",
                filename,
                human_size(total_size),
            )

            # ------------------------------------------------
            # Upload to Telegram.
            # ------------------------------------------------

            message = await upload_to_telegram(
                temp_path,
                filename,
                total_size,
            )

            message_id = getattr(
                message,
                "id",
                None,
            )

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    "size": total_size,
                    "size_human": human_size(total_size),
                    "telegram_message_id": message_id,
                    "message": (
                        "Uploaded to Telegram successfully."
                    ),
                }
            )

        except HTTPException:
            raise

        except Exception as exc:

            log.exception(
                "Upload failed: %s",
                filename,
            )

            raise HTTPException(
                status_code=500,
                detail=f"Upload failed: {str(exc)}",
            )

        finally:

            # ------------------------------------------------
            # ALWAYS delete temporary file.
            # ------------------------------------------------

            if temp_path:

                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)

                        log.info(
                            "Temporary file deleted: %s",
                            filename,
                        )

                except Exception:
                    log.exception(
                        "Could not delete temporary file: %s",
                        temp_path,
                    )

            try:
                await file.close()
            except Exception:
                pass


# ============================================================
# RAILWAY ENTRY POINT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
