# Discord Bridge

Discord の Gateway に常駐し、**許可した1人のユーザー**の発言をローカルキューに保存する
アドオンです。対象は DM と、Bot が参加しているサーバーの全チャンネルです
（Bot のロールで閲覧できるチャンネルだけが見えます）。
キューは 外部のアシスタントが Tailscale 経由でポーリングして読み、返信もこのアドオンを
通して Discord へ投稿されます。加えて、サーバー管理用の HTTP API を持ちます。

アドオン自体は AI を呼びません。「耳と口と手」だけを担当します。
**できることの範囲はコードではなく Bot の Discord 権限で決まります。**
権限が無い操作は Discord 側が拒否し、API は 403 を返します。

## 事前準備（Discord Developer Portal）

1. https://discord.com/developers/applications で Application を新規作成し、Bot を追加する
2. Bot の **Privileged Gateway Intents** で **MESSAGE CONTENT** を有効化する
   （PRESENCE / SERVER MEMBERS は不要）
3. Bot をサーバーに招待する（権限は下の表を参照。後からサーバー設定の
   ロール権限でいつでも広げたり絞ったりできます）
4. Discord の設定で開発者モードをONにし、自分のユーザーIDをコピーする
   （IDは秘密情報ではない）

## 設定項目

| 項目 | 説明 |
|---|---|
| `discord_token` | Bot トークン。**チャットなどに貼らず、この設定欄に直接入力する** |
| `allowed_user_id` | 許可する Discord ユーザーID。**カンマ区切りで複数人指定できます**。リストに無い人の発言はすべて無視されます |
| `allowed_guild_ids` | 対象サーバーID（カンマ区切りで複数可）。**空欄なら Bot が参加する全サーバーが対象** |
| `queue_token` | アシスタント ↔ アドオン間 API の共有トークン。長いランダム文字列を自分で決めて入力する（クライアント側の設定と一致させる） |
| `retention_days` | 処理済みメッセージを保持する日数（既定 7） |
| `log_level` | ログの詳細度（既定 info） |

Bot ロールのチャンネル権限で行ってください）。

## 権限と機能の対応

サーバー設定 → ロール → Bot のロールで切り替えます。チャンネルごとの
権限上書きでも絞れます。

| Discord 権限 | 有効になる機能 |
|---|---|
| View Channel | そのチャンネルの発言受信・履歴読み取り |
| Send Messages | 返信・投稿 |
| Read Message History | 履歴読み取り・再接続時のバックフィル |
| Add Reactions | 👀（受付）/ ✅（返信済み）リアクション |
| Manage Messages | ピン留め、他人のメッセージ削除、一括削除 |
| Manage Channels | チャンネルの作成・編集・削除 |
| Manage Roles | ロールの付け外し（Bot のロールより下位のロールのみ） |
| Create Public Threads 等 | スレッド作成 |

## HTTP API（ポート 8765、Tailscale 経由のみ）

認証不要: `GET /health`、`GET /pending_count`（件数のみ・内容なし）
トークン認証（ヘッダ `X-Queue-Token`）:

- キュー: `GET /pending`、`POST /ack`、`POST /send`、`POST /send_file`（ファイル添付送信、1ファイル最大25MB）。送信先がフォーラムチャンネルの場合は、通常メッセージではなくフォーラム投稿（スレッド）として作成されます（タイトルは `title` フィールド、無ければ本文の1行目）
- 受信添付: キュー対象メッセージに付いたファイルはアドオンのデータ領域に保存され（1ファイル最大25MB）、`GET /pending` の `attachments` に一覧が出ます。本体は `GET /attachments/{message_id}/{index}` で取得します。保存済みファイルは保持期間クリーンアップでメッセージと一緒に削除されます
- 参照: `GET /guilds`、`GET /channels`、`GET /members`、`GET /roles`、`GET /history`
- 管理: `POST /channels/create`、`/channels/edit`、`/channels/delete`、
  `/messages/delete`、`/messages/bulk_delete`、`/messages/pin`、`/messages/unpin`、
  `/threads/create`、`/reactions/add`、`/roles/assign`、`/roles/remove`

## 動作

- 受信した発言には 👀 リアクションが付きます（受付の合図）
- キューはアドオンの `/data/queue.db` に保存され、再起動しても未処理分は残ります
- 再接続時には、オフライン中の発言を各チャンネルの履歴から取り直します
  （バックフィル。チャンネル毎 50 件・合計 200 件まで）
- **ルーターでポート 8765 を開放しないこと**。クライアントからは Tailscale 経由などでのみ
  アクセスする前提です
- Discord トークンが未設定、またはライブラリ初期化に失敗した場合、アドオンは
  落ちずに API のみで起動します（/health の `discord_connected` が false）

## セキュリティ

- 許可ユーザーリストに無い人の発言は内容を保存せず破棄します
- 管理操作の可否は Bot の Discord 権限で決まります。やらせたくない操作は
  ロール権限から外してください
- トークンはこのアドオンの設定内にのみ置いてください。漏れた場合は
  Developer Portal で Bot トークンをリセットし、ここを差し替えてください
