"""Claude API で note 記事の告知文（Instagram / Threads 用）を生成する。

RSS の要約だけではタイトルの言い換えにしかならないため、note の記事
ページから本文を取得して材料にする（有料記事は無料部分のみ）。
本文を取得できなかった場合は、従来どおり要約とタイトルだけで書く。

単体実行:
    python -m src.generate_caption "記事タイトル" "https://note.com/xxx/n/n123"
"""

from __future__ import annotations

import json
import logging
import html as html_lib
import os
import re
from dataclasses import dataclass

import anthropic

from src.check_rss import Article
from src.fetch_body import ArticleBody, fetch_body
from src.dryrun_cache import cached_call, make_key

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"
# thinking ブロックの分も含めて余裕を持たせる（足りないと本文が空で返る）
MAX_TOKENS = 4000

SYSTEM_PROMPT = """\
あなたは note で発信しているクリエイターの SNS 運用担当です。
note に公開された記事の「告知文」を日本語で書きます。

書き方:
- 読み手が「これ、うちのことだ」と感じる一文から入ってください。
  投稿を見た人が置かれている状況を、問いかけや情景として短く描きます。
- そのうえで記事がその話を扱っていることを伝え、最後に読んでもらう一言を添えます。
- タイトルをそのまま引用符で貼り付けただけの文にしないでください。
  「新しい記事を公開しました。「〇〇」」のような機械的な形は避けます。
- 結びの言い回しは案ごとに変えてください（「続きはnoteに書きました」
  「くわしくはnoteで」「よければnoteをのぞいてみてください」など）。
- ただし instagram は例外です。Instagram はキャプション内の URL が
  リンクにならないため、結びは必ず「プロフィールのリンクから読めます」
  「詳しくはプロフィールのリンクから」など、プロフィール欄へ誘導する
  言い回しにしてください。「リンクをタップ」「URLをクリック」のように
  押せる前提の表現は使わないでください。
  URL は検索の手がかりとして、そのまま末尾に残します。

厳守事項:
- 与えられた材料（タイトル・記事本文・見出し・画像のテキスト）に書かれて
  いることだけを使ってください。数値、体験談、効果の約束などを、材料の
  外から補って書いてはいけません。
- 記事本文が与えられている場合は、その記事が実際に扱っている要点を投稿文に
  反映してください。タイトルを言い換えただけの文にしないこと。読んだ人が
  「何について書かれた記事か」を具体的に受け取れる状態にします。
- 記事本文が与えられていない場合は、タイトルから読み取れる範囲にとどめ、
  それ以外は問いかけの形にしてください。
- 煽り表現（「絶対」「必見」「9割の人が知らない」など）は使わないでください。
- 絵文字は0〜1個。ハッシュタグを使うなら2〜3個まで。
- 各案は本文140字以内（末尾に付けるURLは字数に含めない）。
- 末尾は誘導の一文＋記事URL で締めてください。

トーンの違い:
- instagram: やや丁寧・落ち着いた語り口。ですます調。
- threads: ややカジュアル。話しかけるような、短めのテンポ。

3案は切り口を変えてください（問いかけ型・状況描写型・要点提示型など）。

出力は次の JSON のみ。前後に説明文やコードフェンスを付けないこと。
{"instagram": ["案1", "案2", "案3"], "threads": ["案1", "案2", "案3"]}
"""

USER_PROMPT_TEMPLATE = """\
以下の note 記事の投稿文を、Instagram 用3案・Threads 用3案つくってください。

タイトル: {title}
URL: {link}
公開日: {published}
"""

SUMMARY_TEMPLATE = """
記事の冒頭（note の RSS から取得した要約）です。

    {summary}

これは本文ではなく、冒頭の数行だけです。記事の主題とは限りません。

投稿文は、この要約に書かれている内容の範囲で書いてください。
タイトルから想像した場面や、要約にない具体例を足さないでください。

ただし、冒頭の一場面（登場人物のセリフ、特定の出来事など）を投稿文の
主題に据えないでください。記事が何を扱っているかはタイトルの方に表れて
います。冒頭は導入として軽く触れる程度にとどめ、タイトルが示すテーマを
中心に書いてください。
"""

BODY_TEMPLATE = """
記事の本文です{partial_note}。

    {body}
"""

HEADINGS_TEMPLATE = """
記事中の見出しです。記事の要点がそのまま並んでいます。
投稿文は、この中のどれか一つに絞って書くと要点が伝わります。

{headings}
"""

IMAGE_CONTEXT_TEMPLATE = """
この投稿に添える画像には、次のテキストが入っています。

    {heading}

投稿文は、この画像の切り口・言葉づかいに寄せてください。画像と投稿文が
ちぐはぐにならないようにします。タイトルの言葉をそのまま繰り返すより、
画像が投げかけている問いに沿って書く方が自然になります。
ただし、画像とタイトルから読み取れない事実は足さないでください。
"""

HINT_TEMPLATE = """
運用者からの追加指示です。他の指示と矛盾する場合はこちらを優先してください。

    {hint}
"""


@dataclass
class Captions:
    """プラットフォームごとの投稿文の候補（各3案）。"""

    instagram: list[str]
    threads: list[str]

    def pick(self, platform: str, index: int = 0) -> str:
        variants = getattr(self, platform)
        if not variants:
            raise ValueError(f"{platform} の投稿文候補が空です。")
        return variants[index % len(variants)]


def _fallback_captions(article: Article) -> Captions:
    """Claude API が使えないときの、テンプレートによる最低限の告知文。"""
    logger.warning("Claude API を使えなかったため、テンプレート文で代替します。")
    ig = (
        f"新しい記事を公開しました。\n"
        f"「{article.title}」\n"
        f"詳しくはnoteでご覧ください。\n{article.link}"
    )
    th = (
        f"noteに新しい記事を書きました。\n"
        f"「{article.title}」\n"
        f"詳しくはnoteで。\n{article.link}"
    )
    return Captions(instagram=[ig], threads=[th])


def _extract_json(text: str) -> dict:
    """モデル出力から JSON 部分を取り出す。"""
    text = text.strip()
    # ```json ... ``` で囲まれている場合を剥がす
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        braced = re.search(r"\{.*\}", text, re.DOTALL)
        if braced:
            text = braced.group(0)
    return json.loads(text)


def _plain_text(html: str) -> str:
    """RSS の要約は HTML のため、タグと余分な空白を落として渡す。"""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = html_lib.unescape(text)
    # 末尾の「続きをみる」など、本文ではない導線は落とす
    text = re.sub(r"続きをみる\s*$", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def generate_captions(
    article: Article,
    image_heading: str = "",
    caption_hint: str = "",
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
    body: ArticleBody | None = None,
) -> Captions:
    """記事1件分の投稿文候補を生成する。失敗してもテンプレート文を返す。

    image_heading にアイキャッチ画像から読み取ったテキストを渡すと、
    投稿文をその切り口に寄せる。caption_hint は運用者からの手動指示。
    body を渡さない場合は note から本文を取得する。取得できなければ
    RSS の要約だけで生成する。
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key and client is None:
        logger.error("ANTHROPIC_API_KEY が設定されていません。")
        return _fallback_captions(article)

    client = client or anthropic.Anthropic(api_key=api_key)
    model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL

    prompt = USER_PROMPT_TEMPLATE.format(
        title=article.title,
        link=article.link,
        published=article.published or "（不明）",
    )
    if body is None:
        body = fetch_body(article.link)

    if body:
        prompt += BODY_TEMPLATE.format(
            body=body.text,
            partial_note="（有料記事のため無料部分のみ）" if body.is_paid else "",
        )
        if body.headings:
            prompt += HEADINGS_TEMPLATE.format(
                headings="\n".join(f"    - {h}" for h in body.headings)
            )
    else:
        # 本文を取れなかったときだけ、RSS の要約に頼る
        summary = _plain_text(article.summary)
        if summary:
            # 長すぎると要点がぼやけるため、冒頭のみを渡す
            prompt += SUMMARY_TEMPLATE.format(summary=summary[:1200])
    if image_heading:
        prompt += IMAGE_CONTEXT_TEMPLATE.format(heading=image_heading)
    if caption_hint:
        prompt += HINT_TEMPLATE.format(hint=caption_hint)

    def call_api() -> str | None:
        # 応答に thinking ブロックが入ると、出力上限が小さいときに本文が
        # 空のまま打ち切られることがある。余裕を持たせ、空なら1度やり直す。
        for attempt in (1, 2):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                )
            except anthropic.APIError as exc:
                logger.error("Claude API の呼び出しに失敗しました: %s", exc)
                return None

            text = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", "") == "text"
            )
            if text.strip():
                return text
            logger.warning(
                "投稿文の応答が空でした（stop_reason=%s, %d回目）。",
                response.stop_reason,
                attempt,
            )
        return None

    # dry-run 中は、同じ記事なら再度 API を呼ばずキャッシュを使う
    text = cached_call(
        make_key("caption", model, SYSTEM_PROMPT, prompt),
        f"投稿文の生成（{article.title}）",
        call_api,
    )
    if text is None:
        return _fallback_captions(article)

    try:
        data = _extract_json(text)
        instagram = [str(s).strip() for s in data.get("instagram", []) if str(s).strip()]
        threads = [str(s).strip() for s in data.get("threads", []) if str(s).strip()]
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.error("Claude の出力を JSON として解釈できませんでした: %s", exc)
        logger.debug("生の出力: %s", text)
        return _fallback_captions(article)

    if not instagram or not threads:
        logger.error("Claude の出力に投稿文候補が含まれていませんでした。")
        return _fallback_captions(article)

    # モデルが URL を落とした場合に備えて、必ずリンクが入るようにする
    instagram = [c if article.link in c else f"{c}\n{article.link}" for c in instagram]
    threads = [c if article.link in c else f"{c}\n{article.link}" for c in threads]

    logger.info(
        "投稿文を生成しました（Instagram %d案 / Threads %d案）",
        len(instagram),
        len(threads),
    )
    return Captions(instagram=instagram, threads=threads)


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 3:
        print('使い方: python -m src.generate_caption "タイトル" "URL"')
        raise SystemExit(1)

    captions = generate_captions(Article(title=sys.argv[1], link=sys.argv[2]))
    for platform in ("instagram", "threads"):
        print(f"\n=== {platform} ===")
        for i, caption in enumerate(getattr(captions, platform)):
            print(f"--- 案{i + 1} ---\n{caption}")
