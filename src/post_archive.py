"""過去記事を Threads へ再投稿する（Instagram には出さない）。

Threads はフォロワー以外への配信が主で、投稿が「おすすめ」に拾われるか
どうかで閲覧数が 0〜335 に分かれる。中身・時間帯・1日の本数のいずれとも
相関が見つからなかったため、拾われる回数を増やす目的で、既存記事から
1日数本を追加投稿する。

新着記事の投稿（src.main）とは記録を分ける。posted_articles.json には
一切書き込まず、archive_state.json だけを更新する。

    python -m src.post_archive --dry-run   # 投稿せず、選ばれる記事だけ見る
    python -m src.post_archive --status    # 今日の枠と候補数を表示する
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import requests

from src.check_rss import Article
from src.generate_caption import generate_captions
from src.notify_slack import notify_message
from src.post_threads import post_to_threads

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = REPO_ROOT / "archive_state.json"
POSTED_PATH = REPO_ROOT / "posted_articles.json"

CONTENTS_API = "https://note.com/api/v2/creators/{urlname}/contents?kind=note&page={page}"
REQUEST_TIMEOUT = 30

# 3月の記事は書き方が現在と大きく違い、5ヶ月で53ビュー・スキ0という実績のため
# 対象に含めない。再開後（2026-07-21）の記事だけを使う。
DEFAULT_MIN_PUBLISHED = "2026-07-21"
DEFAULT_PER_DAY = 2
DEFAULT_WINDOW = "09:00-21:00"

# タイトルから4系統へ振り分ける。同じ系統が続くと、外れたときに連敗するため
# 順番に回す。判定できないものは「その他」に入れて同じ列に混ぜる。
SERIES_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("中途", ("中途", "転職", "採用面接")),
    ("新卒", ("新卒", "OJT", "メンター", "新人")),
    ("管理職", ("管理職", "課長", "部長", "権限", "昇進", "マネージャー", "任せ", "委譲")),
    ("辞める", ("辞め", "退職", "離職", "評価", "定着")),
]
SERIES_ORDER = ["中途", "新卒", "管理職", "辞める", "その他"]

# 一度投稿した記事を出し直すため、前回と同じ入り方にならないようにする
RETRY_HINT = (
    "この記事は一度投稿しており、今回は切り口を変えた2度目の投稿です。"
    "記事の中で前回とは別の要点を一つ選び、そこだけを扱ってください。"
    "タイトルの言い換えから入らず、本文中の具体的な場面や条件から入ってください。"
    "ただし切り口を変えることより、本文と食い違わないことを優先してください。"
    "本文の条件・主語・因果を変えてまで別の要点にする必要はありません。"
)


def is_enabled() -> bool:
    return os.getenv("ARCHIVE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _per_day() -> int:
    try:
        return max(0, int(os.getenv("ARCHIVE_POSTS_PER_DAY", DEFAULT_PER_DAY)))
    except ValueError:
        return DEFAULT_PER_DAY


def _force_now() -> bool:
    """起動した時点で投稿するか。決まった時刻に走る環境で使う。"""
    return os.getenv("ARCHIVE_FORCE_NOW", "").strip().lower() in ("1", "true", "yes", "on")


def _include_paid() -> bool:
    """有料記事も追加投稿の対象にするか。既定は無料記事のみ。"""
    return os.getenv("ARCHIVE_INCLUDE_PAID", "").strip().lower() in ("1", "true", "yes", "on")


def _reach_threshold() -> int:
    """この閲覧数に届かなかった投稿は「誰にも読まれていない」とみなす。"""
    try:
        return max(1, int(os.getenv("ARCHIVE_REACH_THRESHOLD", 20)))
    except ValueError:
        return 20


def _window() -> tuple[time, time]:
    raw = os.getenv("ARCHIVE_WINDOW", DEFAULT_WINDOW).strip()
    try:
        start_s, end_s = raw.split("-")
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
        if start < end:
            return start, end
    except ValueError:
        pass
    logger.warning("ARCHIVE_WINDOW=%r を読めないため既定値を使います。", raw)
    return time(9, 0), time(21, 0)


@dataclass
class Candidate:
    title: str
    link: str
    published: str
    series: str
    price: int = 0


def classify(title: str) -> str:
    for name, keywords in SERIES_RULES:
        if any(k in title for k in keywords):
            return name
    return "その他"


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"date": "", "slots": [], "used": 0, "history": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("archive_state.json を読めませんでした: %s", exc)
        return {"date": "", "slots": [], "used": 0, "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _reached_links(threshold: int) -> set[str]:
    """すでに Threads で読まれた記事のリンク。

    Threads はおすすめに拾われるかどうかで閲覧が 0〜335 に分かれる。
    拾われなかった投稿は誰の目にも触れていないため、切り口を変えて
    出し直す。閲覧が threshold 以上だったものだけ「届いた」として除く。
    """
    token = os.getenv("THREADS_ACCESS_TOKEN", "")
    user_id = os.getenv("THREADS_USER_ID", "")
    if not token or not user_id:
        logger.warning("Threads の資格情報がないため、閲覧数で絞り込めません。")
        return set()

    # 記事URLと投稿IDの対応は posted_articles.json が持っている。
    # 本文からURLを拾う方式だと、リンクを返信へ移した新形式を取りこぼす。
    if not POSTED_PATH.exists():
        return set()
    try:
        records = json.loads(POSTED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("posted_articles.json を読めませんでした: %s", exc)
        return set()

    reached: set[str] = set()
    unknown = 0
    for record in records:
        threads = record.get("threads") or {}
        post_id = threads.get("post_id")
        link = record.get("link", "")
        if threads.get("status") != "success" or not post_id or not link:
            continue
        views = _views_of(post_id, token)
        if views is None:
            # 取れなかったものは「届いた」側に寄せて、出し直しを控える
            unknown += 1
            reached.add(link)
        elif views >= threshold:
            reached.add(link)
    if unknown:
        logger.info("閲覧数を取得できなかった投稿が%d件あり、対象から外しました。", unknown)
    return reached


def _permalink(post_id: str | None) -> str:
    """投稿のURL。取れなければ空文字を返す（通知のためだけに使う）。"""
    token = os.getenv("THREADS_ACCESS_TOKEN", "")
    if not post_id or not token:
        return ""
    try:
        payload = requests.get(
            f"https://graph.threads.net/v1.0/{post_id}"
            f"?fields=permalink&access_token={token}",
            timeout=REQUEST_TIMEOUT,
        ).json()
        return payload.get("permalink", "")
    except (requests.RequestException, ValueError):
        return ""


def _views_of(post_id: str, token: str) -> int | None:
    try:
        payload = requests.get(
            f"https://graph.threads.net/v1.0/{post_id}/insights"
            f"?metric=views&access_token={token}",
            timeout=REQUEST_TIMEOUT,
        ).json()
    except (requests.RequestException, ValueError):
        return None
    for metric in payload.get("data", []):
        if metric.get("name") == "views":
            values = metric.get("values")
            if values:
                return values[0].get("value")
            return (metric.get("total_value") or {}).get("value")
    return None


def fetch_candidates(urlname: str | None = None) -> list[Candidate]:
    """note の公開APIから、まだ Threads へ出していない記事を集める。"""
    urlname = urlname or os.getenv("NOTE_URLNAME", "fast_lily4472")
    min_published = os.getenv("ARCHIVE_MIN_PUBLISHED", DEFAULT_MIN_PUBLISHED)

    articles: list[Candidate] = []
    for page in range(1, 21):
        response = requests.get(
            CONTENTS_API.format(urlname=urlname, page=page), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json().get("data", {})
        for content in data.get("contents", []):
            published = content.get("publishAt", "")
            if published[:10] < min_published:
                continue
            price = content.get("price") or 0
            if price > 0 and not _include_paid():
                # 初見の読み手が飛んだ先が販売ページになるため、既定では出さない
                continue
            title = content.get("name", "")
            articles.append(
                Candidate(
                    title=title,
                    link=f"https://note.com/{urlname}/n/{content.get('key')}",
                    published=published,
                    series=classify(title),
                    price=price,
                )
            )
        if data.get("isLastPage"):
            break

    state = load_state()
    used_links = {entry.get("link") for entry in state.get("history", [])}
    excluded = used_links | _reached_links(_reach_threshold())
    return [a for a in articles if a.link not in excluded]


def pick_next(candidates: list[Candidate], state: dict) -> Candidate | None:
    """系統を順番に回して1本選ぶ。直前と同じ系統を避ける。"""
    if not candidates:
        return None

    history = state.get("history", [])
    last_series = history[-1].get("series") if history else None
    start = SERIES_ORDER.index(last_series) + 1 if last_series in SERIES_ORDER else 0

    for offset in range(len(SERIES_ORDER)):
        series = SERIES_ORDER[(start + offset) % len(SERIES_ORDER)]
        in_series = [c for c in candidates if c.series == series]
        if in_series:
            # 同じ系統の中では古いものから消化する
            return min(in_series, key=lambda c: c.published)
    return None


def _ensure_today(state: dict, per_day: int) -> dict:
    """日付が変わっていたら、その日の投稿時刻を引き直す。"""
    today = date.today().isoformat()
    if state.get("date") == today:
        return state

    # 呼び出し元は30分おきに走るため、枠も30分刻みで決める。
    # 分単位にすると、枠の直後のループを取り逃がして最大30分ずれる。
    start, end = _window()
    base = start.hour * 60 + start.minute
    steps = ((end.hour * 60 + end.minute) - base) // 30
    picked = sorted(random.sample(range(steps), k=min(per_day, steps)))
    state["date"] = today
    state["slots"] = [
        f"{(base + s * 30) // 60:02d}:{(base + s * 30) % 60:02d}" for s in picked
    ]
    state["used"] = 0
    logger.info("本日の追加投稿の時刻: %s", ", ".join(state["slots"]) or "なし")
    return state


def run_archive(dry_run: bool = False) -> None:
    """1回の実行で、時刻が来ていれば過去記事を1本だけ投稿する。"""
    if not is_enabled():
        return

    per_day = _per_day()
    if per_day == 0:
        return

    # 決まった時刻に起動する環境（GitHub Actions など）では、枠の時刻を待たずに
    # その場で投稿する。1日の上限だけを見る。
    if _force_now():
        state = load_state()
        today = date.today().isoformat()
        if state.get("date") != today:
            state.update({"date": today, "slots": [], "used": 0})
        used = state.get("used", 0)
        if used >= per_day:
            logger.info("本日の追加投稿は済んでいます（%d/%d）。", used, per_day)
            save_state(state)
            return
    else:
        state = _ensure_today(load_state(), per_day)
        used = state.get("used", 0)
        slots = state.get("slots", [])

        if used >= len(slots):
            save_state(state)
            return

        now = datetime.now().strftime("%H:%M")
        if now < slots[used]:
            save_state(state)
            return

    try:
        candidates = fetch_candidates()
    except requests.RequestException as exc:
        logger.error("過去記事の一覧を取得できませんでした: %s", exc)
        return

    chosen = pick_next(candidates, state)
    if chosen is None:
        logger.info("追加投稿できる過去記事がなくなりました。")
        save_state(state)
        return

    logger.info(
        "過去記事を追加投稿します（%d/%d 本目・系統=%s）: %s",
        used + 1,
        per_day,
        chosen.series,
        chosen.title,
    )

    article = Article(title=chosen.title, link=chosen.link, published=chosen.published)
    # 同じ記事の2度目なので、前回と同じ切り口にならないようにする
    caption = generate_captions(article, caption_hint=RETRY_HINT).pick("threads", 0)
    logger.info("--- threads 用の投稿文（過去記事）---\n%s", caption)

    # 生成に失敗するとテンプレート文（本文にURLを含む定型文）が返る。
    # 追加投稿は読み物として出すものなので、その状態では投稿しない。
    if "http" in caption or caption.lstrip().startswith("noteに新しい記事"):
        logger.error(
            "投稿文の生成に失敗したとみられるため、追加投稿を見送ります。\n%s", caption
        )
        notify_message(
            "⚠️ 過去記事の追加投稿を見送りました（投稿文の生成に失敗）\n"
            f"対象: {chosen.title}\n生成された文面: {caption[:200]}"
        )
        state["used"] = used + 1
        save_state(state)
        return

    if dry_run:
        logger.info("DRY_RUN のため投稿しません。")
        return

    result = post_to_threads(caption, link=chosen.link)
    if not result.ok:
        # 失敗しても再試行せず、翌日の枠に回す（連続で叩かない）
        logger.error("追加投稿に失敗しました。今日はこれ以上試みません。\n%s", result.error)
        notify_message(
            "⚠️ 過去記事の追加投稿に失敗しました\n"
            f"対象: {chosen.title}\n{result.error}"
        )
        state["used"] = used + 1
        save_state(state)
        return

    state.setdefault("history", []).append(
        {
            "link": chosen.link,
            "title": chosen.title,
            "series": chosen.series,
            "posted_at": datetime.now().astimezone().isoformat(),
            "post_id": result.post_id,
            "caption": caption,
        }
    )
    state["used"] = used + 1
    save_state(state)
    logger.info("追加投稿しました (post_id=%s)", result.post_id)

    # 投稿の確認はSlackで行う。文面と投稿URLを流し、おかしければ
    # その場で消せるようにする（削除はAPIに権限がないため手作業）。
    notify_message(
        "📮 過去記事を追加投稿しました（Threads）\n"
        f"記事: {chosen.title}\n"
        f"系統: {chosen.series}\n\n"
        f"{caption}\n\n"
        f"投稿: {_permalink(result.post_id) or result.post_id}\n"
        f"記事URL: {chosen.link}"
    )


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import argparse

    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="過去記事を Threads へ追加投稿する")
    parser.add_argument("--dry-run", action="store_true", help="投稿しない")
    parser.add_argument("--status", action="store_true", help="候補と枠を表示するだけ")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.status:
        state = load_state()
        candidates = fetch_candidates()
        counts: dict[str, int] = {}
        for c in candidates:
            counts[c.series] = counts.get(c.series, 0) + 1
        print(f"有効: {is_enabled()} / 1日の上限: {_per_day()} / 窓: {_window()}")
        print(f"本日の枠: {state.get('slots')} / 消化: {state.get('used')}")
        print(f"投稿済み（追加分）: {len(state.get('history', []))}")
        print(f"残り候補: {len(candidates)}  内訳: {counts}")
        nxt = pick_next(candidates, state)
        print(f"次の1本: {nxt.series if nxt else '-'} / {nxt.title if nxt else 'なし'}")
    else:
        run_archive(dry_run=args.dry_run)
