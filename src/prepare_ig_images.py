"""Instagram 投稿用に、アイキャッチ画像を正方形 JPEG へ変換する。

Instagram のフィード投稿には次の制約がある。

- 縦横比は 4:5 〜 1.91:1 の範囲内であること
- 公式に指定されている形式は JPEG（PNG や WebP は拒否されることがある）

note 標準のアイキャッチ 1280x670 は比率 1.9104 で、上限 1.91 をわずかに超える。
そのため、元画像には手を触れずに、1080x1080 の JPEG（余白は白）へ変換したものを
images_ig/ に作り、Instagram にはそちらの URL を渡す。

元画像（images/）はそのまま残す。画像内テキストの読み取りは元画像に対して行う。

    python -m src.prepare_ig_images            # 未変換・更新された画像だけ変換
    python -m src.prepare_ig_images --force    # すべて作り直す
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "images"
OUTPUT_DIR = REPO_ROOT / "images_ig"
DEFAULT_IMAGE = REPO_ROOT / "assets" / "default_post_image.png"

CANVAS_SIZE = 1080  # 1080x1080（Instagram で表示面積が最大になる正方形）
FALLBACK_BACKGROUND = (255, 255, 255)
JPEG_QUALITY = 88
SOURCE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def background_color(img: Image.Image) -> tuple[int, int, int]:
    """余白の色を、元画像の上下の縁から決める。

    白背景の画像には白、黒背景の画像には黒が入るので、
    余白が帯として目立たない。縁の色がばらついている場合は白に戻す。
    """
    width, height = img.size
    edge = list(img.crop((0, 0, width, 1)).getdata()) + list(
        img.crop((0, height - 1, width, height)).getdata()
    )
    if not edge:
        return FALLBACK_BACKGROUND

    average = tuple(sum(channel) // len(edge) for channel in zip(*edge))
    # 平均から大きく外れる画素が多い＝縁が単色でない場合は白にする
    spread = sum(
        1
        for pixel in edge
        if max(abs(p - a) for p, a in zip(pixel, average)) > 24
    )
    if spread > len(edge) * 0.2:
        return FALLBACK_BACKGROUND
    return average


def convert(src: Path, dest: Path) -> None:
    """1枚を正方形 JPEG に変換する（切り抜かず、余白を足して収める）。"""
    with Image.open(src) as img:
        img = img.convert("RGB")
        background = background_color(img)
        img.thumbnail((CANVAS_SIZE, CANVAS_SIZE), Image.LANCZOS)
        canvas = Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), background)
        canvas.paste(img, ((CANVAS_SIZE - img.width) // 2, (CANVAS_SIZE - img.height) // 2))
        dest.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(dest, format="JPEG", quality=JPEG_QUALITY, optimize=True)


def _is_stale(src: Path, dest: Path) -> bool:
    return not dest.exists() or src.stat().st_mtime > dest.stat().st_mtime


def prepare_all(force: bool = False) -> tuple[int, int]:
    """images/ 配下と既定画像を変換する。戻り値は (変換した数, 対象総数)。"""
    targets: list[tuple[Path, Path]] = []

    if SOURCE_DIR.is_dir():
        for src in sorted(SOURCE_DIR.iterdir()):
            if src.is_file() and src.suffix.lower() in SOURCE_EXTENSIONS:
                targets.append((src, OUTPUT_DIR / f"{src.stem}.jpg"))
    else:
        logger.warning("画像フォルダがありません: %s", SOURCE_DIR)

    # 既定画像も同じ場所に JPEG 版を用意しておく
    if DEFAULT_IMAGE.exists():
        targets.append((DEFAULT_IMAGE, DEFAULT_IMAGE.with_suffix(".jpg")))

    converted = 0
    for src, dest in targets:
        if not force and not _is_stale(src, dest):
            continue
        convert(src, dest)
        converted += 1
        logger.info("変換: %s → %s", src.name, dest.name)

    logger.info("変換 %d 件 / 対象 %d 件", converted, len(targets))
    return converted, len(targets)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Instagram 投稿用に画像を正方形 JPEG へ変換する"
    )
    parser.add_argument(
        "--force", action="store_true", help="変換済みの画像も作り直す"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    prepare_all(force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
