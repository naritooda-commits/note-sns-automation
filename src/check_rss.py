"""note の RSS フィードをポーリングして、未投稿の記事を検知する。

note には公式の投稿 API が存在しないため、このプロジェクトでは
「RSS フィードの読み取り」だけを行う。note への投稿は自動化しない。

単体実行:
    python -m src.check_rss
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import feedparser

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = REPO_ROOT / "posted_articles.json"

PLATFORMS = ("instagram", "threads")


@dataclass
class Article:
    """RSS から取り出した1記事分の情報。"""

    title: str
    link: str
    published: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "link": self.link,
            "published": self.published,
        }


@dataclass
class PostResult:
    """1プラットフォームへの投稿結果。PostedStore にそのまま記録される。"""

    platform: str
    ok: bool
    post_id: str | None = None
    error: str | None = None
    skipped: bool = False


@dataclass
class PostedStore:
    """投稿済み記事の永続化を担当する。

    posted_articles.json のフォーマットは次の2種類を受け付ける。

    1. 文字列の配列（旧フォーマット / 初期状態）
       ["https://note.com/xxx/n/n123", ...]
    2. オブジェクトの配列（本ツールが書き出すフォーマット）
       [{"link": "...", "title": "...", "published": "...",
         "instagram": {"status": "success", "posted_at": "...", "post_id": "..."},
         "threads":   {"status": "failed",  "error": "..."}}]

    プラットフォームごとに状態を持つことで、Instagram だけ成功して
    Threads が失敗した場合に、次回実行で Threads だけ再試行できる。
    """

    path: Path = DEFAULT_STATE_PATH
    records: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PostedStore":
        state_path = Path(path) if path else DEFAULT_STATE_PATH
        if not state_path.is_absolute():
            state_path = REPO_ROOT / state_path

        records: dict[str, dict[str, Any]] = {}
        if state_path.exists():
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8") or "[]")
            except json.JSONDecodeError:
                logger.error(
                    "%s が壊れています。空のリストとして扱います。"
                    "（手動で中身を確認してください）",
                    state_path,
                )
                raw = []
            for entry in raw:
                if isinstance(entry, str):
                    # 旧フォーマット: URL だけ。両プラットフォーム投稿済みとみなす。
                    records[entry] = {
                        "link": entry,
                        "title": "",
                        "published": "",
                        "instagram": {"status": "success"},
                        "threads": {"status": "success"},
                    }
                elif isinstance(entry, dict) and entry.get("link"):
                    records[entry["link"]] = entry
        return cls(path=state_path, records=records)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = list(self.records.values())
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
        logger.debug("状態を保存しました: %s (%d件)", self.path, len(payload))

    def get(self, link: str) -> dict[str, Any] | None:
        return self.records.get(link)

    def is_done(self, link: str, platform: str) -> bool:
        """指定プラットフォームへの投稿が成功済みかどうか。"""
        record = self.records.get(link)
        if not record:
            return False
        return record.get(platform, {}).get("status") == "success"

    def is_fully_posted(self, link: str) -> bool:
        return all(self.is_done(link, platform) for platform in PLATFORMS)

    def record_result(
        self,
        article: Article,
        platform: str,
        status: str,
        **details: Any,
    ) -> None:
        """1プラットフォーム分の結果を記録する（成功・失敗どちらも）。"""
        record = self.records.setdefault(article.link, article.to_dict())
        # タイトルが空のまま引き継がれるのを防ぐ
        if article.title:
            record["title"] = article.title
        if article.published:
            record["published"] = article.published
        entry: dict[str, Any] = {"status": status}
        entry.update({k: v for k, v in details.items() if v is not None})
        record[platform] = entry


def fetch_new_articles(
    rss_url: str | None = None,
    store: PostedStore | None = None,
) -> list[Article]:
    """RSS を取得し、まだどちらかのプラットフォームに未投稿の記事を返す。

    新しい記事が末尾に来るよう、フィードの逆順（古い順）で返す。
    """
    rss_url = rss_url or os.getenv("NOTE_RSS_URL", "")
    if not rss_url:
        raise ValueError("NOTE_RSS_URL が設定されていません。.env を確認してください。")

    store = store or PostedStore.load(os.getenv("POSTED_ARTICLES_PATH"))

    logger.info("RSS を取得します: %s", rss_url)
    feed = feedparser.parse(rss_url)

    if getattr(feed, "bozo", 0) and getattr(feed, "bozo_exception", None):
        logger.warning("RSS の解析で警告が出ました: %s", feed.bozo_exception)

    status = getattr(feed, "status", None)
    if status is not None and status >= 400:
        raise RuntimeError(f"RSS の取得に失敗しました (HTTP {status}): {rss_url}")

    if not feed.entries:
        logger.warning("RSS にエントリがありませんでした: %s", rss_url)
        return []

    new_articles: list[Article] = []
    for entry in reversed(feed.entries):  # 古い記事から処理する
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        if store.is_fully_posted(link):
            continue
        new_articles.append(
            Article(
                title=(entry.get("title") or "").strip(),
                link=link,
                published=(entry.get("published") or "").strip(),
                # 有料記事では本文が入らない。あくまで参考情報として保持する。
                summary=(entry.get("summary") or "").strip(),
            )
        )

    logger.info(
        "RSS 全%d件のうち、未投稿は%d件です。", len(feed.entries), len(new_articles)
    )
    return new_articles


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for article in fetch_new_articles():
        print(f"- {article.title}\n  {article.link}  ({article.published})")
