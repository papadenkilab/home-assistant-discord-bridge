import importlib.util
import os
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

BRIDGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "discord-bridge",
    "rootfs", "app", "bridge.py")
spec = importlib.util.spec_from_file_location("bridge", BRIDGE_PATH)
bridge_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_mod)

USER = "111"
CHAN = "222"
GUILD = "777"
TOKEN = "test-queue-token"


def make_cfg(api_only=True):
    return {
        "discord_token": "",
        "allowed_user_id": USER,
        "allowed_guild_ids": "",
        "queue_token": TOKEN,
        "retention_days": 7,
        "log_level": "info",
        "api_only": api_only,
    }


# ---------------- filter ----------------

def test_filter_allows_owner_any_channel_any_guild():
    assert bridge_mod.is_allowed(USER, CHAN, GUILD, False, False, False, USER, "")
    assert bridge_mod.is_allowed(USER, "555", "888", False, False, False, USER, "")


def test_filter_allows_owner_dm():
    assert bridge_mod.is_allowed(USER, "999", None, True, False, False, USER, "")


def test_filter_rejects_other_user():
    assert not bridge_mod.is_allowed("333", CHAN, GUILD, False, False, False, USER, "")


def test_filter_rejects_bots_and_self():
    assert not bridge_mod.is_allowed(USER, CHAN, GUILD, False, True, False, USER, "")
    assert not bridge_mod.is_allowed(USER, CHAN, GUILD, False, False, True, USER, "")


def test_filter_guild_restriction():
    assert bridge_mod.is_allowed(USER, CHAN, GUILD, False, False, False, USER, "777, 888")
    assert not bridge_mod.is_allowed(USER, CHAN, "999", False, False, False, USER, "777")
    # DM stays allowed even with a guild restriction
    assert bridge_mod.is_allowed(USER, "999", None, True, False, False, USER, "777")


def test_filter_multiple_users():
    allowed = "111, 222"
    assert bridge_mod.is_allowed("111", CHAN, GUILD, False, False, False, allowed, "")
    assert bridge_mod.is_allowed("222", CHAN, GUILD, False, False, False, allowed, "")
    assert not bridge_mod.is_allowed("333", CHAN, GUILD, False, False, False, allowed, "")
    assert not bridge_mod.is_allowed(USER, CHAN, GUILD, False, False, False, "", "")


def test_parse_id_list():
    assert bridge_mod.parse_id_list("") == set()
    assert bridge_mod.parse_id_list("777, 888 ,") == {"777", "888"}


# ---------------- splitting ----------------

def test_split_short_text_unchanged():
    assert bridge_mod.split_content("hello") == ["hello"]


def test_split_long_text_within_limit():
    text = ("word " * 600).strip()  # 2999 chars
    chunks = bridge_mod.split_content(text)
    assert len(chunks) >= 2
    assert all(len(c) <= 2000 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_split_hard_cut():
    text = "a" * 4500
    chunks = bridge_mod.split_content(text)
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


# ---------------- queue ----------------

def test_queue_insert_dedupe_pending_ack(tmp_path):
    q = bridge_mod.Queue(str(tmp_path / "q.db"))
    assert q.insert_message("m1", CHAN, USER, "hello")
    assert not q.insert_message("m1", CHAN, USER, "hello")  # duplicate
    assert q.insert_message("m2", CHAN, USER, "second")
    assert q.pending_count() == 2
    items = q.fetch_pending()
    assert [i["content"] for i in items] == ["hello", "second"]
    assert q.pending_count() == 2  # fetched still counts as pending work
    assert q.ack([items[0]["id"]]) == 1
    assert q.pending_count() == 1


# ---------------- HTTP API ----------------

@pytest.fixture()
async def client(tmp_path):
    q = bridge_mod.Queue(str(tmp_path / "q.db"))
    q.insert_message("m1", CHAN, USER, "ping from discord")
    b = bridge_mod.Bridge(make_cfg(), q)
    app = bridge_mod.create_app(b)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_health_and_count_no_auth(client):
    r = await client.get("/health")
    assert r.status == 200
    data = await r.json()
    assert data["ok"] is True and data["pending"] == 1
    assert "content" not in str(data)
    r = await client.get("/pending_count")
    assert r.status == 200
    assert (await r.json()) == {"count": 1}


async def test_pending_requires_token(client):
    r = await client.get("/pending")
    assert r.status == 401
    r = await client.get("/pending", headers={"X-Queue-Token": "wrong"})
    assert r.status == 401
    r = await client.get("/pending", headers={"X-Queue-Token": TOKEN})
    assert r.status == 200
    msgs = (await r.json())["messages"]
    assert len(msgs) == 1 and msgs[0]["content"] == "ping from discord"


async def test_send_dedupe_and_ack(client):
    payload = {"channel_id": CHAN, "content": "reply text",
               "dedupe_id": "d-1"}
    r = await client.post("/send", json=payload,
                          headers={"X-Queue-Token": TOKEN})
    assert r.status == 200
    first = await r.json()
    assert first["status"] == "simulated"
    r = await client.post("/send", json=payload,
                          headers={"X-Queue-Token": TOKEN})
    second = await r.json()
    assert second["sent_message_id"] == first["sent_message_id"]
    assert second.get("deduped") is True

    r = await client.get("/pending", headers={"X-Queue-Token": TOKEN})
    mid = (await r.json())["messages"][0]["id"]
    r = await client.post("/ack", json={"ids": [mid]},
                          headers={"X-Queue-Token": TOKEN})
    assert (await r.json())["acked"] == 1
    r = await client.get("/pending_count")
    assert (await r.json()) == {"count": 0}


async def test_send_validation(client):
    r = await client.post("/send", json={"channel_id": CHAN},
                          headers={"X-Queue-Token": TOKEN})
    assert r.status == 400


# ---------------- /send_file ----------------

def send_file_form(payload=b"hello file", filename="note.txt",
                   **fields):
    from aiohttp import FormData

    form = FormData()
    for key, value in fields.items():
        form.add_field(key, value)
    if payload is not None:
        form.add_field("file", payload, filename=filename,
                       content_type="application/octet-stream")
    return form


async def test_send_file_simulated_and_dedupe(client):
    form = send_file_form(channel_id=CHAN, dedupe_id="f-1",
                          content="see attached")
    r = await client.post("/send_file", data=form,
                          headers={"X-Queue-Token": TOKEN})
    assert r.status == 200
    first = await r.json()
    assert first["status"] == "simulated"
    assert first["filename"] == "note.txt"
    assert first["size"] == len(b"hello file")
    # same dedupe_id must not send twice
    form = send_file_form(channel_id=CHAN, dedupe_id="f-1",
                          content="see attached")
    r = await client.post("/send_file", data=form,
                          headers={"X-Queue-Token": TOKEN})
    second = await r.json()
    assert second["sent_message_id"] == first["sent_message_id"]
    assert second.get("deduped") is True


async def test_send_file_content_optional(client):
    form = send_file_form(channel_id=CHAN, dedupe_id="f-2")
    r = await client.post("/send_file", data=form,
                          headers={"X-Queue-Token": TOKEN})
    assert r.status == 200
    assert (await r.json())["status"] == "simulated"


async def test_send_file_requires_token(client):
    form = send_file_form(channel_id=CHAN, dedupe_id="f-3")
    r = await client.post("/send_file", data=form)
    assert r.status == 401
    form = send_file_form(channel_id=CHAN, dedupe_id="f-3")
    r = await client.post("/send_file", data=form,
                          headers={"X-Queue-Token": "wrong"})
    assert r.status == 401


async def test_send_file_validation_missing_fields(client):
    headers = {"X-Queue-Token": TOKEN}
    # no dedupe_id
    form = send_file_form(channel_id=CHAN)
    r = await client.post("/send_file", data=form, headers=headers)
    assert r.status == 400
    # no channel_id
    form = send_file_form(dedupe_id="f-4")
    r = await client.post("/send_file", data=form, headers=headers)
    assert r.status == 400
    # no file part at all
    form = send_file_form(payload=None, channel_id=CHAN, dedupe_id="f-5")
    r = await client.post("/send_file", data=form, headers=headers)
    assert r.status == 400
    # not multipart
    r = await client.post("/send_file",
                          json={"channel_id": CHAN, "dedupe_id": "f-6"},
                          headers=headers)
    assert r.status == 400


async def test_send_file_too_large(client):
    big = b"x" * (bridge_mod.MAX_FILE_SIZE + 1)
    form = send_file_form(payload=big, filename="big.bin",
                          channel_id=CHAN, dedupe_id="f-7")
    r = await client.post("/send_file", data=form,
                          headers={"X-Queue-Token": TOKEN})
    assert r.status == 400
    body = await r.json()
    assert "too large" in body["error"]


# ---------------- management API ----------------

async def test_mgmt_requires_token(client):
    r = await client.get("/guilds")
    assert r.status == 401
    r = await client.post("/channels/delete", json={"channel_id": CHAN})
    assert r.status == 401


async def test_mgmt_unavailable_when_discord_not_connected(client):
    headers = {"X-Queue-Token": TOKEN}
    r = await client.get("/guilds", headers=headers)
    assert r.status == 503
    r = await client.get("/channels", headers=headers)
    assert r.status == 503
    r = await client.post("/channels/create",
                          json={"guild_id": GUILD, "name": "x"},
                          headers=headers)
    assert r.status == 503
    r = await client.post("/roles/assign",
                          json={"guild_id": GUILD, "user_id": USER,
                                "role_id": "5"},
                          headers=headers)
    assert r.status == 503


# ---------------- client init guard ----------------

def test_init_client_failure_degrades_to_none(tmp_path, monkeypatch):
    q = bridge_mod.Queue(str(tmp_path / "q.db"))
    b = bridge_mod.Bridge(make_cfg(api_only=False), q)

    def boom(_bridge):
        raise ModuleNotFoundError("No module named 'audioop'")

    monkeypatch.setattr(bridge_mod, "build_discord_client", boom)
    assert bridge_mod.init_discord_client(b) is None


def test_init_client_success_passthrough(tmp_path, monkeypatch):
    q = bridge_mod.Queue(str(tmp_path / "q.db"))
    b = bridge_mod.Bridge(make_cfg(api_only=False), q)
    sentinel = object()
    monkeypatch.setattr(bridge_mod, "build_discord_client",
                        lambda _bridge: sentinel)
    assert bridge_mod.init_discord_client(b) is sentinel


async def test_api_refuses_when_queue_token_empty(tmp_path):
    cfg = make_cfg()
    cfg["queue_token"] = ""
    q = bridge_mod.Queue(str(tmp_path / "q.db"))
    b = bridge_mod.Bridge(cfg, q)
    app = bridge_mod.create_app(b)
    async with TestClient(TestServer(app)) as c:
        r = await c.get("/pending")
        assert r.status == 503
