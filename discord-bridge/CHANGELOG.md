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
