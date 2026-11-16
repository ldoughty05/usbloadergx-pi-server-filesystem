"""HTTP API and web UI.

/api/wii/*  plain-text endpoints polled by USB Loader GX on the LAN (no token).
/api/*      JSON management API used by the web UI (optional bearer token).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from . import __version__, discs
from .config import Config
from .service import Service

log = logging.getLogger("wiipi.web")
STATIC = Path(__file__).parent / "static"
PROXY_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For", "Forwarded", "X-Real-IP")
READ_CHUNK = 1024 * 1024


@dataclass
class Upload:
    id: str
    name: str
    size: int
    path: Path
    received: int = 0


def _is_lan(request: web.Request) -> bool:
    """True for direct requests from a private address (not via a tunnel or reverse proxy)."""
    if any(header in request.headers for header in PROXY_HEADERS):
        return False
    try:
        address = ipaddress.ip_address(request.remote or "")
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def create_app(service: Service, cfg: Config) -> web.Application:
    uploads: dict[str, Upload] = {}

    @web.middleware
    async def guard(request: web.Request, handler):
        path = request.path
        if path.startswith("/api/wii/"):
            if not _is_lan(request):
                raise web.HTTPForbidden(text="error=LAN only\n")
        elif path.startswith("/api/") and cfg.token:
            supplied = request.headers.get("Authorization", "")
            supplied = supplied[7:].strip() if supplied.startswith("Bearer ") else ""
            if not hmac.compare_digest(supplied.encode(), cfg.token.encode()):
                raise web.HTTPUnauthorized(text="token required")
        return await handler(request)

    def text(body: str) -> web.Response:
        return web.Response(text=body, content_type="text/plain",
                            headers={"Cache-Control": "no-store"})

    # -- Wii endpoints -----------------------------------------------------
    async def wii_status(_request: web.Request) -> web.Response:
        return text(service.wii_status())

    async def wii_release(_request: web.Request) -> web.Response:
        accepted = service.wii_release()
        return text(f"accepted={int(accepted)}\n" + service.wii_status())

    # -- management API ----------------------------------------------------
    async def status(_request: web.Request) -> web.Response:
        data = service.status()
        data["version"] = __version__
        data["uploads"] = [{"id": u.id, "name": u.name, "size": u.size, "received": u.received}
                           for u in uploads.values()]
        data["staging_free"] = shutil.disk_usage(cfg.uploads).free
        return web.json_response(data, headers={"Cache-Control": "no-store"})

    async def upload_create(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            name = Path(str(body["name"])).name
            size = int(body["size"])
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text="expected JSON with name and size")
        suffix = Path(name).suffix.lower()
        if suffix not in discs.EXTENSIONS:
            raise web.HTTPBadRequest(
                text=f"unsupported file type {suffix or name!r}; use " + ", ".join(sorted(discs.EXTENSIONS)))
        if suffix != ".wbfs" and not service.wit:
            raise web.HTTPBadRequest(text="only .wbfs uploads are possible: wit is not installed")
        if size <= 0:
            raise web.HTTPBadRequest(text="empty file")
        if size > shutil.disk_usage(cfg.uploads).free:
            raise web.HTTPInsufficientStorage(text="not enough free space on the Pi for this upload")
        upload_id = secrets.token_hex(8)
        upload = Upload(upload_id, name, size, cfg.uploads / f"{upload_id}{suffix}.part")
        upload.path.touch()
        uploads[upload_id] = upload
        return web.json_response({"id": upload_id, "offset": 0})

    def find_upload(request: web.Request) -> Upload:
        upload = uploads.get(request.match_info["id"])
        if upload is None:
            raise web.HTTPNotFound(text="unknown upload")
        return upload

    async def upload_chunk(request: web.Request) -> web.Response:
        upload = find_upload(request)
        try:
            offset = int(request.query.get("offset", "-1"))
        except ValueError:
            offset = -1
        if offset != upload.received:
            # Tell the client where to resume from.
            return web.json_response({"offset": upload.received}, status=409)
        with open(upload.path, "r+b") as fh:
            fh.seek(offset)
            async for chunk in request.content.iter_chunked(READ_CHUNK):
                if upload.received + len(chunk) > upload.size:
                    raise web.HTTPBadRequest(text="more data than the announced size")
                fh.write(chunk)
                upload.received += len(chunk)
        return web.json_response({"offset": upload.received})

    async def upload_complete(request: web.Request) -> web.Response:
        upload = find_upload(request)
        if upload.received != upload.size:
            return web.json_response({"offset": upload.received}, status=409)
        final = upload.path.with_suffix("")  # drop ".part"
        upload.path.rename(final)
        del uploads[upload.id]
        item = service.add_file(final, upload.name)
        return web.json_response({"item": item.id})

    async def upload_cancel(request: web.Request) -> web.Response:
        upload = find_upload(request)
        upload.path.unlink(missing_ok=True)
        del uploads[upload.id]
        return web.json_response({"ok": True})

    async def item_cancel(request: web.Request) -> web.Response:
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPBadRequest(text="bad item id")
        if not service.cancel(item_id):
            raise web.HTTPConflict(text="item is busy or does not exist")
        return web.json_response({"ok": True})

    async def game_delete(request: web.Request) -> web.Response:
        body = await request.json()
        path = str(body.get("path", ""))
        ok = service.undo_delete(path) if body.get("undo") else service.request_delete(path)
        if not ok:
            raise web.HTTPNotFound(text="no such game")
        return web.json_response({"ok": True})

    async def sync(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        service.request_sync(force=bool(body.get("force")), rescan=bool(body.get("rescan")))
        return web.json_response({"ok": True})

    async def index(_request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def clean_partials(_app: web.Application) -> None:
        for stale in cfg.uploads.glob("*.part"):
            stale.unlink(missing_ok=True)

    app = web.Application(middlewares=[guard])
    app.on_startup.append(clean_partials)
    app.add_routes([
        web.get("/", index),
        web.get("/api/wii/status", wii_status),
        web.get("/api/wii/release", wii_release),
        web.get("/api/status", status),
        web.post("/api/uploads", upload_create),
        web.put("/api/uploads/{id}", upload_chunk),
        web.post("/api/uploads/{id}/complete", upload_complete),
        web.delete("/api/uploads/{id}", upload_cancel),
        web.delete("/api/items/{id}", item_cancel),
        web.post("/api/games/delete", game_delete),
        web.post("/api/sync", sync),
    ])
    return app
