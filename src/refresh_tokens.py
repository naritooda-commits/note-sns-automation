"""Instagram と Threads の長期アクセストークンを延長する。

長期トークンの有効期限は 60 日で、切れると投稿が全部止まる。更新 API を
呼ぶたびに期限が 60 日先に戻るので、月に 1 回これを動かしておけば切れない。

    python -m src.refresh_tokens            # 更新して .env を書き換える
    python -m src.refresh_tokens --dry-run  # 呼ぶだけで .env は書き換えない

Windows のタスクスケジューラから月 1 回実行する前提。成功したときは静かに
終わり、失敗したときだけ Slack に通知する（切れる前に気づけるようにする）。

更新できる条件:
  * 長期トークンであること（短期トークンは更新できない）
  * 発行から 24 時間以上たっていること（Meta 側の制限）

残りの期限は token_state.json に記録する。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from src import notify_slack

logger = logging.getLogger("refresh_tokens")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
STATE_PATH = REPO_ROOT / "token_state.json"
LOG_PATH = REPO_ROOT / "logs" / "refresh_tokens.log"

REQUEST_TIMEOUT = 30

# 残りがこれを切ったら、更新に失敗したとき強めに知らせる
WARN_DAYS = 14

PLATFORMS = {
    "instagram": {
        "label": "Instagram",
        "env_key": "IG_ACCESS_TOKEN",
        "url": "https://graph.instagram.com/refresh_access_token",
        "grant_type": "ig_refresh_token",
    },
    "threads": {
        "label": "Threads",
        "env_key": "THREADS_ACCESS_TOKEN",
        "url": "https://graph.threads.net/refresh_access_token",
        "grant_type": "th_refresh_token",
    },
}


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


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("token_state.json を読めませんでした。作り直します。")
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def update_env(env_key: str, value: str) -> None:
    """.env の 1 行だけを書き換える。他の行とコメントはそのまま残す。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    prefix = f"{env_key}="
    replaced = False

    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = prefix + value
            replaced = True
            break

    if not replaced:
        raise KeyError(f".env に {env_key} の行が見つかりません。")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_one(platform: str, dry_run: bool) -> tuple[bool, str]:
    """1 プラットフォーム分を更新する。(成功したか, 説明) を返す。"""
    spec = PLATFORMS[platform]
    label = spec["label"]
    token = os.getenv(spec["env_key"], "").strip()

    if not token:
        return False, f"{spec['env_key']} が設定されていません。"

    try:
        response = requests.get(
            spec["url"],
            params={"grant_type": spec["grant_type"], "access_token": token},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, f"通信に失敗しました: {exc}"

    if not response.ok:
        # 本文にトークンは含まれないが、念のため先頭だけ載せる
        return False, f"HTTP {response.status_code}: {response.text[:200]}"

    try:
        payload = response.json()
    except ValueError:
        return False, "応答が JSON ではありませんでした。"

    new_token = payload.get("access_token", "")
    expires_in = payload.get("expires_in")

    if not new_token or not expires_in:
        return False, f"応答に access_token か expires_in がありません: {payload}"

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))
    days = int(expires_in) // 86400

    if dry_run:
        logger.info("[dry-run] %s は更新できます（残り %s 日になります）。", label, days)
        return True, f"{label}: 更新できます（残り {days} 日）"

    update_env(spec["env_key"], new_token)

    state = load_state()
    state[platform] = {
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "expires_in_days": days,
    }
    save_state(state)

    logger.info("%s のトークンを更新しました（残り %s 日）。", label, days)
    return True, f"{label}: 更新しました（残り {days} 日）"


def remaining_days(platform: str) -> int | None:
    """token_state.json に記録された残り日数。記録がなければ None。"""
    entry = load_state().get(platform)
    if not entry:
        return None
    try:
        expires_at = datetime.fromisoformat(entry["expires_at"])
    except (KeyError, ValueError):
        return None
    return (expires_at - datetime.now(timezone.utc)).days


def main() -> int:
    parser = argparse.ArgumentParser(description="アクセストークンの期限を延長する")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="更新 API を呼ぶだけで .env は書き換えない",
    )
    args = parser.parse_args()

    setup_logging()
    load_dotenv(ENV_PATH)

    failures: list[str] = []

    for platform in PLATFORMS:
        label = PLATFORMS[platform]["label"]
        ok, message = refresh_one(platform, args.dry_run)
        if ok:
            continue

        logger.error("%s のトークンを更新できませんでした。%s", label, message)

        left = remaining_days(platform)
        if left is None:
            failures.append(f"• {label}: {message}")
        else:
            failures.append(f"• {label}（残り {left} 日）: {message}")

    if not failures:
        return 0

    # 失敗したときだけ知らせる。期限が迫っているなら見出しを強くする
    soon = any(
        (days := remaining_days(p)) is not None and days <= WARN_DAYS
        for p in PLATFORMS
    )
    head = (
        "🚨 アクセストークンの更新に失敗しました（まもなく投稿が止まります）"
        if soon
        else "⚠️ アクセストークンの更新に失敗しました"
    )
    notify_slack.notify_message(
        "\n".join(
            [head, *failures, "_Meta の管理画面でトークンを取り直す必要があります。_"]
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
