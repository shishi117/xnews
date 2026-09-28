# 初期設定手順書

ゼロからこのツールを動かすまでの通し手順。各ステップの詳細（画面操作レベル）は
専用の手順書に譲り、ここでは「何を・どの順で」やるかだけをまとめる。
仕様の詳細は `docs/SPEC.md` を参照。
**コマンドはすべてプロジェクトルート（`run.bat` のあるフォルダ）で実行する前提。**

対象環境: Windows 11。翻訳はGroq（クラウド・無料枠）を使うので、Docker/GPUは不要。

> **クラウド運用（GitHub Actions + Cloudflare）にしたい場合**は、この手順書ではなく
> `docs/CLOUD_SETUP.md` を参照する。自分のPCを常時起動しておく必要がなくなり、
> ビューアもGoogleアカウント限定でどこからでも見られるようになる。

---

## 全体の流れ

```
1. ファイルを所定のフォルダ構成に配置する
2. Python仮想環境を作り、依存関係を入れる
3. GroqのAPIキーを取得する（無料・クレジットカード不要）
4. 捨て垢を作り、セッションCookieを取得する
5. config/config.ini に秘密情報を記入する
6. 段階的に初回実行する（selftest → collect-only → no-mail → フル実行）
7. 日次運用に組み込む（run.bat）
```

---

## 1. フォルダ構成

```
<ルート>/
├ run.bat                 … 日常の入口（ここから実行）
├ requirements.txt
├ .gitignore
├ src/x_collector.py      … 本体
├ config/
│   ├ config.ini          … 秘密情報（config.example.ini をコピーして作る）
│   └ config.example.ini
├ data/                   … 実行時に自動生成（news_log.db, state.json）
├ web/db_viewer.html
├ scripts/                … venv.bat, req.bat, test_mail.py
├ docs/                   … SPEC.md, SETUP.md, 各手順書
└ env/                    … Python仮想環境（venv.bat で作成）
```

`data/` の中身は初回実行時に自動生成されるので、事前に用意しなくてよい
（`data/` フォルダ自体も無ければ自動で作られる）。

## 2. Python環境

```powershell
cd （このフォルダ）
scripts\venv.bat     # env\ を作成
scripts\req.bat      # requirements.txt の依存関係をインストール
```

`run.bat` はルートに `env` があれば自動で使う（無ければシステムのpythonで
動くので必須ではないが、依存関係を汚さないため推奨）。

**まずここで自己テストが通ることを確認する**（ネットワーク一切不要）：

```powershell
python src\x_collector.py --selftest
```

`[SELFTEST OK] …` が出れば環境自体は問題ない。

## 3. GroqのAPIキー

1. https://console.groq.com/keys でアカウントを作り、APIキーを発行する（`gsk_` で始まる）。
   クレジットカードの登録は不要。
2. 次のステップで `config.ini` の `[llm] api_key` に貼る。

疎通確認（キーを書いた後に実行する）：

```powershell
python src\x_collector.py --test-llm
```

接続先・モデル名・キーの有無を表示し、1回だけ実際に問い合わせて結果を出す。
使えるモデルの一覧はこちらで確認できる：

```powershell
python src\x_collector.py --list-models
```

`HTTP 403 (error code: 1010)` ならUser-Agentが送られていない（キーの問題ではない）、
`HTTP 401` ならキーが違う、`HTTP 404` ならモデル名が現行でない
（https://console.groq.com/docs/models で確認して `config.ini` の `[llm] model` に書く）。

**無料枠の目安**は30リクエスト/分・6,000トークン/分（モデルにより日次上限あり）。
上限に当たっても止まらず、待ってから再試行する。1回の実行で「記事数＋軸数」ぶん
問い合わせるので、22件なら約26回・待ち時間は数分になる。
正確な上限はGroqのアカウント設定の Limits ページで確認できる。

## 4. 捨て垢セッションの取得

詳細手順は **`auth-session-setup.md`** を参照（捨て垢の作り方・DevToolsでの
`auth_token`/`ct0` の採り方・安全上の注意）。要点：

1. 個人・会社アカウントとは別に捨て垢を新規作成する（低頻度・読み取り専用で使う）
2. シークレットウィンドウでログインし、DevTools → Application/Storage → Cookies →
   `https://x.com` から `auth_token` と `ct0` の値をコピーする
3. `config/config.ini` に貼る（次のステップ）

## 5. config/config.ini の記入

`config/config.example.ini` を `config/config.ini` にコピーし、実値を埋める。

```ini
[mail]
gmail_user = you@gmail.com
gmail_app_password = 16文字のアプリパスワード
mail_to = 送信先アドレス（当面は個人アドレス推奨）

[x]
auth_token = ブラウザから採った値
ct0 = ブラウザから採った値

[llm]
api_key = gsk_で始まるGroqのAPIキー
model =            ; 空ならコードの既定値(openai/gpt-oss-120b)
rpm =              ; 空なら25
tpm =              ; 空なら5000
```

- `gmail_app_password` はGoogleアカウントの「アプリパスワード」（2段階認証が前提）。
  https://myaccount.google.com/apppasswords で発行。表示時のスペース区切りは
  そのまま貼ってよい（読み込み時に自動除去される）。
- `[x]` セクションは空でも動く（対象2垢は自動的にゲストへフォールバックするが、
  実測上ゲストでは新しい投稿が取れないため、実運用では埋めることを強く推奨）。

メール送信だけを単体で確認したい場合：

```powershell
python scripts\test_mail.py
```

`534 Application-specific password required` が出た場合は `gmail_app_password` が
アプリパスワードになっていない（通常のログインパスワードを貼っていないか確認）。

## 6. 段階的な初回実行

いきなりフル実行せず、切り分けながら進める。

```powershell
# ① 収集だけ（翻訳・送信・db_viewerなし。Groq不要）
python src\x_collector.py --collect-only          # 当日 0:00〜8:00(JST)
python src\x_collector.py --collect-only --from 8:00 --to 18:00   # 時間帯を指定
```
- ログの `打ち切り` が `窓を覆った` になっているかを確認する（なっていれば取りこぼし無し）。
  `[ALERT]` が出た場合は `docs/SPEC.md` §12.6 の見方に従って原因を切り分ける。
- 各ページの `cursor=有/無` を確認する。`cursor=無` で1ページしか取れない場合は
  `extract_cursor` の調整が必要（`docs/SPEC.md` §4.2）。
- financialjuice/DeItaone の行が `(🔐auth)` と表示されるか確認（`(guest)` のままなら
  `config/config.ini` の `[x]` セクションを見直す）。

```powershell
# ② 翻訳・ラベリングまで（送信・db_viewerなし）
python src\x_collector.py --no-mail
```
- `data/news_log.db` に訳文と4軸（国・地域／業界／経済政策／金融機関）のラベルが入って
  いるか確認したい場合は、
  `web/db_viewer.html` をダブルクリックで開く（`data/news_data.js` を読むので、
  一度でも実行していれば中身が見える。サーバは使わない）。
- ラベル正規化のログ（`国・地域: 33種類 → 16種類` など）が出る。統合がおかしい場合は
  `docs/SPEC.md` §6.5 を参照。

```powershell
# ③ フル実行（送信＋db_viewer自動起動まで）
python src\x_collector.py
```
- メールが届くか、届いたメールが「国・地域」ごとのセクション・タイムライン形式に
  なっているかを確認。1件が複数の国に該当する場合は、両方のセクションに載る。
- 送信後、ブラウザが自動で開き `db_viewer.html` がデータを自動読み込みした
  状態で表示されるか確認（ファイル選択ボタンは無い。サーバは使わずfile://で開く）。

## 7. 日次運用への組み込み

普段は `run.bat` を使う（venvを有効化してPythonを実行するだけ）。

```powershell
run.bat                 # 通常運用（当日0:00〜8:00 JST を収集→翻訳→送信→db_viewer）
run.bat --from 8:00 --to 18:00   # 時間帯を指定
run.bat --window 8      # 旧方式（実行時刻から8時間遡る）
run.bat --collect-only  # 収集のみ
run.bat --no-mail       # 翻訳まで・送信なし
run.bat --test-llm      # Groqへの疎通確認
run.bat --selftest      # オフライン自己テストのみ
```

Windowsタスクスケジューラに登録する場合：
- 既定は **当日0:00〜8:00(JST)** を収集する。時間帯を変えるには `--from` / `--to`、
  または `src\x_collector.py` の `DEFAULT_RANGE_FROM` / `DEFAULT_RANGE_TO` を編集する。
  1日を複数回に分けて配信する場合は、タスクを時間帯ごとに登録する
  （例: 8時に `--from 0:00 --to 8:00`、18時に `--from 8:00 --to 18:00`）。
- 投稿日時から **7日（`RETENTION_DAYS`）** を過ぎたデータは毎回の実行で自動削除される。
  長く残したい場合は `src/x_collector.py` の `RETENTION_DAYS` を変更する。
- **登録した時間帯しか集めない**ので、1日ぶんを漏らさず集めるにはタスクを
  時間帯で分けて登録する（`docs/SPEC.md` §4.4）。
  旧方式（実行時刻から遡る）を使う場合は `--window 8` を付け、実行間隔を8時間以内にする。
- 「操作」に `run.bat` へのフルパスを指定し、「開始（オプション）」に
  ルートフォルダを指定する（相対パス解決のため）。
- PCがスリープ状態でもタスクを実行する設定を有効にする（P4: 収集時刻にPC起動が必須）。

## トラブル時にどのファイルを見るか

| 症状 | 参照先 |
|---|---|
| 翻訳が動かない・`HTTP 401`/`404` が出る | `--test-llm` と `--list-models` を実行 |
| `[WAIT] レート上限` が頻発する | `docs/SPEC.md` §6（`config.ini` の `rpm`/`tpm` を下げる） |
| financialjuice/DeItaoneが0件・ゲストのまま | `docs/auth-session-setup.md` |
| メール送信が失敗する（`Connection unexpectedly closed`等） | `scripts\test_mail.py` を実行してログを見る |

| ラベルの表記が揃わない・統合がおかしい | `docs/SPEC.md` §6.5 |
| 軸を増減したい／廃止した軸がビューアに残る | `docs/SPEC.md` §6（`--prune-axes`） |
| db_viewer が真っ白／古いデータのまま | まず `python src\\x_collector.py --check-viewer`（DB件数・news_data.jsの件数と生成時刻を一度に確認） |
| 絞り込みで0件になっている | db_viewer の「フィルタをクリア」を押す（条件はブラウザに保存される） |
| db_viewer が「データがありません」と出る | 一度 `run.bat`（または `--no-mail` 以上）を実行してから開き直す（`docs/SPEC.md` §8） |
| 絞り込み条件が前回のまま残る／消えない | db_viewerの「フィルタをクリア」（`docs/SPEC.md` §8） |
| 動作の詳しい仕様・設定値の意味を確認したい | `docs/SPEC.md` |
| `404`/`400` でツイートが取れなくなった | `docs/SPEC.md` §4.1（queryId/features陳腐化） |
| 件数が少なすぎる・取りこぼしが疑わしい | `docs/SPEC.md` §12.6（ログの見方） |
