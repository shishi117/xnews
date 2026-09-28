#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X の指定アカウント（複数）から「本人オリジナル投稿の未取得分」を集め、翻訳・ラベリング
してHTMLメールで配信する。D4対象: financialjuice / DeItaone（いずれも捨て垢セッション）。

  - 収集: curl_cffi + 捨て垢セッション(auth_token/ct0)で X 内部GraphQLを直叩き
    未設定時はゲストトークンへ自動退避（動作は継続する）
  - ページネーション: 1ページで WINDOW_HOURS に届かない超高頻度アカウント向けに
    カーソルで複数ページ取得（MAX_PAGES で必ず打ち切り）
  - 秘密情報: config.ini に集約（システム環境変数は使わない）。取得手順は
    auth-session-setup.md 参照。config.ini は <ルート>/config/ に置く
  - 差分抽出: state.json の per-account last_seen_id で id 数値比較（スノーフレークID=時刻順）
  - 本人オリジナルのみ: RT(retweeted_status_result)・引用(is_quote_status)・他人ツイートを除外
  - 常時 WINDOW_HOURS 時間の窓でも足切り（実行間隔は窓以内にすること）
  - 翻訳＋ラベリング: Groq(クラウド・無料枠/OpenAI互換)で日本語訳と、LABEL_AXES に列挙した任意の軸
    （個数・内容は自由に変更可）を付与。news_labels テーブルに縦持ちで保存
  - 配信: Gmail SMTP + HTMLメール（GAS版 escapeHtml_ の順序を踏襲）
  - メール送信後、DBの中身をJSON化して data/news_data.js に書き出し、db_viewer.html を
    file:// で直接開く（サーバは使わない）
  - 耐障害: state.json はアトミック置換 / アカウントごとに独立 try/except / F6失敗検知
  - 自己チェック: ネットワーク不要の --selftest（分類・差分・保存・翻訳・HTML・XSS・config・
    ページネーション）

--- 実行 --------------------------------------------------------------------
  自宅PCで:  python src/x_collector.py                # 収集→翻訳→メール送信→db_viewer起動
             python src/x_collector.py                 # 当日 0:00〜8:00(JST) を収集
             python src/x_collector.py --from 8:00 --to 18:00   # 時間帯を指定
             python src/x_collector.py --window 8      # 旧方式（実行時刻から8時間遡る）
             python src/x_collector.py --collect-only # 収集のみ（翻訳・送信・db_viewerなし）
             python src/x_collector.py --no-mail       # 翻訳まで実施、送信・db_viewerは抑止
             python src/x_collector.py --selftest      # オフライン自己テスト
             python src/x_collector.py --test-llm      # Groqへの疎通確認（1回だけ呼ぶ）
             python src/x_collector.py --list-models   # このキーで使えるモデル一覧
             python src/x_collector.py --check-viewer  # db_viewerが表示されないときの切り分け
  依存: curl_cffi（収集時のみ）。--selftest は標準ライブラリだけで走る。
  ★queryId/features は回転する。古いと 404/400。更新手順は本ファイル内コメント参照。
"""

import os
import re
import sys
import json
import time
import sqlite3
import smtplib
import webbrowser
import configparser
import urllib.request
import urllib.error
import io
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta

# ==== 設定 ====================================================================
HANDLES = ["financialjuice", "DeItaone"]   # ★D4確定分。いずれも捨て垢セッション必須
COUNT = 40                    # 1ページの要求件数（Xは厳密に守らず~100件前後返すことがある）
# ---- 収集する時間帯 -----------------------------------------------------------
# 既定は【時間範囲モード】: 当日のこの時刻帯（日本時間）を対象にする。
#   例) 0:00〜8:00 → 早朝ぶんをまとめて1通にする運用。
# コマンドラインで上書きできる:
#   --from 6:00 --to 12:00            … 当日の6〜12時（JST）
#   --from "2026/09/23 22:00" --to "2026/09/24 02:00"   … 日付をまたぐ指定
#   --window 8                        … 旧方式（実行時刻から遡ってN時間）
DEFAULT_RANGE_FROM = "00:00"
DEFAULT_RANGE_TO = "08:00"

WINDOW_HOURS = 8              # --window の既定値（実行時刻からN時間遡る）
MAX_PAGES = 10                 # ★超高頻度アカウント対策のページネーション安全弁。
                               #   1ページで WINDOW_HOURS に届かない場合カーソルで追加取得するが、
                               #   ここで必ず打ち切る（無限ページングを防ぐ）。2垢×最大5P=10req/回、
                               #   ゲストのUserTweetsレート上限(50req/窓)に対し十分余裕がある。
# ---- パス: プロジェクトルート（src/ の1つ上）を基準に解決 -----------------------
#   config/config.ini … 秘密情報 / data/ … 実行時生成物 / web/db_viewer.html … ビューア
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
STORE_PATH = os.path.join(DATA_DIR, "news_log.db")
# ★db_viewer.html はローカルサーバを使わず file:// で直接開く運用にした（サーバ廃止）。
#   file:// はブラウザがfetchでのローカルファイル読み込みを禁止するため、DBの中身を
#   tweet_idごとに組み立てたJSONとして書き出し、db_viewer が <script src> で読み込む
#   （<script>読み込みはfile://でも禁止されない。以前はここにsqlite本体をbase64で
#   埋め込んでいたが、ブラウザ側でSQLを実行する必要が無いのでJSONへ簡素化した）。
VIEWER_DATA_PATH = os.path.join(DATA_DIR, "news_data.js")

# ---- 翻訳＋ラベリング（Groq のクラウド推論。OpenAI互換）------------------------
# ローカル(Ollama/Docker)はPC負荷が高すぎたため廃止し、Groqの無料枠に移行した。
# APIキーは config.ini の [llm] api_key（console.groq.com/keys で発行）。
LLM_BASE = "https://api.groq.com/openai/v1"
# ★2026/06/17に llama-3.3-70b-versatile / llama-3.1-8b-instant の
#   無料・開発者ティアでの非推奨がアナウンスされている。後継として案内されているのが
#   openai/gpt-oss-120b / openai/gpt-oss-20b / qwen/qwen3.8-27b。
#   ここでは訳文品質を優先して120bを既定にする（軽くしたいなら20bへ1行差し替え）。
#   ※モデル名は変わりやすい。404が出たら console.groq.com/docs/models で現行名を確認する。
LLM_MODEL = "openai/gpt-oss-120b"
# ---- ラベリングの軸（個数・内容とも自由に変更可）------------------------------
# ここに列挙した軸それぞれについて、LLMが短い値（目安10文字以内）を日本語で付ける。
# 増減・名称変更は自由（news_labels テーブルは軸名をそのまま保存する縦持ち設計のため、
# 列追加などのスキーマ変更は不要）。
LABEL_AXES = ["国・地域", "業界", "経済政策", "金融機関"]
EMAIL_GROUP_BY = "国・地域"   # メールをセクション分けする軸（ユーザー選択）。変更自由

# ---- ラベル正規化（表記ゆれの統一。メール送信の直前に全件へ適用）----------------
# 1件ずつのラベリングでは「米国/アメリカ」「なし/無/×」のように表記が割れる。
# そこで enrich 後に、DB内の【全ての現存値】をLLMに見せて代表表記へ寄せ直す。
# 1つの値に複数対象が入っていた場合（"イラン, 米国"）は複数ラベルへ分割する
# （news_labels の主キーが (tweet_id, axis, value) なので1軸に複数値を持てる）。
NORMALIZE_CHUNK = 15      # 1回のLLM呼び出しに渡す値の数。大きいと出力が途中で切れる
                          # （実機で値30個の軸が HTTP 500。プロンプト+出力が
                          #   OLLAMA_CONTEXT_LENGTH を超えたのが原因と推定）
NORMALIZE_MAX_SPLIT = 4   # 1つの値を分割してよい上限（暴走防止）
NORMALIZE_MAX_LEN = 20    # 正規化後の値の最大文字数（超えたら採用せず元の値を残す）

# ---- 保持期間（これより古い投稿はDBから削除する）--------------------------------
RETENTION_DAYS = 7        # news_log.created_at(JST) 基準。news_labels も連動して削除

# ---- 秘密情報: config.ini（ローカル）または環境変数（GitHub Actions等クラウド）----------
# プロジェクトルートの config/config.ini から読む。無い/項目欠損でも起動時に落とさず、
# 該当機能だけ実行時にスキップ/警告（メール未設定→送信失敗を表示、X未設定→ゲストへ退避）。
# ★クラウド運用（GitHub Actions）ではconfig.iniをリポジトリに置けない（秘密情報のため）。
#   その場合はGitHub Secretsに登録した値がワークフロー実行時に環境変数として渡される。
#   config.ini の値を優先し、無ければ同名の環境変数を見る（secret()）。ローカルでconfig.ini
#   を使っている人には一切影響しない。環境変数名の対応は docs/CLOUD_SETUP.md 参照。
CONFIG_PATH = os.path.join(BASE_DIR, "config", "config.ini")


def cfg_get(cfg, section, key):
    """configparserの薄いラッパー。値なし/セクションなしはすべて空文字に正規化。"""
    return cfg.get(section, key, fallback="").strip()


def secret(cfg, section, key, env_name):
    """config.ini の値を優先し、無ければ環境変数(env_name)にフォールバックする。
    ローカル(config.ini)とGitHub Actions(Secrets→環境変数)の両方に対応するための薄い層。"""
    v = cfg_get(cfg, section, key)
    if v:
        return v
    return os.environ.get(env_name, "").strip()


def load_config(path=CONFIG_PATH):
    cfg = configparser.ConfigParser()
    if os.path.exists(path):
        cfg.read(path, encoding="utf-8")
    return cfg


_CFG = load_config()

# ---- メール（Gmail SMTP + アプリパスワード）----------------------------------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
MAIL_FROM = secret(_CFG, "mail", "gmail_user", "GMAIL_USER")            # 例: you@gmail.com
MAIL_APP_PASSWORD = secret(_CFG, "mail", "gmail_app_password", "GMAIL_APP_PASSWORD").replace(" ", "")
# ★Googleは表示時に4桁区切りでスペースを入れる（例: "abcd efgh ijkl mnop"）。
#   そのままコピペされる事故が多いため、内部スペースは読み込み時に除去する。
#   （本来16文字。除去後の長さが16でなければ値そのものが違う可能性が高い）
MAIL_TO = secret(_CFG, "mail", "mail_to", "MAIL_TO")                    # 当面は個人アドレス推奨(D5据置)
MAIL_SUBJECT = "【Xニュース】"

# ---- LLM(Groq)の設定とレート制限 ----------------------------------------------
LLM_API_KEY = secret(_CFG, "llm", "api_key", "GROQ_API_KEY")
LLM_MODEL = cfg_get(_CFG, "llm", "model") or LLM_MODEL       # config.iniで上書き可
# 無料枠の目安: 30リクエスト/分・6,000トークン/分（組織単位・モデル単位）。
# 上限ちょうどに張り付くと429が多発するので、既定は少し内側に取る。
# ★実際の上限はアカウントの Limits ページで確認できる。違っていればここを直す。
LLM_RPM = int(cfg_get(_CFG, "llm", "rpm") or 25)             # 1分あたりのリクエスト上限
LLM_TPM = int(cfg_get(_CFG, "llm", "tpm") or 5000)           # 1分あたりのトークン上限
# ★GroqのAPIはCloudflareの背後にある。User-Agentを付けないと、Pythonの既定UA
#   （Python-urllib/3.x）がブラウザ整合性チェックに弾かれ、**APIキーが検証される前に**
#   `HTTP 403: error code: 1010` で落ちる（実機で発生）。UAは何でもよいが必ず付ける。
LLM_USER_AGENT = "x-collector/1.0 (+https://localhost)"
# ★gpt-oss系は「推論モデル」で、隠れた推論トークンが出力予算を食う。予算が足りないと
#   JSONを書き始める前に打ち切られ、Groq側のJSON検証が
#   `HTTP 400 json_validate_failed`（failed_generation が空）で弾く（実機で発生）。
#   対策は3つ: ①出力予算を大きく取る ②reasoning_effort=low で推論を短くする
#   ③それでも駄目ならJSONモード(response_format)を外す（こちらで緩くパースしている）。
LLM_REASONING_HEADROOM = 1500   # 推論モデル向けに上乗せする出力予算
LLM_REASONING_EFFORT = "low"    # 分類・翻訳なので深い推論は不要
LLM_MAX_RETRY = 6            # 429/5xx のリトライ回数
LLM_MAX_WAIT = 900           # 1回の待機の上限(秒)。これを超える指示が来たら諦めて例外
# ★1件ずつ問い合わせるとRPM(リクエスト数)上限に真っ先に当たり、待ち時間が支配的になる。
#   複数件をまとめて1回のAPI呼び出しで処理し、呼び出し回数そのものを減らす。
#   バッチ呼び出しが失敗した場合は半分に分割して再試行し、1件まで割ってもだめなら
#   個別処理（中国語再試行つき）にフォールバックする（normalize_labelsと同じ考え方）。
ENRICH_BATCH_SIZE = int(cfg_get(_CFG, "llm", "batch_size") or 10)   # config.iniで上書き可

# ---- 認証済みセッション（捨て垢）フォールバック ------------------------------
# ★財経速報系(financialjuice/DeItaone)はゲストAPIだと古いキャッシュしか返らない実測
#   （ブラウザのゲストでは新しい投稿が見えるのにAPI直叩きだけ古い＝要件P3の発現）。
#   この2垢だけ捨て垢セッションを使う。取得方法は auth-session-setup.md 参照。
#   auth_token/ct0 未設定ならこの2垢もゲストへフォールバックする（動作は継続する）。
AUTH_HANDLES = {"financialjuice", "deitaone"}   # 小文字で比較。ここだけ捨て垢を使う
X_AUTH_TOKEN = secret(_CFG, "x", "auth_token", "X_AUTH_TOKEN")
X_CT0 = secret(_CFG, "x", "ct0", "X_CT0")

# ---- 通信部（Slice 0 で実証済み。回転する定数の更新手順は slice0_fetch.py 参照）----
WEB_BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4"
              "puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
QUERY_IDS = {                 # ★回転する。DevTools の graphql から現行値を貼る。
    "UserByScreenName": "Yka-W8dz7RaEuQNkroPkYw",
    "UserTweets":       "E3opETHurmVJflFsUBVuUQ",
}
FEATURES_USER = {
    "hidden_profile_subscriptions_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}
FEATURES_TWEETS = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}
GRAPHQL = "https://x.com/i/api/graphql"
TW_FMT = "%a %b %d %H:%M:%S %z %Y"     # 例: "Tue Sep 22 03:12:02 +0000 2026"
JST = timezone(timedelta(hours=9))     # 固定+9（日本はDST無し）。tzdata依存を避ける
# =============================================================================


class FetchError(RuntimeError):
    def __init__(self, step, status, body, headers=None):
        self.step, self.status, self.body = step, status, body
        self.headers = headers or {}
        super().__init__(f"[{step}] HTTP {status}")


# ---- 通信（curl_cffi は収集時のみ遅延 import。--selftest は依存なしで動かすため）----
def _session():
    from curl_cffi import requests
    return requests.Session(impersonate="chrome")


def get_guest_token(s):
    r = s.post("https://api.x.com/1.1/guest/activate.json",
               headers={"Authorization": WEB_BEARER})
    if r.status_code != 200 or not r.json().get("guest_token"):
        raise FetchError("guest_token", r.status_code, r.text, r.headers)
    return r.json()["guest_token"]


def guest_headers(gt):
    return {"Authorization": WEB_BEARER, "x-guest-token": gt,
            "x-twitter-active-user": "yes", "x-twitter-client-language": "en",
            "Content-Type": "application/json"}


def auth_headers():
    """捨て垢セッション用。auth_token/ct0 は auth-session-setup.md の手順で取得した値。"""
    return {"Authorization": WEB_BEARER,
            "Cookie": f"auth_token={X_AUTH_TOKEN}; ct0={X_CT0}",
            "x-csrf-token": X_CT0,
            "x-twitter-active-user": "yes", "x-twitter-client-language": "en",
            "Content-Type": "application/json"}


def headers_for(handle, gt):
    """handleがAUTH_HANDLES所属かつauth_token/ct0が設定済みなら捨て垢、それ以外はゲスト。"""
    if handle.lower() in AUTH_HANDLES:
        if X_AUTH_TOKEN and X_CT0:
            return auth_headers(), "auth"
        print(f"[WARN] @{handle}: config.ini の [x] 未設定のためゲストで実行")
    if not gt:   # ゲストトークンが取れていない。呼び出し元のtryで1垢ぶんだけ失敗させる
        raise FetchError("guest_token", 0, "ゲストトークン未取得のためゲスト実行不可")
    return guest_headers(gt), "guest"


def graphql_get(s, headers, op, variables, features, field_toggles=None):
    qid = QUERY_IDS.get(op)
    if not qid:
        raise FetchError(op, 0, f"QUERY_IDS['{op}'] 未設定")
    params = {"variables": json.dumps(variables, separators=(",", ":")),
              "features": json.dumps(features, separators=(",", ":"))}
    if field_toggles is not None:
        params["fieldToggles"] = json.dumps(field_toggles, separators=(",", ":"))
    r = s.get(f"{GRAPHQL}/{qid}/{op}", params=params, headers=headers)
    if r.status_code != 200 or not r.text.strip():   # 空200+計量=認可の壁
        raise FetchError(op, r.status_code, r.text, r.headers)
    try:
        return r.json()
    except ValueError:
        raise FetchError(op, r.status_code, r.text, r.headers)


def resolve_user_id(s, headers, handle):
    data = graphql_get(s, headers, "UserByScreenName", {"screen_name": handle},
                       FEATURES_USER, field_toggles={"withAuxiliaryUserLabels": False})
    for d in walk(data):
        if d.get("rest_id") and isinstance(d.get("legacy"), dict) \
           and d["legacy"].get("screen_name"):
            return d["rest_id"]
    raise FetchError("UserByScreenName", 200, "rest_id 発見できず: " + json.dumps(data)[:600])


# ---- 抽出・分類（決め打ちパスを避け汎用ウォーク。純粋関数=--selftestで検証）--------
def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def extract_all(payload):
    """legacy に full_text+created_at を持つ dict をツイート実体として全部拾い、分類する。
    RT/引用の内側ツイートも一旦拾うが、後段で author 一致と RT/引用フラグで落とす。"""
    seen, out = set(), []
    for d in walk(payload):
        legacy = d.get("legacy")
        if not isinstance(legacy, dict):
            continue
        text, created = legacy.get("full_text"), legacy.get("created_at")
        if not (text and created):
            continue
        rid = d.get("rest_id") or legacy.get("id_str")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        sn = next((u["legacy"]["screen_name"] for u in walk(d.get("core", {}))
                   if isinstance(u.get("legacy"), dict) and u["legacy"].get("screen_name")),
                  None)
        is_rt = ("retweeted_status_result" in legacy) or text.startswith("RT @")
        is_quote = bool(legacy.get("is_quote_status")) or ("quoted_status_result" in d)
        url = f"https://x.com/{sn}/status/{rid}" if sn else f"https://x.com/i/web/status/{rid}"
        out.append({"id": rid, "author": sn or "", "created_at": created, "text": text,
                    "likes": legacy.get("favorite_count"),
                    "retweets": legacy.get("retweet_count"),
                    "url": url, "is_rt": is_rt, "is_quote": is_quote})
    return out


def parse_time(s):
    return datetime.strptime(s, TW_FMT)


def extract_cursor(payload, cursor_type="Bottom"):
    """次ページ用カーソルを汎用ウォークで抽出。無ければNone（＝最終ページ扱い）。
    ★X実機での cursorType/value キー名は未検証。ページネーションが進まない場合は
    DevToolsでUserTweetsのcursor-bottomエントリの実際のキー名を確認して調整する。"""
    for d in walk(payload):
        if d.get("cursorType") == cursor_type and d.get("value"):
            return d["value"]
    return None


def parse_jst_time(text, now_jst):
    """'8:00' / '08:00' / '2026/09/24 8:00' / '09/24 8:00' をJSTのdatetimeにする。
    時刻だけの指定は「今日（now_jst基準）」の日付を補う。"""
    t = text.strip().replace("-", "/")
    for fmt, need_date in (("%Y/%m/%d %H:%M", False), ("%m/%d %H:%M", "year"),
                           ("%H:%M", "date"), ("%Y/%m/%d", False)):
        try:
            d = datetime.strptime(t, fmt)
        except ValueError:
            continue
        if need_date == "year":
            d = d.replace(year=now_jst.year)
        elif need_date == "date":
            d = d.replace(year=now_jst.year, month=now_jst.month, day=now_jst.day)
        return d.replace(tzinfo=JST)
    raise ValueError(f"時刻の書式が不正です: {text!r}"
                     "（例: 8:00 / 2026/09/24 8:00）")


def arg_value(argv, name):
    """--name VALUE / --name=VALUE を取り出す。無ければ None。"""
    for i, a in enumerate(argv):
        if a == name:
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def resolve_range(argv, now):
    """収集する時間帯 (start_utc, end_utc, 表示用ラベル) を決める。
    既定は【時間範囲モード】（当日 DEFAULT_RANGE_FROM〜DEFAULT_RANGE_TO のJST）。
    --window N を付けたときだけ旧方式（実行時刻から遡ってN時間）になる。"""
    now_jst = now.astimezone(JST)
    win = arg_value(argv, "--window")
    if win is not None:
        hours = float(win) if win else WINDOW_HOURS
        start = now - timedelta(hours=hours)
        return start, now, (f"直近{hours:g}時間"
                            f"（{start.astimezone(JST).strftime('%m/%d %H:%M')}"
                            f"〜{now_jst.strftime('%m/%d %H:%M')} JST）")
    f = arg_value(argv, "--from") or DEFAULT_RANGE_FROM
    t = arg_value(argv, "--to") or DEFAULT_RANGE_TO
    start_jst = parse_jst_time(f, now_jst)
    end_jst = parse_jst_time(t, now_jst)
    if end_jst <= start_jst:          # 22:00〜02:00 のような日跨ぎ指定を素直に解釈する
        end_jst += timedelta(days=1)
    return (start_jst.astimezone(timezone.utc), end_jst.astimezone(timezone.utc),
            f"{start_jst.strftime('%m/%d %H:%M')}〜{end_jst.strftime('%m/%d %H:%M')} JST")


def filter_originals(raw, handle, prev, start, end):
    """未取得の本人オリジナルだけ返し、次回用の高水位(last_seen)も返す。
    [start, end] の時間帯で足切りし（ピン留めは日付が古いので自動的に外れる）、
    既送信(id<=prev)を除外する。
    ★高水位は【範囲内】の最大 id。範囲より新しい投稿（ページ送りの都合で raw には
      入っている）は「送信済み」にしない＝まだ配信していないものを取りこぼさないため。
    ※範囲を毎回適用するため、実行間隔が範囲より空くと隙間は取りこぼす。"""
    in_range = [t for t in raw
                if start <= parse_time(t["created_at"]) <= end]
    base = in_range
    if prev is not None:                            # 既に送った分は id 比較で重複回避
        base = [t for t in base if int(t["id"]) > prev]
    d_author = d_rt = d_quote = 0
    emit = []
    for t in base:                       # 除外理由を数えながら本人オリジナルを選ぶ
        if t["author"].lower() != handle.lower():
            d_author += 1
        elif t["is_rt"]:
            d_rt += 1
        elif t["is_quote"]:
            d_quote += 1
        else:
            emit.append(t)
    emit.sort(key=lambda t: int(t["id"]))
    ids = [int(t["id"]) for t in in_range]
    new_high = max([prev or 0] + ids) if ids else prev
    # ★ピン留め（何年も前）は診断表示の範囲・頻度を壊すので、範囲の1日前より
    #   古いものは統計から外す（ページ送りの判定には影響しない）。
    span_floor = start - timedelta(days=1)
    raw_times = [tt for tt in (parse_time(t["created_at"]) for t in raw) if tt >= span_floor]
    newest = max(raw_times) if raw_times else None
    stats = {"raw": len(raw), "base": len(base), "drop_author": d_author,
             "drop_rt": d_rt, "drop_quote": d_quote, "emit": len(emit),
             # 検出した著者名の例（handleと食い違うなら解決先ミス or RTの内側）
             "authors": sorted({t["author"] for t in raw})[:6],
             # 最新投稿がいつか（8h窓で0になる原因＝谷間/キャッシュ古 の切り分け）
             "newest_jst": newest.astimezone(JST).strftime("%m/%d %H:%M") if newest else "-",
             "newest_age_h": round(
                 (datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1) if newest else None,
             # 取得できた範囲そのもの。窓内が少ないとき「取れていない」のか
             # 「その時間帯に投稿が無い」のかを切り分けるために出す。
             "raw_span": (f"{min(raw_times).astimezone(JST).strftime('%m/%d %H:%M')}〜"
                          f"{newest.astimezone(JST).strftime('%m/%d %H:%M')}") if raw_times else "-",
             "raw_rate_h": (round(len(raw) / max(
                 (newest - min(raw_times)).total_seconds() / 3600, 0.01), 1)) if len(raw_times) > 1 else None}
    return emit, new_high, stats


# ---- 状態（アトミック置換）--------------------------------------------------
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)   # 同一FS上のアトミック置換。書き込み途中の破損を防ぐ


# ---- 保存: ローカル SQLite の news_log（構成b: Sheets を使わずここに蓄積）--------
# §7 由来の8列に加え、構成b用に translation を追加（レベルは廃止で常に空）。
# tweet_id を主キーにして F2 冪等性（再実行で重複行を作らない）を DB 制約で保証。
# ラベル(country/genre)は news_labels（軸名を値として持つ縦持ちテーブル）に分離した。
# LABEL_AXES の個数・名称を変えてもスキーマ変更が要らないのがこの設計の狙い。
SCHEMA = """
CREATE TABLE IF NOT EXISTS news_log (
    tweet_id    TEXT PRIMARY KEY,  -- 重複排除キー（表示しない内部キー）
    created_at  TEXT,              -- 投稿日時 JST yyyy/MM/dd HH:mm
    keyword     TEXT,              -- 空(v0)
    level       TEXT,              -- 空（レベル判定は廃止）
    rank        TEXT,              -- 空
    title       TEXT,              -- 原文の冒頭
    summary     TEXT,              -- 原文全文（英語ツイート等）
    url         TEXT,              -- permalink
    source      TEXT,              -- "X @handle"
    translation TEXT               -- 日本語訳（未処理は NULL → enrich が埋める）
);
"""
LABELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_labels (
    tweet_id TEXT NOT NULL,        -- news_log.tweet_id
    axis     TEXT NOT NULL,        -- 軸名（LABEL_AXESの要素。個数・名称は自由）
    value    TEXT NOT NULL,        -- その軸でのLLM判定値
    -- ★valueも主キーに含める＝1軸に複数値を持てる（"イラン, 米国"→2行に分割するため）。
    --   旧スキーマ(tweet_id, axis)のDBは init_db が自動で作り替える。
    PRIMARY KEY (tweet_id, axis, value)
);
"""


def migrate_labels_pk(conn):
    """旧スキーマ（主キー = tweet_id, axis）のDBを、新スキーマ（+ value）へ作り替える。
    ALTER TABLE では主キーを変更できないため、新テーブルへコピーして差し替える。
    新スキーマなら何もしない＝毎起動で呼んでも安全（冪等）。"""
    info = conn.execute("PRAGMA table_info(news_labels)").fetchall()
    if not info:
        return False
    pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    if pk_cols == ["tweet_id", "axis", "value"]:
        return False
    conn.execute("DROP TABLE IF EXISTS news_labels_new")
    conn.execute(LABELS_SCHEMA.replace("news_labels", "news_labels_new"))
    # 旧データのうち value が空/NULL の行は移さない（新スキーマでは NOT NULL）
    conn.execute("INSERT OR IGNORE INTO news_labels_new (tweet_id, axis, value) "
                 "SELECT tweet_id, axis, value FROM news_labels "
                 "WHERE value IS NOT NULL AND value <> ''")
    conn.execute("DROP TABLE news_labels")
    conn.execute("ALTER TABLE news_labels_new RENAME TO news_labels")
    conn.commit()
    print("[*] news_labels を新スキーマ（1軸に複数値）へ移行しました")
    return True


def purge_old(conn, days=RETENTION_DAYS, now=None):
    """created_at（JST）が days 日より古い行を news_log / news_labels から削除する。
    state.json の last_seen_id は消さないので、削除済みツイートが再収集されることはない。
    created_at は 'yyyy/MM/dd HH:mm' の固定長なので文字列比較で日付順に一致する。"""
    now = now or datetime.now(JST)
    cutoff = (now - timedelta(days=days)).strftime("%Y/%m/%d %H:%M")
    n = conn.execute("SELECT COUNT(*) FROM news_log WHERE created_at < ?", (cutoff,)).fetchone()[0]
    if n:
        conn.execute("DELETE FROM news_labels WHERE tweet_id IN "
                     "(SELECT tweet_id FROM news_log WHERE created_at < ?)", (cutoff,))
        conn.execute("DELETE FROM news_log WHERE created_at < ?", (cutoff,))
    # 本体が無いのにラベルだけ残った行（過去の不整合）もここで掃除する
    orphan = conn.execute(
        "DELETE FROM news_labels WHERE tweet_id NOT IN (SELECT tweet_id FROM news_log)").rowcount
    conn.commit()
    return n, max(orphan, 0), cutoff


def init_db(conn):
    conn.execute(SCHEMA)
    conn.execute(LABELS_SCHEMA)
    migrate_labels_pk(conn)
    # 既存DB（旧スキーマ）向けの前方互換マイグレーション: 足りない列だけ足す
    have = {r[1] for r in conn.execute("PRAGMA table_info(news_log)")}
    for col in ("translation", "country", "genre"):
        if col not in have:
            conn.execute(f"ALTER TABLE news_log ADD COLUMN {col} TEXT")
    # 旧country/genre列に値が残っていれば news_labels へ一度だけ移行（データを失わない）。
    # INSERT OR IGNORE なので2回目以降は何もしない（毎起動時に呼んでも安全・冪等）。
    conn.execute(
        "INSERT OR IGNORE INTO news_labels (tweet_id, axis, value) "
        "SELECT tweet_id, '国・地域', country FROM news_log "
        "WHERE country IS NOT NULL AND country <> ''")
    conn.execute(
        "INSERT OR IGNORE INTO news_labels (tweet_id, axis, value) "
        "SELECT tweet_id, 'ジャンル', genre FROM news_log "
        "WHERE genre IS NOT NULL AND genre <> ''")
    conn.commit()
    return conn


def row_from_tweet(handle, t):
    dt = parse_time(t["created_at"]).astimezone(JST)
    head = t["text"].strip().splitlines()[0] if t["text"].strip() else ""
    return (t["id"], dt.strftime("%Y/%m/%d %H:%M"), "", "", "",
            head[:60], t["text"], t["url"], f"X @{handle}")


def append_rows(conn, handle, emit):
    """INSERT OR IGNORE で冪等追記。実際に挿入された件数を返す。"""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO news_log "
        "(tweet_id,created_at,keyword,level,rank,title,summary,url,source) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [row_from_tweet(handle, t) for t in emit])
    conn.commit()
    return conn.total_changes - before


# ---- 翻訳＋ラベリング（Groq / OpenAI互換をurllibで直叩き＝新規依存なし）----------
def escape_html(s):
    # ★ & を最初に置換（GAS踏襲の順序。逆だと二重エスケープで壊れる）
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


ENRICH_SYS = ("あなたは日本語専門のニュース編集者です。英語のツイートを日本語だけに訳し、"
              "ラベルを付けます。中国語（簡体字・繁体字）や英語を一切混ぜないでください。"
              "JSONのみを返し、前後に一切の文字を出力しないでください。")

# ★中国語混入対策: 実在の日本語文を出力例に入れる（記号"…"だけより言語の手がかりが強い）。
_JA_EXAMPLE_TRANSLATION = "米連邦準備制度理事会は政策金利を据え置いた"
# 中国語判定で再試行する際、プロンプト末尾に足す念押し文
CHINESE_RETRY_HINT = ("\n\n★重要: 前回の出力は日本語になっていませんでした"
                       "（中国語などが混入した可能性があります）。"
                       "今度こそ必ず日本語だけで訳し直してください。")


def enrich_prompt(text, retry_hint=""):
    axes_list = "、".join(LABEL_AXES)
    example = ", ".join(f'"{a}":"…"' for a in LABEL_AXES)
    return (f"次のツイートを処理してJSONで返してください。\n"
            f"- translation: 全文の自然な【日本語】訳（既に日本語ならそのまま）。"
            f"中国語には絶対にしないこと\n"
            f"- labels: 次の軸それぞれについて、短い値（日本語・目安10文字以内）を"
            f"付けたオブジェクト: {axes_list}\n"
            f"  - 該当が無い軸は必ず「なし」と書く。'-' '—' '…' '無' '？' などの記号や"
            f"省略表現は使わない\n"
            f"  - 値は必ず日本語。国名・企業名も日本語表記にする"
            f"（Venezuela→ベネズエラ、中国人民银行→中国人民銀行）\n"
            f"  - その軸に当てはまらない値を入れない"
            f"（例:「国・地域」に『為替』『金融』のような語を入れない）\n\n"
            f"ツイート:\n{text}\n\n"
            f'出力例（日本語の文体の見本。中身は無関係）: '
            f'{{"translation":"{_JA_EXAMPLE_TRANSLATION}","labels":{{{example}}}}}'
            f"{retry_hint}")


ENRICH_BATCH_SYS = ("あなたは日本語専門のニュース編集者です。複数の英語ツイートをまとめて"
                    "処理します。各ツイートを日本語だけに訳し、ラベルを付けます。"
                    "中国語（簡体字・繁体字）や英語を一切混ぜないでください。"
                    "JSONのみを返し、前後に一切の文字を出力しないでください。")


def enrich_batch_prompt(items):
    """複数ツイートをまとめて処理させるプロンプト（items=[(tweet_id, text), ...]）。
    tweet_idをキーにしたJSONオブジェクトで結果を返させる。1件ずつの enrich_prompt と
    ルールは同じだが、件数ぶん繰り返さないよう説明は1回だけ書く。"""
    axes_list = "、".join(LABEL_AXES)
    example = ", ".join(f'"{a}":"…"' for a in LABEL_AXES)
    body = json.dumps({str(tid): text for tid, text in items}, ensure_ascii=False, indent=None)
    return (f"次の{len(items)}件のツイートを1件ずつ処理してJSONで返してください。\n"
            f"- translation: 全文の自然な【日本語】訳（既に日本語ならそのまま）。"
            f"中国語には絶対にしないこと\n"
            f"- labels: 次の軸それぞれについて、短い値（日本語・目安10文字以内）を"
            f"付けたオブジェクト: {axes_list}\n"
            f"  - 該当が無い軸は必ず「なし」と書く。'-' '—' '…' '無' '？' などの記号や"
            f"省略表現は使わない\n"
            f"  - 値は必ず日本語。国名・企業名も日本語表記にする"
            f"（Venezuela→ベネズエラ、中国人民银行→中国人民銀行）\n"
            f"  - その軸に当てはまらない値を入れない"
            f"（例:「国・地域」に『為替』『金融』のような語を入れない）\n\n"
            f"ツイート一覧（キーはID。**全キーぶん必ず回答すること**）:\n{body}\n\n"
            f'出力形式（キーは入力と同じID文字列。中身は無関係な見本）: '
            f'{{"items":{{"<id>":{{"translation":"{_JA_EXAMPLE_TRANSLATION}",'
            f'"labels":{{{example}}}}}}}}}')


# 簡体字にしか存在せず日本語では使わない漢字（強い中国語シグナル。誤検知しにくい厳選セット）
_SIMPLIFIED_CHINESE_ONLY = set("们这个说没还来现实吗吧啊呢")
_HIRAGANA_KATAKANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_IDEOGRAPH_RE = re.compile(r"[\u4e00-\u9fff]")


def looks_like_chinese(text):
    """日本語訳のはずが中国語になっていないかの簡易判定（完璧な言語判定ではなく、
    実害＝目に見える誤訳の検知が目的）。
    ①簡体字専用漢字が1文字でも混じっていれば即アウト（強いシグナル）。
    ②CJK漢字はあるのにひらがな/カタカナが一切無い長い文は疑う
      （短い断片は「日本経済」のように地の文でもかな無しがあり得るため文字数で足切り）。"""
    if not text:
        return False
    if any(ch in _SIMPLIFIED_CHINESE_ONLY for ch in text):
        return True
    has_cjk = bool(_CJK_IDEOGRAPH_RE.search(text))
    has_kana = bool(_HIRAGANA_KATAKANA_RE.search(text))
    return has_cjk and not has_kana and len(text) >= 8


class RateLimiter:
    """直近60秒のリクエスト数とトークン数を見て、上限を超えそうなら待つ。
    無料枠は「30リクエスト/分」かつ「6,000トークン/分」で、**先に当たるのはトークン**。
    送る前に見積りで待ち、送った後に実測値へ置き換える（見積りが外れても追従する）。
    clock/sleeper を差し替えられるので、実時間を待たずに --selftest で検証できる。"""

    def __init__(self, rpm=LLM_RPM, tpm=LLM_TPM, clock=time.monotonic, sleeper=time.sleep):
        self.rpm, self.tpm = rpm, tpm
        self.clock, self.sleeper = clock, sleeper
        self.events = []                    # [(時刻, トークン数), ...] 直近60秒ぶん

    def _prune(self, now):
        self.events = [e for e in self.events if now - e[0] < 60]

    def acquire(self, est_tokens):
        """上限に収まるまで待ってから戻る。待った秒数を返す（テスト用）。"""
        waited = 0.0
        while True:
            now = self.clock()
            self._prune(now)
            used_req = len(self.events)
            used_tok = sum(e[1] for e in self.events)
            if used_req < self.rpm and used_tok + est_tokens <= self.tpm:
                self.events.append([now, est_tokens])
                return waited
            # 一番古いイベントが60秒の窓から出るまで待てば、必ず空きができる
            wait = max(0.05, 60 - (now - self.events[0][0]) + 0.05)
            self.sleeper(wait)
            waited += wait

    def settle(self, actual_tokens):
        """直前のリクエストの実トークン数で見積りを置き換える。"""
        if self.events:
            self.events[-1][1] = actual_tokens


_LIMITER = RateLimiter()


def is_reasoning_model(name):
    """推論モデルかどうか（出力予算の上乗せと reasoning_effort の要否を決める）。
    gpt-oss / qwen3系が該当。判定を外しても動くが、予算が足りずJSON検証に落ちやすくなる。"""
    n = (name or "").lower()
    return any(k in n for k in ("gpt-oss", "qwen3", "reason", "thinking"))


_OUTPUT_EMA = [400.0]      # 出力トークンの実測平均（レート制限の見積りに使う）


def _observed_output():
    return int(_OUTPUT_EMA[0] * 1.2) + 50      # 少し余裕を持たせる


def _record_output(completion_tokens):
    if completion_tokens:
        _OUTPUT_EMA[0] = _OUTPUT_EMA[0] * 0.7 + float(completion_tokens) * 0.3


def parse_retry_after(headers, body):
    """429のときに何秒待つべきかを決める。retry-afterヘッダを最優先し、
    無ければ本文の 'try again in 6m 11.52s' を読む。読めなければNone。"""
    ra = (headers or {}).get("retry-after") if hasattr(headers, "get") else None
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    m = re.search(r"try again in\s+(?:(\d+)m\s*)?([\d.]+)s", body or "")
    if m:
        return float(m.group(1) or 0) * 60 + float(m.group(2))
    return None


def list_models(opener=None):
    """GET /models で、このAPIキーで実際に使えるモデルIDを取得する。
    ドキュメントを読むより確実（提供モデルはティアによって違う）。"""
    if not LLM_API_KEY:
        raise RuntimeError("Groq APIキーが未設定です（config.ini の [llm] api_key）")
    opener = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
    req = urllib.request.Request(LLM_BASE + "/models", headers={
        "Accept": "application/json",
        "User-Agent": LLM_USER_AGENT,      # ★ここもUA必須（Cloudflare 1010対策）
        "Authorization": f"Bearer {LLM_API_KEY}"})
    try:
        with opener(req) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {body[:200]}")
    return sorted(m.get("id", "") for m in (data.get("data") or []) if m.get("id"))


def llm_chat(messages, max_tokens=300, limiter=None, opener=None, sleeper=time.sleep,
             est_items=1):
    """Groq の OpenAI互換 /chat/completions を叩く（依存を増やさずurllibで直叩き）。
    max_tokens は用途で変える（翻訳=300 / ラベル正規化=長いJSONを返すのでもっと大きく）。
    est_items はこの1回の呼び出しで処理する件数（バッチ処理時）。出力トークンの
    事前見積り（_observed_output()は1件あたりの実測平均）をこれに応じて倍にし、
    レート制限の見積りがバッチ時に過小評価されないようにする。

    無料枠向けに以下を行う:
      - 送信前にレート制限内へ収まるまで待つ（RateLimiter）
      - 429/5xx は retry-after に従って待ってから再試行（上限 LLM_MAX_RETRY 回）
    opener/sleeper を差し替えられるので、通信せずに --selftest で検証できる。"""
    if not LLM_API_KEY:
        raise RuntimeError("Groq APIキーが未設定です（config.ini の [llm] api_key）。"
                           "console.groq.com/keys で発行してください")
    limiter = limiter or _LIMITER
    opener = opener or (lambda req: urllib.request.urlopen(req, timeout=120))
    reasoning = is_reasoning_model(LLM_MODEL)
    # 推論モデルは「推論＋答え」で予算を共有するので、答えぶんに上乗せして渡す
    budget = max_tokens + (LLM_REASONING_HEADROOM if reasoning else 0)

    def make_body(json_mode=True, effort=True):
        payload = {"model": LLM_MODEL, "messages": messages, "temperature": 0.2,
                   "stream": False,
                   # max_tokens は非推奨。OpenAI互換の現行名はこちら
                   "max_completion_tokens": budget}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if effort and reasoning:
            payload["reasoning_effort"] = LLM_REASONING_EFFORT
        return json.dumps(payload).encode()

    json_mode, effort = True, True
    body = make_body(json_mode, effort)
    # 入力は4文字≒1トークンで概算。出力は「実測の平均」を使う（予算全部を使うわけではない
    # ため、budgetで見積もるとレート制限が過剰に効いて極端に遅くなる）。
    in_tokens = int(sum(len(m.get("content", "")) for m in messages) / 4)
    est = in_tokens + min(_observed_output() * max(1, est_items), budget)
    last_err = None
    for attempt in range(LLM_MAX_RETRY + 1):
        limiter.acquire(est)
        req = urllib.request.Request(LLM_BASE + "/chat/completions", data=body,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json",
                                              "User-Agent": LLM_USER_AGENT,   # ★必須（上のコメント）
                                              "Authorization": f"Bearer {LLM_API_KEY}"})
        try:
            with opener(req) as r:
                data = json.loads(r.read())
            usage = data.get("usage") or {}
            limiter.settle(int(usage.get("total_tokens") or est))
            _record_output(usage.get("completion_tokens"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last_err = RuntimeError(f"HTTP {e.code}: {err_body[:200]}")
            if e.code == 429:
                wait = parse_retry_after(getattr(e, "headers", None), err_body)
                wait = min(wait if wait is not None else 20.0, LLM_MAX_WAIT)
                if attempt >= LLM_MAX_RETRY:
                    break
                print(f"    [WAIT] レート上限。{wait:.0f}秒待って再試行します"
                      f"（{attempt + 1}/{LLM_MAX_RETRY}）")
                limiter.settle(est)          # 失敗ぶんも枠を消費した扱いにして詰め込みを防ぐ
                sleeper(wait)
                continue
            if e.code == 403 and "1010" in err_body:
                raise RuntimeError(
                    "HTTP 403 (Cloudflare error 1010): User-Agentが拒否されました。"
                    "LLM_USER_AGENT が送信されているか確認してください"
                    "（APIキーの問題ではありません）")
            if e.code == 401:
                raise RuntimeError(f"HTTP 401: APIキーが不正です（config.ini の [llm] api_key）"
                                   f" / {err_body[:120]}")
            if e.code == 404:
                raise RuntimeError(
                    f"HTTP 404: モデル {LLM_MODEL} が存在しないか、このアカウントでは使えません。"
                    f"`--list-models` で実際に使えるモデルを確認し、config.ini の "
                    f"[llm] model を直してください / {err_body[:120]}")
            if e.code == 400 and "json_validate_failed" in err_body and json_mode:
                # 推論に予算を食われてJSONが生成されなかった。JSONモードを外して再試行する
                #（こちら側で緩くパースするので、素のテキストでも取り出せる）
                json_mode = False
                body = make_body(json_mode, effort)
                print("    [INFO] JSONモードを外して再試行します"
                      "（推論モデルの出力がサーバ側検証に通らなかった）")
                continue
            if e.code == 400 and effort and "reasoning_effort" in err_body:
                effort = False               # このモデルは reasoning_effort を受け付けない
                body = make_body(json_mode, effort)
                continue
            if 500 <= e.code < 600 and attempt < LLM_MAX_RETRY:
                sleeper(min(2 ** attempt, 30))
                continue
            raise last_err
        except urllib.error.URLError as e:
            last_err = RuntimeError(f"接続エラー: {e.reason}")
            if attempt >= LLM_MAX_RETRY:
                break
            sleeper(min(2 ** attempt, 30))
    raise last_err


def parse_enrich(raw, orig):
    """LLM出力を頑健に解釈。壊れても原文フォールバックで欠測にしない（信頼境界検証）。
    labelsはLABEL_AXES全軸ぶんを必ず埋める（LLMが答えなかった/JSON崩れの軸は"不明"）。"""
    s = raw.strip()
    if "{" in s and "}" in s:            # コードフェンス等の混入に耐える
        s = s[s.find("{"):s.rfind("}") + 1]
    try:
        o = json.loads(s)
    except Exception:
        o = {}
    labels_in = o.get("labels") if isinstance(o.get("labels"), dict) else {}
    labels = {axis: str(labels_in.get(axis) or "不明").strip() for axis in LABEL_AXES}
    return {"translation": (o.get("translation") or orig).strip(), "labels": labels}


def parse_enrich_batch(raw, items):
    """バッチ応答(items=[(tweet_id, text), ...])を解釈し、tweet_id(文字列)→結果 の
    dictを返す。回答漏れ・JSON崩れ・翻訳欠落の項目は結果に含めない
    （呼び出し元が個別に enrich_one へフォールバックする＝欠測のまま握りつぶさない）。"""
    s = raw.strip()
    if "{" in s and "}" in s:
        s = s[s.find("{"):s.rfind("}") + 1]
    try:
        o = json.loads(s)
    except Exception:
        o = {}
    items_in = o.get("items") if isinstance(o.get("items"), dict) else {}
    out = {}
    for tid, _text in items:
        got = items_in.get(str(tid))
        if not isinstance(got, dict):
            continue                     # 回答漏れ → フォールバック対象
        translation = got.get("translation")
        if not isinstance(translation, str) or not translation.strip():
            continue                     # 翻訳が空/型違い → フォールバック対象
        labels_in = got.get("labels") if isinstance(got.get("labels"), dict) else {}
        labels = {axis: str(labels_in.get(axis) or "不明").strip() for axis in LABEL_AXES}
        out[str(tid)] = {"translation": translation.strip(), "labels": labels}
    return out


MAX_LANG_RETRIES = 2   # 中国語混入時、初回にプラスして最大この回数だけその場で訳し直す


def enrich_one(chat_fn, tid, text):
    """1件ぶんの翻訳＋全軸のラベル付与（中国語再試行つき）。
    バッチ処理からのフォールバック先として使う共通処理（元の1件ずつ実装そのもの）。
    戻り値: (結果dict, 実施したリトライ回数)。直らなければ例外を投げる。"""
    o, hint, retried = None, "", 0
    for attempt in range(MAX_LANG_RETRIES + 1):
        retried = attempt   # attempt=0は初回（リトライではない）。0,1,2=実施済みリトライ回数
        raw = chat_fn([{"role": "system", "content": ENRICH_SYS},
                       {"role": "user", "content": enrich_prompt(text, hint)}])
        o = parse_enrich(raw, text)
        if not looks_like_chinese(o["translation"]):
            return o, retried
        o, hint = None, CHINESE_RETRY_HINT
    raise ValueError(f"訳文が日本語になりませんでした（中国語混入・{retried}回リトライしても失敗）")


def enrich(conn, chat_fn=llm_chat):
    """translation が未設定の行だけ翻訳＋全軸のラベル付与。失敗行は残して次回再試行。

    ENRICH_BATCH_SIZE件ずつまとめて1回のAPI呼び出しで処理する（RPM上限に真っ先に
    当たって待ち時間が支配的になるのを避けるため）。バッチ呼び出し自体が失敗したら
    半分に分割して再試行し、1件まで割ってもだめならenrich_one()（中国語再試行つき）に
    フォールバックする。バッチ呼び出しが成功しても、回答漏れ・中国語混入の項目だけは
    その場で個別に立て直す（他の項目の成功を巻き込んで捨てない）。"""
    rows = conn.execute(
        "SELECT tweet_id, summary FROM news_log "
        "WHERE translation IS NULL OR translation = ''").fetchall()
    total = len(rows)
    if total:
        print(f"[*] 翻訳/ラベリング開始: {total} 件"
              f"（Groq・{ENRICH_BATCH_SIZE}件ずつバッチ処理、失敗時は自動分割）")
    counts = {"done": 0, "failed": 0}

    def save(tid, o, note=""):
        conn.execute("UPDATE news_log SET translation=? WHERE tweet_id=?",
                     (o["translation"], tid))
        conn.executemany(
            "INSERT OR REPLACE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
            [(tid, axis, val) for axis, val in o["labels"].items()])
        conn.commit()                # ★1件ごとに確定＝途中で中断しても既訳ぶんは残る
        counts["done"] += 1
        summary = " ".join(f"{a}={v}" for a, v in o["labels"].items())
        print(f"    {counts['done'] + counts['failed']}/{total} ✓ [{summary}]{note}")

    def fail(tid, err):
        counts["failed"] += 1
        print(f"    {counts['done'] + counts['failed']}/{total} 失敗 {tid}: {err}")

    def run_one(tid, text):
        try:
            o, retried = enrich_one(chat_fn, tid, text)
        except Exception as e:
            fail(tid, e)
            return
        note = f" (中国語再試行{retried}回)" if retried else ""
        save(tid, o, note)

    def run_batch(batch):
        if not batch:
            return
        if len(batch) == 1:
            run_one(*batch[0])
            return
        try:
            # 出力上限はバッチ件数から見積もる（固定値だと件数が多いとき途中で切れる）
            budget = min(4000, 220 * len(batch) + 200)
            raw = chat_fn([{"role": "system", "content": ENRICH_BATCH_SYS},
                           {"role": "user", "content": enrich_batch_prompt(batch)}],
                          max_tokens=budget, est_items=len(batch))
        except Exception as e:
            print(f"    [WARN] {len(batch)}件のバッチ処理に失敗。分割して再試行します: {e}")
            half = len(batch) // 2
            run_batch(batch[:half])
            run_batch(batch[half:])
            return
        parsed = parse_enrich_batch(raw, batch)
        for tid, text in batch:
            o = parsed.get(str(tid))
            if o is None or looks_like_chinese(o["translation"]):
                run_one(tid, text)       # 回答漏れ/中国語混入ぶんだけ個別に立て直す
            else:
                save(tid, o)

    for i in range(0, total, ENRICH_BATCH_SIZE):
        run_batch(rows[i:i + ENRICH_BATCH_SIZE])

    return counts["done"], counts["failed"]


# ---- ラベル正規化（表記ゆれの統一）------------------------------------------
NORMALIZE_SYS = ("あなたは日本語のラベル整理担当です。必ずJSONのみを出力します。"
                 "説明文・コードフェンスは出力しません。")


def normalize_prompt(axis, values, known):
    """1軸ぶんの表記ゆれ統一をLLMに依頼するプロンプト。
    known（先行チャンクで確定済みの代表表記）を渡して、チャンクを跨いでも表記が割れないようにする。"""
    known_note = ("\n既に使用中の代表表記（同じ意味ならこの表記に合わせること）: "
                  + "、".join(known) + "\n") if known else "\n"
    return (f'「{axis}」という軸のラベル値が表記ゆれを起こしています。'
            f'各値を代表表記へ統一してください。\n'
            f'ルール:\n'
            f'- 同じ意味の値は必ず同じ代表表記にする（例: アメリカ→米国、Fed→FRB）\n'
            f'- 日本語で一般的な表記を使う（英語・中国語の値も日本語にする）\n'
            f'- 1つの値に複数の対象が含まれる場合は分割して配列の複数要素にする'
            f'（例: "イラン, 米国" → ["イラン","米国"]、"米中" → ["米国","中国"]）\n'
            f'- 固有名詞の中の「・」は区切りではないので分割しない'
            f'（例: "ゴールドマン・サックス" は1要素のまま）\n'
            f'- 該当が無いことを表す値（なし・無・×・N/A・不明など）はすべて ["なし"] にする\n'
            f'- 「{axis}」の値として明らかに不適切なものは ["不明"] にする\n'
            f'- 値は{NORMALIZE_MAX_LEN}文字以内。分割は最大{NORMALIZE_MAX_SPLIT}要素まで\n'
            f'{known_note}'
            f'対象の値: {json.dumps(values, ensure_ascii=False)}\n\n'
            f'出力形式（入力の値すべてをキーにすること）: '
            f'{{"mapping":{{"元の値":["代表表記"],"元の値2":["代表表記A","代表表記B"]}}}}')


def parse_normalize(raw, values):
    """LLMの返したマッピングを検証して採用する（信頼境界の検証）。
    壊れた値・知らないキー・長すぎる値は捨て、その値は「元のまま」にフォールバックする
    ＝正規化に失敗してもラベルが消えたり化けたりしない。"""
    s = raw.strip()
    if "{" in s and "}" in s:
        s = s[s.find("{"):s.rfind("}") + 1]
    try:
        o = json.loads(s)
    except Exception:
        o = {}
    m = o.get("mapping") if isinstance(o.get("mapping"), dict) else {}
    out = {}
    for v in values:
        got = m.get(v)
        if isinstance(got, str):
            got = [got]
        if not isinstance(got, list):
            out[v] = [v]                      # 回答漏れ → 元のまま
            continue
        clean = []
        for x in got[:NORMALIZE_MAX_SPLIT]:
            if not isinstance(x, (str, int, float)):
                continue
            x = str(x).strip()
            if x and len(x) <= NORMALIZE_MAX_LEN and x not in clean:
                clean.append(x)
        out[v] = clean or [v]                 # 全部弾かれた → 元のまま
    return out


def normalize_labels(conn, chat_fn=llm_chat, verbose=True):
    """DB内の全ラベルを軸ごとに見て、表記ゆれを代表表記へ統一する（メール送信の直前に実行）。
    新規ぶんだけでなく既存ぶんも対象にするので、DB全体の表記が揃う。
    軸ごとに独立したトランザクションで差し替えるため、途中で失敗しても他の軸は無傷。
    戻り値: (変更した(tweet_id,axis,value)行数, 統一で減った値の種類数, 失敗した軸数)"""
    # ★LABEL_AXES に載っている軸だけ。外した軸にLLMを使わない（無駄打ち防止）。
    axes = [a for a in LABEL_AXES if conn.execute(
        "SELECT 1 FROM news_labels WHERE axis=? LIMIT 1", (a,)).fetchone()]
    changed_rows, shrunk, failed = 0, 0, 0
    for axis in axes:
        values = [r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM news_labels WHERE axis=? ORDER BY value", (axis,))]
        # 値が1種類でもLLMに通す。「イラン, 米国」のように単独でも分割が要る値があるため
        # （その軸で唯一の値だと素通りしてしまう穴があった）。
        if not values:
            continue
        mapping, known = {}, []

        def ask(chunk, depth=0):
            """1チャンクぶん問い合わせる。失敗したらチャンクを半分に割って再試行する
            （コンテキスト超過などで500が返ることがあるため。Ollama時代に実機で発生）。"""
            try:
                # 出力上限は値の数から見積もる（固定2000だとコンテキストを圧迫する）
                budget = min(1200, 80 * len(chunk) + 200)
                raw = chat_fn([{"role": "system", "content": NORMALIZE_SYS},
                               {"role": "user", "content": normalize_prompt(axis, chunk, known)}],
                              max_tokens=budget)
            except Exception as e:
                if len(chunk) < 2 or depth >= 3:
                    raise
                print(f"    [WARN] 「{axis}」{len(chunk)}件の問い合わせに失敗。"
                      f"分割して再試行します: {e}")
                half = len(chunk) // 2
                ask(chunk[:half], depth + 1)
                ask(chunk[half:], depth + 1)
                return
            part = parse_normalize(raw, chunk)
            mapping.update(part)
            for canon in part.values():        # 次チャンクへ代表表記を引き継ぐ
                for c in canon:
                    if c not in known:
                        known.append(c)

        try:
            for i in range(0, len(values), NORMALIZE_CHUNK):
                ask(values[i:i + NORMALIZE_CHUNK])
        except Exception as e:
            failed += 1
            print(f"    [WARN] 「{axis}」の正規化に失敗（元の値のまま）: {e}")
            continue
        if all(v == [k] for k, v in mapping.items()):
            if verbose:
                print(f"    {axis}: 変更なし（{len(values)}種類）")
            continue
        # 差し替え: 軸ごとに一括で入れ替える（古い値が残らないよう一度消してから入れ直す）
        old = conn.execute(
            "SELECT tweet_id, value FROM news_labels WHERE axis=?", (axis,)).fetchall()
        new_rows = {(tid, axis, c) for tid, val in old for c in mapping.get(val, [val])}
        try:
            # ★明示的な BEGIN は使わない。Pythonのsqlite3はDML前に自動でトランザクションを
            #   開始するため、既に開いていると "cannot start a transaction within a
            #   transaction" になる（実際にこれで軸ごと巻き戻る不具合を踏んだ）。
            #   代わりに、呼び出し元の未コミット分を巻き込まないよう先にcommitしておく。
            conn.commit()
            conn.execute("DELETE FROM news_labels WHERE axis=?", (axis,))
            conn.executemany(
                "INSERT OR IGNORE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
                sorted(new_rows))
            conn.commit()
        except Exception as e:
            conn.rollback()
            failed += 1
            print(f"    [WARN] 「{axis}」の書き換えに失敗（ロールバック済み）: {e}")
            continue
        after = len({c for cs in mapping.values() for c in cs})
        changed_rows += sum(1 for tid, val in old if mapping.get(val, [val]) != [val])
        shrunk += len(values) - after
        if verbose:
            samples = [f"{k}→{'/'.join(v)}" for k, v in mapping.items() if v != [k]][:4]
            print(f"    {axis}: {len(values)}種類 → {after}種類　例: " + "、".join(samples))
    return changed_rows, shrunk, failed


# ---- HTMLメール生成＆送信（EMAIL_GROUP_BY軸でセクション分け・各セクション内はタイムライン）----
BADGE_PALETTE = ["#0f3460", "#276439", "#842029", "#7a4a00", "#4a235a",
                  "#155e63", "#6b4d1f", "#33526e", "#7c2d92", "#1d6f5e"]


def axis_color(text):
    """文字列から決定的に色を割り当てる（軸名にもラベル値にも使う。ハードコードのマップ不要）。"""
    return BADGE_PALETTE[sum(ord(c) for c in text) % len(BADGE_PALETTE)]


def build_card_compact(row, labels, exclude_axis):
    """タイムライン1件ぶんのコンパクトカード（labels は {軸: [値,...]}）。
    セクション見出しで既に分かる exclude_axis の
    バッジは重複するので省く（原文全文も省き「時刻・訳文・他の軸バッジ」だけに絞る）。"""
    d = dict(row)
    tr = escape_html(d.get("translation") or "(訳なし)")
    url = escape_html(d.get("url") or "")
    src = escape_html(d.get("source") or "")
    date = d.get("created_at") or ""
    time_only = escape_html(date.split(" ")[-1] if " " in date else date)
    title = (f'<a href="{url}" style="color:#1a1a2e;text-decoration:none;font-size:13px;'
             f'font-weight:600;line-height:1.4;">{tr}</a>') if url else tr
    # labels は {軸: [値, ...]}（1軸に複数値あり）。値ごとに1バッジ出す。
    badges = "".join(
        f'<span style="background:{axis_color(v)};color:#fff;font-size:10px;'
        f'padding:1px 6px;border-radius:3px;margin:0 3px 3px 0;display:inline-block;">'
        f'{escape_html(v)}</span>'
        for a, vs in labels.items() if a != exclude_axis
        for v in vs if v)
    return (
        f'<span style="font-size:11px;color:#9aa5b1;">{time_only} ・ {src}</span><br>'
        f'{title}'
        f'<div style="margin-top:4px;">{badges}</div>')


def build_timeline_row(card_html, color):
    """タイムライン1行＝左に縦線+ドット、右にコンパクトカード（案C: 最低限のタイムライン感）。
    ★同一tableの<tr>を跨いでborder-leftが隣接する仕組みで縦線に見せている
    （cellspacing=0が前提。歯抜けにならないようtableを分割しないこと）。"""
    return (
        '<tr>'
        f'<td width="18" valign="top" style="width:18px;border-left:2px solid {color};'
        f'padding:14px 0 0 0;">'
        '<table cellspacing="0" cellpadding="0"><tr>'
        f'<td width="9" height="9" style="width:9px;height:9px;line-height:9px;'
        f'font-size:0;background:{color};border-radius:50%;">&nbsp;</td>'
        '</tr></table></td>'
        f'<td style="padding:10px 0 10px 12px;">{card_html}</td>'
        '</tr>')


def build_section(group_value, item_rows, labels_by_tweet, exclude_axis):
    color = axis_color(group_value)
    header = (
        '<tr><td colspan="2" style="padding:18px 2px 8px 2px;">'
        f'<span style="background:{color};color:#fff;font-size:13px;font-weight:700;'
        f'padding:4px 12px;border-radius:14px;">{escape_html(group_value)}'
        f'（{len(item_rows)}件）</span></td></tr>')
    body = "".join(
        build_timeline_row(
            build_card_compact(r, labels_by_tweet.get(r["tweet_id"], {}), exclude_axis), color)
        for r in item_rows)
    return (f'<table width="100%" cellspacing="0" cellpadding="0" '
            f'style="table-layout:fixed;">{header}{body}</table>')


def fetch_labels(conn, tweet_ids):
    """{tweet_id: {軸: [値, ...]}} を作る。1軸に複数値があり得るのでリストで持つ。
    ★LABEL_AXES から外した軸の行は無視する（軸を減らしてもメールに出続けないように）。
    DBには残るが、保持期間(RETENTION_DAYS)を過ぎれば本体ごと消える。
    今すぐ消したい場合は `--prune-axes` を実行する。"""
    if not tweet_ids:
        return {}
    qs = ",".join("?" * len(tweet_ids))
    order = {a: i for i, a in enumerate(LABEL_AXES)}
    out = {}
    for tid, axis, val in conn.execute(
            f"SELECT tweet_id, axis, value FROM news_labels WHERE tweet_id IN ({qs}) "
            f"ORDER BY axis, value", tweet_ids):
        if axis in order:
            out.setdefault(tid, {}).setdefault(axis, []).append(val)
    return {tid: dict(sorted(d.items(), key=lambda kv: order[kv[0]])) for tid, d in out.items()}


def prune_axes(conn):
    """LABEL_AXES から外した軸のラベル行を削除する（`--prune-axes` 用の手動掃除）。
    通常は呼ばない。軸を減らした直後にDBもすぐ揃えたいとき用。削除件数を返す。"""
    qs = ",".join("?" * len(LABEL_AXES))
    n = conn.execute(f"SELECT COUNT(*) FROM news_labels WHERE axis NOT IN ({qs})",
                     LABEL_AXES).fetchone()[0]
    conn.execute(f"DELETE FROM news_labels WHERE axis NOT IN ({qs})", LABEL_AXES)
    conn.commit()
    return n


def build_html(rows, labels_by_tweet, today):
    """rows は created_at DESC 済み。EMAIL_GROUP_BY の値でセクション分けしつつ、
    セクション内・セクション自体の並びとも元の時系列順を保つ
    （＝そのグループの最新記事が来た順にセクションが並ぶ）。"""
    # ★1件が EMAIL_GROUP_BY 軸に複数値を持つ場合（例: 国・地域が["イラン","米国"]）は
    #   該当する全セクションに重複して掲載する（どちらの国から見ても漏れないようにする）。
    groups, order = {}, []
    for r in rows:
        vals = labels_by_tweet.get(r["tweet_id"], {}).get(EMAIL_GROUP_BY) or ["不明"]
        for val in vals:
            if val not in groups:
                groups[val] = []
                order.append(val)
            groups[val].append(r)
    sections = "".join(
        build_section(v, groups[v], labels_by_tweet, EMAIL_GROUP_BY) for v in order)
    return (
        '<html><head><meta charset="UTF-8"></head>'
        '<body style="margin:0;padding:0;background:#f0f4f8;'
        'font-family:Arial,sans-serif;">'
        '<table width="100%" cellspacing="0" cellpadding="0"><tr>'
        '<td align="center" style="padding:24px 12px;">'
        '<table width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;">'
        '<tr><td bgcolor="#16213e" style="border-radius:10px 10px 0 0;padding:20px 24px;">'
        '<div style="color:#fff;font-size:20px;font-weight:700;">Xニュース</div>'
        f'<div style="color:#7fb3d3;font-size:13px;">{escape_html(today)}　'
        f'<strong style="color:#fff;">{len(rows)}件</strong></div></td></tr>'
        f'<tr><td style="background:#f0f4f8;padding:4px 16px 16px 16px;">{sections}</td></tr>'
        '</table></td></tr></table></body></html>')


def send_mail(subject, html):
    if not (MAIL_FROM and MAIL_APP_PASSWORD and MAIL_TO):
        raise RuntimeError("メール設定が未設定（config.ini の [mail] セクション: "
                           "gmail_user / gmail_app_password / mail_to を設定）")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, MAIL_TO
    msg.set_content("HTMLメールです。対応クライアントで表示してください。")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(MAIL_FROM, MAIL_APP_PASSWORD)
        smtp.send_message(msg)


def build_viewer_items(db_path=STORE_PATH):
    """news_log(基本情報)とnews_labels(軸ごとの縦持ちラベル)を読み、
    tweet_idごとに labels:{axis:[値,...]} へ組み立てたリストを返す
    （db_viewer.html側のJSと同じ形。1軸に複数値があり得るため配列で持つ）。"""
    with sqlite3.connect(db_path) as c:
        main = c.execute(
            "SELECT tweet_id, created_at, source, translation, summary, url "
            "FROM news_log ORDER BY created_at DESC").fetchall()
        label_rows = c.execute(
            "SELECT tweet_id, axis, value FROM news_labels").fetchall()
    labels_by_id = {}
    for tid, axis, value in label_rows:
        if value in (None, ""):
            continue
        labels_by_id.setdefault(tid, {}).setdefault(axis, []).append(value)
    return [{
        "tweet_id": tid, "created_at": created_at, "source": source,
        "translation": translation, "summary": summary, "url": url,
        "labels": labels_by_id.get(tid, {}),
    } for tid, created_at, source, translation, summary, url in main]


def write_viewer_data(db_path=STORE_PATH, out_path=VIEWER_DATA_PATH):
    """DBの中身をJSONにして data/news_data.js に書き出す。db_viewer.html はサーバ経由の
    fetchではなく、この<script>ファイルだけを読んで表示する（サーバ廃止・file://直開き化）。
    file:// では fetch によるローカルファイル読み込みがブロックされるが、
    <script src> の読み込みは通るため、以前と同じ手法（中身だけJSONに変更）で回避する。"""
    try:
        items = build_viewer_items(db_path)
        tmp = out_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("// x_collector.py が自動生成（手で編集しない）。\n"
                    "// db_viewer.html はこのファイルだけを読む（サーバ不要・file://で直接開ける）。\n"
                    f"window.__NEWS_ITEMS = {json.dumps(items, ensure_ascii=False)};\n"
                    f'window.__NEWS_DATA_AT = "{datetime.now(JST).strftime("%Y/%m/%d %H:%M")}";\n')
        os.replace(tmp, out_path)          # アトミック置換（読み込み中の半端なファイルを作らない）
        newest = items[0]["created_at"] if items else "なし"
        print(f"[*] news_data.js を更新: {len(items)} 件（最新 {newest}） "
              f"{os.path.getsize(out_path) // 1024}KB")
        return True
    except Exception as e:
        print(f"[WARN] news_data.js の書き出しに失敗: {e}")
        return False


def open_db_viewer(viewer_path=None, opener=None):
    """db_viewer.html を file:// で直接開く（サーバは使わない）。
    write_viewer_data() を先に呼んでnews_data.jsを最新化してから使うこと。
    opener を差し替えられるので、実際にブラウザを起動せずに --selftest で検証できる。
    ★GitHub Actions等のCI環境にはブラウザが無いため、GITHUB_ACTIONS環境変数を見て
    自動でスキップする（失敗時の[WARN]ログが並ぶのを避け、意図的なスキップだと分かるようにする）。
    ビューア自体はCloudflare Pages等から見るので、CI側でこれを開けなくても運用上問題ない。"""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("[*] db_viewer: CI環境のためブラウザでの自動オープンをスキップ"
              "（Cloudflare Pages等の公開URLから閲覧する運用のため）")
        return
    viewer_path = viewer_path or os.path.join(BASE_DIR, "web", "db_viewer.html")
    opener = opener or webbrowser.open
    if not os.path.exists(viewer_path):
        print(f"[WARN] db_viewer.html が見つかりません: {viewer_path}")
        return
    url = "file://" + os.path.abspath(viewer_path).replace(os.sep, "/")
    try:
        opener(url)
        print(f"[*] db_viewer を開きました: {url}")
    except Exception as e:
        print(f"[WARN] ブラウザを開けませんでした: {e} / 手動で開いてください: {url}")


# ---- 1アカウント収集 --------------------------------------------------------
def paginate_tweets(fetch_page, cutoff, max_pages=MAX_PAGES, verbose=False):
    """fetch_page(cursor) -> (page_raw, next_cursor) を呼び、窓(cutoff)を覆うまで
    ページを重ねて集める（重複IDは除去）。戻り値は (重複除去済みraw, ページ数, 打ち切り理由)。
    ネットワークから切り離した純粋ロジックなので --selftest で検証できる。

    ★打ち切りは「そのページに窓内の投稿が1件も無い」= 窓を追い越した時点。
      以前は「ページ内の最古が窓より古ければ終了」としていたが、**ピン留めツイートは
      何年も前の日付でページ先頭に混ざる**ため、1ページ目で必ず打ち切られていた
      （実機で raw 21 / pages 1 / 8時間のうち3時間ぶんしか取れない事象の原因）。
    ★安全弁: cursorが尽きない/進まない場合でも max_pages で必ず打ち切る。"""
    raw, cursor, pages, seen = [], None, 0, set()
    reason = "unknown"
    while True:
        page_raw, next_cursor = fetch_page(cursor)
        fresh = [t for t in page_raw if t["id"] not in seen]
        for t in fresh:
            seen.add(t["id"])
        raw += fresh
        pages += 1
        in_window = [t for t in page_raw if parse_time(t["created_at"]) >= cutoff]
        if verbose:
            times = [parse_time(t["created_at"]).astimezone(JST) for t in page_raw]
            span = (f"{min(times).strftime('%m/%d %H:%M')}〜{max(times).strftime('%m/%d %H:%M')}"
                    if times else "-")
            print(f"     page{pages}: {len(page_raw)}件（新規{len(fresh)} / 窓内{len(in_window)}）"
                  f" {span} cursor={'有' if next_cursor else '無'}")
        if not page_raw:
            reason = "空ページ"
        elif not in_window:
            reason = "窓を覆った"          # 窓より古い投稿だけのページに到達＝取りこぼし無し
        elif not fresh:
            reason = "cursorが進まない"    # 同じページが返り続ける（無限ループ防止）
        elif not next_cursor:
            reason = "cursorが無い"        # ★窓を覆う前に打ち切り＝取りこぼしの可能性
        elif pages >= max_pages:
            reason = f"MAX_PAGES({max_pages})到達"   # ★同上
        else:
            cursor = next_cursor
            continue
        break
    return raw, pages, reason


def collect_one(s, headers, handle, prev, start, end):
    """[start, end] の時間帯を覆うまでページを辿って集める。
    ページは新しい順に返るので、endより新しい投稿も raw には入る（範囲外として捨てる）。"""
    uid = resolve_user_id(s, headers, handle)

    def fetch_page(cursor):
        variables = {"userId": uid, "count": COUNT, "includePromotedContent": False,
                     "withQuickPromoteEligibilityTweetFields": False,
                     "withVoice": False, "withV2Timeline": True}
        if cursor:
            variables["cursor"] = cursor   # ★未検証: extract_cursor同様、実機での確認推奨
        data = graphql_get(s, headers, "UserTweets", variables, FEATURES_TWEETS)
        return extract_all(data), extract_cursor(data)

    # 打ち切り判定の基準は範囲の下端。startより古い投稿だけのページに達したら終了。
    dedup, pages, reason = paginate_tweets(fetch_page, start, verbose=True)
    emit, new_high, stats = filter_originals(dedup, handle, prev, start, end)
    stats["pages"] = pages
    stats["reason"] = reason
    return emit, new_high, len(dedup), stats


def run(do_enrich=True, do_mail=True, argv=None):
    os.makedirs(DATA_DIR, exist_ok=True)   # 初回はdata/が無い。無いとDB作成・state保存が失敗する
    state = load_state(STATE_PATH)
    now = datetime.now(timezone.utc)
    start, end, range_label = resolve_range(argv if argv is not None else sys.argv, now)
    print(f"[*] 収集対象の時間帯: {range_label}")
    if end < now - timedelta(days=1):
        print("     [WARN] 指定範囲が1日以上前です。Xのタイムラインを遡れる範囲を超えると"
              "取得できません（MAX_PAGESにも注意）。")
    s = _session()
    # ★ゲストトークンは「ゲストで取りにいく垢がある場合」だけ必要。取得に失敗しても
    #   ここで落とさない（落ちるとDB作成にも到達せず、初回実行が何も残さず終わる）。
    #   失敗時は gt=None のまま進め、ゲストが必要な垢だけが個別に失敗する。
    gt = None
    if any(h.lower() not in AUTH_HANDLES or not (X_AUTH_TOKEN and X_CT0) for h in HANDLES):
        try:
            gt = get_guest_token(s)
            print(f"[*] guest_token OK / {len(HANDLES)} accounts")
        except Exception as e:
            print(f"[FAIL] guest_token 取得失敗（ゲストが必要な垢はスキップします）: {e}")
    else:
        print(f"[*] 全垢が捨て垢セッション / {len(HANDLES)} accounts")
    conn = init_db(sqlite3.connect(STORE_PATH))
    conn.row_factory = sqlite3.Row
    results, new_ids = {}, []
    for h in HANDLES:                      # ★アカウント独立: 1垢の失敗を隔離
        prev = state.get(h, {}).get("last_seen_id")
        prev = int(prev) if prev is not None else None
        try:
            headers, mode = headers_for(h, gt)
            emit, new_high, raw_n, stats = collect_one(s, headers, h, prev, start, end)
            stored = append_rows(conn, h, emit)          # SQLite へ冪等追記
            new_ids += [t["id"] for t in emit]
            results[h] = {"emit": emit, "raw": raw_n, "stored": stored}
            if new_high is not None:
                state[h] = {"last_seen_id": str(new_high)}
            tag = "🔐auth" if mode == "auth" else "guest"
            print(f"[OK] @{h}({tag}): 新規 {len(emit)} 件 / 保存 {stored} 件"
                  f"（raw {raw_n} / pages {stats['pages']} / prev={prev}"
                  f" / 打ち切り={stats.get('reason')}）")
            if stats.get("reason") not in ("窓を覆った", None):
                print(f"     [ALERT] 指定範囲（{range_label}）を覆う前に打ち切りました。"
                      f"取りこぼしの可能性があります（上のpageログを確認）。")
            if stats["raw"] > 0:      # 何が落ちたのかを常に出す（0件のときだけでは原因が追えない）
                print(f"     └ 内訳: 範囲/差分後{stats['base']}件 → "
                      f"著者不一致-{stats['drop_author']} RT-{stats['drop_rt']} "
                      f"引用-{stats['drop_quote']} = emit {stats['emit']}件"
                      f" / 最新投稿={stats['newest_jst']}（{stats['newest_age_h']}h前）")
                print(f"     └ 取得できた範囲: {stats['raw_span']}"
                      f"（{stats['raw_rate_h']}件/時） / 対象: {range_label}")
            if stats["emit"] == 0 and stats["raw"] > 0:   # 0件の原因を実測表示
                print(f"        検出著者例={stats['authors']}"
                      f"  ← 範囲内0件なら、その時間帯に投稿が無い/差分で除外済み")
        except FetchError as e:            # ここで捕まえるので次の垢へ進める
            results[h] = {"error": e}
            print(f"[FAIL] @{h}: step={e.step} HTTP={e.status}")
            print("       " + (e.body[:300].replace("\n", " ")))
    save_state(STATE_PATH, state)          # 成功分だけでも last_seen を確定・保存
    ok = sum(1 for r in results.values() if "emit" in r)

    if do_enrich and new_ids:              # 翻訳＋ラベリング（未処理行のみ・再開可能）
        d, f = enrich(conn)
        print(f"[*] enrich: 翻訳/ラベル {d} 件 / 失敗 {f} 件")

    purged, orphans, cutoff = purge_old(conn)   # 保持期間超過の削除（毎回実行）
    if purged or orphans:
        print(f"[*] 保持期間({RETENTION_DAYS}日)超過を削除: {purged} 件"
              f"（{cutoff} より前）/ 孤児ラベル {orphans} 行")

    # ★メール送信の前に、DB全体のラベルの表記ゆれを統一する（新規ぶんだけでなく既存も）
    if do_enrich:
        print("[*] ラベル正規化（表記ゆれ統一）…")
        try:
            ch, shr, nf = normalize_labels(conn)
            print(f"[*] 正規化: {ch} 行を書き換え / 値の種類を {shr} 減 / 失敗軸 {nf}")
        except Exception as e:
            print(f"[WARN] 正規化をスキップ（元のラベルのまま送信）: {e}")

    if do_mail and new_ids:                # 本日収集分のみ・時系列降順で配信
        qs = ",".join("?" * len(new_ids))
        rows = conn.execute(f"SELECT * FROM news_log WHERE tweet_id IN ({qs}) "
                            f"ORDER BY created_at DESC", new_ids).fetchall()
        labels_by_tweet = fetch_labels(conn, new_ids)
        today = range_label
        try:
            send_mail(f"{MAIL_SUBJECT} {today}", build_html(rows, labels_by_tweet, today))
            print(f"[*] メール送信 {len(rows)} 件 → {MAIL_TO}")
        except Exception as e:
            print(f"[FAIL] メール送信: {e}")   # データは保存済み。送信失敗で失わない
        write_viewer_data()                 # news_data.js を最新化
        open_db_viewer()                    # ⑥ 送信後（成否問わず）db_viewer.htmlをfile://で開く
    conn.close()

    # F6 失敗検知（黙って欠測しない）
    print(f"\n[SUMMARY] {ok}/{len(HANDLES)} 垢成功 / 新規 {len(new_ids)} 件 / state: {STATE_PATH}")
    if ok == 0:
        print("[ALERT] 全垢失敗。定数陳腐化 or ゲスト封鎖の可能性。body を確認。")
    elif len(new_ids) == 0:
        print("[ALERT] 新規0件。PC未起動での欠測 or 投稿無しの可能性（黙って欠測しない）。")
    return results


# ---- オフライン自己テスト（フレームワーク不要）--------------------------------
def _node(rid, sn, created, text, quote=False, rt=False):
    legacy = {"full_text": text, "created_at": created, "favorite_count": 1, "retweet_count": 0}
    if quote:
        legacy["is_quote_status"] = True
    if rt:
        legacy["retweeted_status_result"] = {"result": {}}
    return {"rest_id": rid, "legacy": legacy,
            "core": {"user_results": {"result": {"legacy": {"screen_name": sn}}}}}


def _payload(nodes):
    entries = [{"content": {"itemContent": {"tweet_results": {"result": n}}}} for n in nodes]
    return {"data": {"user": {"result": {"timeline_v2": {"timeline":
            {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}}


def selftest():
    now = datetime(2026, 9, 22, 3, 30, 0, tzinfo=timezone.utc)
    recent = "Tue Sep 22 03:00:00 +0000 2026"   # now-30分
    old24 = "Sun Sep 21 00:00:00 +0000 2026"    # now-27h（24h窓外）

    # 分類テスト: original / RT / quote / 他人 を正しく仕分けるか
    nodes = [
        _node("200", "nikkei", recent, "本人オリジナル"),                 # emit対象
        _node("201", "nikkei", recent, "RT @x: 他人", rt=True),          # RT除外
        _node("202", "nikkei", recent, "引用コメント", quote=True),       # 引用除外
        _node("203", "other",  recent, "他人の投稿"),                     # author不一致除外
        _node("50",  "nikkei", recent, "古い本人（prev以下）"),           # id<=prev除外
    ]
    raw = extract_all(_payload(nodes))
    assert len(raw) == 5, f"抽出漏れ: {len(raw)}"
    by = {t["id"]: t for t in raw}
    assert by["201"]["is_rt"] and not by["200"]["is_rt"], "RT分類ミス"
    assert by["202"]["is_quote"] and not by["200"]["is_quote"], "引用分類ミス"

    win = lambda n=now, h=24: (n - timedelta(hours=h), n)   # 従来の「直近h時間」相当
    emit, high, _ = filter_originals(raw, "nikkei", 100, *win())
    assert [t["id"] for t in emit] == ["200"], f"差分後emit不一致: {[t['id'] for t in emit]}"
    assert high == 203, f"高水位不一致: {high}"   # raw全体の最大id

    # 初回テスト: prev=None は24h窓で足切り（古いピン留め相当を落とす）
    nodes2 = [_node("300", "nikkei", recent, "新しい本人"),
              _node("299", "nikkei", old24, "24h外の古い本人（ピン留め相当）")]
    raw2 = extract_all(_payload(nodes2))
    emit2, high2, _ = filter_originals(raw2, "nikkei", None, *win())
    assert [t["id"] for t in emit2] == ["300"], f"初回窓ミス: {[t['id'] for t in emit2]}"
    assert high2 == 300, f"初回高水位ミス: {high2}"

    # 冪等性テスト: 直前の高水位をprevに再実行 → 新規0件
    emit3, _, _ = filter_originals(raw2, "nikkei", high2, *win())
    assert emit3 == [], f"冪等性NG（再実行で重複）: {[t['id'] for t in emit3]}"

    # 保存テスト: 冪等追記 & 8列マッピング（JST整形・title冒頭・source）
    conn = init_db(sqlite3.connect(":memory:"))
    tw = {"id": "200", "author": "nikkei", "created_at": recent,
          "text": "見出しだ\n本文の続き", "url": "https://x.com/nikkei/status/200",
          "likes": 1, "retweets": 0, "is_rt": False, "is_quote": False}
    n1 = append_rows(conn, "nikkei", [tw])
    n2 = append_rows(conn, "nikkei", [tw])          # 再追記しても増えない
    assert (n1, n2) == (1, 0), f"store冪等性NG: {(n1, n2)}"
    r = conn.execute("SELECT created_at,title,summary,source FROM news_log").fetchone()
    assert r[0] == "2026/09/22 12:00", f"JST整形不一致: {r[0]}"  # 03:00Z -> 12:00 JST
    assert r[1] == "見出しだ", f"title(冒頭行)不一致: {r[1]}"
    assert r[2] == "見出しだ\n本文の続き", "summary(全文)不一致"
    assert r[3] == "X @nikkei", f"source不一致: {r[3]}"
    conn.close()

    # 翻訳＋ラベリング（LLMはモック）: NULL行だけ埋め、全軸のlabels検証、再実行で0件
    conn2 = init_db(sqlite3.connect(":memory:"))
    conn2.row_factory = sqlite3.Row
    tw2 = {"id": "900", "author": "acc", "created_at": recent,
           "text": "Fed holds rates steady", "url": "https://x.com/acc/status/900",
           "likes": 0, "retweets": 0, "is_rt": False, "is_quote": False}
    append_rows(conn2, "acc", [tw2])
    fake = lambda msgs: ('{"translation":"FRBは金利を据え置いた","labels":'
                          '{"国・地域":"米国","業界":"金融","経済政策":"金融政策",'
                          '"金融機関":"FRB"}}')
    d, f = enrich(conn2, chat_fn=fake)
    assert (d, f) == (1, 0), f"enrich件数NG: {(d, f)}"
    d2, _ = enrich(conn2, chat_fn=fake)          # 再実行→未処理なし＝冪等
    assert d2 == 0, "enrich冪等性NG（再処理された）"
    lbl = fetch_labels(conn2, ["900"])["900"]
    assert lbl == {"国・地域": ["米国"], "業界": ["金融"],
                   "経済政策": ["金融政策"], "金融機関": ["FRB"]}, f"ラベル保存NG: {lbl}"
    assert list(lbl) == LABEL_AXES, f"軸の並びがLABEL_AXES順でない: {list(lbl)}"

    # 軸を減らしたときの挙動: DBに残る旧軸の行はメール側に出さない（LABEL_AXESが正）
    conn2.execute("INSERT OR REPLACE INTO news_labels VALUES ('900','重要度','高')")
    assert "重要度" not in fetch_labels(conn2, ["900"])["900"], "廃止した軸がメールに出ている"
    assert conn2.execute(
        "SELECT COUNT(*) FROM news_labels WHERE axis='重要度'").fetchone()[0] == 1, \
        "fetch_labelsがDBを書き換えている（読むだけのはず）"
    # --prune-axes 相当: 明示的に呼んだときだけDBからも消える
    assert prune_axes(conn2) == 1, "prune_axesの削除件数NG"
    assert conn2.execute(
        "SELECT COUNT(*) FROM news_labels WHERE axis='重要度'").fetchone()[0] == 0, "prune_axes未削除"
    assert conn2.execute(
        "SELECT COUNT(*) FROM news_labels WHERE tweet_id='900'").fetchone()[0] == len(LABEL_AXES), \
        "prune_axesが現役の軸まで消した"

    # looks_like_chinese: 簡体字専用漢字での検知／かな無し長文での検知／正常な日本語は通す
    assert looks_like_chinese("联准会维持利率不变，市场反应平淡") is True, \
        "簡体字専用漢字の検知NG"
    assert looks_like_chinese("アメリカ経済成長率発表予定") is False, \
        "短いかな無し語句を誤検知（False Positive）"
    assert looks_like_chinese("FRBは政策金利を据え置いた") is False, \
        "正常な日本語を誤検知NG"
    assert looks_like_chinese("これは十分に長いひらがな入り日本語の文章です") is False, \
        "かな入り長文を誤検知NG"
    assert looks_like_chinese("") is False, "空文字での誤検知NG"

    # enrich: 中国語→日本語の順で返すモックで自動リトライして成功するか（conn2は汚さない）
    conn4 = init_db(sqlite3.connect(":memory:"))
    conn4.row_factory = sqlite3.Row
    tw4 = {"id": "901", "author": "acc", "created_at": recent,
           "text": "Fed holds rates steady", "url": "https://x.com/acc/status/901",
           "likes": 0, "retweets": 0, "is_rt": False, "is_quote": False}
    append_rows(conn4, "acc", [tw4])
    calls = {"n": 0}

    def flaky_chinese_then_japanese(msgs):
        calls["n"] += 1
        if calls["n"] == 1:
            return '{"translation":"联准会维持利率不变","labels":{"業界":"金融"}}'
        return '{"translation":"FRBは金利を据え置いた","labels":{"業界":"金融"}}'

    d3, f3 = enrich(conn4, chat_fn=flaky_chinese_then_japanese)
    assert (d3, f3) == (1, 0), f"中国語リトライ成功件数NG: {(d3, f3)}"
    assert calls["n"] == 2, f"リトライ回数NG（2回目で成功するはず）: {calls['n']}"
    tr3 = conn4.execute(
        "SELECT translation FROM news_log WHERE tweet_id='901'").fetchone()[0]
    assert tr3 == "FRBは金利を据え置いた", f"リトライ後の訳文NG: {tr3}"

    # enrich: 毎回中国語のモック→再試行上限に達し失敗扱い（次回再試行に持ち越す＝translationはNULLのまま）
    conn4.execute("UPDATE news_log SET translation=NULL WHERE tweet_id='901'")
    always_chinese = lambda msgs: '{"translation":"联准会维持利率不变","labels":{}}'
    d4, f4 = enrich(conn4, chat_fn=always_chinese)
    assert (d4, f4) == (0, 1), f"中国語失敗扱いNG: {(d4, f4)}"
    tr4 = conn4.execute(
        "SELECT translation FROM news_log WHERE tweet_id='901'").fetchone()[0]
    assert tr4 is None, f"失敗行のtranslationがNULLのままでない（次回再試行できない）: {tr4}"
    conn4.close()

    # ---- enrich のバッチ処理（複数件まとめてのAPI呼び出し）------------------------
    # parse_enrich_batch: 正常系（全件回答）・回答漏れ・翻訳空欄を区別できるか
    pb = parse_enrich_batch(
        json.dumps({"items": {
            "1": {"translation": "日本語訳1", "labels": {"業界": "金融"}},
            "3": {"translation": "", "labels": {}},          # 翻訳が空→フォールバック対象
        }}),
        [("1", "text1"), ("2", "text2"), ("3", "text3")])
    assert set(pb) == {"1"}, f"parse_enrich_batchの欠測判定NG: {set(pb)}"
    assert pb["1"]["labels"]["業界"] == "金融", "parse_enrich_batchのラベル取得NG"
    assert pb["1"]["labels"]["金融機関"] == "不明", "parse_enrich_batchの未回答軸フォールバックNG"

    # enrich: 複数件を1回のAPI呼び出しでまとめて処理し、レスポンスの回答漏れ・中国語混入
    # だけは個別に立て直す（他の項目の成功を巻き込んで捨てない）
    conn5 = init_db(sqlite3.connect(":memory:"))
    for n in (930, 931, 932):
        append_rows(conn5, "acc", [{
            "id": str(n), "author": "acc", "created_at": recent,
            "text": f"Test tweet {n}", "url": f"https://x.com/acc/status/{n}",
            "likes": 0, "retweets": 0, "is_rt": False, "is_quote": False}])
    batch_calls = {"n": 0}

    def fake_partial(msgs, max_tokens=300, est_items=1, **kw):
        if est_items >= 2:
            batch_calls["n"] += 1
            # 930は正常回答／931は回答漏れ／932は中国語混入のまま返す
            # → 931・932の2件だけがrun_one（個別処理）に回されるはず
            return json.dumps({"items": {
                "930": {"translation": "日本語訳930", "labels": {}},
                "932": {"translation": "联准会维持利率不变", "labels": {}},
            }}, ensure_ascii=False)
        return '{"translation":"個別回収した訳文","labels":{"業界":"金融"}}'

    d6, f6 = enrich(conn5, chat_fn=fake_partial)
    assert (d6, f6) == (3, 0), f"バッチ+個別フォールバックの成功件数NG: {(d6, f6)}"
    assert batch_calls["n"] == 1, f"バッチ呼び出し回数NG（1回にまとまるはず）: {batch_calls['n']}"
    tr = {n: conn5.execute(
        "SELECT translation FROM news_log WHERE tweet_id=?", (str(n),)).fetchone()[0]
        for n in (930, 931, 932)}
    assert tr[930] == "日本語訳930", f"バッチ成功ぶんの訳文NG: {tr}"
    assert tr[931] == "個別回収した訳文", f"回答漏れの個別フォールバックNG: {tr}"
    assert tr[932] == "個別回収した訳文", f"中国語混入の個別フォールバックNG: {tr}"
    conn5.close()

    # enrich: バッチ呼び出し自体が失敗（500等）したら半分に分割して再試行し、
    # 1件まで割ってもだめならenrich_one（中国語再試行つき個別処理）にフォールバックする
    conn6 = init_db(sqlite3.connect(":memory:"))
    for n in range(940, 945):        # 5件（ENRICH_BATCH_SIZE=10以下＝最初は1バッチ）
        append_rows(conn6, "acc", [{
            "id": str(n), "author": "acc", "created_at": recent,
            "text": f"Test tweet {n}", "url": f"https://x.com/acc/status/{n}",
            "likes": 0, "retweets": 0, "is_rt": False, "is_quote": False}])
    split_sizes = []

    def fake_split(msgs, max_tokens=300, est_items=1, **kw):
        if est_items >= 2:
            split_sizes.append(est_items)
            if est_items >= 3:
                raise RuntimeError("HTTP Error 500: Internal Server Error")
            ids = re.findall(r'"(\d+)":\s*"', msgs[1]["content"])
            items = {tid: {"translation": f"日本語訳{tid}", "labels": {}} for tid in ids}
            return json.dumps({"items": items}, ensure_ascii=False)
        return '{"translation":"個別処理での訳文","labels":{"業界":"金融"}}'

    d7, f7 = enrich(conn6, chat_fn=fake_split)
    assert (d7, f7) == (5, 0), f"分割再試行の成功件数NG: {(d7, f7)}"
    assert sorted(split_sizes) == [2, 2, 3, 5], f"分割の内訳NG（5→2+3→1+2の想定）: {split_sizes}"
    tr940 = conn6.execute(
        "SELECT translation FROM news_log WHERE tweet_id='940'").fetchone()[0]
    tr942 = conn6.execute(
        "SELECT translation FROM news_log WHERE tweet_id='942'").fetchone()[0]
    assert tr940 == "日本語訳940", f"分割後バッチ成功ぶんの訳文NG: {tr940}"
    assert tr942 == "個別処理での訳文", f"1件まで割った後の個別フォールバックNG: {tr942}"
    conn6.close()

    # parse_enrich フォールバック: 壊れJSON/未回答軸→原文・"不明"で欠測にしない
    o = parse_enrich('{"labels":{"業界":"金融"}}', "orig text")
    assert o["translation"] == "orig text", "翻訳フォールバックNG"
    assert o["labels"]["業界"] == "金融", "回答済み軸の取得NG"
    assert o["labels"]["金融機関"] == "不明", "未回答軸のフォールバックNG"
    assert set(o["labels"]) == set(LABEL_AXES), "labelsがLABEL_AXES全軸ぶんで埋まっていない"

    # エスケープ順序（& が最初）＝GAS踏襲の回帰チェック
    assert escape_html('<a href="x">&</a>') == \
        "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;", "escape順序NG"

    # HTML生成: 訳文・セクション見出し(国・地域)・他軸バッジ・Xバッジを含む
    rows = conn2.execute("SELECT * FROM news_log").fetchall()
    labels_by_tweet = fetch_labels(conn2, [r["tweet_id"] for r in rows])
    html = build_html(rows, labels_by_tweet, "2026/09/22")
    for token in ("FRBは金利を据え置いた", "米国", "金融", "FRB", "X @acc"):
        assert token in html, f"HTMLに {token} が無い"
    conn2.close()

    # XSSエスケープ（信頼境界: 訳文・ラベル値に<script>が来ても素通ししない）
    ev = build_card_compact({"translation": "<script>x</script>", "summary": "", "url": "",
                             "source": "X @a", "created_at": ""},
                             {"業界": ["<script>y</script>"]}, exclude_axis="国・地域")
    assert "<script>" not in ev and "&lt;script&gt;" in ev, "XSSエスケープNG"

    # セクション分け: EMAIL_GROUP_BY(国・地域)ごとに正しくグルーピングされるか
    conn3 = init_db(sqlite3.connect(":memory:"))
    conn3.row_factory = sqlite3.Row
    tw_us = {"id": "1000", "author": "acc", "created_at": recent, "text": "US news",
             "url": "https://x.com/acc/status/1000", "likes": 0, "retweets": 0,
             "is_rt": False, "is_quote": False}
    tw_jp = {"id": "1001", "author": "acc", "created_at": recent, "text": "JP news",
             "url": "https://x.com/acc/status/1001", "likes": 0, "retweets": 0,
             "is_rt": False, "is_quote": False}
    append_rows(conn3, "acc", [tw_us, tw_jp])
    conn3.executemany(
        "INSERT OR REPLACE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
        [("1000", "国・地域", "米国"), ("1001", "国・地域", "日本")])
    rows3 = conn3.execute(
        "SELECT * FROM news_log ORDER BY tweet_id DESC").fetchall()  # 1001(日本)→1000(米国)の順
    labels3 = {"1000": {"国・地域": ["米国"]}, "1001": {"国・地域": ["日本"]}}
    html3 = build_html(rows3, labels3, "2026/09/22")
    assert "米国（1件）" in html3 and "日本（1件）" in html3, "セクション見出しNG"
    assert html3.index("日本（1件）") < html3.index("米国（1件）"), "セクション並び順NG"
    assert '<meta charset="UTF-8">' in html3, "meta charset欠落（文字化けの原因になる）"

    # ★1軸に複数値がある記事は、該当する全セクションに重複して載る
    labels3m = {"1000": {"国・地域": ["米国", "中国"]}, "1001": {"国・地域": ["日本"]}}
    html3m = build_html(rows3, labels3m, "2026/09/22")
    assert "米国（1件）" in html3m and "中国（1件）" in html3m, "複数値セクションNG"
    assert html3m.count("https://x.com/acc/status/1000") == 2, "複数値の記事が両セクションに出ていない"
    conn3.close()

    # ---- ラベル正規化（表記ゆれ統一。LLMはモック）----------------------------
    conn5 = init_db(sqlite3.connect(":memory:"))
    conn5.executemany(
        "INSERT OR REPLACE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
        [("1", "国・地域", "アメリカ"), ("2", "国・地域", "米国"),
         ("3", "国・地域", "イラン, 米国"), ("4", "金融機関", "ゴールドマン・サックス")])
    norm_map = {"アメリカ": ["米国"], "イラン, 米国": ["イラン", "米国"]}
    def fake_norm(msgs, max_tokens=300):
        body = msgs[1]["content"]
        vals = json.loads(body[body.index("対象の値: ") + len("対象の値: "):].split("\n")[0])
        return json.dumps({"mapping": {v: norm_map.get(v, [v]) for v in vals}}, ensure_ascii=False)
    ch, shr, nf = normalize_labels(conn5, chat_fn=fake_norm, verbose=False)
    got = {(t, v) for t, v in conn5.execute(
        "SELECT tweet_id, value FROM news_labels WHERE axis='国・地域' ORDER BY 1,2")}
    assert got == {("1", "米国"), ("2", "米国"), ("3", "イラン"), ("3", "米国")}, f"正規化NG: {got}"
    assert nf == 0, f"正規化の失敗軸NG: {nf}"
    # 値が1種類だけの軸はLLMを呼ばずに素通し（固有名詞の「・」で割られない）
    # 値が1種類だけの軸も対象にする（単独の "イラン, 米国" が分割されない穴を塞ぐ）。
    # ただし固有名詞の「・」では割らない＝分割判断はLLM任せ。
    assert [r[0] for r in conn5.execute(
        "SELECT value FROM news_labels WHERE axis='金融機関'")] == ["ゴールドマン・サックス"], "単一値軸NG"
    conn5.executemany("INSERT OR REPLACE INTO news_labels VALUES (?,?,?)",
                      [("9", "経済政策", "イラン, 米国")])
    normalize_labels(conn5, chat_fn=lambda m, max_tokens=300: json.dumps(
        {"mapping": {"イラン, 米国": ["イラン", "米国"]}}, ensure_ascii=False), verbose=False)
    assert {r[0] for r in conn5.execute(
        "SELECT value FROM news_labels WHERE axis='経済政策'")} == {"イラン", "米国"}, \
        "唯一の値が分割されない"
    # LLMが壊れた応答を返しても、元のラベルを失わない（信頼境界の検証）
    conn5.executemany(
        "INSERT OR REPLACE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
        [("5", "業界", "エネルギー"), ("6", "業界", "金融")])
    normalize_labels(conn5, chat_fn=lambda m, max_tokens=300: "壊れた応答", verbose=False)
    assert {r[0] for r in conn5.execute(
        "SELECT value FROM news_labels WHERE axis='業界'")} == {"エネルギー", "金融"}, \
        "壊れた応答でラベルが消えた"
    # 例外を投げても他の軸は無傷（軸ごとに独立）
    def boom(msgs, max_tokens=300):
        raise RuntimeError("LLM落ち")
    before_boom = conn5.execute("SELECT COUNT(*) FROM news_labels").fetchone()[0]
    _, _, nf2 = normalize_labels(conn5, chat_fn=boom, verbose=False)
    assert nf2 >= 1, "例外時に失敗軸が数えられていない"
    assert conn5.execute("SELECT COUNT(*) FROM news_labels").fetchone()[0] == before_boom, \
        "例外時にラベルが失われた"

    # ★回帰: 大きいチャンクでLLMが落ちても、分割して再試行し取りこぼさない
    #   （実機で値30個の軸が HTTP 500 になり、その軸だけ正規化されなかった）
    conn5.executemany("INSERT OR REPLACE INTO news_labels VALUES (?,?,?)",
                      [(f"r{i}", "国・地域", f"国{i}") for i in range(8)] + [("r9", "国・地域", "-")])
    seen_sizes = []
    def fail_when_big(msgs, max_tokens=300):
        body = msgs[1]["content"]
        vals = json.loads(body[body.index("対象の値: ") + len("対象の値: "):].split("\n")[0])
        seen_sizes.append(len(vals))
        if len(vals) > 3:
            raise RuntimeError("HTTP Error 500: Internal Server Error")
        return json.dumps({"mapping": {v: (["なし"] if v == "-" else [v]) for v in vals}},
                          ensure_ascii=False)
    _, _, nf3 = normalize_labels(conn5, chat_fn=fail_when_big, verbose=False)
    assert nf3 == 0, f"分割再試行で救えていない（失敗軸 {nf3}）"
    assert min(seen_sizes) <= 3, f"チャンクが分割されていない: {seen_sizes}"
    assert "-" not in {r[0] for r in conn5.execute(
        "SELECT value FROM news_labels WHERE axis='国・地域'")}, "分割再試行後に正規化されていない"

    # 廃止した軸にはLLMを呼ばない（無駄打ち防止）
    conn5.execute("INSERT OR REPLACE INTO news_labels VALUES ('7','重要度','高')")
    conn5.execute("INSERT OR REPLACE INTO news_labels VALUES ('8','重要度','中')")
    seen_axes = []
    def spy(msgs, max_tokens=300):
        body = msgs[1]["content"]
        seen_axes.append(body[1:body.index("」")])
        return "{}"
    normalize_labels(conn5, chat_fn=spy, verbose=False)
    assert "重要度" not in seen_axes, f"廃止した軸を正規化しようとした: {seen_axes}"
    assert set(seen_axes) <= set(LABEL_AXES), f"LABEL_AXES外の軸を処理した: {seen_axes}"
    conn5.close()

    # ---- 保持期間(RETENTION_DAYS)の削除 --------------------------------------
    conn6 = init_db(sqlite3.connect(":memory:"))
    base = datetime(2026, 9, 24, 12, 0, tzinfo=JST)
    for tid, delta in [("old8", timedelta(days=8)), ("edge", timedelta(days=7, minutes=1)),
                       ("keep", timedelta(days=6, hours=23)), ("new0", timedelta(0))]:
        conn6.execute(
            "INSERT INTO news_log (tweet_id,created_at,title,summary,url,source) VALUES (?,?,?,?,?,?)",
            (tid, (base - delta).strftime("%Y/%m/%d %H:%M"), "t", "s", "u", "X @a"))
        conn6.execute("INSERT INTO news_labels VALUES (?,?,?)", (tid, "国・地域", "米国"))
    conn6.execute("INSERT INTO news_labels VALUES ('gone','国・地域','孤児')")   # 本体の無いラベル
    purged, orphans, cutoff = purge_old(conn6, now=base)
    left = {r[0] for r in conn6.execute("SELECT tweet_id FROM news_log")}
    assert (purged, left) == (2, {"keep", "new0"}), f"保持期間の削除NG: {purged} / {left}"
    assert {r[0] for r in conn6.execute("SELECT tweet_id FROM news_labels")} == {"keep", "new0"}, \
        "削除済みツイートのラベルが残っている"
    assert orphans == 1, f"孤児ラベルの掃除NG: {orphans}"
    assert purge_old(conn6, now=base)[0] == 0, "削除の冪等性NG（2回目で再削除）"
    conn6.close()

    # ---- 旧スキーマ(主キー: tweet_id, axis)のDBが自動移行されるか --------------
    conn7 = sqlite3.connect(":memory:")
    conn7.execute("CREATE TABLE news_labels (tweet_id TEXT NOT NULL, axis TEXT NOT NULL, "
                  "value TEXT, PRIMARY KEY (tweet_id, axis))")
    conn7.executemany("INSERT INTO news_labels VALUES (?,?,?)",
                      [("1", "国・地域", "米国"), ("2", "国・地域", "")])   # 空値は移行しない
    init_db(conn7)
    pk = [r[1] for r in sorted((r for r in conn7.execute("PRAGMA table_info(news_labels)") if r[5]),
                               key=lambda r: r[5])]
    assert pk == ["tweet_id", "axis", "value"], f"主キー移行NG: {pk}"
    assert conn7.execute("SELECT COUNT(*) FROM news_labels").fetchone()[0] == 1, "移行でデータが増減した"
    conn7.executemany("INSERT INTO news_labels VALUES (?,?,?)",
                      [("1", "国・地域", "イラン")])        # 移行後は1軸に複数値を入れられる
    assert conn7.execute("SELECT COUNT(*) FROM news_labels WHERE tweet_id='1'").fetchone()[0] == 2, \
        "移行後に複数値を保存できない"
    assert migrate_labels_pk(conn7) is False, "移行の冪等性NG（2回目も走った）"
    conn7.close()

    # config.ini 読み込み: 存在しないファイル→空文字、値ありは取得できる（起動時に落とさない）
    import tempfile
    no_such = os.path.join(tempfile.gettempdir(), "__xnews_selftest_no_such_file.ini")
    missing_cfg = load_config(no_such)
    assert cfg_get(missing_cfg, "mail", "gmail_user") == "", "存在しないconfigで空文字にならない"
    real_cfg = configparser.ConfigParser()
    real_cfg.read_string("[mail]\ngmail_user = test@example.com\n[x]\nauth_token = abc\n")
    assert cfg_get(real_cfg, "mail", "gmail_user") == "test@example.com", "config取得値NG"
    assert cfg_get(real_cfg, "x", "ct0") == "", "未設定キーが空文字にならない"

    # secret(): config.ini優先、無ければ環境変数（GitHub Actions運用向け）にフォールバック
    _env_key = "__XNEWS_SELFTEST_ENV__"
    os.environ[_env_key] = "from-env"
    try:
        assert secret(missing_cfg, "mail", "gmail_user", _env_key) == "from-env", \
            "secret(): config未設定時に環境変数へフォールバックしない"
        assert secret(real_cfg, "mail", "gmail_user", _env_key) == "test@example.com", \
            "secret(): config.iniの値より環境変数が優先されてしまっている"
        assert secret(missing_cfg, "mail", "no_such_key", "__XNEWS_SELFTEST_NO_SUCH_ENV__") == "", \
            "secret(): 両方未設定なのに空文字にならない"
    finally:
        del os.environ[_env_key]

    # アプリパスワードのスペース除去（Google表示の4桁区切りをそのまま貼る事故対策）の回帰チェック
    pw_cfg = configparser.ConfigParser()
    pw_cfg.read_string("[mail]\ngmail_app_password = abcd efgh ijkl mnop\n")
    assert cfg_get(pw_cfg, "mail", "gmail_app_password").replace(" ", "") == "abcdefghijklmnop", \
        "アプリパスワードのスペース除去NG"

    # ページネーション: cutoffに届くまで複数ページを重ね、重複除去し、上限で必ず止まる
    cutoff_pg = datetime(2026, 9, 22, 0, 0, 0, tzinfo=timezone.utc)
    page_new = extract_all(_payload(
        [_node("600", "acc", "Tue Sep 22 05:00:00 +0000 2026", "new")]))   # cutoffより新しい
    page_new2 = extract_all(_payload(
        [_node("601", "acc", "Tue Sep 22 04:00:00 +0000 2026", "new2")]))  # cutoffより新しい
    page_old = extract_all(_payload(
        [_node("500", "acc", "Mon Sep 21 20:00:00 +0000 2026", "old")]))   # cutoffより古い

    def mock_pages(seq):
        it = iter(seq)
        return lambda cursor: next(it)

    # (a) 窓より古い投稿【だけ】のページに到達したら終了（窓を覆えた）
    dedup, pages, reason = paginate_tweets(mock_pages([(page_old, "C1")]), cutoff_pg)
    assert (pages, len(dedup), reason) == (1, 1, "窓を覆った"), f"1ページ終了NG: {pages}/{reason}"

    # (b) 1ページ目が全部新しい→cursorを辿って2ページ目まで取得
    dedup, pages, reason = paginate_tweets(
        mock_pages([(page_new, "C1"), (page_old, "C2")]), cutoff_pg)
    assert (pages, len(dedup)) == (2, 2), f"2ページ継続NG: pages={pages}"

    # (c) cursorが無ければ窓を満たしていなくても打ち切り（無限ループにしない）
    dedup, pages, reason = paginate_tweets(mock_pages([(page_new, None)]), cutoff_pg)
    assert (pages, reason) == (1, "cursorが無い"), f"cursor無しでの打ち切りNG: {pages}/{reason}"

    # (d) max_pagesで必ず止まる（cursorが尽きずcutoffにも届かなくても）
    fresh_page = lambda i: extract_all(_payload(
        [_node(str(700 + i), "acc", "Tue Sep 22 05:00:00 +0000 2026", "new")]))
    dedup, pages, reason = paginate_tweets(
        mock_pages([(fresh_page(1), "C1"), (fresh_page(2), "C2"), (fresh_page(3), "C3")]),
        cutoff_pg, max_pages=2)
    assert pages == 2 and "MAX_PAGES" in reason, f"max_pages打ち切りNG: {pages}/{reason}"

    # (e) ページ跨ぎの重複IDは1件に集約
    dedup, pages, reason = paginate_tweets(
        mock_pages([(page_new, "C1"), (page_new + page_old, None)]), cutoff_pg)
    ids = [t["id"] for t in dedup]
    assert ids.count("600") == 1, f"重複除去NG: {ids}"

    # (f) ★回帰: ピン留め（何年も前の投稿）がページ先頭に混ざっても打ち切らない
    #     実機で raw21/pages1 となり8時間の窓のうち3時間ぶんしか取れなかった事象の再発防止。
    pinned = extract_all(_payload(
        [_node("1", "acc", "Fri Mar 07 00:00:00 +0000 2025", "ピン留め")]))
    dedup, pages, reason = paginate_tweets(
        mock_pages([(page_new + pinned, "C1"), (page_new2, "C2"), (page_old, "C3")]), cutoff_pg)
    assert (pages, reason) == (3, "窓を覆った"), f"ピン留めで打ち切られた: {pages}/{reason}"
    assert len(dedup) == 4, f"ピン留め時の取得件数NG: {len(dedup)}"
    # ピン留め自体は窓の外なので配信対象には入らない（従来どおり）
    emit_pin, _, _ = filter_originals(
        dedup, "acc", None, cutoff_pg, datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc))
    assert [t["id"] for t in emit_pin] == ["600", "601"], f"ピン留め除外NG: {emit_pin}"

    # (g) 同じページが返り続けても止まる（cursorが進まないケース）
    dedup, pages, reason = paginate_tweets(lambda c: (page_new, "SAME"), cutoff_pg)
    assert (pages, reason, len(dedup)) == (2, "cursorが進まない", 1), \
        f"cursor停滞の打ち切りNG: {pages}/{reason}"

    # ---- db_viewer用データの書き出し（news_data.js。サーバは廃止しfile://直開き）--------
    import tempfile
    tmp_db_fd, tmp_db_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_db_fd)
    os.remove(tmp_db_path)          # init_dbが新規作成する形にする（既存ファイル前提にしない）
    try:
        conn_v = init_db(sqlite3.connect(tmp_db_path))
        twv = {"id": "950", "author": "acc", "created_at": recent,
               "text": "Viewer test tweet", "url": "https://x.com/acc/status/950",
               "likes": 0, "retweets": 0, "is_rt": False, "is_quote": False}
        append_rows(conn_v, "acc", [twv])
        conn_v.execute("UPDATE news_log SET translation=? WHERE tweet_id=?",
                       ("ビューア用テスト訳", "950"))
        conn_v.executemany(
            "INSERT OR REPLACE INTO news_labels (tweet_id, axis, value) VALUES (?,?,?)",
            [("950", "国・地域", "米国"), ("950", "国・地域", "日本"), ("950", "業界", "金融")])
        conn_v.commit()
        conn_v.close()

        # build_viewer_items: news_log+news_labelsをtweet_idごとに組み立てる
        # （1軸に複数値があり得るので配列で持つ＝db_viewer.html側のJSと同じ形）
        items = build_viewer_items(tmp_db_path)
        assert len(items) == 1, f"build_viewer_items件数NG: {len(items)}"
        it = items[0]
        assert it["tweet_id"] == "950" and it["translation"] == "ビューア用テスト訳", \
            f"build_viewer_itemsの内容NG: {it}"
        assert sorted(it["labels"]["国・地域"]) == ["日本", "米国"], \
            f"複数値ラベルの組み立てNG: {it['labels']}"
        assert it["labels"]["業界"] == ["金融"], "単一値ラベルの組み立てNG"

        # write_viewer_data: <script>から読み込めるJS（JSON埋め込み）として書き出す
        tmp_out = tmp_db_path + ".data.js"
        assert write_viewer_data(db_path=tmp_db_path, out_path=tmp_out) is True, \
            "write_viewer_dataが失敗を返した"
        txt = open(tmp_out, encoding="utf-8").read()
        assert "window.__NEWS_ITEMS = [" in txt, "news_data.jsの中身がJSON配列になっていない"
        assert "ビューア用テスト訳" in txt, "news_data.jsに訳文が含まれていない"
        m = re.search(r"__NEWS_ITEMS = (\[.*\]);", txt, re.S)
        assert json.loads(m.group(1)) == items, "書き出したJSONを読み直した結果が元と一致しない"
        os.remove(tmp_out)

        # DBが空でも例外にならない（初回実行＝データ0件でも書き出せる）
        tmp_db2_fd, tmp_db2_path = tempfile.mkstemp(suffix=".db")
        os.close(tmp_db2_fd)
        os.remove(tmp_db2_path)
        init_db(sqlite3.connect(tmp_db2_path)).close()
        assert build_viewer_items(tmp_db2_path) == [], "空DBでのbuild_viewer_itemsNG"
        tmp_out2 = tmp_db2_path + ".data.js"
        assert write_viewer_data(db_path=tmp_db2_path, out_path=tmp_out2) is True, \
            "空DBでのwrite_viewer_data失敗"
        os.remove(tmp_out2)
        os.remove(tmp_db2_path)
    finally:
        if os.path.exists(tmp_db_path):
            os.remove(tmp_db_path)

    # open_db_viewer: サーバを起動せず、file://のURLでブラウザを開こうとすること
    opened = {}
    open_db_viewer(viewer_path=os.path.join(BASE_DIR, "web", "db_viewer.html"),
                   opener=lambda u: opened.setdefault("url", u))
    assert opened.get("url", "").startswith("file://"), f"file://で開いていない: {opened}"
    assert opened["url"].endswith("db_viewer.html"), f"開いたURLがdb_viewer.htmlでない: {opened}"
    # 存在しないパスなら、例外にせず警告だけで済ませる（openerは呼ばれない）
    opened2 = {}
    open_db_viewer(viewer_path=os.path.join(BASE_DIR, "web", "no_such_file.html"),
                   opener=lambda u: opened2.setdefault("url", u))
    assert "url" not in opened2, "存在しないファイルなのにブラウザを開こうとした"

    # GitHub Actions環境(GITHUB_ACTIONS=true)ではブラウザを開こうとせず即スキップすること
    os.environ["GITHUB_ACTIONS"] = "true"
    try:
        opened3 = {}
        open_db_viewer(viewer_path=os.path.join(BASE_DIR, "web", "db_viewer.html"),
                       opener=lambda u: opened3.setdefault("url", u))
        assert "url" not in opened3, "CI環境なのにブラウザを開こうとした"
    finally:
        del os.environ["GITHUB_ACTIONS"]

    # ---- LLM(Groq)のレート制限とリトライ ------------------------------------
    # 仮想時計＋仮想sleepで検証する（実時間を待たない）
    class FakeClock:
        def __init__(self):
            self.t = 0.0
            self.slept = []

        def now(self):
            return self.t

        def sleep(self, sec):
            self.slept.append(sec)
            self.t += sec

    fc = FakeClock()
    lim = RateLimiter(rpm=3, tpm=1000, clock=fc.now, sleeper=fc.sleep)
    for _ in range(3):
        assert lim.acquire(100) == 0, "上限内なのに待たされた"
    waited = lim.acquire(100)          # 4回目＝RPM超過なので60秒窓が空くまで待つ
    assert waited >= 60, f"RPM超過で待っていない: {waited}"
    fc2 = FakeClock()
    lim2 = RateLimiter(rpm=100, tpm=1000, clock=fc2.now, sleeper=fc2.sleep)
    lim2.acquire(900)
    assert lim2.acquire(200) >= 60, "TPM超過で待っていない"   # 900+200 > 1000
    # 実測トークン数が見積りより小さければ、その分だけ枠が空く
    fc3 = FakeClock()
    lim3 = RateLimiter(rpm=100, tpm=1000, clock=fc3.now, sleeper=fc3.sleep)
    lim3.acquire(900)
    lim3.settle(100)                   # 実際は100だった
    assert lim3.acquire(200) == 0, "実測反映後も待たされた（settleが効いていない）"

    # retry-after の解釈（ヘッダ優先 → 本文のフォールバック）
    assert parse_retry_after({"retry-after": "12"}, "") == 12
    assert abs(parse_retry_after({}, "Please try again in 6m 11.52s") - 371.52) < 0.01
    assert parse_retry_after({}, "no hint") is None

    # 429 → retry-after に従って待ち、再試行して成功する（待って続行）
    saved_key = LLM_API_KEY
    globals()["LLM_API_KEY"] = "test-key"
    try:
        class FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        ok_payload = {"choices": [{"message": {"content": '{"ok":1}'}}],
                      "usage": {"total_tokens": 42}}
        calls = {"n": 0}

        def flaky_opener(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {"retry-after": "7"},
                    io.BytesIO(b'{"error":{"message":"Rate limit reached"}}'))
            return FakeResp(ok_payload)

        fc4 = FakeClock()
        lim4 = RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep)
        out = llm_chat([{"role": "user", "content": "hi"}], limiter=lim4,
                       opener=flaky_opener, sleeper=fc4.sleep)
        assert out == '{"ok":1}', f"429後の再試行NG: {out}"
        assert 7 in fc4.slept, f"retry-afterに従って待っていない: {fc4.slept}"
        assert calls["n"] == 2, f"再試行回数NG: {calls['n']}"

        # 429が続けば最終的に例外（黙って空の結果を返さない）
        def always_429(req):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"retry-after": "1"}, io.BytesIO(b"{}"))
        try:
            llm_chat([{"role": "user", "content": "hi"}],
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=always_429, sleeper=fc4.sleep)
            raise AssertionError("429が続いたのに例外にならない")
        except RuntimeError as e:
            assert "429" in str(e), f"例外メッセージNG: {e}"

        # ★回帰: リクエストに必ず User-Agent が付く
        #   （無いとCloudflareに 403 error code: 1010 で弾かれる。実機で発生した）
        seen_headers = {}

        def capture_opener(req):
            seen_headers.update({k.lower(): v for k, v in req.header_items()})
            return FakeResp(ok_payload)

        llm_chat([{"role": "user", "content": "hi"}],
                 limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                 opener=capture_opener, sleeper=fc4.sleep)
        assert seen_headers.get("User-agent".lower()), f"User-Agentが無い: {seen_headers}"
        assert "urllib" not in seen_headers["user-agent"].lower(), "既定UAのままになっている"
        assert seen_headers.get("authorization", "").startswith("Bearer "), "認証ヘッダNG"

        # 403(1010)は待たずに落とし、原因が分かるメッセージにする
        def cf_block(req):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b"error code: 1010\n"))
        before_cf = len(fc4.slept)
        try:
            llm_chat([{"role": "user", "content": "hi"}],
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=cf_block, sleeper=fc4.sleep)
            raise AssertionError("403(1010)で例外にならない")
        except RuntimeError as e:
            assert "User-Agent" in str(e), f"403(1010)のメッセージNG: {e}"
        assert len(fc4.slept) == before_cf, "403でリトライ待機している"

        # 401/404 は原因別のメッセージにする（キーの問題かモデル名の問題かを切り分ける）
        for code, needle in ((401, "APIキー"), (404, "使えません")):
            def bad(req, _c=code):
                raise urllib.error.HTTPError(req.full_url, _c, "", {}, io.BytesIO(b"{}"))
            try:
                llm_chat([{"role": "user", "content": "hi"}],
                         limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                         opener=bad, sleeper=fc4.sleep)
                raise AssertionError(f"{code}で例外にならない")
            except RuntimeError as e:
                assert needle in str(e), f"{code}のメッセージNG: {e}"

        # ★回帰: 推論モデル向けの出力予算・reasoning_effort・JSONモードのフォールバック
        #   （max_tokens=300のままだと推論に食われ、400 json_validate_failed になる）
        sent = []

        def body_capture(req):
            sent.append(json.loads(req.data))
            return FakeResp(ok_payload)

        saved_model = LLM_MODEL
        globals()["LLM_MODEL"] = "openai/gpt-oss-120b"
        try:
            assert is_reasoning_model("openai/gpt-oss-120b"), "推論モデル判定NG"
            assert not is_reasoning_model("llama-3.3-70b-versatile"), "非推論の誤判定"
            llm_chat([{"role": "user", "content": "hi"}], max_tokens=300,
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=body_capture, sleeper=fc4.sleep)
            b = sent[-1]
            assert "max_tokens" not in b, "非推奨の max_tokens を送っている"
            assert b["max_completion_tokens"] == 300 + LLM_REASONING_HEADROOM, \
                f"推論ぶんの上乗せNG: {b.get('max_completion_tokens')}"
            assert b.get("reasoning_effort") == LLM_REASONING_EFFORT, "reasoning_effort NG"
            assert b.get("response_format") == {"type": "json_object"}, "初回はJSONモードのはず"

            # json_validate_failed → JSONモードを外して再試行し、成功する
            seq = {"n": 0}

            def jvf_then_ok(req):
                seq["n"] += 1
                sent.append(json.loads(req.data))
                if seq["n"] == 1:
                    raise urllib.error.HTTPError(
                        req.full_url, 400, "", {},
                        io.BytesIO(b'{"error":{"code":"json_validate_failed",'
                                   b'"failed_generation":""}}'))
                return FakeResp(ok_payload)

            out = llm_chat([{"role": "user", "content": "hi"}],
                           limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now,
                                               sleeper=fc4.sleep),
                           opener=jvf_then_ok, sleeper=fc4.sleep)
            assert out == '{"ok":1}', f"JSONモード外しの再試行NG: {out}"
            assert "response_format" not in sent[-1], "2回目もJSONモードのままになっている"
            assert seq["n"] == 2, f"再試行回数NG: {seq['n']}"

            # reasoning_effort を受け付けないモデルなら、その項目を外して再試行する
            seq2 = {"n": 0}

            def effort_reject(req):
                seq2["n"] += 1
                sent.append(json.loads(req.data))
                if seq2["n"] == 1:
                    raise urllib.error.HTTPError(
                        req.full_url, 400, "", {},
                        io.BytesIO(b'{"error":{"message":"reasoning_effort is not supported"}}'))
                return FakeResp(ok_payload)

            llm_chat([{"role": "user", "content": "hi"}],
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=effort_reject, sleeper=fc4.sleep)
            assert "reasoning_effort" not in sent[-1], "reasoning_effortを外していない"

            # 非推論モデルなら上乗せも reasoning_effort も付けない
            globals()["LLM_MODEL"] = "some-plain-model"
            llm_chat([{"role": "user", "content": "hi"}], max_tokens=300,
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=body_capture, sleeper=fc4.sleep)
            assert sent[-1]["max_completion_tokens"] == 300, "非推論モデルに上乗せしている"
            assert "reasoning_effort" not in sent[-1], "非推論モデルにreasoning_effortを送った"
        finally:
            globals()["LLM_MODEL"] = saved_model

        # 出力トークンの実測がレート見積りに反映される（予算満額で見積もらない）
        _OUTPUT_EMA[0] = 400.0
        base_est = _observed_output()
        _record_output(1200)
        assert _observed_output() > base_est, "実測が見積りに反映されていない"
        _OUTPUT_EMA[0] = 400.0

        # ★回帰: est_items（バッチ件数）が大きいほどレート制限の見積りも大きくなる
        #   （バッチ処理時に見積りを1件ぶんのままにすると、TPM上限を過小評価して429が
        #   多発する。enrich()のバッチ呼び出しはest_items=バッチ件数を渡す）
        class _RecordingLimiter(RateLimiter):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.recorded = []

            def acquire(self, est_tokens):
                self.recorded.append(est_tokens)
                return super().acquire(est_tokens)

        rlim = _RecordingLimiter(rpm=1000, tpm=10 ** 9, clock=fc4.now, sleeper=fc4.sleep)
        llm_chat([{"role": "user", "content": "hi"}], limiter=rlim,
                 opener=capture_opener, sleeper=fc4.sleep, est_items=1)
        llm_chat([{"role": "user", "content": "hi"}], limiter=rlim,
                 opener=capture_opener, sleeper=fc4.sleep, est_items=8)
        assert rlim.recorded[-1] > rlim.recorded[-2], \
            f"est_itemsがレート見積りに反映されていない: {rlim.recorded}"

        # モデル一覧（GET /models）。UAが付くこと・IDだけを取り出すことを確認
        model_headers = {}

        def models_opener(req):
            model_headers.update({k.lower(): v for k, v in req.header_items()})
            return FakeResp({"object": "list", "data": [
                {"id": "openai/gpt-oss-120b", "object": "model"},
                {"id": "openai/gpt-oss-20b"}, {"object": "model"}]})

        ids = list_models(opener=models_opener)
        assert ids == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"], f"モデル一覧NG: {ids}"
        assert model_headers.get("user-agent"), "モデル一覧にUser-Agentが無い"

        # 400（モデル名ミス等）は即座に失敗させる＝無駄に待たない
        def bad_model(req):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                         io.BytesIO(b'{"error":{"message":"model not found"}}'))
        before = len(fc4.slept)
        try:
            llm_chat([{"role": "user", "content": "hi"}],
                     limiter=RateLimiter(rpm=100, tpm=100000, clock=fc4.now, sleeper=fc4.sleep),
                     opener=bad_model, sleeper=fc4.sleep)
            raise AssertionError("400なのに例外にならない")
        except RuntimeError as e:
            assert "400" in str(e), f"400の扱いNG: {e}"
        assert len(fc4.slept) == before, "400でリトライ待機している"
    finally:
        globals()["LLM_API_KEY"] = saved_key

    # APIキー未設定なら、通信せずに分かりやすく落とす
    globals()["LLM_API_KEY"] = ""
    try:
        llm_chat([{"role": "user", "content": "hi"}])
        raise AssertionError("キー未設定でも通信しようとした")
    except RuntimeError as e:
        assert "api_key" in str(e), f"未設定時のメッセージNG: {e}"
    finally:
        globals()["LLM_API_KEY"] = saved_key

    # ---- 時間範囲モード ------------------------------------------------------
    now_r = datetime(2026, 9, 24, 13, 18, tzinfo=JST).astimezone(timezone.utc)
    # (a) 引数なし＝当日 DEFAULT_RANGE_FROM〜DEFAULT_RANGE_TO（JST）
    st_, en_, lab = resolve_range(["x_collector.py"], now_r)
    assert st_.astimezone(JST).strftime("%m/%d %H:%M") == "09/24 00:00", f"既定の開始NG: {lab}"
    assert en_.astimezone(JST).strftime("%m/%d %H:%M") == "09/24 08:00", f"既定の終了NG: {lab}"
    # (b) --from/--to（スペース区切りと = の両方）
    st_, en_, _ = resolve_range(["x", "--from", "6:00", "--to=12:30"], now_r)
    assert (st_.astimezone(JST).hour, en_.astimezone(JST).hour,
            en_.astimezone(JST).minute) == (6, 12, 30), "--from/--to 解釈NG"
    # (c) 日付をまたぐ指定（to <= from なら翌日扱い）
    st_, en_, _ = resolve_range(["x", "--from", "22:00", "--to", "2:00"], now_r)
    assert (en_ - st_) == timedelta(hours=4), f"日跨ぎ範囲NG: {en_ - st_}"
    assert en_.astimezone(JST).day == 25, "日跨ぎの日付NG"
    # (d) 日付つき指定
    st_, en_, _ = resolve_range(["x", "--from", "2026/09/23 22:00", "--to", "09/24 02:00"], now_r)
    assert st_.astimezone(JST).strftime("%m/%d %H:%M") == "09/23 22:00", "日付つき指定NG"
    # (e) --window は従来どおり実行時刻から遡る
    st_, en_, lab = resolve_range(["x", "--window", "8"], now_r)
    assert (en_ - st_) == timedelta(hours=8) and en_ == now_r, f"--window NG: {lab}"
    st_, en_, _ = resolve_range(["x", "--window"], now_r)   # 値省略＝WINDOW_HOURS
    assert (en_ - st_) == timedelta(hours=WINDOW_HOURS), "--window 既定値NG"
    # (f) 書式ミスはValueError（トレースバックではなくメッセージで落とす）
    try:
        resolve_range(["x", "--from", "8時"], now_r)
        raise AssertionError("不正な時刻書式が素通りした")
    except ValueError:
        pass

    # (g) 範囲の上端より新しい投稿は配信対象にしない。高水位も範囲内の最大IDに留める
    #     （まだ配信していない新しい投稿を「送信済み」にしてしまわないため）
    def node_at(rid, jst_str):
        t = datetime.strptime(jst_str, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
        return _node(rid, "acc", t.astimezone(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y"),
                     f"t{rid}")
    raw_r = extract_all(_payload([
        node_at("3000", "2026/09/24 13:00"),   # 範囲より新しい（ページ送りで拾うが対象外）
        node_at("2000", "2026/09/24 07:30"),   # 範囲内
        node_at("1000", "2026/09/24 01:00"),   # 範囲内
        node_at("500",  "2026/09/23 23:00"),   # 範囲より古い
    ]))
    st_, en_, _ = resolve_range(["x"], now_r)
    emit_r, high_r, stats_r = filter_originals(raw_r, "acc", None, st_, en_)
    assert [t["id"] for t in emit_r] == ["1000", "2000"], f"範囲フィルタNG: {emit_r}"
    assert high_r == 2000, f"高水位が範囲外まで進んだ: {high_r}"
    assert stats_r["base"] == 2, f"範囲内件数NG: {stats_r['base']}"
    # 同じ範囲で再実行しても0件（差分除外は従来どおり効く）
    emit_r2, _, _ = filter_originals(raw_r, "acc", high_r, st_, en_)
    assert emit_r2 == [], "範囲モードの冪等性NG"

    # ゲストトークンが取れていない場合、ゲスト実行の垢はFetchErrorで隔離される
    #（run側のtryで受けて他の垢へ進む＝初回実行が何も残さず落ちるのを防ぐ）
    try:
        headers_for("someone_else", None)
        raise AssertionError("gt=None でも例外にならない（全体が落ちる経路が残る）")
    except FetchError as e:
        assert e.step == "guest_token", f"例外の種類NG: {e.step}"
    hdr, mode = headers_for("someone_else", "GT123")
    assert mode == "guest" and hdr.get("x-guest-token") == "GT123", "ゲストヘッダNG"

    print("[SELFTEST OK] 分類・差分・初回窓・冪等性・保存・翻訳/ラベル・正規化・保持期間削除・"
          "スキーマ移行・軸の増減・時間範囲・LLMレート制限/リトライ・HTML・config すべて通過")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--check-viewer" in sys.argv:
        # db_viewerが表示されないときの切り分け。DB・news_data.js・htmlの3点を見る
        #（サーバを廃止したので、ポート云々の確認は不要になった）。
        viewer_html = os.path.join(BASE_DIR, "web", "db_viewer.html")
        print(f"[*] BASE_DIR: {BASE_DIR}")
        for path in (STORE_PATH, VIEWER_DATA_PATH, viewer_html):
            print(f"    {'OK ' if os.path.exists(path) else 'なし'} {path}"
                  f" {os.path.getsize(path) // 1024 if os.path.exists(path) else 0}KB")
        if os.path.exists(STORE_PATH):
            c = sqlite3.connect(STORE_PATH)
            n = c.execute("SELECT COUNT(*) FROM news_log").fetchone()[0]
            newest = c.execute("SELECT MAX(created_at) FROM news_log").fetchone()[0]
            print(f"[*] news_log.db: {n} 件（最新 {newest}）")
            c.close()
        if os.path.exists(VIEWER_DATA_PATH):
            txt = open(VIEWER_DATA_PATH, encoding="utf-8").read()
            at = re.search(r'__NEWS_DATA_AT = "([^"]+)"', txt)
            try:
                m = re.search(r"__NEWS_ITEMS = (\[.*\]);", txt, re.S)
                items = json.loads(m.group(1)) if m else []
                print(f"[*] news_data.js: {len(items)} 件 / 生成 {at.group(1) if at else '不明'}"
                      f"  ← db_viewer.html が実際に表示するデータ")
            except Exception as e:
                print(f"[FAIL] news_data.js を読めません: {e}")
        print(f"[*] db_viewer.html は file:// で直接開いてください（サーバは使いません）: "
              f"file://{os.path.abspath(viewer_html)}")
        print("[*] 画面が空なら、絞り込み条件がブラウザに残っている可能性があります"
              "（db_viewer上部の『フィルタをクリア』）。")
    elif "--list-models" in sys.argv:
        # このAPIキーで使えるモデルIDを一覧する（config.ini の [llm] model に書く値）
        try:
            ids = list_models()
        except Exception as e:
            print(f"[FAIL] {e}")
            sys.exit(1)
        print(f"[*] 利用可能なモデル {len(ids)} 件:")
        for i in ids:
            mark = "  ← 現在の設定" if i == LLM_MODEL else ""
            print(f"    {i}{mark}")
        if LLM_MODEL not in ids:
            print(f"[WARN] 現在の設定 {LLM_MODEL} は上の一覧にありません。"
                  f"config.ini の [llm] model を一覧の中から選んでください。")
    elif "--test-llm" in sys.argv:
        # Groqに1回だけ投げて、キー・モデル名・疎通をまとめて確認する
        print(f"[*] 接続先: {LLM_BASE} / モデル: {LLM_MODEL}")
        print(f"[*] APIキー: {'設定あり(' + LLM_API_KEY[:6] + '…)' if LLM_API_KEY else '未設定'}"
              f" / レート設定: {LLM_RPM}req/分・{LLM_TPM}トークン/分")
        try:
            t0 = time.monotonic()
            out = llm_chat([{"role": "system", "content": ENRICH_SYS},
                            {"role": "user", "content": enrich_prompt(
                                "Fed holds rates steady, says inflation still elevated")}])
            print(f"[OK] {time.monotonic() - t0:.1f}秒で応答: {out[:200]}")
            got = parse_enrich(out, "orig")
            print(f"[OK] 解釈結果: 訳={got['translation'][:40]} / ラベル={got['labels']}")
        except Exception as e:
            print(f"[FAIL] {e}")
            if "404" in str(e):
                print("      → `python src\\x_collector.py --list-models` で使えるモデルを確認")
            sys.exit(1)
    elif "--prune-axes" in sys.argv:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = init_db(sqlite3.connect(STORE_PATH))
        print(f"[*] LABEL_AXES 外のラベルを削除: {prune_axes(_conn)} 行"
              f"（現在の軸: {'、'.join(LABEL_AXES)}）")
        _conn.close()
    else:
        # --collect-only: 収集＋保存のみ / --no-mail: 翻訳まで実施し送信のみ抑止
        # 時間帯は --from/--to（既定は当日 DEFAULT_RANGE_FROM〜DEFAULT_RANGE_TO）、
        # --window N を付けたときだけ「実行時刻から遡ってN時間」になる。
        try:
            run(do_enrich="--collect-only" not in sys.argv,
                do_mail="--collect-only" not in sys.argv and "--no-mail" not in sys.argv)
        except ValueError as e:            # 時刻書式のミスはトレースバックを出さない
            print(f"[FAIL] {e}")
            sys.exit(2)
