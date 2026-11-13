#!/usr/bin/env python3
"""Discord Bridge - Home Assistant add-on.

Ears, mouth and hands: keeps a Discord Gateway connection, stores
messages from one allowed user (DM, or any channel in the guilds the bot
belongs to) in a local SQLite queue, and exposes a small HTTP API that
An external assistant polls the queue over HTTP (for example via Tailscale).

Endpoints:
  GET  /health         no auth  - status only, no message content
  GET  /pending_count  no auth  - {"count": n} only
  GET  /pending        token    - pending messages (marks them fetched)
  POST /ack            token    - {"ids": [...]} mark processed
  POST /send           token    - {channel_id, content, dedupe_id,
                                  in_reply_to?, title?}
  POST /send_file      token    - multipart/form-data: channel_id,
                                  dedupe_id, file (one attachment, max
                                  25 MB), content? (optional text),
                                  in_reply_to?, title?

  When channel_id is a forum channel, /send and /send_file create a
  forum post (thread) instead of a plain message: `title` becomes the
  post title (default: the first line of the content).
  GET  /attachments/{message_id}/{index}
                       token    - download one stored inbound attachment

Inbound attachments (images/files on queued messages) are downloaded
to the data directory (25 MB per-file cap) and listed in /pending.

Management (all token auth). What these can actually do is limited by
the bot's Discord permissions; a missing permission comes back as 403:
  GET  /guilds                   - guilds the bot belongs to
  GET  /channels?guild_id=       - channels (optionally one guild)
  GET  /threads?guild_id=        - active threads + archived forum
                                   posts (forum posts are threads);
                                   read one with /history?channel_id=
  GET  /members?guild_id=&limit= - guild members
  GET  /roles?guild_id=          - guild roles
  GET  /history?channel_id=&limit= - recent messages in a channel
  POST /channels/create  {guild_id, name, kind?, category_id?, topic?,
                          slowmode_delay?, nsfw?}
  POST /channels/edit    {channel_id, name?, topic?, slowmode_delay?,
                          nsfw?, position?, category_id?}
  POST /channels/delete  {channel_id}
  POST /messages/delete  {channel_id, message_id}
  POST /messages/bulk_delete {channel_id, message_ids[]}
  POST /messages/pin     {channel_id, message_id}
  POST /messages/unpin   {channel_id, message_id}
  POST /threads/create   {channel_id, name, message_id?}
  POST /reactions/add    {channel_id, message_id, emoji}
  POST /roles/assign     {guild_id, user_id, role_id}
  POST /roles/remove     {guild_id, user_id, role_id}

Config: Home Assistant options at /data/options.json.
Local-test overrides via env: BRIDGE_OPTIONS, BRIDGE_DB, BRIDGE_PORT,
BRIDGE_BIND, API_ONLY=1 (HTTP API + queue only, no Discord connection).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

VERSION = "1.3.1"
DISCORD_LIMIT = 2000
# Discord's per-file upload cap for bots in normal channels is 25 MB.
MAX_FILE_SIZE = 25 * 1024 * 1024
# HTTP request ceiling: the file cap plus room for multipart overhead
# and the other form fields. Files over MAX_FILE_SIZE are rejected
# with 400 by the /send_file handler itself before this is reached.
MAX_REQUEST_SIZE = MAX_FILE_SIZE + 1024 * 1024

log = logging.getLogger("bridge")


def utcnow():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- config

def load_config():
    path = os.environ.get("BRIDGE_OPTIONS", "/data/options.json")
    opts = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            opts = json.load(fh)
    cfg = {
        "discord_token": os.environ.get("DISCORD_TOKEN", opts.get("discord_token", "")),
        "allowed_user_id": str(opts.get("allowed_user_id", "") or opts.get("allowed_user_ids", "") or ""),
        "allowed_guild_ids": str(opts.get("allowed_guild_ids", "") or ""),
        "queue_token": os.environ.get("QUEUE_TOKEN", opts.get("queue_token", "")),
        "retention_days": int(opts.get("retention_days", 7) or 7),
        "log_level": str(opts.get("log_level", "info") or "info"),
        "db_path": os.environ.get("BRIDGE_DB", "/data/queue.db"),
        "port": int(os.environ.get("BRIDGE_PORT", "8765")),
        "bind": os.environ.get("BRIDGE_BIND", "0.0.0.0"),
        "api_only": os.environ.get("API_ONLY", "") == "1",
    }
    return cfg


# ---------------------------------------------------------------- filter

def parse_id_list(text):
    return {part.strip() for part in str(text or "").split(",") if part.strip()}


def is_allowed(author_id, channel_id, guild_id, is_dm, author_is_bot, is_self,
               allowed_user_ids, allowed_guild_ids):
    """Only the configured users' messages count as requests.

    `allowed_user_ids` is a comma-separated list (one or more user IDs).
    Messages are accepted in DM and in any channel of a guild the bot
    belongs to, optionally restricted to `allowed_guild_ids`
    (comma-separated; empty = every guild the bot is in). Which
    channels the bot can see and what it can do in them is governed by
    its Discord permissions (role + channel overrides), not by this
    code.
    """
    if author_is_bot or is_self:
        return False
    users = parse_id_list(allowed_user_ids)
    if not users or str(author_id) not in users:
        return False
    if is_dm:
        return True
    guilds = parse_id_list(allowed_guild_ids)
    if guilds and str(guild_id) not in guilds:
        return False
    return True


# ---------------------------------------------------------------- text

def split_content(text, limit=DISCORD_LIMIT):
    """Split text into chunks of at most `limit` chars, preferring newlines,
    then spaces, then hard cuts."""
    chunks = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n ")
    if rest:
        chunks.append(rest)
    return chunks


def forum_post_title(content, title=None):
    """Title for a forum post: explicit title, else the content's
    first non-empty line, else a generic fallback. Discord caps
    thread names at 100 characters."""
    if title and str(title).strip():
        name = str(title).strip()
    else:
        name = ""
        for line in str(content or "").splitlines():
            if line.strip():
                name = line.strip()
                break
        if not name:
            name = "post"
    return name[:100]


def resolve_forum_tags(available, wanted):
    """Resolve requested forum tags (names or IDs) against a forum's
    available tags, given as (id, name) pairs.

    Returns (matched_ids, missing). Raises ValueError when a wanted
    name matches more than one tag.
    """
    by_id = {str(i): str(i) for i, _ in available}
    by_name = {}
    for i, n in available:
        by_name.setdefault(str(n), []).append(str(i))
    matched, missing = [], []
    for w in wanted or []:
        w = str(w).strip()
        if not w:
            continue
        if w in by_id:
            matched.append(w)
        elif w in by_name:
            if len(by_name[w]) > 1:
                raise ValueError("tag name %r is ambiguous" % w)
            matched.append(by_name[w][0])
        else:
            missing.append(w)
    return matched, missing


# ---------------------------------------------------------------- queue

def safe_filename(name):
    base = os.path.basename(str(name or "attachment"))
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "attachment"
    return base[:100]


class Queue:
    def __init__(self, db_path):
        self.db_path = db_path
        data_dir = os.path.dirname(db_path) or "."
        os.makedirs(data_dir, exist_ok=True)
        self.attachments_dir = os.path.join(data_dir, "attachments")
        os.makedirs(self.attachments_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT UNIQUE NOT NULL,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                fetched_at TEXT,
                acked_at TEXT,
                attachments TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS outbox (
                dedupe_id TEXT UNIQUE NOT NULL,
                channel_id TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                sent_message_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Migration for databases created before attachments existed.
        cols = [r[1] for r in self.conn.execute(
            "PRAGMA table_info(messages)").fetchall()]
        if "attachments" not in cols:
            self.conn.execute(
                "ALTER TABLE messages ADD COLUMN"
                " attachments TEXT NOT NULL DEFAULT '[]'")
        self.conn.commit()
        self.lock = asyncio.Lock()

    def insert_message(self, discord_message_id, channel_id, author_id, content, created_at=None, attachments=None):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO messages"
            " (discord_message_id, channel_id, author_id, content, created_at, attachments)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (str(discord_message_id), str(channel_id), str(author_id),
             content, created_at or utcnow(),
             json.dumps(attachments or [])),
        )
        self.conn.commit()
        self.set_meta("last_message_at", utcnow())
        return cur.rowcount == 1

    def set_attachments(self, discord_message_id, attachments):
        self.conn.execute(
            "UPDATE messages SET attachments=? WHERE discord_message_id=?",
            (json.dumps(attachments), str(discord_message_id)),
        )
        self.conn.commit()

    def get_attachments(self, discord_message_id):
        row = self.conn.execute(
            "SELECT attachments FROM messages WHERE discord_message_id=?",
            (str(discord_message_id),),
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0] or "[]")
        except ValueError:
            return []

    def attachment_path(self, discord_message_id, index, filename):
        folder = Path(self.attachments_dir) / str(discord_message_id)
        return folder / ("%d-%s" % (int(index), safe_filename(filename)))

    def save_attachment_bytes(self, discord_message_id, index, filename, data):
        path = self.attachment_path(discord_message_id, index, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.name

    def pending_count(self):
        row = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE status IN ('pending', 'fetched')"
        ).fetchone()
        return row[0]

    def fetch_pending(self, limit=50):
        rows = self.conn.execute(
            "SELECT id, discord_message_id, channel_id, author_id, content, created_at, attachments"
            " FROM messages WHERE status IN ('pending', 'fetched')"
            " ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            self.conn.execute(
                "UPDATE messages SET status='fetched', fetched_at=?"
                " WHERE id IN (%s)" % ",".join("?" * len(ids)),
                [utcnow()] + ids,
            )
            self.conn.commit()
        out = []
        for r in rows:
            try:
                atts = json.loads(r[6] or "[]")
            except ValueError:
                atts = []
            out.append(
                {"id": r[0], "discord_message_id": r[1], "channel_id": r[2],
                 "author_id": r[3], "content": r[4], "created_at": r[5],
                 "attachments": atts})
        return out

    def ack(self, ids):
        done = 0
        for mid in ids:
            cur = self.conn.execute(
                "UPDATE messages SET status='done', acked_at=? WHERE id=?",
                (utcnow(), int(mid)),
            )
            done += cur.rowcount
        self.conn.commit()
        return done

    def outbox_get(self, dedupe_id):
        row = self.conn.execute(
            "SELECT sent_message_id, status FROM outbox WHERE dedupe_id=?",
            (dedupe_id,),
        ).fetchone()
        return {"sent_message_id": row[0], "status": row[1]} if row else None

    def outbox_put(self, dedupe_id, channel_id, content, sent_message_id, status="sent"):
        self.conn.execute(
            "INSERT OR IGNORE INTO outbox"
            " (dedupe_id, channel_id, content, status, sent_message_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (dedupe_id, str(channel_id), content, status, sent_message_id, utcnow()),
        )
        self.conn.commit()

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def cleanup(self, retention_days):
        rows = self.conn.execute(
            "SELECT discord_message_id FROM messages WHERE status='done'"
            " AND acked_at < datetime('now', ?)",
            ("-%d days" % int(retention_days),),
        ).fetchall()
        cur = self.conn.execute(
            "DELETE FROM messages WHERE status='done'"
            " AND acked_at < datetime('now', ?)",
            ("-%d days" % int(retention_days),),
        )
        self.conn.commit()
        for (mid,) in rows:
            shutil.rmtree(
                os.path.join(self.attachments_dir, str(mid)),
                ignore_errors=True)
        return cur.rowcount


# ---------------------------------------------------------------- HTTP API

class Bridge:
    """Holds shared state between the Discord client and the HTTP API."""

    def __init__(self, cfg, queue):
        self.cfg = cfg
        self.queue = queue
        self.client = None  # discord.Client, set when connected mode runs

    def discord_connected(self):
        return bool(self.client and self.client.is_ready())

    async def _forum_applied_tags(self, channel, tags):
        """Resolve requested tag names/IDs to ForumTag objects."""
        if not tags:
            return None
        available = [(str(t.id), t.name) for t in channel.available_tags]
        matched, missing = resolve_forum_tags(available, tags)
        if missing:
            raise ValueError(
                "unknown forum tag(s): %s (available: %s)"
                % (", ".join(missing),
                   ", ".join(n for _, n in available)))
        by_id = {str(t.id): t for t in channel.available_tags}
        return [by_id[i] for i in matched]

    async def send_to_discord(self, channel_id, content, in_reply_to=None,
                              title=None, tags=None):
        """Returns the last sent message id (str). Raises if not connected.

        Forum channels have no plain send: the content becomes a new
        forum post whose title is `title` or the first content line.
        """
        import discord

        if not self.discord_connected():
            raise RuntimeError("discord client not connected")
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        if isinstance(channel, discord.ForumChannel):
            applied = await self._forum_applied_tags(channel, tags)
            kwargs = {}
            if applied:
                kwargs["applied_tags"] = applied
            chunks = split_content(content)
            created = await channel.create_post(
                name=forum_post_title(content, title),
                content=chunks[0] if chunks else "", **kwargs)
            thread = created.thread
            last_id = str(created.message.id) if created.message \
                else str(thread.id)
            for chunk in chunks[1:]:
                msg = await thread.send(chunk)
                last_id = str(msg.id)
            return last_id
        last_id = None
        for chunk in split_content(content):
            msg = await channel.send(chunk)
            last_id = str(msg.id)
        if in_reply_to:
            try:
                orig = await channel.fetch_message(int(in_reply_to))
                await orig.add_reaction("✅")
            except Exception as exc:  # best effort only
                log.debug("check reaction failed: %s", exc)
        return last_id

    async def send_file_to_discord(self, channel_id, content, filename,
                                   data, in_reply_to=None, title=None,
                                   tags=None):
        """Send one file attachment, with optional text alongside.

        Returns the sent message id (str). Raises if not connected.
        The file rides on the last text chunk when the content has to
        be split, so the message order matches /send. Forum channels
        get a new forum post carrying the file instead.
        """
        import io

        import discord

        if not self.discord_connected():
            raise RuntimeError("discord client not connected")
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        if isinstance(channel, discord.ForumChannel):
            applied = await self._forum_applied_tags(channel, tags)
            kwargs = {}
            if applied:
                kwargs["applied_tags"] = applied
            attach = discord.File(io.BytesIO(data),
                                  filename=os.path.basename(filename)
                                  or "attachment")
            created = await channel.create_post(
                name=forum_post_title(content, title),
                content=content or None, file=attach, **kwargs)
            return str(created.message.id) if created.message \
                else str(created.thread.id)
        chunks = split_content(content) if content else []
        last_id = None
        for chunk in chunks[:-1]:
            msg = await channel.send(chunk)
            last_id = str(msg.id)
        attach = discord.File(io.BytesIO(data),
                              filename=os.path.basename(filename)
                              or "attachment")
        if chunks:
            msg = await channel.send(chunks[-1], file=attach)
        else:
            msg = await channel.send(file=attach)
        last_id = str(msg.id)
        if in_reply_to:
            try:
                orig = await channel.fetch_message(int(in_reply_to))
                await orig.add_reaction("✅")
            except Exception as exc:  # best effort only
                log.debug("check reaction failed: %s", exc)
        return last_id

    async def get_channel(self, channel_id):
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        return channel

    async def get_guild(self, guild_id):
        guild = self.client.get_guild(int(guild_id))
        if guild is None:
            guild = await self.client.fetch_guild(int(guild_id))
        return guild


def discord_error_response(exc):
    """Map a discord.py exception onto an HTTP response.

    Forbidden (403) is the permissions gate the operator controls from
    Discord's role/channel settings; it is reported, never worked around.
    """
    import discord

    if isinstance(exc, discord.Forbidden):
        return web.json_response(
            {"error": "forbidden", "detail": str(exc)}, status=403)
    if isinstance(exc, discord.NotFound):
        return web.json_response(
            {"error": "not_found", "detail": str(exc)}, status=404)
    return web.json_response(
        {"error": "discord_error", "detail": str(exc)}, status=502)


def require_token(bridge):
    def decorator(handler):
        async def wrapper(request):
            token = bridge.cfg["queue_token"]
            if not token:
                return web.json_response(
                    {"error": "queue_token not configured"}, status=503)
            supplied = request.headers.get("X-Queue-Token", "")
            if not hmac.compare_digest(supplied, token):
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)
        return wrapper
    return decorator


def create_app(bridge):
    app = web.Application(client_max_size=MAX_REQUEST_SIZE)
    q = bridge.queue

    async def health(request):
        return web.json_response({
            "ok": True,
            "version": VERSION,
            "discord_connected": bridge.discord_connected(),
            "pending": q.pending_count(),
            "last_message_at": q.get_meta("last_message_at"),
        })

    async def pending_count(request):
        return web.json_response({"count": q.pending_count()})

    @require_token(bridge)
    async def pending(request):
        items = q.fetch_pending(limit=50)
        return web.json_response({"messages": items})

    @require_token(bridge)
    async def ack(request):
        body = await request.json()
        ids = body.get("ids", [])
        done = q.ack(ids)
        return web.json_response({"acked": done})

    @require_token(bridge)
    async def send(request):
        body = await request.json()
        channel_id = str(body.get("channel_id", ""))
        content = body.get("content", "")
        dedupe_id = body.get("dedupe_id", "")
        in_reply_to = body.get("in_reply_to")
        title = body.get("title")
        tags = body.get("tags")
        if not channel_id or not content or not dedupe_id:
            return web.json_response(
                {"error": "channel_id, content and dedupe_id are required"},
                status=400)
        existing = q.outbox_get(dedupe_id)
        if existing:
            return web.json_response({
                "sent_message_id": existing["sent_message_id"],
                "status": existing["status"], "deduped": True})
        if bridge.cfg["api_only"] or not bridge.discord_connected():
            if not bridge.cfg["api_only"]:
                return web.json_response(
                    {"error": "discord client not connected"}, status=503)
            fake_id = "simulated-" + hashlib.sha256(
                dedupe_id.encode()).hexdigest()[:16]
            q.outbox_put(dedupe_id, channel_id, content, fake_id, status="simulated")
            return web.json_response(
                {"sent_message_id": fake_id, "status": "simulated"})
        try:
            sent_id = await bridge.send_to_discord(
                channel_id, content, in_reply_to, title=title, tags=tags)
        except ValueError as exc:
            return web.json_response(
                {"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception as exc:
            return discord_error_response(exc)
        q.outbox_put(dedupe_id, channel_id, content, sent_id, status="sent")
        return web.json_response({"sent_message_id": sent_id, "status": "sent"})

    @require_token(bridge)
    async def send_file(request):
        """POST /send_file - multipart/form-data with one attachment.

        Fields: channel_id (required), dedupe_id (required),
        file (required, one file, at most MAX_FILE_SIZE bytes),
        content (optional text sent with the file), in_reply_to
        (optional), title (optional; forum post title when the
        channel is a forum). Dedupe/outbox handling matches /send.
        """
        if not request.content_type.startswith("multipart/"):
            return web.json_response(
                {"error": "multipart/form-data is required"}, status=400)
        fields = {}
        filename = None
        data = None
        try:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.name == "file":
                    filename = part.filename or "attachment"
                    buf = bytearray()
                    while True:
                        chunk = await part.read_chunk(65536)
                        if not chunk:
                            break
                        buf.extend(chunk)
                        if len(buf) > MAX_FILE_SIZE:
                            return web.json_response(
                                {"error": "file too large",
                                 "detail": "file exceeds the %d MB limit"
                                           % (MAX_FILE_SIZE // (1024 * 1024))},
                                status=400)
                    data = bytes(buf)
                elif part.name in ("channel_id", "content", "dedupe_id",
                                   "in_reply_to", "title", "tags"):
                    fields[part.name] = (await part.text()).strip()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response(
                {"error": "file too large",
                 "detail": "request exceeds the upload limit"}, status=400)
        channel_id = fields.get("channel_id", "")
        content = fields.get("content", "")
        dedupe_id = fields.get("dedupe_id", "")
        in_reply_to = fields.get("in_reply_to") or None
        if not channel_id or not dedupe_id or data is None:
            return web.json_response(
                {"error": "channel_id, dedupe_id and file are required"},
                status=400)
        existing = q.outbox_get(dedupe_id)
        if existing:
            return web.json_response({
                "sent_message_id": existing["sent_message_id"],
                "status": existing["status"], "deduped": True})
        if bridge.cfg["api_only"] or not bridge.discord_connected():
            if not bridge.cfg["api_only"]:
                return web.json_response(
                    {"error": "discord client not connected"}, status=503)
            fake_id = "simulated-" + hashlib.sha256(
                dedupe_id.encode()).hexdigest()[:16]
            q.outbox_put(dedupe_id, channel_id, content, fake_id,
                         status="simulated")
            return web.json_response(
                {"sent_message_id": fake_id, "status": "simulated",
                 "filename": filename, "size": len(data)})
        file_tags = None
        if fields.get("tags"):
            file_tags = [t.strip() for t in fields["tags"].split(",")
                         if t.strip()]
        try:
            sent_id = await bridge.send_file_to_discord(
                channel_id, content, filename, data, in_reply_to,
                title=fields.get("title") or None, tags=file_tags)
        except ValueError as exc:
            return web.json_response(
                {"error": "bad_request", "detail": str(exc)}, status=400)
        except Exception as exc:
            return discord_error_response(exc)
        q.outbox_put(dedupe_id, channel_id, content, sent_id, status="sent")
        return web.json_response(
            {"sent_message_id": sent_id, "status": "sent",
             "filename": filename, "size": len(data)})

    @require_token(bridge)
    async def attachment_file(request):
        """GET /attachments/{message_id}/{index} - one stored file."""
        message_id = request.match_info["message_id"]
        try:
            index = int(request.match_info["index"])
        except ValueError:
            return web.json_response({"error": "bad index"}, status=400)
        atts = q.get_attachments(message_id)
        if atts is None:
            return web.json_response(
                {"error": "message not found"}, status=404)
        entry = next((a for a in atts if a.get("index") == index), None)
        if entry is None or not entry.get("stored_name"):
            return web.json_response(
                {"error": "attachment not stored"}, status=404)
        path = Path(q.attachments_dir) / message_id / entry["stored_name"]
        if not path.is_file():
            return web.json_response(
                {"error": "attachment file missing"}, status=404)
        return web.FileResponse(
            path,
            headers={"Content-Type": entry.get("content_type")
                     or "application/octet-stream"})

    # ---------------- management API ----------------
    # Everything below is gated only by the bot's Discord permissions:
    # discord.py raises Forbidden when the bot's role (or a channel
    # override) does not allow the action, and that becomes a 403 here.

    def mgmt(handler):
        @require_token(bridge)
        async def wrapper(request):
            if not bridge.discord_connected():
                return web.json_response(
                    {"error": "discord client not connected"}, status=503)
            try:
                return await handler(request)
            except ValueError:
                return web.json_response(
                    {"error": "invalid id or value"}, status=400)
            except Exception as exc:  # discord.py errors included
                return discord_error_response(exc)
        return wrapper

    def bad_request(msg):
        return web.json_response({"error": msg}, status=400)

    @mgmt
    async def guilds(request):
        out = [{"id": str(g.id), "name": g.name,
                "member_count": g.member_count}
               for g in bridge.client.guilds]
        return web.json_response({"guilds": out})

    @mgmt
    async def channels(request):
        guild_id = request.query.get("guild_id")
        if guild_id:
            targets = [await bridge.get_guild(guild_id)]
        else:
            targets = list(bridge.client.guilds)
        out = []
        for g in targets:
            for ch in g.channels:
                out.append({
                    "id": str(ch.id), "guild_id": str(g.id),
                    "name": ch.name, "type": str(ch.type),
                    "position": ch.position,
                    "category_id": str(ch.category_id)
                    if getattr(ch, "category_id", None) else None,
                    "topic": getattr(ch, "topic", None),
                })
        return web.json_response({"channels": out})

    @mgmt
    async def threads(request):
        """Active threads plus archived forum posts.

        Forum posts ARE threads whose parent is the forum channel, so
        this is how forum content is enumerated; read a post's
        messages with /history?channel_id=<thread id>.
        """
        import discord

        guild_id = request.query.get("guild_id")
        if guild_id:
            targets = [await bridge.get_guild(guild_id)]
        else:
            targets = list(bridge.client.guilds)
        out = []
        seen = set()

        def add(t, guild):
            if t.id in seen:
                return
            seen.add(t.id)
            out.append({
                "id": str(t.id), "name": t.name,
                "parent_id": str(t.parent_id) if t.parent_id else None,
                "guild_id": str(guild.id),
                "owner_id": str(t.owner_id) if t.owner_id else None,
                "archived": bool(t.archived),
                "created_at": discord.utils.snowflake_time(
                    t.id).isoformat(),
            })

        for g in targets:
            for t in await g.active_threads():
                add(t, g)
            for ch in g.channels:
                if isinstance(ch, discord.ForumChannel):
                    try:
                        async for t in ch.archived_threads(limit=50):
                            add(t, g)
                    except Exception as exc:
                        log.debug("archived threads skipped %s: %s",
                                  ch.id, exc)
        return web.json_response({"threads": out})

    @mgmt
    async def members(request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id:
            return bad_request("guild_id is required")
        limit = min(max(int(request.query.get("limit", "200")), 1), 1000)
        guild = await bridge.get_guild(guild_id)
        out = []
        async for m in guild.fetch_members(limit=limit):
            out.append({"id": str(m.id), "name": m.name,
                        "display_name": m.display_name, "bot": m.bot,
                        "roles": [str(r.id) for r in m.roles]})
        return web.json_response({"members": out})

    @mgmt
    async def roles(request):
        guild_id = request.query.get("guild_id", "")
        if not guild_id:
            return bad_request("guild_id is required")
        guild = await bridge.get_guild(guild_id)
        out = [{"id": str(r.id), "name": r.name, "position": r.position,
                "colour": str(r.colour),
                "permissions": r.permissions.value} for r in guild.roles]
        return web.json_response({"roles": out})

    @mgmt
    async def history(request):
        channel_id = request.query.get("channel_id", "")
        if not channel_id:
            return bad_request("channel_id is required")
        limit = min(max(int(request.query.get("limit", "30")), 1), 100)
        channel = await bridge.get_channel(channel_id)
        out = []
        async for m in channel.history(limit=limit):
            out.append({
                "id": str(m.id), "author_id": str(m.author.id),
                "author_name": m.author.name, "content": m.content,
                "created_at": m.created_at.isoformat()
                if m.created_at else None,
                "pinned": m.pinned,
                "attachments": [
                    {"filename": a.filename,
                     "content_type": a.content_type, "size": a.size,
                     "url": a.url}
                    for a in m.attachments],
            })
        return web.json_response({"messages": out})

    @mgmt
    async def channels_create(request):
        body = await request.json()
        guild_id = str(body.get("guild_id", ""))
        name = body.get("name", "")
        kind = body.get("kind", "text")
        if not guild_id or not name:
            return bad_request("guild_id and name are required")
        guild = await bridge.get_guild(guild_id)
        category = None
        if body.get("category_id"):
            category = await bridge.get_channel(str(body["category_id"]))
        if kind == "category":
            ch = await guild.create_category(name)
        elif kind == "voice":
            ch = await guild.create_voice_channel(name, category=category)
        else:
            kwargs = {}
            if body.get("topic"):
                kwargs["topic"] = body["topic"]
            if body.get("slowmode_delay") is not None:
                kwargs["slowmode_delay"] = int(body["slowmode_delay"])
            if body.get("nsfw") is not None:
                kwargs["nsfw"] = bool(body["nsfw"])
            ch = await guild.create_text_channel(
                name, category=category, **kwargs)
        return web.json_response(
            {"id": str(ch.id), "name": ch.name, "type": str(ch.type)})

    @mgmt
    async def channels_edit(request):
        body = await request.json()
        channel_id = str(body.get("channel_id", ""))
        if not channel_id:
            return bad_request("channel_id is required")
        channel = await bridge.get_channel(channel_id)
        edits = {}
        for key in ("name", "topic", "nsfw"):
            if key in body:
                edits[key] = body[key]
        if "slowmode_delay" in body:
            edits["slowmode_delay"] = int(body["slowmode_delay"])
        if "position" in body:
            edits["position"] = int(body["position"])
        if "category_id" in body:
            edits["category"] = (
                await bridge.get_channel(str(body["category_id"]))
                if body["category_id"] else None)
        if not edits:
            return bad_request("nothing to change")
        await channel.edit(**edits)
        return web.json_response(
            {"id": str(channel.id), "edited": sorted(edits)})

    @mgmt
    async def channels_delete(request):
        body = await request.json()
        channel_id = str(body.get("channel_id", ""))
        if not channel_id:
            return bad_request("channel_id is required")
        channel = await bridge.get_channel(channel_id)
        await channel.delete()
        return web.json_response({"deleted": channel_id})

    async def _fetch_message(body):
        channel_id = str(body.get("channel_id", ""))
        message_id = str(body.get("message_id", ""))
        if not channel_id or not message_id:
            return None, None, bad_request(
                "channel_id and message_id are required")
        channel = await bridge.get_channel(channel_id)
        msg = await channel.fetch_message(int(message_id))
        return channel, msg, None

    @mgmt
    async def messages_delete(request):
        body = await request.json()
        _, msg, err = await _fetch_message(body)
        if err:
            return err
        await msg.delete()
        return web.json_response({"deleted": str(msg.id)})

    @mgmt
    async def messages_bulk_delete(request):
        import discord

        body = await request.json()
        channel_id = str(body.get("channel_id", ""))
        message_ids = body.get("message_ids", [])
        if not channel_id or not message_ids:
            return bad_request("channel_id and message_ids are required")
        if len(message_ids) > 100:
            return bad_request("message_ids is capped at 100")
        channel = await bridge.get_channel(channel_id)
        objects = [discord.Object(id=int(mid)) for mid in message_ids]
        await channel.delete_messages(objects)
        return web.json_response({"deleted": len(objects)})

    @mgmt
    async def messages_pin(request):
        body = await request.json()
        _, msg, err = await _fetch_message(body)
        if err:
            return err
        await msg.pin()
        return web.json_response({"pinned": str(msg.id)})

    @mgmt
    async def messages_unpin(request):
        body = await request.json()
        _, msg, err = await _fetch_message(body)
        if err:
            return err
        await msg.unpin()
        return web.json_response({"unpinned": str(msg.id)})

    @mgmt
    async def threads_create(request):
        body = await request.json()
        channel_id = str(body.get("channel_id", ""))
        name = body.get("name", "")
        if not channel_id or not name:
            return bad_request("channel_id and name are required")
        channel = await bridge.get_channel(channel_id)
        if body.get("message_id"):
            msg = await channel.fetch_message(int(body["message_id"]))
            thread = await msg.create_thread(name=name)
        else:
            thread = await channel.create_thread(name=name)
        return web.json_response(
            {"id": str(thread.id), "name": thread.name})

    @mgmt
    async def reactions_add(request):
        body = await request.json()
        emoji = body.get("emoji", "")
        if not emoji:
            return bad_request("emoji is required")
        _, msg, err = await _fetch_message(body)
        if err:
            return err
        await msg.add_reaction(emoji)
        return web.json_response({"ok": True})

    async def _role_change(request, add):
        body = await request.json()
        guild_id = str(body.get("guild_id", ""))
        user_id = str(body.get("user_id", ""))
        role_id = str(body.get("role_id", ""))
        if not guild_id or not user_id or not role_id:
            return bad_request("guild_id, user_id and role_id are required")
        guild = await bridge.get_guild(guild_id)
        role = guild.get_role(int(role_id))
        if role is None:
            return web.json_response(
                {"error": "role not found"}, status=404)
        member = await guild.fetch_member(int(user_id))
        if add:
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
        return web.json_response({"ok": True})

    @mgmt
    async def roles_assign(request):
        return await _role_change(request, add=True)

    @mgmt
    async def roles_remove(request):
        return await _role_change(request, add=False)

    app.router.add_get("/health", health)
    app.router.add_get("/pending_count", pending_count)
    app.router.add_get("/pending", pending)
    app.router.add_post("/ack", ack)
    app.router.add_post("/send", send)
    app.router.add_post("/send_file", send_file)
    app.router.add_get("/attachments/{message_id}/{index}", attachment_file)
    app.router.add_get("/guilds", guilds)
    app.router.add_get("/channels", channels)
    app.router.add_get("/threads", threads)
    app.router.add_get("/members", members)
    app.router.add_get("/roles", roles)
    app.router.add_get("/history", history)
    app.router.add_post("/channels/create", channels_create)
    app.router.add_post("/channels/edit", channels_edit)
    app.router.add_post("/channels/delete", channels_delete)
    app.router.add_post("/messages/delete", messages_delete)
    app.router.add_post("/messages/bulk_delete", messages_bulk_delete)
    app.router.add_post("/messages/pin", messages_pin)
    app.router.add_post("/messages/unpin", messages_unpin)
    app.router.add_post("/threads/create", threads_create)
    app.router.add_post("/reactions/add", reactions_add)
    app.router.add_post("/roles/assign", roles_assign)
    app.router.add_post("/roles/remove", roles_remove)
    return app


async def retention_loop(bridge):
    while True:
        try:
            removed = bridge.queue.cleanup(bridge.cfg["retention_days"])
            if removed:
                log.info("retention cleanup removed %d messages", removed)
        except Exception as exc:
            log.warning("retention cleanup failed: %s", exc)
        await asyncio.sleep(24 * 3600)


# ---------------------------------------------------------------- Discord

def build_discord_client(bridge):
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    intents.messages = True
    intents.dm_messages = True
    intents.guild_messages = True

    client = discord.Client(intents=intents)
    cfg = bridge.cfg
    q = bridge.queue

    async def handle(message):
        is_dm = isinstance(message.channel, discord.DMChannel)
        guild_id = message.guild.id if message.guild else None
        if not is_allowed(message.author.id, message.channel.id, guild_id,
                          is_dm, message.author.bot,
                          message.author.id == client.user.id,
                          cfg["allowed_user_id"], cfg["allowed_guild_ids"]):
            return
        atts = [
            {"index": i, "filename": a.filename,
             "content_type": a.content_type, "size": a.size,
             "url": a.url, "downloaded": False}
            for i, a in enumerate(message.attachments)
        ]
        inserted = q.insert_message(
            message.id, message.channel.id, message.author.id,
            message.content,
            created_at=message.created_at.isoformat()
            if message.created_at else None,
            attachments=atts)
        q.set_meta("last_seen_%s" % message.channel.id, str(message.id))
        if inserted:
            log.info("queued message id=%s channel=%s len=%d attachments=%d",
                     message.id, message.channel.id, len(message.content),
                     len(atts))
            # Download the attachment bytes so the poller can fetch
            # them later via /attachments/... (Discord CDN URLs expire).
            changed = False
            for entry, att in zip(atts, message.attachments):
                if att.size and att.size > MAX_FILE_SIZE:
                    entry["note"] = "too large to store"
                    changed = True
                    continue
                try:
                    path = q.attachment_path(
                        message.id, entry["index"], entry["filename"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await att.save(path)
                    entry["downloaded"] = True
                    entry["stored_name"] = path.name
                except Exception as exc:
                    entry["note"] = "download failed: %s" % exc
                    log.warning("attachment download failed id=%s: %s",
                                message.id, exc)
                changed = True
            if changed:
                q.set_attachments(message.id, atts)
            try:
                await message.add_reaction("👀")
            except Exception as exc:
                log.debug("eyes reaction failed: %s", exc)

    @client.event
    async def on_ready():
        log.info("discord connected as %s", client.user)
        # Backfill every guild text channel for messages from the
        # allowed user that were missed while offline. Per-channel
        # last-seen markers keep this incremental; caps keep a long
        # outage from flooding the queue.
        total = 0
        text_channels = []
        for guild in client.guilds:
            text_channels.extend(guild.text_channels)
        for channel in text_channels:
            if total >= 200:
                break
            last_seen = q.get_meta("last_seen_%s" % channel.id)
            if not last_seen:
                continue
            try:
                marker = await channel.fetch_message(int(last_seen))
                async for msg in channel.history(
                        after=marker, oldest_first=True, limit=50):
                    await handle(msg)
                    total += 1
                    if total >= 200:
                        break
            except Exception as exc:
                log.debug("backfill skipped channel %s: %s",
                          channel.id, exc)

    @client.event
    async def on_message(message):
        await handle(message)

    return client


def init_discord_client(bridge):
    """Build the Discord client, or return None on any failure.

    A library-level problem (e.g. an import error after a Python
    upgrade) must never crash-loop the whole add-on: the HTTP API
    stays up so the failure is visible via /health instead.
    """
    try:
        return build_discord_client(bridge)
    except Exception as exc:
        log.error("failed to initialise Discord client (%s); "
                  "running API only", exc)
        return None


# ---------------------------------------------------------------- main

async def amain():
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    queue = Queue(cfg["db_path"])
    bridge = Bridge(cfg, queue)

    token = cfg["discord_token"]
    token_ok = bool(token) and "paste" not in token.lower()
    if cfg["api_only"]:
        log.info("API_ONLY mode: Discord client disabled")
    elif token_ok:
        bridge.client = init_discord_client(bridge)
    else:
        log.error("discord_token is not configured; running API only. "
                  "Set the token in the add-on configuration.")

    app = create_app(bridge)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg["bind"], cfg["port"])
    await site.start()
    log.info("HTTP API listening on %s:%d", cfg["bind"], cfg["port"])

    tasks = [asyncio.create_task(retention_loop(bridge))]
    if bridge.client is not None:
        tasks.append(asyncio.create_task(bridge.client.start(token)))
    try:
        await asyncio.gather(*tasks)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(amain())
