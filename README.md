# Redmine Kanban Board

Redmine REST API からIssueを取得し、ブラウザで開ける `kanban.html` を生成するローカル実行前提のPythonツールです。Redmine APIキーは各ユーザーの `.env` に保存し、GitHubには登録しません。

## セキュリティ

- APIキーを画面共有やスクリーンショットで見せないでください。
- `.env` はコミットしないでください。
- GitHubには `.env.example` のみ登録します。
- 誤ってAPIキーをコミットした場合は、履歴削除だけでなくAPIキーを再発行してください。
- 可能であれば読み取り専用のAPIキーを使ってください。
- GitHubにpushする前に必ず `git status` を確認してください。

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
PROJECT_ID=your_project_id
USE_SAMPLE_DATA=false
```

`REDMINE_URL` は自分のRedmine URL、`REDMINE_API_KEY` は自分のAPIキー、`PROJECT_ID` はRedmineのプロジェクト識別子に置き換えてください。

## 実行

```bash
python3 redmine_issues.py
```

成功すると、標準出力に取得件数、表示対象件数、注意Issueの一覧、生成されたHTMLのパスが表示されます。

```text
kanban.html: /path/to/redmine-kanban-agent/kanban.html
```

`kanban.html` は生成物です。GitHubには登録しません。

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
