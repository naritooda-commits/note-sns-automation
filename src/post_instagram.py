"""Instagram Graph API（公式）でフィード投稿を行う。

Instagram Graph API はテキストのみの投稿に対応していないため、
必ず画像を1枚添付する。また、画像はローカルファイルを直接アップロードできず、
「インターネットから到達できる公開 URL」を渡す必要がある。

添付する画像は src.find_eyecatch が決める（ローカルフォルダ →
Google ドライブ → 既定画像 の順に探す）。その結果を公開 URL に解決するのが
find_eyecatch.resolve_public_url で、最終的なフォールバック先が
環境変数 IG_IMAGE_URL（assets/default_post_image.png の公開 URL）になる。

投稿は2ステップ:
    1. POST /{ig-user-id}/media          → creation_id を得る
    2. POST /{ig-user-id}/media_publish  → 公開

単体実行:
    python -m src.post_instagram "投稿本文"
"""

from __future__ import annotations

import logging
import os
import time

import requests

from src.check_rss import PostResult
from src.find_eyecatch import Eyecatch, resolve_public_url

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
# Instagram API には2つの方式があり、接続先ホストが異なる。
#   Instagramログイン方式 : https://graph.instagram.com/v21.0
#     権限は instagram_business_* 系。トークンは Instagram のユーザートークン。
#   Facebookログイン方式  : https://graph.facebook.com/v21.0
#     権限は instagram_basic / instagram_content_publish 系。
#     トークンは Facebook のユーザートークンで、Facebookページ経由で紐づける。
# 投稿の手順（コンテナ作成 → 公開）はどちらも同じなので、接続先だけ切り替える。
DEFAULT_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
CAPTION_MAX_LENGTH = 2200  # Instagram のキャプション上限
REQUEST_TIMEOUT = 60
# コンテナ（画像）の処理待ち
CONTAINER_POLL_INTERVAL = 3
CONTAINER_TIMEOUT = 180


def api_base() -> str:
    return (os.getenv("IG_API_BASE", "").strip() or DEFAULT_API_BASE).rstrip("/")


class InstagramError(RuntimeError):
    """Instagram Graph API 呼び出しの失敗。"""


def _describe_error(response: requests.Response) -> str:
    """Graph API のエラーレスポンスを、読んで分かる日本語ログにする。"""
    try:
        payload = response.json().get("error", {})
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"

    code = payload.get("code")
    subcode = payload.get("error_subcode")
    message = payload.get("message", "")
    detail = f"HTTP {response.status_code} code={code} subcode={subcode}: {message}"

    # トークン切れは運用中いちばん起きやすいので、対処法まで出す
    if code == 190 or "access token" in message.lower():
        detail += (
            "\n  → アクセストークンが無効か期限切れの可能性があります。"
            "\n     Meta Developer のアプリ →「Instagramでメッセージとコンテンツを管理」"
            "\n     →「Instagramログインによる API設定」→「アクセストークンを生成する」で"
            "\n     発行し直し、長期トークン（60日）に交換して IG_ACCESS_TOKEN を"
            "\n     更新してください。手順は README の「アクセストークンの取得と更新」を参照。"
        )
    elif code in (4, 17, 32, 613):
        detail += "\n  → API のレート制限に達しています。時間を置いて再実行してください。"
    elif code == 100:
        detail += (
            "\n  → パラメータが不正です。IG_BUSINESS_ACCOUNT_ID と IG_IMAGE_URL"
            "\n     （公開アクセス可能な JPEG/PNG の URL か）を確認してください。"
        )
    elif code == 200:
        detail += (
            "\n  → 権限不足です。instagram_basic / instagram_content_publish /"
            "\n     pages_read_engagement の各権限が付与されているか確認してください。"
        )
    return detail


def _post(url: str, params: dict[str, str]) -> dict:
    response = requests.post(url, data=params, timeout=REQUEST_TIMEOUT)
    if not response.ok:
        raise InstagramError(_describe_error(response))
    return response.json()


def _wait_until_ready(
    base: str, creation_id: str, access_token: str, timeout: float = CONTAINER_TIMEOUT
) -> None:
    """コンテナの処理が終わるまで待つ。

    Instagram は画像をサーバー側で取得・処理してからでないと公開できない。
    処理中に公開しようとすると code=9007「Media ID is not available」で
    失敗するため、status_code が FINISHED になるのを待つ。
    """
    deadline = time.monotonic() + timeout
    last_status = None

    while time.monotonic() < deadline:
        response = requests.get(
            f"{base}/{creation_id}",
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=REQUEST_TIMEOUT,
        )
        if not response.ok:
            raise InstagramError(_describe_error(response))

        payload = response.json()
        status = payload.get("status_code")
        if status != last_status:
            logger.info("Instagram: コンテナの状態 %s", status)
            last_status = status

        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise InstagramError(
                f"コンテナの処理に失敗しました（status_code={status}）: "
                f"{payload.get('status')}"
                "\n  → 画像URLが公開されているか、JPEG かつ縦横比が"
                "\n     4:5〜1.91:1 の範囲内かを確認してください。"
            )
        time.sleep(CONTAINER_POLL_INTERVAL)

    raise InstagramError(
        f"コンテナの処理が {timeout:.0f} 秒以内に終わりませんでした"
        f"（最後の状態: {last_status}）。時間を置いて再実行してください。"
    )


def post_to_instagram(
    caption: str,
    eyecatch: Eyecatch | None = None,
    image_url: str | None = None,
    access_token: str | None = None,
    business_account_id: str | None = None,
) -> PostResult:
    """キャプションと画像で Instagram にフィード投稿する。

    eyecatch を渡すと、その画像の公開 URL を解決して添付する。
    image_url を直接渡した場合はそちらを優先する。
    どちらもなければ IG_IMAGE_URL（既定画像）を使う。
    """
    access_token = access_token or os.getenv("IG_ACCESS_TOKEN", "")
    business_account_id = business_account_id or os.getenv("IG_BUSINESS_ACCOUNT_ID", "")

    if not image_url and eyecatch is not None:
        image_url = resolve_public_url(eyecatch) or ""
        logger.info("Instagram に添付する画像: %s", eyecatch.describe())
    image_url = image_url or os.getenv("IG_IMAGE_URL", "")

    missing = [
        name
        for name, value in (
            ("IG_ACCESS_TOKEN", access_token),
            ("IG_BUSINESS_ACCOUNT_ID", business_account_id),
            ("IG_IMAGE_URL", image_url),
        )
        if not value
    ]
    if missing:
        message = (
            f"{', '.join(missing)} が設定されていません。.env / GitHub Secrets を確認してください。"
        )
        if "IG_IMAGE_URL" in missing:
            message += (
                "\n  → Instagram はテキストのみの投稿ができず、画像の公開URLが必須です。"
                "\n     assets/default_post_image.png を公開URL（GitHub の raw URL など）に置き、"
                "\n     その URL を IG_IMAGE_URL に設定してください。"
                "\n     ローカルフォルダのアイキャッチを使う場合は IMAGE_PUBLIC_BASE_URL も"
                "\n     設定してください（README「アイキャッチ画像の探索」を参照）。"
            )
        logger.error(message)
        return PostResult(platform="instagram", ok=False, error=message)

    if len(caption) > CAPTION_MAX_LENGTH:
        logger.warning(
            "キャプションが%d字あるため、%d字に切り詰めます。",
            len(caption),
            CAPTION_MAX_LENGTH,
        )
        caption = caption[:CAPTION_MAX_LENGTH]

    base = api_base()
    logger.info("Instagram API 接続先: %s", base)

    try:
        # 1. メディアコンテナを作成
        container = _post(
            f"{base}/{business_account_id}/media",
            {
                "image_url": image_url,
                "caption": caption,
                "access_token": access_token,
            },
        )
        creation_id = container.get("id")
        if not creation_id:
            raise InstagramError(f"コンテナ作成のレスポンスに id がありません: {container}")
        logger.info("Instagram: メディアコンテナを作成しました (id=%s)", creation_id)

        # 2. 画像の処理が終わるまで待つ（待たずに公開すると code=9007 で失敗する）
        _wait_until_ready(base, creation_id, access_token)

        # 3. 公開
        published = _post(
            f"{base}/{business_account_id}/media_publish",
            {
                "creation_id": creation_id,
                "access_token": access_token,
            },
        )
        post_id = published.get("id")
        if not post_id:
            raise InstagramError(f"公開のレスポンスに id がありません: {published}")

        logger.info("Instagram: 投稿しました (post_id=%s)", post_id)
        return PostResult(platform="instagram", ok=True, post_id=post_id)

    except InstagramError as exc:
        logger.error("Instagram への投稿に失敗しました。\n%s", exc)
        return PostResult(platform="instagram", ok=False, error=str(exc))
    except requests.RequestException as exc:
        logger.error("Instagram への通信に失敗しました: %s", exc)
        return PostResult(platform="instagram", ok=False, error=str(exc))


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print('使い方: python -m src.post_instagram "投稿本文"')
        raise SystemExit(1)

    result = post_to_instagram(sys.argv[1])
    raise SystemExit(0 if result.ok else 1)
