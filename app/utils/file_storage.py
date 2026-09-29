"""
Хранилище картинок (фото товаров, логотипы, обложки категорий).

Интерфейс Storage — чтобы на проде подменить локальную папку на
S3-совместимое хранилище без правок роутеров. Сейчас реализована только
LocalStorage: путь — UPLOADS_DIR (см. app/config/config.py), в
docker-compose он смонтирован volume'ом, иначе файлы теряются при пересборке.

Все картинки при загрузке пережимаются в webp и ужимаются до
MAX_IMAGE_SIDE по большей стороне — фото с телефона весят мегабайты,
а в мини-аппе они показываются в карточке шириной 390px.
"""
import io
import uuid
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_SIDE = 1600
WEBP_QUALITY = 85


class InvalidImage(ValueError):
    pass


class Storage(Protocol):
    def save(self, key: str, content: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # ключ приходит в том числе из URL (/v1/files/{key}) — не даём выйти за корень
        if self.root not in path.parents:
            raise FileNotFoundError(key)
        return path

    def save(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def process_image(content: bytes) -> bytes:
    """Любой формат, который понимает Pillow -> webp, повёрнутый по EXIF и ужатый"""
    if len(content) > MAX_UPLOAD_BYTES:
        raise InvalidImage("Файл больше 15 МБ")
    try:
        image = Image.open(io.BytesIO(content))
        image = ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImage("Не удалось прочитать картинку") from exc
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    out = io.BytesIO()
    image.save(out, format="WEBP", quality=WEBP_QUALITY)
    return out.getvalue()


def build_image_key(tenant_id: int, folder: str) -> str:
    return f"tenants/{tenant_id}/{folder}/{uuid.uuid4().hex}.webp"


def file_url(key: str | None) -> str | None:
    """Относительный URL — фронт приклеивает к нему адрес API"""
    return f"/v1/files/{key}" if key else None
