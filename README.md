# Redmine Kanban Board

Redmine REST API からIssueを取得し、ブラウザで開ける `kanban.html` を生成するローカル実行前提のPythonツールです。Redmine APIキーは各ユーザーの `.env` に保存し、GitHubには登録しません。

## セキュリティ

- APIキーを画面共有やスクリーンショットで見せないでください。
- `.env` はコミットしないでください。
- GitHubには `.env.example` のみ登録します。
- 誤ってAPIキーをコミットした場合は、履歴削除だけでなくAPIキーを再発行してください。
- 可能であれば読み取り専用のAPIキーを使ってください。
- GitHubにpushする前に必ず `git status` を確認してください。
- `docker compose config` は `.env` の値を表示するため、出力を共有しないでください。

## セットアップ

Python 3.10以上を想定しています。外部ライブラリは使っていませんが、将来の依存追加に備えて `requirements.txt` を置いています。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` を編集します。

```env
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=replace_with_your_api_key
PROJECT_ID=forkers-v3-development
USE_SAMPLE_DATA=false
HTTP_PROXY=
HTTPS_PROXY=
NO_PROXY=localhost,127.0.0.1
```

`REDMINE_URL` は自分のRedmine URL、`REDMINE_API_KEY` は自分のAPIキー、`PROJECT_ID` はRedmineのプロジェクト識別子に置き換えてください。
Dockerコンテナ内からプロキシ経由でRedmineへアクセスする場合は、必要に応じて `HTTP_PROXY` と `HTTPS_PROXY` も設定してください。

## 実行

静的HTMLを1回生成する場合:

```bash
python3 redmine_issues.py
```

成功すると、標準出力に取得件数、表示対象件数、注意Issueの一覧、生成されたHTMLのパスが表示されます。

```text
kanban.html: /path/to/redmine-kanban-agent/kanban.html
```

`kanban.html` は生成物です。GitHubには登録しません。

画面の `更新` ボタンでRedmineの最新状態へ追随したい場合は、ローカルサーバーモードを使います。

```bash
python3 redmine_issues.py --serve
```

ブラウザで開きます。

```text
http://127.0.0.1:8000/kanban.html
```

このURLを開くと、Python側がキャッシュしているIssueをHTMLとして返します。画面上部の `更新` を押したときだけ、前回取得日の1日前以降に変わったIssueをRedmine APIから差分取得してキャッシュに反映します。`全更新` を押すと全Issueを再取得します。APIキーはブラウザには出さず、ローカルのPythonプロセス内だけで使います。終了するときはターミナルで `Ctrl+C` を押してください。

画面上部の `PROJECT_ID` に別のプロジェクト識別子を入力して `表示` を押すと、そのPROJECT_IDのキャッシュを表示します。未取得のPROJECT_IDでは初回だけRedmine APIから全件取得します。最新状態へ更新したい場合は `更新` を押してください。Issue削除や別プロジェクトへの移動まで反映したい場合は `全更新` を押してください。初期値は `.env` の `PROJECT_ID` で、未設定の場合は `forkers-v3-development` です。

## Dockerで実行

Docker内でローカルサーバーモードを起動できます。APIキーを含む `.env` はイメージにコピーせず、実行時に `env_file` として読み込みます。

```bash
cp .env.example .env
```

`.env` を編集したうえで起動します。

```bash
docker compose up --build
```

ブラウザで開きます。

```text
http://127.0.0.1:8000/kanban.html
```

画面上部の `更新` を押すと、コンテナ内のPythonプロセスがRedmine APIから差分取得します。必要に応じて `全更新` で全Issueを再取得できます。

ポート `8000` が使用中の場合は、`docker-compose.yml` の左側のポートを変更します。

```yaml
ports:
  - "8001:8000"
```

この場合は `http://127.0.0.1:8001/kanban.html` を開きます。

Docker Composeを使わずに起動する場合:

```bash
docker build -t redmine-kanban .
docker run --rm --env-file .env -p 8000:8000 redmine-kanban
```

`.dockerignore` により `.env`, `kanban.html`, `__pycache__/` などはDockerビルドコンテキストから除外されます。

## サンプルデータモード

Redmine APIに接続せず、サンプルIssueで画面だけ確認できます。

`.env` の値を変更します。

```env
USE_SAMPLE_DATA=true
```

その後、通常どおり実行します。

```bash
python3 redmine_issues.py
```

サンプルデータモードではAPIキーを使いません。フィルタ、作業負荷サマリー、古い完了Issueの非表示などの画面挙動を確認できます。

## 生成されるHTML

`kanban.html` はCSSとJavaScriptを同じファイル内に含む静的HTMLです。ブラウザでそのまま開けます。

- ステータスごとの横スクロールKanban
- 担当者フィルタ
- 対象バージョンの複数選択フィルタ
- フィルタ解除ボタン
- ライト / ダーク / OS設定連動のテーマ切替
- フィルタ後のカラム件数更新
- フィルタ後の担当者別作業負荷サマリー
- 注意ラベルと夕会確認項目
- closed / canceled / 終了 / 完了 / キャンセルで7日以上更新がないIssueの非表示

WSL上で作業している場合は、Windows側から以下で開けます。

```bash
explorer.exe /path/to/redmine-kanban-agent/kanban.html
```

または簡易HTTPサーバーを使います。

```bash
python3 -m http.server 8000
```

ブラウザで `http://localhost:8000/kanban.html` を開きます。

ポート `8000` が使用中の場合は、別のポートを指定します。

```bash
python3 redmine_issues.py --serve --port 8001
```

ブラウザで `http://127.0.0.1:8001/kanban.html` を開きます。

## テーマ切替

`kanban.html` 上部のテーマ選択で以下を切り替えられます。

- `OS設定に合わせる`
- `ライト`
- `ダーク`

選択したテーマはブラウザの `localStorage` に `redmine-kanban-theme` というキーで保存されます。次回 `kanban.html` を開いたときも同じテーマで表示されます。

テーマ切替、担当者フィルタ、対象バージョンフィルタ、作業負荷サマリーはすべて `kanban.html` 内のCSSとJavaScriptだけで動作します。サーバーは不要です。

ただし、Redmine本体の更新を画面の `更新` ボタンで反映したい場合は `python3 redmine_issues.py --serve` のローカルサーバーモードを使ってください。静的な `kanban.html` を直接開いている場合、F5は既存ファイルを読み直すだけで、Redmine APIへの再取得は行いません。

## GitHub登録前の確認

まずローカルGit環境を準備します。

```bash
git init
git branch -M main
```

`.env` がGit管理対象に入らないことを確認します。

```bash
git status
git check-ignore -v .env
```

`git check-ignore -v .env` で `.gitignore` の `.env` 行が表示されればOKです。`git status` に `.env` が表示されないことも確認してください。

`.env.example` はGit管理対象に含めます。

```bash
git add .
git status
```

この時点で `.env` や `kanban.html` が含まれていないこと、`.env.example` が含まれていることを確認してください。

## 初回コミット

```bash
git commit -m "Initial local Redmine kanban agent"
```

GitHubでリポジトリを作成した後、remoteを追加してpushします。

```bash
git remote add origin https://github.com/OWNER/redmine-kanban-agent.git
git push -u origin main
```

`OWNER` は自分のGitHubユーザー名またはOrganization名に置き換えてください。

## Git操作の流れ

```bash
git init
git branch -M main
git status
git check-ignore -v .env
git add .
git status
git commit -m "Initial local Redmine kanban agent"
git remote add origin https://github.com/OWNER/redmine-kanban-agent.git
git push -u origin main
```

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

### まず画面だけ確認したい

`.env` でサンプルデータモードを有効にします。

```env
USE_SAMPLE_DATA=true
```

その後 `python3 redmine_issues.py` を実行してください。

### `kanban.html` がGitに入りそうになる

`kanban.html` は生成物です。以下で無視されていることを確認してください。

```bash
git check-ignore -v kanban.html
```

### `Address already in use` と表示される

ローカルサーバーのポートが既に使われています。別のポートで起動してください。

```bash
python3 redmine_issues.py --serve --port 8001
```
