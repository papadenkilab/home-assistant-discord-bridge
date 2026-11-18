# home-assistant-discord-bridge

Home Assistant 用のアドオン「Discord Bridge」のリポジトリです。

Discord の Gateway に常駐し、許可したユーザーの発言（Bot が参加しているサーバーの全テキストチャンネル、または DM）をキューに保存します。返信の投稿やファイル添付もこのアドオンを通して行えます。小さな HTTP API を持ち、外部のアシスタントが Bot トークンを直接持たずに Discord を通して作業できるようにするためのものです。API の詳細仕様は、作者の解説記事にまとめています。

## インストール

### リポジトリを登録する

下のボタンを押すと、Home Assistant の画面でこのリポジトリを登録するダイアログが開きます。

[![Home Assistant を開いて、このリポジトリを登録するダイアログを表示する](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fpapadenkilab%2Fhome-assistant-discord-bridge)

手動で登録する場合は、リポジトリ URL として次を使ってください。

```text
https://github.com/papadenkilab/home-assistant-discord-bridge
```

手順：Home Assistant の「設定」→「アドオン」→「アドオンストア」→ 右上の「⋮」→「リポジトリ」から URL を追加します。

### アドオンを入れる

1. アドオンストアに表示される「Discord Bridge」をインストールする
2. 設定タブで各項目を入力する（Discord Bot の用意と各項目の意味は `discord-bridge/DOCS.md` を参照）
3. アドオンを開始する

## リポジトリの構成

- `repository.yaml` — アドオンリポジトリのメタデータ
- `discord-bridge/` — アドオン本体（config.yaml、Dockerfile、rootfs/app/bridge.py）
- `tests/` — pytest のテスト一式（フィルター、キュー、長文分割、API_ONLY モードでの HTTP API）

## 設計メモ

- アドオンは「耳と口」だけを担当します。Discord Gateway への接続と、`/data` の SQLite キューを持ちます。「頭脳」側の外部アシスタントは HTTP API でキューを読み、返信します。
- キューに入るのは、設定で許可したユーザー ID の発言だけです。それ以外の人の発言は無視されます。
- チャンネルやロールなどの管理操作は Bot 自身が実行するため、できることの範囲は Bot の Discord 権限で決まります。権限が無い操作は 403 になります。

## ライセンス

MIT
