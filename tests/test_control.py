import asyncio
import json

from evdev import ecodes as e

from fireblaster.control import ControlServer

from test_controller import SRC, make


async def connect(path):
    reader, writer = await asyncio.open_unix_connection(path)

    async def recv():
        return json.loads(await asyncio.wait_for(reader.readline(), 1))

    async def send(**msg):
        writer.write((json.dumps(msg) + "\n").encode())
        await writer.drain()

    return recv, send, writer


async def recv_until(recv, predicate):
    while True:
        msg = await recv()
        if predicate(msg):
            return msg


def test_snapshot_on_connect_and_setup_commands(tmp_path):
    async def body():
        ctl, tx, state = make(tmp_path, "sony")
        server = ControlServer(ctl, tmp_path / "run" / "control.sock")
        await server.start()
        recv, send, writer = await connect(server.path)

        snap = await recv()
        assert snap["event"] == "state" and snap["mode"] == "normal"
        assert snap["active"] == {"id": "sony", "brand": "Sony", "name": "sony"}
        assert snap["combo"]["keys"] == ["KEY_BACK", "KEY_KPENTER"]
        assert snap["combo"]["remaining"] is None
        assert "VOLUME_UP" in snap["functions"]

        await send(cmd="start_setup")
        snap = await recv_until(recv, lambda m: m["event"] == "state")
        assert snap["mode"] == "setup"
        s = snap["setup"]
        assert s["candidate"]["id"] == "sony" and s["count"] == 4
        assert s["brands"] == ["LG", "Samsung", "Sony"] and s["brand_index"] == 2
        assert 0 < s["idle_timeout"] <= 0.5
        tx_event = await recv()
        assert tx_event == {"event": "tx", "function": "VOLUME_UP", "profile": s["candidate"]}

        await send(cmd="next")  # wraps to lg-a
        snap = await recv_until(recv, lambda m: m["event"] == "state")
        assert snap["setup"]["candidate"]["id"] == "lg-a"
        assert (snap["setup"]["brand_position"], snap["setup"]["brand_count"]) == (0, 2)
        assert snap["setup"]["brand_codesets"] == ["lg-a", "lg-b"]

        await send(cmd="next_code")
        snap = await recv_until(recv, lambda m: m["event"] == "state")
        assert snap["setup"]["candidate"]["id"] == "lg-b"
        await send(cmd="prev_code")
        snap = await recv_until(recv, lambda m: m["event"] == "state")
        assert snap["setup"]["candidate"]["id"] == "lg-a"

        await send(cmd="test", function="MUTE_TOGGLE")
        await recv_until(recv, lambda m: m["event"] == "tx" and m["function"] == "MUTE_TOGGLE")

        await send(cmd="accept")
        saved = await recv_until(recv, lambda m: m["event"] == "saved")
        assert saved == {"event": "saved", "profile": {"id": "lg-a", "brand": "LG", "name": "lg-a"}}
        snap = await recv()
        assert snap["mode"] == "normal" and snap["active"]["id"] == "lg-a"
        assert state.profile == "lg-a"

        writer.close()
        await server.close()
        assert not server.path.exists()

    asyncio.run(body())


def test_errors(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, None)
        server = ControlServer(ctl, tmp_path / "control.sock")
        await server.start()
        recv, send, writer = await connect(server.path)
        await recv()

        writer.write(b"not json\n")
        assert (await recv())["event"] == "error"
        await send(cmd="next")  # not in setup
        assert (await recv())["event"] == "error"
        await send(cmd="test", function="VOLUME_UP")  # no active profile
        assert (await recv())["event"] == "error"

        writer.close()
        await server.close()

    asyncio.run(body())


def test_combo_countdown_is_broadcast(tmp_path):
    async def body():
        ctl, _, _ = make(tmp_path, "sony")
        server = ControlServer(ctl, tmp_path / "control.sock")
        await server.start()
        recv, send, writer = await connect(server.path)
        await recv()

        ctl.handle(SRC, e.KEY_BACK, 1)
        ctl.handle(SRC, e.KEY_KPENTER, 1)
        snap = await recv()
        assert snap["mode"] == "normal" and 0 < snap["combo"]["remaining"] <= 0.1
        ctl.handle(SRC, e.KEY_KPENTER, 0)
        snap = await recv()
        assert snap["combo"]["remaining"] is None

        writer.close()
        await server.close()

    asyncio.run(body())


def test_stale_socket_replaced(tmp_path):
    async def body():
        import socket

        path = tmp_path / "control.sock"
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(path))
        stale.close()
        ctl, _, _ = make(tmp_path, None)
        server = ControlServer(ctl, path)
        await server.start()
        recv, _, writer = await connect(path)
        assert (await recv())["event"] == "state"
        writer.close()
        await server.close()

    asyncio.run(body())
