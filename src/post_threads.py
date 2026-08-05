"""Threads API（公式）で投稿を行う。

投稿は2ステップ:
    1. POST /{threads-user-id}/threads          (media_type=TEXT) → creation_id
    2. POST /{threads-user-id}/threads_publish  (creation_id)     → 公開

Threads はテキストのみで投稿できるため、既定ではテキスト投稿。
THREADS_ATTACH_IMAGE=true にすると、src.find_eyecatch が見つけた
アイキャッチ画像を添付して IMAGE 投稿にする（Instagram と同じく公開 URL が必要）。

単体実行:
    python -m src.post_threads "投稿本文"
"""

from __future__ import annotations

import logging
import os
import time

import requests

from src.check_rss import PostResult
from src.find_eyecatch import Eyecatch, resolve_public_url

logger = logging.getLogger(__name__)

THREADS_API_VERSION = "v1.0"
THREADS_API_BASE = f"https://graph.threads.net/{THREADS_API_VERSION}"
TEXT_MAX_LENGTH = 500  # Threads の本文上限
REQUEST_TIMEOUT = 60
# コンテナ作成直後に publish すると失敗することがあるため、少し待つ
PUBLISH_DELAY_SECONDS = 5


class ThreadsError(RuntimeError):
    """Threads API 呼び出しの失敗。"""


def _describe_error(response: requests.Response) -> str:
    try:
        payload = response.json().get("error", {})
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"

    code = payload.get("code")
    message = payload.get("message", "")
    detail = f"HTTP {response.status_code} code={code}: {message}"

    if code == 190 or "access token" in message.lower():
        detail += (
            "\n  → アクセストークンが無効か期限切れの可能性があります。"
            "\n     Threads の設定からトークンを再発行し、長期トークン（60日）に交換して"
            "\n     THREADS_ACCESS_TOKEN を更新してください。README を参照。"
        )
    elif code in (4, 17, 32, 613):
        detail += "\n  → レート制限に達しています。時間を置いて再実行してください。"
    elif code == 200:
        detail += (
            "\n  → 権限不足です。threads_basic / threads_content_publish の"
            "\n     権限が付与されているか確認してください。"
        )
    return detail


def _post(url: str, params: dict[str, str]) -> dict:
    response = requests.post(url, data=params, timeout=REQUEST_TIMEOUT)
    if not response.ok:
        raise ThreadsError(_describe_error(response))
    return response.json()


def post_to_threads(
    text: str,
    eyecatch: Eyecatch | None = None,
    access_token: str | None = None,
    user_id: str | None = None,
    publish_delay: float = PUBLISH_DELAY_SECONDS,
) -> PostResult:
    """Threads に投稿する。

    eyecatch を渡し、かつ THREADS_ATTACH_IMAGE=true のときだけ画像を添付する。
    それ以外はテキストのみの投稿になる。
    """
    access_token = access_token or os.getenv("THREADS_ACCESS_TOKEN", "")
    user_id = user_id or os.getenv("THREADS_USER_ID", "")

    missing = [
        name
        for name, value in (
            ("THREADS_ACCESS_TOKEN", access_token),
            ("THREADS_USER_ID", user_id),
        )
        if not value
    ]
    if missing:
        message = (
            f"{', '.join(missing)} が設定されていません。.env / GitHub Secrets を確認してください。"
        )
        logger.error(message)
        return PostResult(platform="threads", ok=False, error=message)

    if len(text) > TEXT_MAX_LENGTH:
        logger.warning(
            "本文が%d字あるため、%d字に切り詰めます。", len(text), TEXT_MAX_LENGTH
        )
        text = text[:TEXT_MAX_LENGTH]

    params = {
        "media_type": "TEXT",
        "text": text,
        "access_token": access_token,
    }

    # 画像を添付する設定のときだけ IMAGE 投稿にする
    attach = os.getenv("THREADS_ATTACH_IMAGE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if attach and eyecatch is not None:
        image_url = resolve_public_url(eyecatch)
        if image_url:
            params["media_type"] = "IMAGE"
            params["image_url"] = image_url
            logger.info("Threads に添付する画像: %s", eyecatch.describe())
        else:
            logger.warning(
                "画像の公開URLを解決できなかったため、Threads はテキスト投稿にします。"
            )

    try:
        # 1. コンテナ作成
        container = _post(f"{THREADS_API_BASE}/{user_id}/threads", params)
        creation_id = container.get("id")
        if not creation_id:
            raise ThreadsError(f"コンテナ作成のレスポンスに id がありません: {container}")
        logger.info("Threads: コンテナを作成しました (id=%s)", creation_id)

        if publish_delay:
            time.sleep(publish_delay)

        # 2. 公開
        published = _post(
            f"{THREADS_API_BASE}/{user_id}/threads_publish",
            {
                "creation_id": creation_id,
                "access_token": access_token,
            },
        )
        post_id = published.get("id")
        if not post_id:
            raise ThreadsError(f"公開のレスポンスに id がありません: {published}")

        logger.info("Threads: 投稿しました (post_id=%s)", post_id)
        return PostResult(platform="threads", ok=True, post_id=post_id)

    except ThreadsError as exc:
        logger.error("Threads への投稿に失敗しました。\n%s", exc)
        return PostResult(platform="threads", ok=False, error=str(exc))
    except requests.RequestException as exc:
        logger.error("Threads への通信に失敗しました: %s", exc)
        return PostResult(platform="threads", ok=False, error=str(exc))


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print('使い方: python -m src.post_threads "投稿本文"')
        raise SystemExit(1)

    result = post_to_threads(sys.argv[1])
    raise SystemExit(0 if result.ok else 1)
