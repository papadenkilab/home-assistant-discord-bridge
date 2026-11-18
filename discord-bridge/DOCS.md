# Discord Bridge

Discord の Gateway に常駐し、**許可したユーザーの発言**をローカルキューに保存するアドオンです。対象は DM と、Bot が参加しているサーバーの全チャンネルです（Bot のロールで閲覧できるチャンネルだけが見えます）。キューは外部のアシスタントが Tailscale 経由などでポーリングして読み、返信もこのアドオンを通して Discord へ投稿されます。

アドオン自体は AI を呼びません。「耳と口」だけを担当します。できることの範囲はコードではなく Bot の Discord 権限で決まります。権限が無い操作は Discord 側が拒否し、API は 403 を返します。

API の詳細仕様（エンドポイント一覧、権限と機能の対応、運用方法）は、作者の解説記事にまとめています。

## 事前準備（Discord Developer Portal）

1. https://discord.com/developers/applications で Application を新規作成し、Bot を追加する
2. Bot の **Privileged Gateway Intents** で **MESSAGE CONTENT** を有効化する（PRESENCE / SERVER MEMBERS は不要）
3. Bot をサーバーに招待する
4. Discord の設定で開発者モードを ON にし、自分のユーザー ID をコピーする（ID は秘密情報ではありません）

## 設定項目

- `discord_token`：Bot トークン。**チャットなどに貼らず、この設定欄に直接入力する**
- `allowed_user_id`：許可する Discord ユーザー ID（カンマ区切りで複数人指定可）。リストに無い人の発言は、内容を保存せず破棄します
- `allowed_guild_ids`：対象サーバー ID（カンマ区切りで複数可）。空欄なら Bot が参加する全サーバーが対象
- `queue_token`：アシスタント ↔ アドオン間 API の共有トークン。長いランダム文字列を自分で決めて入力する（クライアント側の設定と一致させる）
- `retention_days`：処理済みメッセージを保持する日数（既定 7）
- `log_level`：ログの詳細度（既定 info）

## 注意

- **ルーターでポート 8765 を開放しないこと**。クライアントからは Tailscale 経由など、宅内相当の経路でのみアクセスする前提です
- Discord トークンが未設定、または初期化に失敗した場合、アドオンは落ちずに API のみで起動します（`/health` の `discord_connected` が false）。受信できないときは、まずここ（接続できていない）と MESSAGE CONTENT の有効化を確認してください
- トークンが漏れた場合は、Developer Portal で Bot トークンをリセットし、この設定を差し替えてください
