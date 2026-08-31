"""アイキャッチ画像の受け取りフォルダを見て、新しい画像を GitHub へ送る。

    デスクトップの「投稿アイキャッチ画像」フォルダ
        ↓ 新しい画像・更新された画像だけコピー
    リポジトリの images/
        ↓ 正方形 JPEG に変換
    リポジトリの images_ig/
        ↓ コミットして push
    GitHub（GitHub Actions から参照できる状態になる）

Windows のタスクスケジューラから定期実行する前提。画像が増えていなければ
何もせずに終わる。実行結果は logs/sync_images.log に残る。

    python -m src.sync_images            # 通常実行
    python -m src.sync_images --dry-run  # コピー・変換だけ行い、push はしない
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from src.prepare_ig_images import OUTPUT_DIR, SOURCE_EXTENSIONS, prepare_all

logger = logging.getLogger("sync_images")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INBOX = Path.home() / "Desktop" / "投稿アイキャッチ画像"
IMAGES_DIR = REPO_ROOT / "images"
LOG_PATH = REPO_ROOT / "logs" / "sync_images.log"


def setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH, encoding="utf-8")]
    # pythonw.exe（画面を出さない実行）では標準出力が無いので、その場合は付けない
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)


# pythonw.exe（画面を出さない実行）から git を呼ぶと、呼び出しのたびに
# 黒いコンソールが一瞬表示される。作業中に何度も光るのを防ぐため、
# 子プロセスにウィンドウを作らせない。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """リポジトリ内で git を実行する。"""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} に失敗しました（終了コード {result.returncode}）"
            f"\n{result.stdout}\n{result.stderr}"
        )
    return result


def inbox_path() -> Path:
    raw = os.getenv("IMAGE_INBOX", "").strip()
    return Path(raw).expanduser() if raw else DEFAULT_INBOX


def copy_new_images(inbox: Path) -> list[str]:
    """受け取りフォルダから、新しい画像・更新された画像だけコピーする。"""
    if not inbox.is_dir():
        logger.warning("受け取りフォルダが見つかりません: %s", inbox)
        return []

    IMAGES_DIR.mkdir(exist_ok=True)
    copied: list[str] = []
    for src in sorted(inbox.rglob("*")):
        if not src.is_file() or src.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        dest = IMAGES_DIR / src.name
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            continue
        shutil.copy2(src, dest)
        copied.append(src.name)
        logger.info("コピー: %s", src.name)
    return copied


def remove_deleted_images(inbox: Path) -> list[str]:
    """受け取りフォルダから消された画像を、リポジトリ側からも消す。

    変換後の JPEG（images_ig/）も一緒に消す。
    受け取りフォルダが見つからない、または空のときは何もしない。
    フォルダを移動・リネームしただけのときに全消ししてしまうのを防ぐため。
    """
    if not inbox.is_dir():
        return []

    keep = {
        p.name
        for p in inbox.rglob("*")
        if p.is_file() and p.suffix.lower() in SOURCE_EXTENSIONS
    }
    if not keep:
        logger.warning(
            "受け取りフォルダに画像が1枚もありません（%s）。"
            "削除の同期は行いません。",
            inbox,
        )
        return []

    removed: list[str] = []
    for path in sorted(IMAGES_DIR.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if path.name in keep:
            continue

        path.unlink()
        converted = OUTPUT_DIR / f"{path.stem}.jpg"
        if converted.exists():
            converted.unlink()
        removed.append(path.name)
        logger.info("削除: %s", path.name)

    return removed


def has_changes() -> bool:
    result = git("status", "--porcelain", "images", "images_ig")
    return bool(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="アイキャッチ画像を GitHub に同期する"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="コミットと push を行わない"
    )
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    setup_logging()

    inbox = inbox_path()
    copied = copy_new_images(inbox)
    removed = remove_deleted_images(inbox)
    converted, _ = prepare_all()

    if not copied and not removed and not converted and not has_changes():
        logger.info("画像に変更はありません（%s）。", inbox)
        return 0

    logger.info(
        "追加 %d 件、削除 %d 件、変換 %d 件。", len(copied), len(removed), converted
    )

    if args.dry_run:
        logger.info("--dry-run のため、コミットと push は行いません。")
        return 0

    try:
        # GitHub Actions が投稿記録を書き戻しているため、先に取り込む
        git("fetch", "origin")
        git("merge", "--ff-only", "origin/main")

        git("add", "images", "images_ig")
        if not git("diff", "--cached", "--quiet", check=False).returncode:
            logger.info("コミットする変更はありませんでした。")
            return 0

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        message = (
            f"chore: アイキャッチ画像を同期（追加{len(copied)}件 "
            f"/ 削除{len(removed)}件 / {stamp}）"
        )
        git("commit", "-m", message)
        git("push", "origin", "main")
        logger.info("GitHub に反映しました: %s", message)
    except RuntimeError as exc:
        logger.error("GitHub への反映に失敗しました。\n%s", exc)
        logger.error(
            "  → 画像のコピーと変換は完了しています。"
            "GitHub Desktop で Push すれば反映されます。"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
