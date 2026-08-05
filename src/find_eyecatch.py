"""記事に対応するアイキャッチ画像を「画像の中身」から選ぶ。

画像ファイル名は UUID・日付スラッグ・自動生成名などが混在していて当てにならないため、
ファイル名では判定しない。代わりに次の流れで選ぶ。

    1. image_mapping_override.json に手動指定があれば、それを最優先で使う
    2. 画像を Claude Vision で読み、埋め込まれた見出しテキストを抽出して
       image_index.json にキャッシュする（新しい画像だけを差分スキャン）
    3. 記事タイトルとキャッシュの内容を Claude に渡し、
       意味的にいちばん近い画像を1つ選ばせる（該当なしなら null）
    4. 選ばれなければ既定画像（assets/default_post_image.png）にフォールバック

画像の探索先はローカルフォルダ（LOCAL_IMAGE_FOLDER）と
Google ドライブのフォルダ（GDRIVE_FOLDER_ID）の両方。

なお Instagram Graph API はローカルファイルを直接アップロードできず、
公開 URL しか受け付けない。そのため本モジュールは画像パスに加えて
「投稿に使える公開 URL」の解決も行う（resolve_public_url）。

単体実行:
    python -m src.find_eyecatch "記事タイトル" [記事URL]
    python -m src.find_eyecatch --reindex      # 画像インデックスの更新だけ行う
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import anthropic

from src.dryrun_cache import cached_call, make_key

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE_PATH = REPO_ROOT / "assets" / "default_post_image.png"
DRIVE_CACHE_DIR = REPO_ROOT / ".cache" / "gdrive_images"
IMAGE_INDEX_PATH = REPO_ROOT / "image_index.json"
OVERRIDE_PATH = REPO_ROOT / "image_mapping_override.json"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

DEFAULT_MODEL = "claude-sonnet-5"
# Claude API に送れる画像サイズの上限に余裕を持たせる（base64 で約5MBまで）
MAX_IMAGE_BYTES = 3_500_000
MAX_IMAGE_EDGE = 1568  # これ以上大きい画像は縮小して送る（Pillow がある場合のみ）

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

VISION_PROMPT = """\
この画像はブログ記事のアイキャッチ画像です。

画像内に埋め込まれている見出し・タイトルのテキストを、書かれているとおりに
そのまま書き出してください。改行は半角スペースに置き換えて1行にしてください。

テキストが入っていない画像の場合は、代わりに画像の内容を日本語20字程度で
簡潔に説明してください（例: 「ノートとペンの俯瞰写真」）。

出力は抽出したテキスト（または説明）だけ。前置きや引用符は付けないでください。
"""

SELECT_SYSTEM_PROMPT = """\
あなたはブログ記事に使うアイキャッチ画像を選ぶ担当です。

記事タイトルと、画像候補の一覧（画像ID と、その画像に書かれている見出しテキスト）が
与えられます。記事の内容と意味的にいちばん近い画像を1つだけ選んでください。

判断の基準:
- 見出しテキストの言い回しが違っても、テーマが同じなら一致とみなしてよい。
- ただし「なんとなく近い」程度で選ばないこと。明確に対応する画像がない場合は
  無理に選ばず null を返すこと。
- 汎用的なプレースホルダー画像（「NEW POST」など記事固有でないもの）は選ばない。

出力は次の JSON のみ。前後に説明文やコードフェンスを付けないこと。
{"image_id": "選んだ画像ID または null", "reason": "選んだ理由を日本語1文で"}
"""


@dataclass
class Eyecatch:
    """選ばれたアイキャッチ画像。"""

    source: str  # "override" | "ai" | "default"
    name: str  # 元のファイル名
    local_path: Path | None = None
    drive_file_id: str | None = None
    public_url: str | None = None
    heading: str = ""  # 画像から抽出された見出しテキスト（選定の根拠）
    reason: str = ""  # Claude が挙げた選定理由

    @property
    def is_default(self) -> bool:
        return self.source == "default"

    def describe(self) -> str:
        label = {"override": "手動指定", "ai": "AI判定", "default": "既定画像"}
        where = label.get(self.source, self.source)
        detail = str(self.local_path) if self.local_path else self.drive_file_id or ""
        return f"{where} / {self.name}（{detail}）"


@dataclass
class ImageCandidate:
    """インデックス対象の画像1件。"""

    image_id: str  # image_index.json のキー（ローカルはパス、Drive は gdrive:{id}）
    name: str
    local_path: Path
    drive_file_id: str | None = None
    public_url: str | None = None


# --- image_index.json（見出しテキストのキャッシュ） --------------------------


@dataclass
class ImageIndex:
    """{ "画像パス": "抽出した見出しテキスト" } のキャッシュ。"""

    path: Path = IMAGE_INDEX_PATH
    entries: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ImageIndex":
        index_path = Path(path) if path else IMAGE_INDEX_PATH
        if not index_path.is_absolute():
            index_path = REPO_ROOT / index_path
        entries: dict[str, str] = {}
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    entries = {str(k): str(v) for k, v in data.items()}
            except json.JSONDecodeError:
                logger.error(
                    "%s が壊れています。空のインデックスとして扱います。", index_path
                )
        return cls(path=index_path, entries=entries)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(self.path)
        logger.debug("画像インデックスを保存しました: %s（%d件）", self.path, len(self.entries))


def _load_overrides() -> dict[str, str]:
    """image_mapping_override.json を読む（記事URL/タイトル → 画像パス）。"""
    if not OVERRIDE_PATH.exists():
        return {}
    try:
        data = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        logger.error("%s が壊れています。手動マッピングを無視します。", OVERRIDE_PATH)
        return {}
    if not isinstance(data, dict):
        logger.error("%s はオブジェクト形式である必要があります。", OVERRIDE_PATH)
        return {}
    # "_comment" で始まるキーは説明用なので無視する
    return {
        str(k): str(v)
        for k, v in data.items()
        if not str(k).startswith("_") and isinstance(v, str)
    }


# --- 画像の収集（ローカル / Google ドライブ） --------------------------------


def _is_image(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in IMAGE_EXTENSIONS


def collect_local_images(folder: str | Path | None = None) -> list[ImageCandidate]:
    """LOCAL_IMAGE_FOLDER 配下の画像を集める。"""
    folder = folder or os.getenv("LOCAL_IMAGE_FOLDER", "")
    if not folder:
        logger.debug("LOCAL_IMAGE_FOLDER が未設定のため、ローカル検索をスキップします。")
        return []

    folder_path = Path(folder).expanduser()
    if not folder_path.is_absolute():
        folder_path = REPO_ROOT / folder_path
    if not folder_path.is_dir():
        logger.warning("ローカル画像フォルダが見つかりません: %s", folder_path)
        return []

    candidates = [
        ImageCandidate(image_id=str(p), name=p.name, local_path=p)
        for p in sorted(folder_path.rglob("*"))
        if p.is_file() and _is_image(p.name)
    ]
    logger.info("ローカル画像フォルダ: %s（画像%d件）", folder_path, len(candidates))
    return candidates


def _build_drive_service():
    """サービスアカウント認証で Drive API のクライアントを作る。

    GOOGLE_SERVICE_ACCOUNT_JSON には、JSON ファイルのパスか
    JSON 文字列そのもの（GitHub Secrets 向け）のどちらを入れてもよい。
    """
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        logger.debug("GOOGLE_SERVICE_ACCOUNT_JSON が未設定です。")
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.error(
            "Google ドライブ検索に必要なライブラリが入っていません。"
            "`pip install -r requirements.txt` を実行してください。"
        )
        return None

    try:
        if raw.lstrip().startswith("{"):
            info = json.loads(raw)
        else:
            key_path = Path(raw).expanduser()
            if not key_path.is_absolute():
                key_path = REPO_ROOT / key_path
            if not key_path.is_file():
                logger.error("サービスアカウントの鍵ファイルが見つかりません: %s", key_path)
                return None
            info = json.loads(key_path.read_text(encoding="utf-8"))

        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=DRIVE_SCOPES
        )
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        logger.error("サービスアカウント認証情報を読めませんでした: %s", exc)
        return None


def _list_drive_images(service, folder_id: str) -> list[dict]:
    """フォルダ直下の画像ファイル一覧を取得する（ページング対応）。"""
    files: list[dict] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false and mimeType contains 'image/'"
    while True:
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=200,
                orderBy="name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def _download_drive_file(service, file_id: str, name: str) -> Path | None:
    """Drive の画像をローカルキャッシュにダウンロードする（既にあれば再利用）。"""
    import io

    from googleapiclient.http import MediaIoBaseDownload

    DRIVE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", name)
    dest = DRIVE_CACHE_DIR / f"{file_id}_{safe_name}"
    if dest.exists():
        return dest

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest.write_bytes(buffer.getvalue())
    logger.info("Googleドライブ: 画像をダウンロードしました: %s", dest)
    return dest


def collect_drive_images(folder_id: str | None = None) -> list[ImageCandidate]:
    """GDRIVE_FOLDER_ID のフォルダ内の画像を集める（必要に応じてダウンロード）。"""
    folder_id = folder_id or os.getenv("GDRIVE_FOLDER_ID", "")
    if not folder_id:
        logger.debug("GDRIVE_FOLDER_ID が未設定のため、ドライブ検索をスキップします。")
        return []

    service = _build_drive_service()
    if service is None:
        return []

    try:
        files = _list_drive_images(service, folder_id)
    except Exception as exc:  # HttpError を含む。検索失敗で全体を止めない
        logger.error(
            "Googleドライブの一覧取得に失敗しました: %s"
            "\n  → フォルダIDが正しいか、そのフォルダがサービスアカウントの"
            "\n     メールアドレスに共有されているか確認してください。",
            exc,
        )
        return []

    logger.info("Googleドライブ: 画像%d件", len(files))
    candidates: list[ImageCandidate] = []
    for file_info in files:
        file_id = file_info["id"]
        name = file_info["name"]
        try:
            local_path = _download_drive_file(service, file_id, name)
        except Exception as exc:
            logger.error("Googleドライブからの画像取得に失敗しました（%s）: %s", name, exc)
            continue
        if local_path is None:
            continue
        candidates.append(
            ImageCandidate(
                image_id=f"gdrive:{file_id}",
                name=name,
                local_path=local_path,
                drive_file_id=file_id,
                # 「リンクを知っている全員が閲覧可」なら、この URL で直接取得できる
                public_url=f"https://drive.google.com/uc?export=view&id={file_id}",
            )
        )
    return candidates


def collect_candidates() -> list[ImageCandidate]:
    """ローカルと Google ドライブの両方から画像候補を集める。"""
    candidates = collect_local_images() + collect_drive_images()
    # 同じ image_id が重複した場合は先に見つかった方（ローカル）を残す
    seen: dict[str, ImageCandidate] = {}
    for candidate in candidates:
        seen.setdefault(candidate.image_id, candidate)
    return list(seen.values())


# --- Claude Vision による見出しテキストの抽出 --------------------------------


def _encode_image(path: Path) -> tuple[str, str] | None:
    """画像を base64 にする。大きすぎる場合は Pillow があれば縮小する。"""
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        return None

    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        try:
            import io

            from PIL import Image

            with Image.open(io.BytesIO(data)) as img:
                img = img.convert("RGB")
                img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=85)
                data = buffer.getvalue()
                media_type = "image/jpeg"
            logger.debug("画像を縮小しました: %s（%d bytes）", path.name, len(data))
        except ImportError:
            logger.warning(
                "%s は大きすぎますが Pillow が無いため縮小できません。スキップします。",
                path.name,
            )
            return None
        except Exception as exc:
            logger.warning("%s の縮小に失敗しました: %s", path.name, exc)
            return None

    return base64.b64encode(data).decode("ascii"), media_type


def extract_heading(
    path: Path, client: anthropic.Anthropic, model: str
) -> str | None:
    """Claude Vision で画像内の見出しテキストを1件抽出する。"""
    encoded = _encode_image(path)
    if encoded is None:
        return None
    b64, media_type = encoded

    def call_api() -> str | None:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=300,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": VISION_PROMPT},
                        ],
                    }
                ],
            )
        except anthropic.APIError as exc:
            logger.error("画像の読み取りに失敗しました（%s）: %s", path.name, exc)
            return None

        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()
        return " ".join(text.split()) or None

    # 画像そのものが変わればキーも変わるよう、内容のハッシュを鍵に含める
    image_digest = hashlib.sha256(b64.encode("ascii")).hexdigest()[:32]
    return cached_call(
        make_key("vision", model, VISION_PROMPT, image_digest),
        f"画像の読み取り（{path.name}）",
        call_api,
    )


def update_image_index(
    candidates: list[ImageCandidate] | None = None,
    index: ImageIndex | None = None,
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> ImageIndex:
    """未スキャンの画像だけを読み取り、image_index.json を差分更新する。"""
    index = index or ImageIndex.load()
    candidates = candidates if candidates is not None else collect_candidates()

    known_ids = {c.image_id for c in candidates}
    pending = [c for c in candidates if c.image_id not in index.entries]

    # 元画像が消えたエントリはインデックスから外す
    removed = [key for key in index.entries if key not in known_ids]
    for key in removed:
        del index.entries[key]
    if removed:
        logger.info("存在しなくなった画像%d件をインデックスから削除しました。", len(removed))

    if not pending:
        logger.info(
            "画像インデックスは最新です（%d件、新規スキャンなし）。", len(index.entries)
        )
        if removed:
            index.save()
        return index

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key and client is None:
        logger.error(
            "ANTHROPIC_API_KEY が未設定のため、画像の読み取りができません。"
            "新規画像%d件はインデックスに追加されません。",
            len(pending),
        )
        return index

    client = client or anthropic.Anthropic(api_key=api_key)
    model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL

    logger.info("新しい画像%d件を Claude Vision で読み取ります。", len(pending))
    scanned = 0
    for candidate in pending:
        heading = extract_heading(candidate.local_path, client, model)
        if heading is None:
            logger.warning("見出しを抽出できませんでした: %s", candidate.name)
            continue
        index.entries[candidate.image_id] = heading
        scanned += 1
        logger.info("読み取り: %s → 「%s」", candidate.name, heading)
        # 途中で落ちても、それまでのスキャン結果は残す
        index.save()

    logger.info("画像インデックスを更新しました（新規%d件 / 合計%d件）", scanned, len(index.entries))
    index.save()
    return index


# --- Claude による画像選定 ---------------------------------------------------


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        braced = re.search(r"\{.*\}", text, re.DOTALL)
        if braced:
            text = braced.group(0)
    return json.loads(text)


def select_image_id(
    title: str,
    index: ImageIndex,
    candidates: list[ImageCandidate],
    client: anthropic.Anthropic | None = None,
    model: str | None = None,
) -> tuple[str | None, str]:
    """記事タイトルに意味的にいちばん近い画像 ID を Claude に選ばせる。

    戻り値は (image_id または None, 理由)。
    """
    available = {c.image_id: index.entries[c.image_id] for c in candidates if c.image_id in index.entries}
    if not available:
        return None, "見出しテキストを読み取れた画像がありません。"

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key and client is None:
        return None, "ANTHROPIC_API_KEY が未設定のため、画像を選定できません。"

    client = client or anthropic.Anthropic(api_key=api_key)
    model = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL

    listing = "\n".join(
        f"- {image_id} : {heading}" for image_id, heading in available.items()
    )
    prompt = (
        f"記事タイトル: {title}\n\n"
        f"画像候補（画像ID : 画像に書かれているテキスト）:\n{listing}\n"
    )

    def call_api() -> str | None:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=500,
                system=SELECT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            logger.error("画像選定の API 呼び出しに失敗しました: %s", exc)
            return None
        return "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        )

    # dry-run 中は、同じタイトル・同じ候補一覧なら再度 API を呼ばない
    text = cached_call(
        make_key("select", model, SELECT_SYSTEM_PROMPT, prompt),
        f"画像の選定（{title}）",
        call_api,
    )
    if text is None:
        return None, "画像選定の API 呼び出しに失敗しました。"

    try:
        data = _extract_json(text)
    except json.JSONDecodeError:
        logger.error("画像選定の出力を JSON として解釈できませんでした: %s", text[:300])
        return None, "選定結果を解釈できませんでした。"

    image_id = data.get("image_id")
    reason = str(data.get("reason") or "").strip()

    if image_id in (None, "null", ""):
        return None, reason or "該当する画像がないと判断されました。"
    image_id = str(image_id).strip()
    if image_id not in available:
        logger.warning("選ばれた画像IDが候補にありません: %s", image_id)
        return None, "選ばれた画像IDが候補一覧にありませんでした。"
    return image_id, reason


# --- エントリーポイント ------------------------------------------------------


def default_eyecatch() -> Eyecatch:
    return Eyecatch(
        source="default",
        name=DEFAULT_IMAGE_PATH.name,
        local_path=DEFAULT_IMAGE_PATH if DEFAULT_IMAGE_PATH.exists() else None,
        public_url=os.getenv("IG_IMAGE_URL") or None,
    )


def _resolve_override(
    title: str, link: str, candidates: list[ImageCandidate]
) -> Eyecatch | None:
    """image_mapping_override.json による手動指定を解決する（AI判定より優先）。"""
    overrides = _load_overrides()
    if not overrides:
        return None

    target = overrides.get(link) or overrides.get(title)
    if not target:
        return None

    logger.info("手動マッピングが見つかりました: %s → %s", link or title, target)

    # 候補一覧の image_id / ファイル名と一致すれば、その画像を使う
    for candidate in candidates:
        if target in (candidate.image_id, candidate.name) or (
            candidate.local_path and str(candidate.local_path) == target
        ):
            return Eyecatch(
                source="override",
                name=candidate.name,
                local_path=candidate.local_path,
                drive_file_id=candidate.drive_file_id,
                public_url=candidate.public_url,
                heading="（手動指定のため画像の読み取りは行っていません）",
                reason="image_mapping_override.json による手動指定",
            )

    # 候補になくても、ファイルパスとして存在すれば使う
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.is_file():
        return Eyecatch(
            source="override",
            name=path.name,
            local_path=path,
            heading="（手動指定のため画像の読み取りは行っていません）",
            reason="image_mapping_override.json による手動指定",
        )

    logger.warning(
        "手動指定された画像が見つかりませんでした: %s（AI判定にフォールバックします）",
        target,
    )
    return None


def find_eyecatch(title: str, link: str = "") -> Eyecatch:
    """記事に対応するアイキャッチ画像を返す。必ず何かを返す。"""
    logger.info("アイキャッチ画像を探します（タイトル: %s）", title)

    candidates = collect_candidates()
    logger.info("画像候補: 合計%d件", len(candidates))

    override = _resolve_override(title, link, candidates)
    if override is not None:
        logger.info("アイキャッチ画像を決定: %s", override.describe())
        logger.info("  根拠: %s", override.reason)
        return override

    if not candidates:
        result = default_eyecatch()
        logger.info("画像候補が1件もないため、既定画像を使います: %s", result.describe())
        return result

    index = update_image_index(candidates)
    image_id, reason = select_image_id(title, index, candidates)

    if image_id is None:
        result = default_eyecatch()
        logger.info("アイキャッチ画像を決定: %s", result.describe())
        logger.info("  根拠: 該当画像なし（%s）", reason)
        return result

    chosen = next(c for c in candidates if c.image_id == image_id)
    heading = index.entries.get(image_id, "")
    result = Eyecatch(
        source="ai",
        name=chosen.name,
        local_path=chosen.local_path,
        drive_file_id=chosen.drive_file_id,
        public_url=chosen.public_url,
        heading=heading,
        reason=reason,
    )
    logger.info("アイキャッチ画像を決定: %s", result.describe())
    logger.info("  画像パス  : %s", result.local_path)
    logger.info("  抽出見出し: 「%s」", heading)
    logger.info("  選定理由  : %s", reason or "(なし)")
    return result


def resolve_public_url(eyecatch: Eyecatch) -> str | None:
    """投稿 API に渡せる「公開 URL」を解決する。

    Instagram Graph API はローカルファイルを受け付けず、公開 URL しか使えない。
    次の順で解決する。

        1. eyecatch.public_url（Googleドライブの共有リンクなど）
        2. IMAGE_PUBLIC_BASE_URL + ファイル名（ローカル画像を公開している場合）
        3. IG_IMAGE_URL（既定画像の公開 URL）
    """
    if eyecatch.public_url:
        logger.info("投稿に使う画像URL: %s（%s）", eyecatch.public_url, eyecatch.source)
        return eyecatch.public_url

    base = os.getenv("IMAGE_PUBLIC_BASE_URL", "").strip()
    if base:
        name = eyecatch.name
        # Instagram 用に変換した JPEG を公開している場合、拡張子を差し替える
        # （src.prepare_ig_images が images_ig/ に .jpg を作る運用）
        extension = os.getenv("IMAGE_PUBLIC_EXTENSION", "").strip()
        if extension:
            name = f"{Path(name).stem}{extension if extension.startswith('.') else '.' + extension}"
        url = f"{base.rstrip('/')}/{quote(name)}"
        logger.info("投稿に使う画像URL: %s（IMAGE_PUBLIC_BASE_URL から生成）", url)
        return url

    fallback = os.getenv("IG_IMAGE_URL", "").strip()
    if fallback:
        logger.warning(
            "%s の公開URLを解決できなかったため、既定画像のURLを使います: %s"
            "\n  → ローカル画像をそのまま投稿したい場合は IMAGE_PUBLIC_BASE_URL を"
            "\n     設定するか、画像を Googleドライブ（リンク共有 ON）に置いてください。",
            eyecatch.name,
            fallback,
        )
        return fallback

    logger.error(
        "投稿に使える画像URLがありません。IG_IMAGE_URL または IMAGE_PUBLIC_BASE_URL を"
        "設定してください。"
    )
    return None


if __name__ == "__main__":  # pragma: no cover - 手動確認用
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    args = sys.argv[1:]
    if args and args[0] == "--reindex":
        update_image_index()
        raise SystemExit(0)

    if not args:
        print('使い方: python -m src.find_eyecatch "記事タイトル" [記事URL]')
        print("       python -m src.find_eyecatch --reindex")
        raise SystemExit(1)

    result = find_eyecatch(args[0], args[1] if len(args) > 1 else "")
    print(f"\n選ばれた画像: {result.describe()}")
    print(f"抽出見出し  : {result.heading}")
    print(f"公開URL     : {resolve_public_url(result)}")
