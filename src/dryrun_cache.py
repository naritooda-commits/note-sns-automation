"""dry-run 中だけ有効な、Claude API 呼び出し結果の一時キャッシュ。

テストのたびに課金が発生するのを防ぐため、DRY_RUN が有効なときは
同じ入力に対する API 呼び出し結果を .dryrun_cache.json に保存し、
2回目以降はキャッシュを返す（API を呼ばない）。

本番実行（DRY_RUN なし）では常に無効で、必ず実際の API を呼ぶ。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = REPO_ROOT / ".dryrun_cache.json"

_cache: dict[str, Any] | None = None


def is_enabled() -> bool:
    """dry-run 中だけキャッシュを使う。"""
    return os.getenv("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def cache_path() -> Path:
    path = Path(os.getenv("DRYRUN_CACHE_PATH") or DEFAULT_CACHE_PATH)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def make_key(namespace: str, *parts: str) -> str:
    """入力から安定したキャッシュキーを作る。"""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


def _load() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = cache_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            _cache = data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            logger.warning("%s が壊れています。キャッシュを作り直します。", path)
            _cache = {}
    else:
        _cache = {}
    return _cache


def _save() -> None:
    if _cache is None:
        return
    path = cache_path()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(_cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def cached_call(key: str, label: str, producer: Callable[[], Any]) -> Any:
    """キャッシュがあればそれを返し、無ければ producer() を実行して保存する。

    dry-run でないときは、キャッシュを一切使わず producer() をそのまま実行する。
    失敗した結果（None や空文字）は保存しない。保存してしまうと、
    次回以降その失敗がキャッシュから返り続けてしまうため。
    """
    if not is_enabled():
        return producer()

    cache = _load()
    cached = cache.get(key)
    if cached:
        logger.info("DRY_RUN キャッシュを使用（API呼び出しなし）: %s", label)
        return cached
    if key in cache:
        # 以前の実行で空の結果が入ってしまっていた場合は捨てて呼び直す
        del cache[key]

    value = producer()
    if value:
        cache[key] = value
        _save()
        logger.debug("DRY_RUN キャッシュに保存しました: %s", label)
    return value


def clear() -> None:
    """キャッシュを削除する。"""
    global _cache
    _cache = {}
    path = cache_path()
    if path.exists():
        path.unlink()
        logger.info("DRY_RUN キャッシュを削除しました: %s", path)
    else:
        logger.info("DRY_RUN キャッシュはありません: %s", path)
