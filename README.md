# note → Instagram / Threads 自動投稿

note に新しい記事を公開すると、その告知文を Claude API で生成して
Instagram と Threads に自動投稿するツールです。

note への記事投稿は自動化しません。note は公式の投稿 API を提供していないため、
このツールが note に対して行うのは公式 RSS フィードの読み取りだけです。

```
[人が note に記事を投稿]
        ↓
[30分おきに RSS をポーリング]
        ↓
[未投稿の記事を検知]
        ↓
[Claude API で告知文を生成（Instagram 用 / Threads 用）]
        ↓
[アイキャッチ画像を選ぶ（手動指定 → 画像内テキストの意味マッチ → 既定画像）]
        ↓
[Instagram Graph API・Threads API に投稿]
        ↓
[posted_articles.json に記録（二重投稿を防止）]
```

## ファイル構成

```
note-sns-automation/
├── .env.example
├── requirements.txt
├── posted_articles.json          # 投稿済み記録（二重投稿防止）
├── image_index.json              # 画像から抽出した見出しテキストのキャッシュ
├── image_mapping_override.json   # 記事→画像の手動マッピング（AI判定より優先）
├── assets/
│   └── default_post_image.png    # 既定画像（差し替え前提のプレースホルダー）
├── src/
│   ├── check_rss.py              # RSSポーリング＆新着検知＆状態管理
│   ├── find_eyecatch.py          # アイキャッチ画像の探索（Vision で意味マッチ）
│   ├── generate_caption.py       # Claude API で投稿文生成
│   ├── post_instagram.py         # Instagram Graph API 投稿
│   ├── post_threads.py           # Threads API 投稿
│   └── main.py                   # エントリーポイント
└── .github/workflows/auto_post.yml
```

---

## 1. 事前に用意するもの

- note のアカウント（RSS が公開されていること: `https://note.com/{ユーザー名}/rss`）
- Instagram のプロアカウント（ビジネスまたはクリエイター）
- Instagram と連携済みの Facebook ページ
- Meta（Facebook）アカウント
- Anthropic の API キー（https://console.anthropic.com/ ）
- Python 3.11 以上

---

## 2. Meta Developer アカウントを作る

1. https://developers.facebook.com/ を開き、右上の「ログイン」から
   自分の Facebook アカウントでログインします。
2. 右上メニューの「マイアプリ」→「アプリを作成」を選びます。
3. ユースケースを聞かれたら「他のビジネス」または「その他」→ アプリタイプ
   「ビジネス」を選びます。
4. アプリ名（例: `note-sns-automation`）と連絡先メールを入力して作成します。
5. 作成後、左メニューの「アプリの設定 → ベーシック」で
   アプリ ID とアプリシークレットを確認できます（後で使います）。

---

## 3. Instagram をビジネスアカウントにして Facebook ページと連携する

Instagram Graph API は、Facebook ページに連携されたプロアカウントでしか使えません。

1. Instagram アプリ →「設定とプライバシー」→「アカウントの種類とツール」→
   「プロアカウントに切り替える」で、ビジネスまたはクリエイターを選びます。
2. Facebook 側で、投稿先にしたい Facebook ページを用意します（なければ新規作成）。
3. Instagram アプリの「設定」→「アカウントセンター」→「アカウント」から、
   手順2の Facebook ページを追加して連携します。
4. https://business.facebook.com/settings/ の「ビジネス設定」で、
   ページと Instagram アカウントの両方が同じビジネスに紐づいていることを確認します。
5. Meta Developer のアプリに「Instagram Graph API」と「Threads API」の
   プロダクトを追加します（左メニューの「製品を追加」から）。

---

## 4. アクセストークンの取得と更新

### 4-1. Instagram のトークン

1. https://developers.facebook.com/tools/explorer/ （Graph API Explorer）を開きます。
2. 右上の「Meta App」で手順2で作ったアプリを選びます。
3. 「User or Page」で「User Token」を選び、次の権限を追加します。
   - `instagram_basic`
   - `instagram_content_publish`
   - `pages_show_list`
   - `pages_read_engagement`
   - `business_management`
4. 「Generate Access Token」を押し、Facebook の同意画面を通します。
   ここで得られるのは短期トークン（約1時間）です。
5. 長期トークン（約60日）に交換します。ブラウザで次の URL を開くか curl で叩きます。

   ```bash
   curl -s "https://graph.facebook.com/v21.0/oauth/access_token?grant_type=fb_exchange_token&client_id=<アプリID>&client_secret=<アプリシークレット>&fb_exchange_token=<短期トークン>"
   ```

   返ってきた `access_token` が `IG_ACCESS_TOKEN` の値です。

6. Instagram ビジネスアカウント ID を調べます。

   ```bash
   curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=<長期トークン>"
   ```

   ページ ID が分かったら、そのページに紐づく Instagram アカウント ID を取得します。

   ```bash
   curl -s "https://graph.facebook.com/v21.0/<ページID>?fields=instagram_business_account&access_token=<長期トークン>"
   ```

   返ってきた `instagram_business_account.id` が `IG_BUSINESS_ACCOUNT_ID` です。

### 4-2. Threads のトークン

Threads API は Instagram とは別のトークン体系です（`graph.threads.net`）。

1. Meta Developer のアプリに「Threads API」プロダクトを追加します。
2. 「Threads API」→「設定」で、使用する Threads アカウントを Threads テスターとして
   追加し、Threads アプリ側（設定 → ウェブサイトの許可 / アプリ連携）で承認します。
3. 権限として `threads_basic` と `threads_content_publish` を有効にします。
4. Graph API Explorer もしくは「Threads API」→「Access Token」から
   短期トークンを発行します。
5. 長期トークン（約60日）に交換します。

   ```bash
   curl -s "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=<アプリシークレット>&access_token=<短期トークン>"
   ```

   返ってきた `access_token` が `THREADS_ACCESS_TOKEN` です。

6. Threads のユーザー ID を取得します。

   ```bash
   curl -s "https://graph.threads.net/v1.0/me?fields=id,username&access_token=<長期トークン>"
   ```

   返ってきた `id` が `THREADS_USER_ID` です。

### 4-3. トークンの期限に注意

長期トークンでも有効期限は約60日です。期限が切れると投稿に失敗し、
ログに「アクセストークンが無効か期限切れの可能性があります」と出ます。
2か月に一度、上記の手順でトークンを再発行して更新してください。

なお、長期トークンは有効期限内に一度でも API を呼べば自動で延長されます
（このツールは30分おきに動くため、通常は延長され続けます）。

---

## 5. アイキャッチ画像の探索（画像の中身で選ぶ）

記事に合うアイキャッチ画像を、**ファイル名ではなく画像の中身**で選びます。
ファイル名は UUID・日付スラッグ・自動生成名などが混在していて当てにならないためです。

### 選定の流れ

```
[1] image_mapping_override.json に手動指定があるか？
        ├─ ある → その画像を使う（AI判定より優先）
        └─ ない ↓
[2] ローカルフォルダ + Googleドライブから画像を集める
        ↓
[3] 未スキャンの画像だけ Claude Vision で読み、
    画像内の見出しテキストを image_index.json にキャッシュ（差分更新）
        ↓
[4] 記事タイトル + キャッシュの内容を Claude に渡し、
    意味的にいちばん近い画像を1つ選ばせる
        ├─ 選ばれた → その画像を使う
        └─ 該当なし（null）→ 既定画像 assets/default_post_image.png
```

選ばれた画像パスと、その根拠（抽出された見出しテキスト・選定理由）は
必ずログに出力されます。

```
INFO アイキャッチ画像を決定: AI判定 / a1b2c3d4.png（C:\images\a1b2c3d4.png）
INFO   画像パス  : C:\images\a1b2c3d4.png
INFO   抽出見出し: 「毎日続けるための仕組みづくり」
INFO   選定理由  : 記事タイトルの「習慣化」と画像の見出しが同じテーマのため
```

### 画像インデックス（image_index.json）

`{ "画像ID": "画像から抽出した見出しテキスト" }` の形でキャッシュされます。

```json
{
  "C:\\images\\a1b2c3d4.png": "毎日続けるための仕組みづくり",
  "gdrive:1AbCdEfGhIjKlMnOpQrStUvWxYz": "note運用のはじめかた"
}
```

- 画像IDは、ローカル画像はファイルパス、Googleドライブ画像は `gdrive:{ファイルID}`
- すでにキャッシュにある画像は再スキャンしません（Vision API のコスト削減）
- 元画像が消えたエントリは自動的に削除されます

インデックスの更新だけを先に走らせることもできます。

```bash
python -m src.find_eyecatch --reindex
```

### 手動マッピング（image_mapping_override.json）

AI 判定を上書きしたいときは、記事URL（またはタイトル）をキーに画像を指定します。

```json
{
  "https://note.com/example/n/n0000": "C:\\images\\eyecatch_a.png",
  "はじめてのnote運用": "gdrive:1AbCdEfGhIjKlMnOpQrStUvWxYz"
}
```

値にはローカルの画像パス、ファイル名、`gdrive:{ファイルID}` のいずれかを書けます。
アンダースコアで始まるキー（`_comment` など）は無視されるので、メモに使えます。

### 動作確認

```bash
python -m src.find_eyecatch "記事タイトル" "https://note.com/example/n/n0000"
```

---

## 6. 画像の公開URLについて（重要）

Instagram Graph API は **テキストのみの投稿に対応していません**。必ず画像が必要です。
さらに、画像はローカルファイルを直接アップロードできず、
**インターネットから到達できる公開 URL** を渡す必要があります。

そのため、選ばれた画像は次の順で「公開URL」に解決されます。

1. Googleドライブの画像 → `https://drive.google.com/uc?export=view&id={ファイルID}`
   （フォルダまたはファイルが「リンクを知っている全員が閲覧可」である必要があります）
2. ローカルの画像 → `IMAGE_PUBLIC_BASE_URL` + `/` + ファイル名
3. どちらも解決できない場合 → `IG_IMAGE_URL`（既定画像）

### 既定画像の準備

1. `assets/default_post_image.png` を、自分のブランド画像に差し替えます
   （正方形 1080×1080 px、JPEG または PNG を推奨）。
2. その画像を公開 URL に置きます。いちばん簡単なのは、このリポジトリを
   GitHub の公開リポジトリにして raw URL を使う方法です。

   ```
   https://raw.githubusercontent.com/<ユーザー名>/<リポジトリ名>/main/assets/default_post_image.png
   ```

3. その URL を `IG_IMAGE_URL` に設定します。

### ローカル画像を投稿に使いたい場合

ローカルのフォルダにしかない画像は、そのままでは Instagram に投稿できません。
同じ画像を公開できる場所（GitHub の公開リポジトリ、S3、Cloudflare R2、自分のサイトなど）
に置き、そのベースURLを `IMAGE_PUBLIC_BASE_URL` に設定してください。

```
IMAGE_PUBLIC_BASE_URL=https://raw.githubusercontent.com/<user>/<repo>/main/images
```

公開先を用意しない場合は、画像を Googleドライブ（リンク共有ON）に置くのが簡単です。

Threads はテキストのみで投稿できるため、画像は不要です。
画像も添付したい場合は `THREADS_ATTACH_IMAGE=true` を設定してください。

---

## 6-2. Googleドライブから画像を探す設定

### サービスアカウントを作る

1. https://console.cloud.google.com/ を開き、プロジェクトを作成します
   （既存のプロジェクトがあればそれでも構いません）。
2. 左メニューの「APIとサービス」→「ライブラリ」で `Google Drive API` を検索し、
   「有効にする」を押します。
3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「サービスアカウント」を選びます。
4. サービスアカウント名（例: `note-sns-automation`）を入力して作成します。
   ロールの指定は不要です（ドライブ側の共有設定で権限を渡すため）。
5. 作成したサービスアカウントを開き、「キー」タブ →「鍵を追加」→
   「新しい鍵を作成」→ 形式は `JSON` を選ぶと、JSON ファイルがダウンロードされます。
6. このサービスアカウントのメールアドレス
   （`xxxx@yyyy.iam.gserviceaccount.com` の形式）を控えておきます。次の手順で使います。

ダウンロードした JSON は秘密情報です。リポジトリにコミットしないでください
（`.gitignore` で `service_account.json` を除外済みです）。

### ドライブのフォルダを共有する

1. Google ドライブで、アイキャッチ画像を入れているフォルダを開きます。
2. フォルダを右クリック →「共有」を選びます。
3. 手順6で控えたサービスアカウントのメールアドレスを入力し、
   権限は「閲覧者」で追加します。
4. Instagram に投稿する画像として使う場合は、あわせて
   「一般的なアクセス」を「リンクを知っている全員」→「閲覧者」にしてください
   （公開URLで画像を取得できるようにするため）。

### フォルダIDの調べ方

フォルダを開いたときのブラウザのURLが次の形になっています。

```
https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz
```

`/folders/` の後ろの `1AbCdEfGhIjKlMnOpQrStUvWxYz` の部分がフォルダIDです。
これを `GDRIVE_FOLDER_ID` に設定します。

### 環境変数の設定

```
GDRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz
GOOGLE_SERVICE_ACCOUNT_JSON=./service_account.json
```

`GOOGLE_SERVICE_ACCOUNT_JSON` には、JSON ファイルのパスでも、
JSON の中身そのもの（1行に貼り付けたもの）でも構いません。
GitHub Actions で使う場合は、JSON の中身をそのまま Secret に貼り付けてください。

ドライブの画像は `.cache/gdrive_images/` にダウンロードしてから読み取ります
（このフォルダは `.gitignore` 済みで、2回目以降は再ダウンロードしません）。

---

## 7. ローカルでのセットアップとテスト

```bash
git clone <このリポジトリ>
cd note-sns-automation
python -m venv .venv
```

仮想環境を有効化します。

```bash
source .venv/bin/activate
```

Windows（PowerShell）の場合:

```bash
.venv\Scripts\Activate.ps1
```

依存ライブラリを入れ、環境変数ファイルを用意します。

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

`.env` を開いて各値を埋めてください。`.env` は `.gitignore` 済みで、
コミットされません。

### まず投稿せずに動作確認する

RSS が読めるかだけ確認します。

```bash
python -m src.check_rss
```

アイキャッチ画像の探索を試します（初回は画像の読み取りに少し時間がかかります）。

```bash
python -m src.find_eyecatch "テスト記事のタイトル"
```

投稿文の生成だけ試します。

```bash
python -m src.generate_caption "テスト記事のタイトル" "https://note.com/example/n/n0000"
```

全体を、実際には投稿しない DRY RUN で動かします。

```bash
python -m src.main --dry-run
```

### 本番実行

1件だけ投稿して様子を見るのがおすすめです。

```bash
python -m src.main --limit 1
```

問題なければ、そのまま実行します。

```bash
python -m src.main
```

### よく使うオプション

- `--dry-run` 投稿せず、生成した文面だけ表示する
- `--limit N` 1回の実行で処理する記事数の上限（`0` で無制限）
- `--variant N` 生成された3案のうち何番目を使うか（0/1/2）
- `--clear-cache` dry-run 用の API キャッシュを消してから実行する
- `--verbose` デバッグログも出す

### dry-run 中の API キャッシュ

テストのたびに課金されるのを防ぐため、`--dry-run` のときは Claude API の
呼び出し結果を `.dryrun_cache.json` に保存します。同じ記事・同じ画像に対しては
2回目以降 API を呼ばず、キャッシュを返します。

キャッシュされるのは次の3種類です。

- 投稿文の生成（キー: モデル + プロンプト + 記事タイトル・URL）
- 画像の選定（キー: モデル + プロンプト + 記事タイトル + 候補一覧）
- 画像の読み取り（キー: モデル + プロンプト + 画像内容のハッシュ）

キャッシュが効くと、ログに次のように出ます。

```
INFO DRY_RUN キャッシュを使用（API呼び出しなし）: 投稿文の生成（記事タイトル）
```

プロンプトを書き換えた場合はキーが変わるので、自動的に新しい結果が取り直されます。
文面を作り直したいときは `--clear-cache` を付けて実行してください。

本番実行（`--dry-run` なし）では、このキャッシュは一切使われません。
必ず実際の API を呼び、毎回新しい文面が生成されます。

なお、画像から抽出した見出しは `image_index.json` にも永続化されており、
こちらは本番実行でも効きます（同じ画像を二度読み取ることはありません）。

ログは標準出力と `logs/auto_post.log` の両方に出ます。

---

## 8. GitHub Actions で自動実行する

1. このプロジェクトを GitHub リポジトリにプッシュします。
2. リポジトリの「Settings」→「Secrets and variables」→「Actions」を開きます。
3. 「New repository secret」から、次の7つを登録します。

   | Secret 名 | 値 |
   | --- | --- |
   | `NOTE_RSS_URL` | `https://note.com/{ユーザー名}/rss` |
   | `ANTHROPIC_API_KEY` | Anthropic のコンソールで発行した API キー |
   | `IG_ACCESS_TOKEN` | Instagram の長期アクセストークン |
   | `IG_BUSINESS_ACCOUNT_ID` | Instagram ビジネスアカウント ID |
   | `IG_IMAGE_URL` | 既定画像の公開 URL |
   | `THREADS_ACCESS_TOKEN` | Threads の長期アクセストークン |
   | `THREADS_USER_ID` | Threads のユーザー ID |

   アイキャッチ画像を Googleドライブやローカルから探す場合は、次も登録します。

   | Secret 名 | 値 |
   | --- | --- |
   | `GDRIVE_FOLDER_ID` | 画像フォルダのフォルダ ID |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | サービスアカウントの JSON の中身をそのまま貼る |
   | `IMAGE_PUBLIC_BASE_URL` | ローカル画像を公開している場所のベース URL（使う場合のみ） |

   リポジトリ内の画像フォルダを使う場合は、Secret ではなく
   「Variables」タブに `LOCAL_IMAGE_FOLDER`（例: `images`）を登録します。

4. 「Settings」→「Actions」→「General」→「Workflow permissions」で
   「Read and write permissions」を選んで保存します
   （`posted_articles.json` をリポジトリに書き戻すために必要です）。
5. 「Actions」タブから `auto_post` ワークフローを選び、「Run workflow」で
   手動実行して動作確認します。このとき `dry run` を有効にすると、
   投稿せずに文面だけ確認できます。
6. 以降は30分おきに自動で実行されます。

GitHub Actions の cron は混雑状況によって数分〜十数分遅れることがあります。
即時性が必要な用途には向きません。

---

## 9. 二重投稿の防止のしくみ

投稿の結果は `posted_articles.json` に記録されます。

```json
[
  {
    "link": "https://note.com/example/n/n0000",
    "title": "記事タイトル",
    "published": "Tue, 05 Aug 2026 09:00:00 +0900",
    "instagram": {
      "status": "success",
      "posted_at": "2026-08-05T00:05:00+00:00",
      "post_id": "1789..."
    },
    "threads": {
      "status": "failed",
      "error": "HTTP 400 code=190: ...",
      "last_attempted_at": "2026-08-05T00:05:00+00:00"
    }
  }
]
```

プラットフォームごとに状態を持つため、上の例のように Instagram だけ成功した場合、
次回の実行では Threads にだけ再投稿を試みます。Instagram には再投稿しません。

投稿のたびに即座に保存されるので、途中でプロセスが落ちても二重投稿にはなりません。

手動で投稿済みにしたい記事がある場合は、このファイルに
`{"link": "...", "instagram": {"status": "success"}, "threads": {"status": "success"}}`
を追記してください。

---

## 10. 生成される投稿文について

有料記事の場合、RSS には本文が含まれません（タイトルとリンクのみ）。
そのため投稿文は「新しい記事を公開しました」という告知に徹し、
本文の内容を推測して書かないようプロンプトで制約しています。

トーンはプラットフォームごとに変えています。

- Instagram: やや丁寧・落ち着いた語り口
- Threads: ややカジュアル・短めのテンポ

文面が気に入らない場合は `src/generate_caption.py` の `SYSTEM_PROMPT` を
編集してください。3案が生成され、既定では1案目が使われます
（`CAPTION_VARIANT_INDEX` または `--variant` で変更可能）。

Claude API がエラーになった場合は、テンプレートによる最低限の告知文が使われます
（投稿自体は止まりません）。

---

## 11. スコープ外（やらないこと）

- note への記事投稿の自動化
- Instagram / Threads のプロフィール（bio・名前欄）の自動編集
- コメント・DM への自動返信
- 画像・動画コンテンツの自動生成

---

## 12. トラブルシューティング

| 症状 | 確認すること |
| --- | --- |
| `NOTE_RSS_URL が設定されていません` | `.env` を作ったか、GitHub Secrets を登録したか |
| Instagram で code=190 | トークンの期限切れ。手順4でトークンを再発行する |
| Instagram で code=100 | `IG_BUSINESS_ACCOUNT_ID` と `IG_IMAGE_URL` を確認。画像 URL がブラウザのシークレットウィンドウで開けるか試す |
| Instagram で code=200 | 権限不足。`instagram_content_publish` が付いているか確認 |
| Threads で code=200 | `threads_content_publish` が付いているか、テスターとして承認済みか確認 |
| 何も投稿されない | `posted_articles.json` にすでに記録されていないか確認 |
| 画像がいつも既定画像になる | `python -m src.find_eyecatch "タイトル"` を実行し、候補が何件見つかっているかログで確認。0件なら `LOCAL_IMAGE_FOLDER` / `GDRIVE_FOLDER_ID` の設定を見直す |
| ドライブの画像が0件 | フォルダがサービスアカウントのメールアドレスに共有されているか確認 |
| 画像の見出しが読み取れない | `image_index.json` から該当エントリを削除して再実行すると再スキャンされる |
| 意図と違う画像が選ばれる | `image_mapping_override.json` に記事URLと画像パスを書いて手動指定する |
| 同じ記事が二重に投稿された | `posted_articles.json` が GitHub に書き戻されているか（Workflow permissions）を確認 |
