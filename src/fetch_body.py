"""note の記事ページから本文を取得する。

RSS の要約だけでは投稿文が「タイトルの言い換え」になり、記事の要点を
押さえられないため、公開されている記事本文を投稿文生成の材料にする。

有料記事では無料部分しか取得できない。取得できた範囲を材料とし、
それ以上を推測させないことは generate_caption 側の役割。

単体実行:
    python -m src.fetch_body "https://note.com/xxx/n/n123"
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://note.com/api/v3/notes/{key}"
# note 側に負荷をかけないよう、1記事1回だけ・短いタイムアウトで取りに行く
TIMEOUT_SECONDS = 20
USER_AGENT = "Mozilla/5.0 (compatible; note-sns-automation/1.0)"

# 本文が長いとモデルが要点を拾いにくくなるため、冒頭のみを材料にする
BODY_CHAR_LIMIT = 2500
MAX_HEADINGS = 8


@dataclass
class ArticleBody:
    """記事本文のうち、投稿文の材料として使える範囲。"""

    text: str = ""
    headings: list[str] = field(default_factory=list)
    is_paid: bool = False
    # 有料記事で、無料部分しか取得できていないか
    is_partial: bool = False

    def __bool__(self) -> bool:
        return bool(self.text)


def extract_key(link: str) -> str:
    """記事URLから note の記事キー（n から始まる識別子）を取り出す。"""
    match = re.search(r"/n/(n[0-9a-zA-Z]+)", link)
    return match.group(1) if match else ""


def _headings_from_html(body_html: str) -> list[str]:
    """本文中の見出しを抜き出す。記事の要点がそのまま並んでいることが多い。"""
    found = re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", body_html, re.DOTALL)
    headings: list[str] = []
    for raw in found:
        text = _plain_text(raw)
        if text and text not in headings:
            headings.append(text)
    return headings[:MAX_HEADINGS]


def _plain_text(body_html: str) -> str:
    """HTML からタグを落とす。段落の切れ目は改行として残す。"""
    text = re.sub(r"(?i)<br\s*/?>", "\n", body_html)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def fetch_body(link: str, session: requests.Session | None = None) -> ArticleBody:
    """記事本文を取得する。失敗しても例外は投げず、空の ArticleBody を返す。

    取得できなかった場合、呼び出し側は従来どおり RSS の要約だけで
    投稿文を作る。自動投稿そのものを止めないことを優先する。
    """
    key = extract_key(link)
    if not key:
        logger.warning("記事URLから note の記事キーを取り出せませんでした: %s", link)
        return ArticleBody()

    getter = session.get if session else requests.get
    try:
        response = getter(
            API_TEMPLATE.format(key=key),
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
    except (requests.RequestException, ValueError) as exc:
        logger.warning("記事本文を取得できませんでした（%s）: %s", link, exc)
        return ArticleBody()

    body_html = data.get("body") or ""
    if not body_html:
        logger.warning("記事本文が空でした: %s", link)
        return ArticleBody()

    is_paid = bool(data.get("price"))
    text = _plain_text(body_html)
    truncated = len(text) > BODY_CHAR_LIMIT

    logger.info(
        "記事本文を取得しました（%d文字%s%s）",
        len(text),
        "／有料記事の無料部分" if is_paid else "",
        "／冒頭のみ使用" if truncated else "",
    )
    return ArticleBody(
        text=text[:BODY_CHAR_LIMIT],
        headings=_headings_from_html(body_html),
        is_paid=is_paid,
        is_partial=is_paid or truncated,
    )


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = fetch_body(sys.argv[1])
    print(f"有料: {result.is_paid} / 部分取得: {result.is_partial}")
    print("見出し:")
    for heading in result.headings:
        print(f"  - {heading}")
    print("\n本文:")
    print(result.text[:600])
