"""URL を指定して、note の記事1本を Instagram / Threads に投稿する。

通常の自動投稿は RSS の新着を対象にするが、note の RSS には
有料記事（販売コンテンツ）が含まれない。そうした記事や、過去記事を
あとから告知したい場合にこのコマンドを使う。

投稿の中身（画像の選定、投稿文、記録の付け方、再試行の扱い）は
自動投稿とまったく同じで、対象の決め方だけが違う。

    python -m src.post_article "https://note.com/xxx/n/nXXXX" --title "記事タイトル"
    python -m src.post_article "..." --title "..." --dry-run   # 投稿せず内容だけ確認
"""

from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from src.check_rss import Article, PostedStore
from src.main import process_article, setup_logging

logger = logging.getLogger("post_article")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="URL を指定して note 記事を Instagram / Threads に投稿する"
    )
    parser.add_argument("url", help="note 記事の URL")
    parser.add_argument("--title", required=True, help="記事タイトル")
    parser.add_argument("--published", default="", help="公開日（任意）")
    parser.add_argument(
        "--dry-run", action="store_true", help="投稿せず、生成した内容だけ表示する"
    )
    parser.add_argument(
        "--variant", type=int, default=0, help="投稿文3案のうち何番目を使うか"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="すでに投稿済みとして記録されていても投稿する",
    )
    args = parser.parse_args()

    load_dotenv()
    setup_logging()

    article = Article(title=args.title, link=args.url, published=args.published)
    store = PostedStore.load()

    if args.force:
        # 記録を消してから処理する（過去に投稿済み扱いにしたものを投稿し直す場合）
        if store.records.pop(article.link, None) is not None:
            logger.info("既存の記録を削除しました: %s", article.link)

    if store.is_fully_posted(article.link):
        logger.info(
            "この記事はすでに投稿済みとして記録されています。"
            "投稿し直す場合は --force を付けてください。"
        )
        return 0

    results = process_article(article, store, args.variant, args.dry_run)
    store.save()

    failed = [r for r in results if not r.ok and not r.skipped]
    logger.info(
        "実行完了: 成功 %d件 / 失敗 %d件",
        sum(1 for r in results if r.ok),
        len(failed),
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
