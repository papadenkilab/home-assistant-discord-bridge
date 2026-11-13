## 1.3.1

- `/send` and `/send_file` now return Discord errors (403/404/502
  with detail) instead of a bare HTTP 500, and accept forum tags:
  `tags` (names or IDs) for forum posts, resolved against the
  forum's available tags. Unknown tags return 400 with the list of
  available tag names, which also covers forums that require a tag
  on every new post.

## 1.3.0

- New `GET /threads` (token auth): lists active threads and archived
  forum posts (forum posts are threads). Read a post's messages and
  attachments with `GET /history?channel_id=<thread id>`.
- `GET /history` attachment entries now include the CDN `url`.

## 1.2.0

- Forum channels: `POST /send` and `POST /send_file` now create a
  forum post (thread) when the target channel is a forum channel,
  instead of failing. The post title comes from the new optional
  `title` field, or from the first line of the content.

## 1.1.0

- Inbound attachments: files attached to queued messages are now
  downloaded to the add-on data directory (25 MB per-file cap) and
  listed in `GET /pending`; fetch the bytes with
  `GET /attachments/{message_id}/{index}` (token auth). Stored files
  are removed by the retention cleanup together with their message.
- `GET /history` results now include attachment metadata.

## 1.0.0

- Initial public release.
- Includes file attachment sending via `POST /send_file` (one file, max 25 MB).
