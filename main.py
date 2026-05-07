import uvicorn
import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request
import logging

from utils.process_payment_link import PaymentScanQueue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()

APP_PORT = 8000
CHECK_QR_PATH = "/link_get"
TUNA_POWERSHELL_COMMAND = f"tuna http {APP_PORT}"
TUNA_PUBLIC_URL_TIMEOUT_SECONDS = 30
EXTERNAL_SERVICE_URL = "https://api.apewallet.net/api/v1/sbp_external_service_url"
ADMIN_X_API_KEY = "PASTE_ADMIN_X_API_KEY_HERE"
PUBLIC_URL_RE = re.compile(r"https://[^\s\"'<>]+")


async def start_tuna():
    powershell = os.getenv("POWERSHELL_EXECUTABLE", "powershell.exe" if os.name == "nt" else "powershell")
    tuna_command = os.getenv("TUNA_POWERSHELL_COMMAND", TUNA_POWERSHELL_COMMAND)

    process = await asyncio.create_subprocess_exec(
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        tuna_command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    try:
        public_url = await asyncio.wait_for(
            read_tuna_public_url(process),
            timeout=TUNA_PUBLIC_URL_TIMEOUT_SECONDS,
        )
    except Exception:
        await stop_tuna(process)
        raise

    log_task = asyncio.create_task(log_tuna_output(process))
    return process, public_url, log_task


async def read_tuna_public_url(process: asyncio.subprocess.Process) -> str:
    if process.stdout is None:
        raise RuntimeError("Не удалось прочитать stdout процесса tuna")

    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            exit_code = await process.wait()
            raise RuntimeError(f"tuna завершилась до получения публичного URL. Код выхода: {exit_code}")

        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line:
            logger.info("tuna: %s", line)

        match = PUBLIC_URL_RE.search(line)
        if match:
            return match.group(0).rstrip("/")


async def log_tuna_output(process: asyncio.subprocess.Process):
    if process.stdout is None:
        return

    while True:
        line_bytes = await process.stdout.readline()
        if not line_bytes:
            break

        line = line_bytes.decode("utf-8", errors="replace").strip()
        if line:
            logger.info("tuna: %s", line)


async def stop_tuna(process: asyncio.subprocess.Process | None):
    if process is None or process.returncode is not None:
        return

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def register_external_service_url(public_url: str):
    callback_url = f"{public_url}{CHECK_QR_PATH}"

    if ADMIN_X_API_KEY == "PASTE_ADMIN_X_API_KEY_HERE":
        logger.warning("ADMIN_X_API_KEY не задан. Регистрация внешнего URL пропущена: %s", callback_url)
        return

    await asyncio.to_thread(post_external_service_url, callback_url)
    logger.info("Зарегистрирован внешний URL проверки QR: %s", callback_url)


def post_external_service_url(callback_url: str):
    payload = json.dumps({"url": callback_url}).encode("utf-8")
    request = urllib.request.Request(
        EXTERNAL_SERVICE_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ADMIN_X_API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ошибка регистрации внешнего URL: HTTP {exc.code}. Ответ: {body}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.scanner = PaymentScanQueue()
    app.state.tuna_process = None
    app.state.tuna_log_task = None
    try:
        tuna_process, public_url, tuna_log_task = await start_tuna()
        app.state.tuna_process = tuna_process
        app.state.tuna_log_task = tuna_log_task
        await register_external_service_url(public_url)
        yield
    finally:
        if app.state.tuna_log_task:
            app.state.tuna_log_task.cancel()
            with suppress(asyncio.CancelledError):
                await app.state.tuna_log_task
        await app.state.scanner.stop()
        await stop_tuna(app.state.tuna_process)


app = FastAPI(lifespan=lifespan)


async def extract_link(request: Request) -> str:
    try:
        body = await request.json()
    except Exception:
        body = None

    if isinstance(body, str):
        link = body
    elif isinstance(body, dict):
        link = body.get("link")
    else:
        link = None

    if not link or not isinstance(link, str):
        raise HTTPException(status_code=400, detail="Передайте QR-ссылку строкой или JSON-объектом {'link': '...'}")

    return link


@app.post("/link_get")
async def root(request: Request):
    link = await extract_link(request)
    logger.info(f"Got link from request: {link}")
    return await request.app.state.scanner.scan(link)


@app.get("/status")
async def status(request: Request):
    scanner = request.app.state.scanner
    return await scanner.get_status()


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)