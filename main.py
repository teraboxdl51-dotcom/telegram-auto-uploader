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
# ENVIRONMENT HELPERS
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


# ============================================================
# RAILWAY VARIABLES
# ============================================================

TELEGRAM_API_ID = load_api_id()

TELEGRAM_API_HASH = env_required(
    "TELEGRAM_API_HASH"
)

SESSION_STRING = env_required(
    "SESSION_STRING"
)

CHANNEL_ID = env_required(
    "CHANNEL_ID"
)

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

CHUNK_SIZE = 1024 * 1024

MAX_UPLOAD_BYTES = (
    100 * 1024 * 1024 * 1024
)

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
# CHANNEL RESOLVER
# ============================================================

async def resolve_channel():

    global telegram_client

    if telegram_client is None:
        raise RuntimeError(
            "Telegram client is not running."
        )

    target = CHANNEL_ID.strip()

    log.info(
        "Resolving Telegram target: %s",
        target,
    )

    # --------------------------------------------------------
    # USERNAME
    # --------------------------------------------------------

    if target.startswith("@"):

        try:

            entity = await telegram_client.get_entity(
                target
            )

            log.info(
                "Target resolved by username: %s",
                target,
            )

            return entity

        except Exception as exc:

            log.warning(
                "Username resolution failed: %s",
                exc,
            )


    # --------------------------------------------------------
    # NUMERIC CHANNEL ID
    # --------------------------------------------------------

    if target.lstrip("-").isdigit():

        channel_id = int(target)

        log.info(
            "Numeric channel ID detected: %s",
            channel_id,
        )

        # First try direct resolution.
        try:

            entity = await telegram_client.get_entity(
                channel_id
            )

            log.info(
                "Target resolved directly by ID."
            )

            return entity

        except Exception as exc:

            log.warning(
                "Direct ID resolution failed: %s",
                exc,
            )


        # ----------------------------------------------------
        # SEARCH THROUGH DIALOGS
        # ----------------------------------------------------

        log.info(
            "Searching Telegram dialogs for channel..."
        )

        try:

            async for dialog in telegram_client.iter_dialogs():

                entity = dialog.entity

                entity_id = getattr(
                    entity,
                    "id",
                    None,
                )

                # Telegram channel IDs are normally shown
                # with -100 prefix in Bot/API contexts.
                # Telethon entity.id is the positive ID.

                normalized_channel_id = channel_id

                if str(channel_id).startswith("-100"):

                    normalized_channel_id = int(
                        str(channel_id)[4:]
                    )

                if entity_id == normalized_channel_id:

                    log.info(
                        "Target found in Telegram dialogs."
                    )

                    title = getattr(
                        entity,
                        "title",
                        None,
                    )

                    if title:
                        log.info(
                            "Target channel: %s",
                            title,
                        )

                    return entity

        except Exception:

            log.exception(
                "Error while searching Telegram dialogs."
            )


    # --------------------------------------------------------
    # FINAL ATTEMPT
    # --------------------------------------------------------

    try:

        entity = await telegram_client.get_entity(
            target
        )

        log.info(
            "Target resolved successfully."
        )

        return entity

    except Exception as exc:

        raise RuntimeError(
            "Cannot resolve CHANNEL_ID. "
            "Make sure the Telegram account used "
            "to create SESSION_STRING has access "
            "to the channel and CHANNEL_ID is correct. "
            f"Original error: {exc}"
        )


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
    # StringSession tells Telethon that SESSION_STRING
    # is a Telegram session string, NOT a SQLite filename.

    telegram_client = TelegramClient(

        StringSession(SESSION_STRING),

        TELEGRAM_API_ID,

        TELEGRAM_API_HASH,

        device_model="Telegram Auto Uploader",

        system_version="Railway",

        app_version="3.0.0",

        lang_code="en",

        system_lang_code="en",

        auto_reconnect=True,

        connection_retries=10,

        retry_delay=5,
    )

    try:

        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------

        await telegram_client.connect()

        log.info(
            "Telegram connection established."
        )


        # ----------------------------------------------------
        # AUTH CHECK
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

            log.info(
                "Telegram logged in as: %s%s | ID: %s",

                first_name or "Unknown",

                (
                    f" (@{username})"
                    if username
                    else ""
                ),

                me.id,
            )


        # ----------------------------------------------------
        # RESOLVE TARGET
        # ----------------------------------------------------

        telegram_entity = (
            await resolve_channel()
        )


        # ----------------------------------------------------
        # TARGET INFO
        # ----------------------------------------------------

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

        entity_id = getattr(
            telegram_entity,
            "id",
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
                "Target resolved successfully."
            )

        log.info(
            "Target entity ID: %s",
            entity_id,
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

        rounded = (
            percent // 5
        ) * 5

        if rounded != last_logged_percent:

            last_logged_percent = rounded

            log.info(
                "Upload %s: %d%%",
                filename,
                percent,
            )


    # --------------------------------------------------------
    # FILE TYPE
    # --------------------------------------------------------

    mime = get_mime(filename)

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
    # SEND SETTINGS
    # --------------------------------------------------------

    kwargs = {

        "caption": filename,

        "force_document": force_document,

        "supports_streaming": supports_streaming,

        "progress_callback": progress_callback,
    }


    # --------------------------------------------------------
    # RETRIES
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


        except FloodWaitError as exc:

            wait_seconds = int(
                exc.seconds
            )

            log.warning(
                "Telegram FloodWait: %s seconds.",
                wait_seconds,
            )

            await asyncio.sleep(
                wait_seconds
            )


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

        # Keep Railway web service alive.
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
# FASTAPI
# ============================================================

app = FastAPI(

    title="Telegram Auto Uploader",

    version="3.0.0",

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

        "version": "3.0.0",
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
# UPLOAD ENDPOINT
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


    if telegram_entity is None:

        raise HTTPException(

            status_code=503,

            detail=(
                "Telegram target is not available."
            ),
        )


    # --------------------------------------------------------
    # ONE LARGE UPLOAD AT A TIME
    # --------------------------------------------------------

    async with upload_lock:

        temp_path = None

        temp_file = None

        total_size = 0


        try:

            # ------------------------------------------------
            # CREATE TEMP FILE
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
            # RECEIVE FILE
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


                temp_file.write(
                    chunk
                )


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

                    "size_human":
                        human_size(
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
            # CLOSE TEMP FILE
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
                            "Temporary file deleted."
                        )

                except Exception:

                    log.exception(
                        "Could not delete "
                        "temporary file."
                    )


            # ------------------------------------------------
            # CLOSE UPLOAD
            # ------------------------------------------------

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

    log.info(
        "Starting Uvicorn on port %s",
        port,
    )

    uvicorn.run(

        app,

        host="0.0.0.0",

        port=port,
                        )
