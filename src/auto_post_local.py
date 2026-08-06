"""パソコン側から投稿処理を定期実行する（GitHub Actions の代わり／保険）。

GitHub Actions のスケジュール実行は保証されておらず、短い間隔の指定は
遅延したり実行されないことがある。実際に30分間隔の指定が一度も発火しな
かったため、手元のタスクスケジューラからも同じ処理を回す。

    git pull（GitHub Actions が書き戻した記録を取り込む）
        ↓
    src.main の投稿処理を実行
        ↓
    posted_articles.json / image_index.json をコミットして push

GitHub Actions 側と同時に動いても、投稿済みの記録で二重投稿は防がれる。
先に投稿した方の記録が残り、もう一方は「投稿済み」として飛ばす。

    python -m src.auto_post_local
    python -m src.auto_post_local --dry-run   # 投稿しない
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.main import run
from src.sync_images import git

logger = logging.getLogger("auto_post_local")

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "auto_post_local.log"
STATE_FILES = ("posted_articles.json", "image_index.json")


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    # pythonw.exe から実行されると標準出力が無い
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def push_state() -> None:
    """投稿記録に変更があれば GitHub に反映する。"""
    if not git("status", "--porcelain", *STATE_FILES).stdout.strip():
        logger.info("記録に変更はありません。")
        return

    git("add", *STATE_FILES)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    git("commit", "-m", f"chore: 投稿記録を更新（{stamp}）[skip ci]")
    git("push", "origin", "main")
    logger.info("投稿記録を GitHub に反映しました。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="パソコン側から note の新着を確認して投稿する"
    )
    parser.add_argument("--dry-run", action="store_true", help="投稿しない")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    setup_logging()

    try:
        # GitHub Actions 側が先に投稿していた場合の記録を取り込む
        git("fetch", "origin")
        git("merge", "--ff-only", "origin/main")
    except RuntimeError as exc:
        logger.error(
            "GitHub の最新状態を取り込めませんでした。二重投稿を避けるため中止します。\n%s",
            exc,
        )
        return 1

    exit_code = run(dry_run=args.dry_run)

    if not args.dry_run:
        try:
            push_state()
        except RuntimeError as exc:
            logger.error(
                "投稿記録を GitHub に反映できませんでした。"
                "次回の実行までに解消しないと、GitHub Actions 側で"
                "同じ記事が再投稿される可能性があります。\n%s",
                exc,
            )
            return 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
