import base64
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import APIError
from app.settings import Settings


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    width: int
    height: int

    @property
    def data_url(self) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(self.data).decode("ascii")


def prepare_image(data: bytes, settings: Settings) -> PreparedImage:
    if len(data) > settings.max_upload_bytes:
        raise APIError(413, "IMAGE_TOO_LARGE", "图片不能超过 8 MiB，请压缩后重试。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP"}:
                    raise APIError(
                        400, "UNSUPPORTED_IMAGE", "仅支持 JPEG、PNG 和 WebP 图片。"
                    )
                if original.width * original.height > settings.max_image_pixels:
                    raise APIError(400, "INVALID_IMAGE", "图片像素过多，请缩小后重试。")
                if min(original.size) < 32:
                    raise APIError(
                        400, "INVALID_IMAGE", "图片过小，宽高均需至少 32 像素。"
                    )
                original.verify()

            with Image.open(BytesIO(data)) as original:
                original.load()
                normalized = ImageOps.exif_transpose(original)
                normalized.thumbnail(
                    (settings.max_image_edge, settings.max_image_edge),
                    Image.Resampling.LANCZOS,
                )
                rgba = normalized.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                output = BytesIO()
                # The new RGB image has no source metadata or EXIF attached.
                rgb.save(output, format="JPEG", quality=85, optimize=True)
                return PreparedImage(output.getvalue(), rgb.width, rgb.height)
    except APIError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise APIError(
            400, "INVALID_IMAGE", "无法读取图片，请上传清晰、完整的图片。"
        ) from exc
