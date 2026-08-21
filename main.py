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
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, RPCError


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("telegram-auto-uploader")


# ============================================================
# ENVIRONMENT
# ============================================================

def env_required(name: str) -> str:
    value = os.getenv(name)

    if value is None:
        raise RuntimeError(
            f"Missing Railway variable: {name}"
        )

    value = value.strip()

    if not value:
        raise RuntimeError(
            f"Empty Railway variable: {name}"
        )

    return value


def load_api_id() -> int:
    raw = env_required("TELEGRAM_API_ID")

    try:
        api_id = int(raw)
    except ValueError:
        raise RuntimeError(
            "TELEGRAM_API_ID must contain numbers only."
        )

    if api_id <= 0:
        raise RuntimeError(
            "TELEGRAM_API_ID must be positive."
        )

    return api_id


# Required
TELEGRAM_API_ID = load_api_id()
TELEGRAM_API_HASH = env_required(
    "TELEGRAM_API_HASH"
)
SESSION_STRING = env_required(
    "SESSION_STRING"
)

# Channel can be numeric -100xxxxxxxxxx
# or @username
CHANNEL_ID = env_required("CHANNEL_ID")

# Optional variables
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    ""
).strip()

TELEGRAM_LOCAL = os.getenv(
    "TELEGRAM_LOCAL",
    ""
).strip()


# ============================================================
# SETTINGS
# ============================================================

CHUNK_SIZE = 1024 * 1024  # 1 MB

# Application-side safety limit.
# Actual Telegram limits may differ.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024

MAX_RETRIES = 5

RETRY_BASE_SECONDS = 5


# ============================================================
# GLOBALS
# ============================================================

telegram_client = None
telegram_entity = None

upload_lock = asyncio.Lock()


# ============================================================
# FILE HELPERS
# ============================================================

def safe_filename(filename: str | None) -> str:
    if not filename:
        return "uploaded_file"

    name = Path(filename).name

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

    for unit in [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
        "PB",
    ]:
        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} EB"


# ============================================================
# TELEGRAM STARTUP
# ============================================================

async def start_telegram():
    global telegram_client
    global telegram_entity

    log.info(
        "Starting Telegram client..."
    )

    # IMPORTANT:
    # StringSession prevents Telethon from treating
    # SESSION_STRING as an SQLite filename.
    telegram_client = TelegramClient(
        StringSession(SESSION_STRING),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,

        device_model="Telegram Auto Uploader",
        system_version="Railway",
        app_version="2.1.0",

        lang_code="en",
        system_lang_code="en",

        auto_reconnect=True,
        connection_retries=10,
        retry_delay=5,
    )

    try:

        await telegram_client.connect()

        log.info(
            "Telegram connection established."
        )

        # ----------------------------------------------------
        # AUTHORIZATION CHECK
        # ----------------------------------------------------

        authorized = (
            await telegram_client.is_user_authorized()
        )

        if not authorized:
            raise RuntimeError(
                "SESSION_STRING is not authorized. "
                "Generate a new Telethon StringSession."
            )

        # ----------------------------------------------------
        # ACCOUNT INFO
        # ----------------------------------------------------

        me = await telegram_client.get_me()

        if me:

            username = getattr(
                me,
                "username",
                None,
            )

            first_name = getattr(
                me,
                "first_name",
                "",
            )

            if username:
                account_name = (
                    f"{first_name} "
                    f"(@{username})"
                )
            else:
                account_name = first_name

            log.info(
                "Telegram logged in as: %s | ID: %s",
                account_name,
                me.id,
            )

        # ----------------------------------------------------
        # CHANNEL RESOLUTION
        # ----------------------------------------------------

        log.info(
            "Resolving Telegram target..."
        )

        telegram_entity = (
            await telegram_client.get_entity(
                CHANNEL_ID
            )
        )

        title = getattr(
            telegram_entity,
            "title",
            None,
        )

        username = getattr(
            telegram_entity,
            "username",
            None,
        )

        if title:
            log.info(
                "Target resolved: %s",
                title,
            )

        elif username:
            log.info(
                "Target resolved: @%s",
                username,
            )

        else:
            log.info(
                "Telegram target resolved successfully."
            )

        log.info(
            "Telegram Auto Uploader is READY."
        )

    except Exception:

        log.exception(
            "Telegram startup failed."
        )

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
        raise RuntimeError(
            "Telegram client is not running."
        )

    if telegram_entity is None:
        raise RuntimeError(
            "Telegram target is not available."
        )

    log.info(
        "Starting Telegram upload: %s | %s",
        filename,
        human_size(file_size),
    )

    last_logged_percent = -1

    def progress_callback(
        current: int,
        total: int,
    ):

        nonlocal last_logged_percent

        if total <= 0:
            return

        percent = int(
            (current / total) * 100
        )

        # Log every 5%.
        rounded = (
            percent // 5
        ) * 5

        if (
            rounded !=
            last_logged_percent
        ):

            last_logged_percent = rounded

            log.info(
                "Upload %s: %d%%",
                filename,
                percent,
            )

    mime = get_mime(filename)

    # --------------------------------------------------------
    # FILE TYPE
    # --------------------------------------------------------

    if mime.startswith("video/"):

        force_document = False
        supports_streaming = True

    elif mime.startswith("image/"):

        force_document = False
        supports_streaming = False

    elif mime.startswith("audio/"):

        force_document = False
        supports_streaming = False

    else:

        force_document = True
        supports_streaming = False

    # --------------------------------------------------------
    # CAPTION
    # --------------------------------------------------------

    caption = filename

    kwargs = {
        "caption": caption,
        "force_document": force_document,
        "supports_streaming": supports_streaming,
        "progress_callback": progress_callback,
    }

    # --------------------------------------------------------
    # RETRY LOOP
    # --------------------------------------------------------

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            message = (
                await telegram_client.send_file(
                    telegram_entity,
                    file_path,
                    **kwargs,
                )
            )

            log.info(
                "Telegram upload completed: %s",
                filename,
            )

            return message

        # ----------------------------------------------------
        # FLOOD WAIT
        # ----------------------------------------------------

        except FloodWaitError as exc:

            wait_seconds = int(
                exc.seconds
            )

            log.warning(
                "Telegram FloodWait: "
                "%s seconds.",
                wait_seconds,
            )

            await asyncio.sleep(
                wait_seconds
            )

        # ----------------------------------------------------
        # NETWORK
        # ----------------------------------------------------

        except (
            ConnectionError,
            TimeoutError,
        ) as exc:

            if attempt >= MAX_RETRIES:
                raise

            delay = (
                RETRY_BASE_SECONDS *
                attempt
            )

            log.warning(
                "Network error "
                "(attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

            await asyncio.sleep(
                delay
            )

        # ----------------------------------------------------
        # TELEGRAM RPC
        # ----------------------------------------------------

        except RPCError as exc:

            if attempt >= MAX_RETRIES:
                raise

            delay = (
                RETRY_BASE_SECONDS *
                attempt
            )

            log.warning(
                "Telegram RPC error "
                "(attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

            await asyncio.sleep(
                delay
            )

    raise RuntimeError(
        "Telegram upload failed after "
        "all retry attempts."
    )


# ============================================================
# FASTAPI LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    log.info(
        "Application startup."
    )

    try:

        await start_telegram()

        log.info(
            "Application startup complete."
        )

        yield

    except Exception:

        log.exception(
            "Application startup failed."
        )

        # Keep Railway web process alive
        # so health endpoint remains available.
        yield

    finally:

        log.info(
            "Application shutting down."
        )

        if telegram_client:

            try:

                if telegram_client.is_connected():

                    await telegram_client.disconnect()

                    log.info(
                        "Telegram client disconnected."
                    )

            except Exception:

                log.exception(
                    "Telegram disconnect error."
                )


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Telegram Auto Uploader",
    version="2.1.0",
    lifespan=lifespan,
)


# ============================================================
# HOME
# ============================================================

@app.get("/")
async def home():

    connected = (
        telegram_client is not None
        and telegram_client.is_connected()
    )

    return {
        "status": "running",
        "telegram_connected": connected,
        "service": "Telegram Auto Uploader",
        "version": "2.1.0",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    connected = (
        telegram_client is not None
        and telegram_client.is_connected()
    )

    return {
        "ok": connected,
        "telegram_connected": connected,
    }


# ============================================================
# UPLOAD
# ============================================================

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...)
):

    filename = safe_filename(
        file.filename
    )

    log.info(
        "Incoming file: %s",
        filename,
    )

    # --------------------------------------------------------
    # TELEGRAM CHECK
    # --------------------------------------------------------

    if telegram_client is None:

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram client is not connected."
            ),
        )

    if not telegram_client.is_connected():

        raise HTTPException(
            status_code=503,
            detail=(
                "Telegram client is disconnected."
            ),
        )

    # --------------------------------------------------------
    # SINGLE LARGE UPLOAD LOCK
    # --------------------------------------------------------

    async with upload_lock:

        temp_path = None
        total_size = 0
        temp_file = None

        try:

            # ------------------------------------------------
            # TEMP FILE
            # ------------------------------------------------

            suffix = Path(
                filename
            ).suffix

            temp_file = (
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    delete=False,
                    suffix=suffix,
                    prefix="telegram_upload_",
                )
            )

            temp_path = temp_file.name

            log.info(
                "Temporary file created."
            )

            # ------------------------------------------------
            # STREAM TO DISK
            # ------------------------------------------------

            while True:

                chunk = await file.read(
                    CHUNK_SIZE
                )

                if not chunk:
                    break

                total_size += len(chunk)

                if (
                    total_size >
                    MAX_UPLOAD_BYTES
                ):

                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "File exceeds "
                            "application limit."
                        ),
                    )

                temp_file.write(chunk)

            temp_file.flush()
            temp_file.close()
            temp_file = None

            # ------------------------------------------------
            # EMPTY FILE
            # ------------------------------------------------

            if total_size <= 0:

                raise HTTPException(
                    status_code=400,
                    detail="Empty file.",
                )

            log.info(
                "File received: %s | %s",
                filename,
                human_size(total_size),
            )

            # ------------------------------------------------
            # TELEGRAM UPLOAD
            # ------------------------------------------------

            message = (
                await upload_to_telegram(
                    temp_path,
                    filename,
                    total_size,
                )
            )

            message_id = getattr(
                message,
                "id",
                None,
            )

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------

            log.info(
                "UPLOAD SUCCESS: %s",
                filename,
            )

            return JSONResponse(
                {
                    "success": True,
                    "filename": filename,
                    "size": total_size,
                    "size_human": human_size(
                        total_size
                    ),
                    "telegram_message_id":
                        message_id,
                    "message":
                        "Uploaded successfully.",
                }
            )

        except HTTPException:
            raise

        except Exception as exc:

            log.exception(
                "UPLOAD FAILED: %s",
                filename,
            )

            raise HTTPException(
                status_code=500,
                detail=(
                    f"Upload failed: {str(exc)}"
                ),
            )

        finally:

            # ------------------------------------------------
            # CLOSE FILE
            # ------------------------------------------------

            if temp_file:

                try:
                    temp_file.close()
                except Exception:
                    pass

            # ------------------------------------------------
            # DELETE TEMP FILE
            # ------------------------------------------------

            if temp_path:

                try:

                    if os.path.exists(
                        temp_path
                    ):

                        os.remove(
                            temp_path
                        )

                        log.info(
                            "Temporary file "
                            "deleted."
                        )

                except Exception:

                    log.exception(
                        "Could not delete "
                        "temporary file."
                    )

            # ------------------------------------------------
            # CLOSE REQUEST FILE
            # ------------------------------------------------

            try:
                await file.close()
            except Exception:
                pass


# ============================================================
# RAILWAY START
# ============================================================

if __name__ == "__main__":

    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8080",
        )
    )

    log.info(
        "Starting Uvicorn on port %s",
        port,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
    )
