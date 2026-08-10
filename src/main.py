"""note の新着記事を検知して Instagram / Threads に告知投稿するエントリーポイント。

    python -m src.main              # 通常実行
    python -m src.main --dry-run    # 投稿せず、生成した文面だけ表示
    python -m src.main --limit 1    # 1件だけ処理

Instagram と Threads は独立して記録されるため、片方だけ失敗した場合は
次回の実行で失敗した方だけが再試行される。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from src import dryrun_cache
from src.check_rss import REPO_ROOT, Article, PostedStore, PostResult, fetch_new_articles
from src.find_eyecatch import (
    caption_hint_for,
    caption_override_for,
    find_eyecatch,
    resolve_public_url,
)
from src.generate_caption import Captions, generate_captions
from src.notify_slack import notify_result
from src.post_instagram import post_to_instagram
from src.post_threads import post_to_threads

logger = logging.getLogger("note_sns_automation")

DEFAULT_LOG_PATH = "logs/auto_post.log"


def setup_logging(log_path: str | None = None, verbose: bool = False) -> None:
    """標準出力とログファイルの両方に出力する。"""
    path = Path(log_path or os.getenv("LOG_FILE_PATH") or DEFAULT_LOG_PATH)
    if not path.is_absolute():
        path = REPO_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # requests / urllib3 のログは抑える
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def build_captions(article: Article, eyecatch) -> Captions:
    """投稿文を用意する。手動で確定済みの文面があれば、それを優先して使う。

    両プラットフォームとも手動指定されていれば API は呼ばない。
    片方だけの指定なら、もう片方だけを生成する。
    """
    fixed = caption_override_for(article.title, article.link)

    if len(fixed) == 2:
        return Captions(instagram=[fixed["instagram"]], threads=[fixed["threads"]])

    captions = generate_captions(
        article,
        image_heading="" if eyecatch.is_default else eyecatch.heading,
        caption_hint=caption_hint_for(article.title, article.link),
    )
    for platform, text in fixed.items():
        setattr(captions, platform, [text])
    return captions


def process_article(
    article: Article,
    store: PostedStore,
    variant_index: int,
    dry_run: bool,
) -> list[PostResult]:
    """記事1件を、未完了のプラットフォームにだけ投稿する。"""
    logger.info("記事を処理します: %s (%s)", article.title, article.link)

    pending = [p for p in ("instagram", "threads") if not store.is_done(article.link, p)]
    if not pending:
        logger.info("すでに両方に投稿済みのためスキップします。")
        return []
    logger.info("投稿先: %s", ", ".join(pending))

    # アイキャッチ画像は記事ごとに1回だけ決め、両プラットフォームで共有する。
    # 投稿文を画像の切り口に寄せるため、画像を先に決めてから文面を作る。
    eyecatch = find_eyecatch(article.title, article.link)
    logger.info(
        "この記事に使う画像: %s / 抽出見出し「%s」/ 根拠: %s",
        eyecatch.describe(),
        eyecatch.heading or "(なし)",
        eyecatch.reason or "(なし)",
    )

    captions = build_captions(article, eyecatch)

    results: list[PostResult] = []

    for platform in pending:
        caption = captions.pick(platform, variant_index)
        logger.info("--- %s 用の投稿文 ---\n%s", platform, caption)

        if dry_run:
            # 実際に投稿はしないが、画像の公開URLが解決できるかはここで確かめる
            if platform == "instagram":
                logger.info(
                    "DRY_RUN: Instagram に添付される画像URL: %s",
                    resolve_public_url(eyecatch) or "(解決できませんでした)",
                )
            logger.info("DRY_RUN のため %s には投稿しません。", platform)
            results.append(PostResult(platform=platform, ok=False, skipped=True))
            continue

        if platform == "instagram":
            result = post_to_instagram(caption, eyecatch=eyecatch)
        else:
            result = post_to_threads(caption, eyecatch=eyecatch)
        results.append(result)

        # 成功・失敗どちらもその場で記録し、途中で落ちても状態が残るようにする
        store.record_result(
            article,
            platform,
            "success" if result.ok else "failed",
            posted_at=datetime.now(timezone.utc).isoformat() if result.ok else None,
            post_id=result.post_id,
            caption=caption if result.ok else None,
            image=eyecatch.name,
            image_source=eyecatch.source,
            error=result.error,
            last_attempted_at=datetime.now(timezone.utc).isoformat(),
        )
        store.save()

    if not dry_run:
        notify_result(article, results, image_name=eyecatch.name)

    return results


def run(
    dry_run: bool = False,
    limit: int | None = None,
    variant_index: int | None = None,
) -> int:
    """全体を実行し、終了コード（0=成功、1=失敗あり）を返す。"""
    store = PostedStore.load(os.getenv("POSTED_ARTICLES_PATH"))

    try:
        articles = fetch_new_articles(store=store)
    except (ValueError, RuntimeError) as exc:
        logger.error("RSS の取得に失敗しました: %s", exc)
        return 1

    if not articles:
        logger.info("新しく投稿すべき記事はありませんでした。")
        return 0

    max_posts = limit if limit is not None else _env_int("MAX_POSTS_PER_RUN", 3)
    if max_posts > 0 and len(articles) > max_posts:
        logger.info(
            "対象%d件のうち、今回は古い順に%d件だけ処理します（連投防止）。",
            len(articles),
            max_posts,
        )
        articles = articles[:max_posts]

    if variant_index is None:
        variant_index = _env_int("CAPTION_VARIANT_INDEX", 0)

    succeeded = 0
    failed = 0
    for article in articles:
        try:
            results = process_article(article, store, variant_index, dry_run)
        except Exception:  # 1記事の失敗で全体を止めない
            logger.exception("記事の処理中に予期しないエラーが発生しました: %s", article.link)
            failed += 1
            continue
        succeeded += sum(1 for r in results if r.ok)
        failed += sum(1 for r in results if not r.ok and not r.skipped)

    store.save()
    logger.info("実行完了: 成功 %d件 / 失敗 %d件", succeeded, failed)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="note の新着記事を Instagram / Threads に自動投稿する"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="実際には投稿せず、生成した投稿文だけ表示する",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="1回の実行で処理する記事数の上限（0で無制限）",
    )
    parser.add_argument(
        "--variant",
        type=int,
        default=None,
        help="生成された3案のうち何番目を使うか（0/1/2）",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="dry-run 用の API キャッシュを削除してから実行する",
    )
    parser.add_argument("--verbose", action="store_true", help="デバッグログも出す")
    args = parser.parse_args()

    load_dotenv()
    setup_logging(verbose=args.verbose)

    dry_run = args.dry_run or _env_flag("DRY_RUN")
    # dry-run 用キャッシュは DRY_RUN 環境変数を見て有効化されるため、
    # --dry-run で起動した場合もここで揃えておく
    os.environ["DRY_RUN"] = "true" if dry_run else "false"

    if args.clear_cache:
        dryrun_cache.clear()

    if dry_run:
        logger.info(
            "DRY_RUN モードで実行します（実際の投稿は行いません／"
            "API 結果は %s にキャッシュされます）。",
            dryrun_cache.cache_path().name,
        )

    return run(dry_run=dry_run, limit=args.limit, variant_index=args.variant)


if __name__ == "__main__":
    raise SystemExit(main())
