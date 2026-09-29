"""過去記事の追加投稿を、パソコン側から決まった時刻に実行する。

GitHub Actions の定期実行は時刻が保証されず、18時指定でも実際には
22〜24時台、ときには翌日にずれ込んでいた（2026-09-18〜29 の10回中、
遅れ4〜6時間が常態、2回は日付をまたいだ）。そのため経路を手元へ移した。

    git pull（GitHub 側の記録を取り込む）
        ↓
    src.post_archive を実行（1本投稿）
        ↓
    archive_state.json をコミットして push

経路は1つに保つこと。手元とクラウドの両方から動かすと、記録が届く前に
もう一方が走り、同じ記事を二重投稿する（2026-09-11 に発生）。

    python -m src.archive_local
    python -m src.archive_local --dry-run   # 投稿しない
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

from src.post_archive import run_archive
from src.sync_images import git

logger = logging.getLogger("archive_local")

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / "logs" / "archive_local.log"
STATE_FILE = "archive_state.json"


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
    if not git("status", "--porcelain", STATE_FILE).stdout.strip():
        logger.info("記録に変更はありません。")
        return
    git("add", STATE_FILE)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    git("commit", "-m", f"chore: 追加投稿の記録を更新（{stamp}）[skip ci]")
    git("push", "origin", "main")
    logger.info("追加投稿の記録を GitHub に反映しました。")


def main() -> int:
    parser = argparse.ArgumentParser(description="過去記事を1本、Threads へ追加投稿する")
    parser.add_argument("--dry-run", action="store_true", help="投稿しない")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    setup_logging()

    # 決まった時刻に起動するので、枠の時刻を待たずその場で投稿する
    os.environ["ARCHIVE_FORCE_NOW"] = "true"
    # 日曜は朝と夕方の2本。それ以外は1本
    if date.today().isoweekday() == 7:
        os.environ["ARCHIVE_POSTS_PER_DAY"] = "2"

    try:
        git("fetch", "origin")
        git("merge", "--ff-only", "origin/main")
    except RuntimeError as exc:
        logger.error(
            "GitHub の最新状態を取り込めませんでした。二重投稿を避けるため中止します。\n%s",
            exc,
        )
        return 1

    run_archive(dry_run=args.dry_run)

    if not args.dry_run:
        try:
            push_state()
        except RuntimeError as exc:
            logger.error("記録を GitHub に反映できませんでした。\n%s", exc)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
