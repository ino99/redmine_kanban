# Redmine Kanban Board

Redmine REST API からIssueを取得し、ローカルブラウザでKanban表示するツールです。
Redmine APIキーは各ユーザーの `.env` に保存し、GitHubには登録しません。

## セキュリティ

- APIキーを画面共有やスクリーンショットで見せないでください。
- `.env` はコミットしないでください。
- `.cache/` はRedmineのIssue内容を保存するため、コミットしないでください。
- GitHubには `.env.example` のみ登録します。
- 誤ってAPIキーをコミットした場合は、履歴削除だけでなくAPIキーを再発行してください。
- 可能であれば読み取り専用のAPIキーを使ってください。
- GitHubにpushする前に必ず `git status` と `git ls-files` を確認してください。
- `docker compose config` は `.env` の値を表示するため、出力を共有しないでください。

## 設定

初回起動前に `.env.example` を `.env` にコピーし、Redmine接続情報を編集します。
Windowsの起動スクリプトを使う場合、`.env` がなければ初回実行時に自動作成されます。

```env
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=replace_with_your_api_key
PROJECT_ID=my-redmine-project
USE_SAMPLE_DATA=false
HTTP_PROXY=
HTTPS_PROXY=
NO_PROXY=localhost,127.0.0.1
REDMINE_FETCH_WORKERS=4
REDMINE_FETCH_RETRIES=0
REDMINE_TIME_ENTRY_PAGES=2
REDMINE_TIME_ENTRY_TIMEOUT_SECONDS=8
REDMINE_TIME_ENTRY_RETRIES=0
```

`REDMINE_URL` は自分のRedmine URL、`REDMINE_API_KEY` は自分のAPIキー、`PROJECT_ID` はRedmineのプロジェクト識別子に置き換えてください。
プロキシ経由でRedmineへアクセスする場合は、必要に応じて `HTTP_PROXY` と `HTTPS_PROXY` も設定してください。

`REDMINE_FETCH_WORKERS` はIssueページを並列取得する数です。
`REDMINE_FETCH_RETRIES` はIssue取得のリトライ回数です。
`REDMINE_TIME_ENTRY_PAGES` は作業時間コメントを探すために `/time_entries.json` から取得するページ数です。重い場合は `1`、不要な場合は `0` にしてください。
`REDMINE_TIME_ENTRY_TIMEOUT_SECONDS` と `REDMINE_TIME_ENTRY_RETRIES` は作業時間コメント取得が重いときの待ち時間調整に使います。

## Windowsで起動

PowerShellでこのフォルダを開き、次を実行します。

```powershell
.\run_windows.ps1
```

起動後、ブラウザで次を開きます。

```text
http://127.0.0.1:8015/kanban.html
```

PowerShellの実行ポリシーで `.ps1` を起動できない場合は、コマンドプロンプトから次を実行します。

```bat
run_windows.bat
```

ポートを変更する場合は、起動スクリプトに引数を渡します。

```powershell
.\run_windows.ps1 --serve --port 8001
```

```bat
run_windows.bat --serve --port 8001
```

この場合は `http://127.0.0.1:8001/kanban.html` を開きます。

## Windowsログオン時に自動起動

Windowsにログオンしたとき、自動でローカルサーバーをバックグラウンド起動してブラウザも開くようにする場合は、PowerShellで次を実行します。

```powershell
.\install_windows_startup.ps1
```

登録後は、次回Windowsログオン時に `http://127.0.0.1:8015/kanban.html` が開きます。

自動起動をやめる、またはアンインストールする場合は次を実行します。

```powershell
.\uninstall_windows_startup.ps1
```

## Dockerで起動

Docker内でローカルサーバーを起動できます。
APIキーを含む `.env` はイメージにコピーせず、実行時に `env_file` として読み込みます。

`.env` を編集したうえで起動します。

```bash
docker compose up --build
```

ブラウザで開きます。

```text
http://127.0.0.1:8015/kanban.html
```

ポートを変更したい場合は、起動時に `KANBAN_PORT` を指定します。

```bash
KANBAN_PORT=8001 docker compose up --build
```

この場合は `http://127.0.0.1:8001/kanban.html` を開きます。

Docker Composeを使わずに起動する場合:

```bash
docker build -t redmine-kanban .
docker run --rm --env-file .env -p 8000:8000 redmine-kanban
```

`.dockerignore` により `.env`, `.cache/`, `kanban.html`, `__pycache__/` などはDockerビルドコンテキストから除外されます。

## 画面の使い方

画面上部の `PROJECT_ID` に別のプロジェクト識別子を入力して `表示` を押すと、そのPROJECT_IDのキャッシュを表示します。
未取得のPROJECT_IDでは初回だけRedmine APIから全件取得します。

画面上部の `更新` を押すと、前回取得日の1日前以降に変わったIssueをRedmine APIから差分取得してキャッシュに反映します。
`全更新` を押すと全Issueを再取得します。
更新中は画面上部に小さく状態表示が出ます。

APIキーはブラウザには出さず、ローカルのプロセス内だけで使います。

## ローカルキャッシュ

一度Redmineから取得したIssueは `.cache/` に保存されます。
2回目以降のアプリ起動直後は `.cache/` からすぐ表示し、その後バックグラウンドでRedmineから差分を取得します。
完全に取り直したい場合は画面上部の `全更新` を押してください。

`.cache/` にはRedmineのIssue内容が入るため、GitHubには登録しません。

## サンプルデータモード

Redmine APIに接続せず、サンプルIssueで画面だけ確認できます。
`.env` の値を変更します。

```env
USE_SAMPLE_DATA=true
```

その後、通常どおり `run_windows.ps1`、`run_windows.bat`、またはDockerで起動してください。
サンプルデータモードではAPIキーを使いません。

## 画面機能

- ステータスごとの横スクロールKanban
- 担当者フィルタ
- 対象バージョンの複数選択フィルタ
- フィルタ解除ボタン
- ライト / ダーク / OS設定連動のテーマ切替
- フィルタ後のカラム件数更新
- フィルタ後の担当者別作業負荷サマリー
- 担当者別作業負荷の棒グラフ表示
- カスタムフィールド `残作業時間` と最新作業時間コメントの表示
- 注意ラベルと状況確認項目
- closed / canceled / 終了 / 完了 / キャンセルで7日以上更新がないIssueの非表示

## GitHub登録前の確認

`.env`、`.cache/`、生成物、仮想環境がGit管理対象に入らないことを確認します。

```bash
git status --ignored
git ls-files
```

`git ls-files` に以下が出てこなければOKです。

```text
.env
.cache/
kanban.html
.venv/
__pycache__/
```

`.env.example` はGit管理対象に含めます。

## トラブルシューティング

### `.env` が `git status` に出る

`.gitignore` に以下があるか確認してください。

```gitignore
.env
.env.*
!.env.example
```

すでに `git add .env` してしまった場合は、コミット前なら以下でステージから外します。

```bash
git restore --staged .env
```

コミット済みの場合は、履歴から削除するだけでなくAPIキーを再発行してください。

### Redmine APIに接続できない

- `REDMINE_URL` が正しいか確認してください。
- Redmine側でREST APIが有効か確認してください。
- APIキーの権限を確認してください。
- `PROJECT_ID` がRedmineのプロジェクト識別子と一致しているか確認してください。
- プロキシ環境では `HTTP_PROXY` と `HTTPS_PROXY` を確認してください。

### `Address already in use` と表示される

ローカルサーバーのポートが既に使われています。別のポートで起動してください。

```powershell
.\run_windows.ps1 --serve --port 8001
```

```bat
run_windows.bat --serve --port 8001
```
