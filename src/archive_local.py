"""過去記事の追加投稿を、パソコン側から決まった時刻に実行する。

GitHub Actions の定期実行は時刻が保証されず、18時指定でも実際には
22〜24時台、ときには翌日にずれ込んでいた（2026-09-18〜29 の10回中、
遅れ4〜6時間が常態、2回は日付をまたいだ）。そのため経路を手元へ移した。

手元でも時刻を固定すると、その時刻にパソコンが起動していない日は出せない
（9/29 は 18:46、9/30 は 22:31 になった）。そこで30分おきに実行し、
出してよい時間帯（既定 8:00〜20:00）で、直近の Threads 投稿から十分に
間隔が空いていれば1本出す形にした。新着記事の投稿も起動後すぐに走るため、
間隔を見ないと朝に2本並ぶ。

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
import json
import logging
import os
import sys
from datetime import date, datetime, time
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


def _parse_time(value: str, default: str) -> time:
    raw = (value or default).strip()
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except ValueError:
        logger.warning("時刻の指定を読めないため既定値を使います: %r", raw)
        hh, mm = default.split(":")
        return time(int(hh), int(mm))


def within_window() -> bool:
    """出してよい時間帯かどうか。

    パソコンが起動していない時間は実行されないため、時刻を固定すると
    その日は出せない。時間帯で挟み、起動している最初の機会に出す。
    夜遅くに出るのを避けるため、終わりの時刻を過ぎたら見送る。
    """
    start = _parse_time(os.getenv("ARCHIVE_POST_FROM", ""), "08:00")
    end = _parse_time(os.getenv("ARCHIVE_POST_TO", ""), "20:00")
    now = datetime.now().time()
    if start <= now <= end:
        return True
    logger.info(
        "いまは投稿する時間帯ではありません（%s〜%s / 現在 %s）。",
        start.strftime("%H:%M"), end.strftime("%H:%M"), now.strftime("%H:%M"),
    )
    return False


def _latest_threads_post() -> datetime | None:
    """直近の Threads 投稿時刻（新着・過去の両方から探す）。"""
    times: list[datetime] = []

    posted = json.loads((REPO_ROOT / "posted_articles.json").read_text(encoding="utf-8"))
    for entry in posted:
        at = entry.get("threads", {}).get("posted_at")
        if at:
            times.append(datetime.fromisoformat(at))

    state = json.loads((REPO_ROOT / STATE_FILE).read_text(encoding="utf-8"))
    for h in state.get("history", []):
        if h.get("posted_at"):
            times.append(datetime.fromisoformat(h["posted_at"]))

    if not times:
        return None
    # 記録は UTC と現地時刻が混在しているため、比較できる形に揃える
    tz = datetime.now().astimezone().tzinfo
    return max(t.astimezone(tz) if t.tzinfo else t.replace(tzinfo=tz) for t in times)


def enough_gap() -> bool:
    """直近の Threads 投稿から十分に間隔が空いているか。

    新着記事の投稿も、パソコンが起動して最初の実行で出る。間隔を見ないと
    朝に数分差で2本並ぶため、一定時間空くまで見送る。
    """
    hours = float(os.getenv("ARCHIVE_MIN_GAP_HOURS", "3"))
    latest = _latest_threads_post()
    if latest is None:
        return True
    elapsed = (datetime.now().astimezone() - latest).total_seconds() / 3600
    if elapsed >= hours:
        return True
    logger.info(
        "直近の投稿から %.1f 時間しか経っていないため見送ります（%.0f 時間空けます）。",
        elapsed, hours,
    )
    return False


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

    if not args.dry_run and not (within_window() and enough_gap()):
        return 0

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
