import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
import logging

from utils.process_payment_link import PaymentScanQueue

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.scanner = PaymentScanQueue()
    try:
        yield
    finally:
        await app.state.scanner.stop()


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
    uvicorn.run(app, host="0.0.0.0", port=8000)