# クラウド運用（GitHub Actions + Cloudflare）セットアップ手順

x_collector.py を自分のPC（Windowsタスクスケジューラ）ではなく、GitHub Actions上で
毎日自動実行し、ビューア（db_viewer.html）を「自分のGoogleアカウントだけ」がクラウドから
見られるようにするための手順書。`docs/SETUP.md`（ローカル運用）の代わりに、こちらの手順で
セットアップする。

対象は「② 限定公開設定（Googleアカウント）」の構成（図で確認したもの）。
GitHubリポジトリは**Private**のまま運用する。

---

## 全体の流れ

```
1. GitHubにPrivateリポジトリを作り、このプロジェクトをpushする
2. GitHub Secretsに秘密情報を登録する（config.iniの中身に相当）
3. Actionsがリポジトリへpushできるよう権限を設定する
4. 手動実行(workflow_dispatch)で一度動かし、動作を確認する
5. Cloudflare Pagesでビューアを公開する
6. Cloudflare Access（Zero Trust）でGoogleアカウント限定にする
7. 実際にURLを開いてログイン→閲覧できることを確認する
```

**用語補足**: 「Secrets」＝GitHubがリポジトリごとに提供する、パスワードやAPIキーなどを
暗号化して保存できる専用の保管場所。「Zero Trust」＝Cloudflareが提供する、アクセス制御
（誰にどのページを見せるか）をまとめて管理する管理画面の名称（Accessはその中の一機能）。

---

## 1. GitHubにPrivateリポジトリを作ってpushする

1. GitHubで新規リポジトリを作成する（例: `xnews`）。**Private**を選ぶこと。
   README等は追加しない（空のリポジトリでよい）。
2. 手元のプロジェクトフォルダで、`config/config.ini`（実値の入ったファイル）が
   存在しても構わないが、**絶対にコミットしないこと**。`.gitignore` に
   `config/config.ini` が既に入っているので、通常操作していれば混入しない。
3. 初回のpush:
   ```powershell
   cd （プロジェクトのルートフォルダ）
   git init
   git add .
   git status   # ← config/config.ini が一覧に出ていないことを必ず目視確認する
   git commit -m "initial commit"
   git branch -M main
   git remote add origin https://github.com/<あなたのアカウント>/xnews.git
   git push -u origin main
   ```
   `git status` の一覧に `config/config.ini` が出ていたら、`.gitignore` の内容と
   ファイルの場所を見直してから進める（一度でも `git add` してcommitすると
   履歴に残ってしまうため、その場合はリポジトリを作り直すのが確実）。

---

## 2. GitHub Secretsに秘密情報を登録する

これまで `config/config.ini` に書いていた値を、GitHubの暗号化された保管庫
（Secrets）に登録する。登録した値はワークフロー実行時にだけ環境変数として渡され、
コードやログには残らない。

GitHubのリポジトリ画面 → **Settings** → 左メニュー **Secrets and variables** →
**Actions** → **New repository secret** から、以下を1つずつ登録する
（名前は大文字・記号までワークフロー側と一致させること）。

| Secret名 | 値（config.iniでの対応先） |
|---|---|
| `GMAIL_USER` | `[mail] gmail_user` |
| `GMAIL_APP_PASSWORD` | `[mail] gmail_app_password`（スペース区切りのまま貼ってよい） |
| `MAIL_TO` | `[mail] mail_to` |
| `X_AUTH_TOKEN` | `[x] auth_token` |
| `X_CT0` | `[x] ct0` |
| `GROQ_API_KEY` | `[llm] api_key` |

`[llm] model` / `rpm` / `tpm` / `batch_size` はコードの既定値をそのまま使うなら
登録不要（変えたい場合だけ、`.github/workflows/xnews.yml` の `env:` に
`LLM_MODEL: ...` のような形で追加し、`x_collector.py` 側の対応する
`secret(...)` 呼び出しも増やす必要がある。当面は既定値のままで問題ない）。

---

## 3. Actionsの書き込み権限を有効にする

ワークフローは実行結果（`data/news_log.db` 等）をリポジトリにコミットして
次回実行に引き継ぐため、Actionsからのpushを許可する必要がある。

リポジトリの **Settings** → **Actions** → **General** → 一番下の
**Workflow permissions** で、**Read and write permissions** を選び **Save**。
（既定は読み取り専用になっていることが多く、そのままだと最後のpushでエラーになる）

---

## 4. 手動実行で動作確認する

1. リポジトリの **Actions** タブ → 左の **xnews collect & mail** → 右上の
   **Run workflow** ボタン → **Run workflow** で手動起動する。
2. 実行中のジョブを開き、各ステップのログを確認する。
   - 「収集 → 翻訳 → メール送信 → ビューア用JSON生成」のログに、
     ローカルで見慣れた `[*] ...` の出力が並んでいれば正常。
   - `[*] db_viewer: CI環境のため...スキップ` と出ていれば想定通り
     （CI上にはブラウザが無いため、意図的にスキップする）。
3. 実際にメールが届くか確認する。
4. 実行が終わったら、リポジトリの **Code** タブで `data/news_log.db` /
   `data/state.json` / `data/news_data.js` に新しいコミットが入っているか確認する
   （入っていなければ手順3の権限設定を見直す）。

ここまで確認できれば、あとはスケジュール（毎日08:13 JST）に沿って自動的に動く。
スケジュールを変えたい場合は `.github/workflows/xnews.yml` の `cron:` の行を編集する
（時刻はUTC表記。JST = UTC+9）。

---

## 5. Cloudflare Pagesでビューアを公開する

1. Cloudflareダッシュボード → **Workers & Pages** → **Create** → **Pages** →
   **Connect to Git** で、先ほどのGitHubリポジトリ（xnews）を選ぶ。
   初回はCloudflareにGitHubへのアクセスを許可する画面が出るので許可する。
2. ビルド設定は次の通り（このプロジェクトはビルド不要な静的ファイルのため）。
   - **Build command**: 空欄のまま（何も入力しない）
   - **Build output directory**: `/`（リポジトリのルート。`web/`と`data/`を
     両方とも同じ相対関係のまま公開する必要があるため）
3. **Save and Deploy** を押すとデプロイが始まる。数十秒〜数分で
   `https://xnews-xxxx.pages.dev/web/db_viewer.html` のようなURLが発行される。
4. 一度アクセスして、ビューアが（現時点ではまだ誰でも見える状態で）正しく
   表示されることを確認する。表示されない場合は「Build output directory」の
   指定を見直す。
5. 以後、GitHub Actionsが`data/news_data.js`等を更新してpushするたびに、
   Cloudflare Pagesが自動で再デプロイする（追加の作業は不要）。

---

## 6. Cloudflare Access（Zero Trust）でGoogleアカウント限定にする

ここまでの時点ではURLを知っていれば誰でも見られる状態（①公開設定と同じ）なので、
Googleアカウントでのログインを必須にする設定を追加する。

### 6-1. Zero Trustのチームドメインを作る（初回のみ）

Cloudflareダッシュボード → 左メニュー **Zero Trust** を開く（初回はチーム名の設定を
求められるので、任意の名前を付ける。例: `shishida-xnews`）。無料プランのままでよい
（50ユーザーまで無料）。

### 6-2. ログイン方法にGoogleを追加する

Zero Trust → **Settings** → **Authentication** → **Login methods** →
**Add new** → **Google** を選ぶ。GoogleでログインさせるにはGoogle Cloud Console側で
OAuthクライアントを作る必要がある（Cloudflare側の画面に手順とコールバックURLが
表示されるので、その通りに進める）。おおまかな流れ：

1. [Google Cloud Console](https://console.cloud.google.com/) で新規プロジェクトを作成
   （名前は何でもよい）。
2. **APIとサービス** → **OAuth同意画面** を設定（User Type: 外部 / テストユーザーに
   自分のGoogleアカウントを追加、で十分。公開審査は不要）。
3. **認証情報** → **認証情報を作成** → **OAuthクライアントID** → アプリケーションの種類は
   **ウェブアプリケーション**。**承認済みのリダイレクトURI**にCloudflare側の画面に
   表示されているコールバックURL（`https://<チーム名>.cloudflareaccess.com/cdn-cgi/access/callback`
   の形）を貼り付ける。
4. 発行された **クライアントID** と **クライアントシークレット** をCloudflareの
   Google設定画面に貼り、保存する。

### 6-3. Accessアプリケーションを作る

Zero Trust → **Access** → **Applications** → **Add an application** →
**Self-hosted** を選ぶ。

- **Application name**: 任意（例: `xnews viewer`）
- **Session duration**: 好みで（例: 24h。ログインし直す頻度が決まる）
- **Application domain**: Cloudflare Pagesで発行されたドメイン
  （例: `xnews-xxxx.pages.dev`。パスを絞りたい場合は `xnews-xxxx.pages.dev/web/db_viewer.html`
  のように指定することもできる）

### 6-4. ポリシーを作る（許可するアカウントを指定）

同じ画面の続きで **Add a policy** →

- **Policy name**: 任意（例: `本人のみ許可`）
- **Action**: `Allow`
- **Include** の条件で **Emails** を選び、自分のGoogleアカウントのメールアドレスを
  入力する（複数人に見せたい場合はここに追加していく）
- **Login methods** で、6-2で追加した **Google** だけを選ぶ
  （他の方法＝One-time PINなどを外しておくと、確実にGoogleアカウントでの
  ログインだけに限定できる）

保存すると数分以内に反映される。

---

## 7. 動作確認

1. シークレットウィンドウなど、ログイン状態が残っていないブラウザで
   ビューアのURLを開く。
2. Cloudflareのログイン画面が出て、Googleログインを選ぶとGoogleの
   アカウント選択画面に遷移する → 許可したアカウントでログイン。
3. ログイン後、自動的にビューア（db_viewer.html）にリダイレクトされ、
   データが表示されれば成功。
4. 許可していない別のGoogleアカウントでログインした場合、
   Cloudflareの403（アクセス拒否）画面が出ることも確認しておくとよい。

---

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| Actionsの最後のステップで push が失敗する（Permission denied 等） | 手順3の「Workflow permissions」が Read and write になっているか |
| メールが届かない | Secretsの `GMAIL_USER` / `GMAIL_APP_PASSWORD` / `MAIL_TO` の値、特にアプリパスワードの桁数（16文字）を確認 |
| financialjuice/DeItaoneが `(guest)` のまま | Secretsの `X_AUTH_TOKEN` / `X_CT0` が正しく登録されているか（`docs/auth-session-setup.md`） |
| Cloudflare Pagesのビューアが真っ白 | 「Build output directory」が `/` になっているか。`data/news_data.js` がリポジトリに存在し、Actionsが実際にpushしているか |
| Googleログイン画面が出ずに直接見えてしまう | Accessアプリケーションの「Application domain」がCloudflare Pagesの実際のドメインと一致しているか |
| Googleログインが失敗する（redirect_uri_mismatch等） | Google Cloud Console側のリダイレクトURIとCloudflareが提示するコールバックURLが完全に一致しているか |
| リポジトリのサイズが気になってきた | `data/`配下は保持期間(7日)分しか無いため1回あたりは小さいが、pushのたびに履歴が積み上がる。当面は無料枠内で問題ないが、気になれば数か月おきに履歴を整理（squash）してもよい |

---

## ローカル運用に戻したくなったら

`config/config.ini` を作成すれば、`secret()` の仕組みによりconfig.ini側の値が
常に優先される。ローカルで `python src\x_collector.py` を実行する分には
これまで通り動く（環境変数は未設定でも問題ない）。
