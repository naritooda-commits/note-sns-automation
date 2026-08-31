"""投稿結果を Slack に通知する。

Slack の Incoming Webhook を使う。環境変数 SLACK_WEBHOOK_URL が
設定されていないときは、何もせずに終わる（通知なしで動く）。

    SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
    SLACK_NOTIFY=always   # always（既定）= 毎回通知 / failure = 失敗時だけ

単体実行（設定の確認用）:
    python -m src.notify_slack
"""

from __future__ import annotations

import logging
import os

import requests

from src.check_rss import Article, PostResult

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15
PLATFORM_LABEL = {"instagram": "Instagram", "threads": "Threads"}


def is_enabled() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL", "").strip())


def _send(text: str) -> bool:
    webhook = os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not webhook:
        logger.debug("SLACK_WEBHOOK_URL が未設定のため、通知しません。")
        return False

    try:
        response = requests.post(
            webhook, json={"text": text}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        # 通知の失敗で投稿処理を止めない
        logger.warning("Slack への通知に失敗しました: %s", exc)
        return False

    if not response.ok:
        logger.warning(
            "Slack への通知に失敗しました（HTTP %s）: %s"
            "\n  → Webhook URL が正しいか、削除されていないか確認してください。",
            response.status_code,
            response.text[:200],
        )
        return False

    logger.info("Slack に通知しました。")
    return True


def notify_message(text: str) -> None:
    """任意のメッセージを通知する（投稿結果以外の連絡に使う）。"""
    _send(text)


def notify_result(
    article: Article,
    results: list[PostResult],
    image_name: str = "",
) -> None:
    """1記事分の投稿結果を通知する。"""
    posted = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok and not r.skipped]

    if not posted and not failed:
        return

    mode = os.getenv("SLACK_NOTIFY", "always").strip().lower()
    if mode == "failure" and not failed:
        return

    if failed and posted:
        head = "⚠️ 一部だけ投稿できました"
    elif failed:
        head = "❌ 投稿に失敗しました"
    else:
        head = "✅ note記事を投稿しました"

    lines = [f"{head}", f"*{article.title}*"]

    for result in results:
        label = PLATFORM_LABEL.get(result.platform, result.platform)
        if result.ok:
            lines.append(f"• {label}: 投稿しました")
        elif not result.skipped:
            # エラーは1行目だけ載せる（詳細はログに出ている）
            reason = (result.error or "").splitlines()[0]
            lines.append(f"• {label}: 失敗 — {reason}")

    if image_name:
        lines.append(f"画像: {image_name}")
    lines.append(article.link)

    if failed:
        lines.append("_失敗した方は次回の実行で自動的に再試行されます。_")

    _send("\n".join(lines))


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not is_enabled():
        print("SLACK_WEBHOOK_URL が設定されていません。")
        raise SystemExit(1)

    ok = _send("note-sns-automation からのテスト通知です。この行が見えていれば設定は完了です。")
    raise SystemExit(0 if ok else 1)
