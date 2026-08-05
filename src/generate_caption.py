"""Claude API で note 記事の告知文（Instagram / Threads 用）を生成する。

有料記事だと RSS に本文が含まれないため、「本文の中身」は推測せず、
あくまで「新しい記事を公開しました」という告知文を作る。

単体実行:
    python -m src.generate_caption "記事タイトル" "https://note.com/xxx/n/n123"
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import anthropic

from src.check_rss import Article
from src.dryrun_cache import cached_call, make_key

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
あなたは note で発信しているクリエイターの SNS 運用担当です。
note に公開された記事の「告知文」を日本語で書きます。

厳守事項:
- 記事本文の内容は与えられません。中身を推測して書かないでください。
  「新しい記事を公開しました」という告知に徹してください。
- 断定的な要約、数値、体験談などを捏造しないでください。
- 絵文字は控えめに（1投稿につき0〜2個まで）。
- ハッシュタグの羅列はしないでください（使うなら2〜3個まで）。
- 各案は本文140字以内（末尾に付けるURLは字数に含めない）。
- 末尾は「詳しくはnoteで」に相当する一文＋記事URL で締めてください。

トーンの違い:
- instagram: やや丁寧・落ち着いた語り口。ですます調。
- threads: ややカジュアル。話しかけるような、短めのテンポ。

出力は次の JSON のみ。前後に説明文やコードフェンスを付けないこと。
{"instagram": ["案1", "案2", "案3"], "threads": ["案1", "案2", "案3"]}
"""

USER_PROMPT_TEMPLATE = """\
以下の note 記事の告知文を、Instagram 用3案・Threads 用3案つくってください。

タイトル: {title}
URL: {link}
公開日: {published}
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


def generate_captions(
    article: Article,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> Captions:
    """記事1件分の投稿文候補を生成する。失敗してもテンプレート文を返す。"""
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

    def call_api() -> str | None:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            logger.error("Claude API の呼び出しに失敗しました: %s", exc)
            return None
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )

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
