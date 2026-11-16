import asyncio

from aiohttp.test_utils import TestClient, TestServer

from conftest import disc_header
from wiipi.web import create_app


def run(env, scenario, token=""):
    env.cfg.token = token

    async def main():
        async with TestClient(TestServer(create_app(env.service, env.cfg))) as client:
            await scenario(client)

    asyncio.run(main())


def wbfs_bytes(size=3 * 1024 * 1024):
    head = bytearray(512)
    head[0:4] = b"WBFS"
    head[8], head[9] = 9, 21
    body = disc_header("RSPE01", "Wii Sports")
    return bytes(head) + body + b"\x33" * (size - 512 - len(body))


def test_chunked_upload_with_resume(env):
    data = wbfs_bytes()

    async def scenario(client):
        resp = await client.post("/api/uploads", json={"name": "Wii Sports.wbfs", "size": len(data)})
        assert resp.status == 200
        upload = (await resp.json())["id"]
        half = len(data) // 2
        resp = await client.put(f"/api/uploads/{upload}?offset=0", data=data[:half])
        assert (await resp.json())["offset"] == half
        # A retry with a stale offset is told where to resume and writes nothing.
        resp = await client.put(f"/api/uploads/{upload}?offset=0", data=data[:half])
        assert resp.status == 409 and (await resp.json())["offset"] == half
        resp = await client.post(f"/api/uploads/{upload}/complete")
        assert resp.status == 409   # not finished yet
        resp = await client.put(f"/api/uploads/{upload}?offset={half}", data=data[half:])
        assert (await resp.json())["offset"] == len(data)
        resp = await client.post(f"/api/uploads/{upload}/complete")
        assert resp.status == 200
        status = await (await client.get("/api/status")).json()
        assert status["items"][0]["name"] == "Wii Sports.wbfs" and status["uploads"] == []
        assert "source" not in status["items"][0]

    run(env, scenario)
    assert env.service.prepare_step()
    item = env.service.items[0]
    assert item.status == "ready" and item.size == len(data)
    assert (env.cfg.ready / str(item.id) / "RSPE01.wbfs").read_bytes() == data


def test_upload_validation(env):
    async def scenario(client):
        resp = await client.post("/api/uploads", json={"name": "movie.mkv", "size": 10})
        assert resp.status == 400
        resp = await client.post("/api/uploads", json={"name": "../../etc/x.wbfs", "size": 10})
        upload = (await resp.json())["id"]
        assert [p.parent for p in env.cfg.uploads.glob("*.part")] == [env.cfg.uploads]
        resp = await client.put(f"/api/uploads/{upload}?offset=0", data=b"x" * 11)
        assert resp.status == 400   # more than announced
        resp = await client.delete(f"/api/uploads/{upload}")
        assert resp.status == 200 and not list(env.cfg.uploads.glob("*.part"))
        env.service.wit = False
        resp = await client.post("/api/uploads", json={"name": "game.iso", "size": 10})
        assert resp.status == 400

    run(env, scenario)


def test_token_protects_management_but_not_wii(env):
    async def scenario(client):
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer nope"})).status == 401
        assert (await client.get("/api/status", headers={"Authorization": "Bearer s3cret"})).status == 200
        resp = await client.get("/api/wii/status")
        assert resp.status == 200
        body = await resp.text()
        assert body.startswith("wiipi=1\n") and "state=attached\n" in body
        assert (await client.get("/")).status == 200

    run(env, scenario, token="s3cret")


def test_wii_endpoints_refuse_tunnelled_requests(env):
    async def scenario(client):
        resp = await client.get("/api/wii/release", headers={"CF-Connecting-IP": "203.0.113.9"})
        assert resp.status == 403
        resp = await client.get("/api/wii/release")
        assert resp.status == 200 and (await resp.text()).startswith("accepted=0\n")

    run(env, scenario)


def test_sync_and_delete_endpoints(env):
    async def scenario(client):
        resp = await client.post("/api/games/delete", json={"path": "wbfs/Nope [XXXX01]"})
        assert resp.status == 404
        resp = await client.post("/api/sync", json={"force": True, "rescan": True})
        assert resp.status == 200
        resp = await client.delete("/api/items/12345")
        assert resp.status == 409

    run(env, scenario)
    gen = env.service.gen
    assert env.service.sync_step() and env.service.gen == gen + 1
