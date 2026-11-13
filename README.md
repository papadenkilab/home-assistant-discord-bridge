# home-assistant-discord-bridge

Home Assistant add-on: a resident Discord gateway bridge. It queues messages
from allowed users (any text channel of the invited server, or DMs), posts
replies, and exposes a small HTTP API plus a management API gated by the
bot's own Discord permissions, so an external assistant can work through
Discord without holding the bot token itself.

Layout:
- `repository.yaml` — add-on repository metadata
- `discord-bridge/` — the add-on (config.yaml, Dockerfile, rootfs/app/bridge.py)
- `tests/` — pytest suite (filter, queue, splitting, HTTP API in API_ONLY mode)

Install (local add-on): copy this folder's contents into the HA host's
`/addons/` directory (e.g. via the Samba share add-on), reload local add-ons
in the Home Assistant UI, install "Discord Bridge", fill in the
options, and start it. See `discord-bridge/DOCS.md` for setup.

Design notes:
- The add-on is only the ears and mouth: it holds the Discord gateway
  connection and a SQLite queue in `/data`. The "brain" (an external
  assistant) polls `/pending` over HTTP and posts replies via `/send`
  (file attachments via `/send_file`).
- Only messages from the configured user IDs are queued; everyone else is
  ignored.
- Management actions (channel/role/member administration) are executed by
  the bot itself, so they are limited by the bot's Discord permissions.

## License
MIT
