"""图片暂存（UX-06）：多模态输入的落盘与回收。

**位置**：`data/temp/img/`（用户定）。库里只存**文件名**，不存 base64——
会话库不该被图片撑大，且图片是可回收的临时物，两者生命周期不同。

**容量回收**：`data/temp/` 总大小超过 100MB → 删**最早**的（按 mtime 升序），
删到降到阈值以下。写新图后顺手回收一次，不做后台定时任务（简单够用）。
为什么是"删最早"而不是 LRU：图片落盘即被读走喂模型，之后基本不再用，
"最早 = 最该扔"的近似够准，还免去维护访问时间。

**发给模型**：读文件转 base64 data URI（OpenAI vision 接受 `data:image/png;base64,...`）。

分寸：本模块只做"存/取/清"三件事，不认识 Message / ChatLLM / 会话——
谁用谁取，保持可脱网测穿（download 是唯一碰网络的地方，失败一律返回 None）。
"""

import base64
import hashlib
import logging
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: data/temp/ 总大小上限（字节）。用户定 100MB。
MAX_DIR_BYTES = 100 * 1024 * 1024

#: 单次下载上限，防止被超大图拖垮。
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

#: 单张图上传上限（面板/前端也用这个数提示）。
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: 单条消息最多带几张图。
MAX_IMAGES_PER_MESSAGE = 4

#: image_store.py 在 src/core/ → parents[2] = 项目根
_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = _ROOT / "data" / "temp" / "img"

#: 允许的图片类型 → 扩展名（也用于从字节嗅探）
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)
_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}
_MIME_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp",
}


def _dir(base: Path | None) -> Path:
    return Path(base) if base is not None else DEFAULT_DIR


def sniff_mime(data: bytes) -> str | None:
    """从字节头认图片类型；不认识返回 None（不猜）。"""
    for magic, _ext, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WEBP: 'RIFF' .... 'WEBP'
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def total_bytes(base: Path | None = None) -> int:
    """当前目录里所有文件的总大小（不含 -wal/-shm 之类，只管文件本身）。"""
    d = _dir(base)
    if not d.is_dir():
        return 0
    return sum(p.stat().st_size for p in d.iterdir() if p.is_file())


def cleanup(base: Path | None = None, *, protect: str | None = None) -> int:
    """目录超 MAX_DIR_BYTES 就删最早的，删到降到阈值以下。返回删了几个。

    protect：刚写的文件名——绝不能被自己删掉（它是"最新"的，正常轮不到，
    但万一单张巨大、删完它才够，就跳过它、继续删别的）。
    """
    d = _dir(base)
    if not d.is_dir():
        return 0
    files = [p for p in d.iterdir() if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    if total <= MAX_DIR_BYTES:
        return 0
    files.sort(key=lambda p: p.stat().st_mtime)  # 最早的在前
    removed = 0
    for p in files:
        if total <= MAX_DIR_BYTES:
            break
        if protect is not None and p.name == protect:
            continue
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
            removed += 1
        except OSError:
            logger.warning("删旧图失败：%s", p, exc_info=True)
    if total > MAX_DIR_BYTES:
        # 删完仍超限：protect 的那张本身超过阈值、或文件删不掉。正常路径到不了
        # （上传上限 5MB ≪ 100MB），绕过上传直接往目录塞大文件才会；留一条痕迹，
        # 别让回收卡住时无声无息（评审 rev2，行为不变）。
        logger.warning(
            "cleanup 后目录仍超限（%.1f MB > %.1f MB），protect=%r 可能过大",
            total / 1024 / 1024, MAX_DIR_BYTES / 1024 / 1024, protect,
        )
    return removed


def save_bytes(data: bytes, *, base: Path | None = None) -> str:
    """把图片字节写进目录，返回文件名（含扩展名）。

    文件名 = 内容 sha1 前 16 位 + 扩展名：同样内容只占一份、天然去重，
    也不用生成随机名再记映射。
    """
    mime = sniff_mime(data)
    if mime is None:
        raise ValueError("不是认识的图片类型（png/jpg/gif/webp）")
    ext = _MIME_EXT.get(mime, ".bin")
    digest = hashlib.sha1(data).hexdigest()[:16]
    name = f"{digest}{ext}"
    d = _dir(base)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    if not path.exists():  # 同内容已存在就复用，不重写
        path.write_bytes(data)
    cleanup(base, protect=name)
    return name


def save_data_uri(uri: str, *, base: Path | None = None) -> str | None:
    """`data:image/png;base64,XXXX` → 落盘，返回文件名。畸形返回 None。"""
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return None
    try:
        header, _, b64 = uri.partition(",")
        if ";base64" not in header:
            return None
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if sniff_mime(raw) is None:
        return None
    return save_bytes(raw, base=base)


def to_data_uri(name: str, *, base: Path | None = None) -> str | None:
    """文件名 → base64 data URI（喂模型用）。文件不在/类型不认识 → None。

    图片可能已被回收（见 cleanup）——所以要能优雅地"没有它"，而不是抛。
    """
    if not name:
        return None
    path = _dir(base) / Path(name).name  # 只取文件名，防目录穿越
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mime = sniff_mime(data) or _EXT_MIME.get(path.suffix.lower())
    if mime is None:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def download(url: str, *, base: Path | None = None, timeout: float = 10.0) -> str | None:
    """下载一张图（QQ 图片 URL 会过期，必须落地）。失败返回 None，绝不抛。

    网络是外部依赖：超时、404、过大、非图片——一律当"没这张图"，不阻断链路。
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TaleAI/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 只读 MAX_DOWNLOAD_BYTES+1，超出即判过大
            data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    except Exception:
        logger.warning("下载图片失败：%s", url, exc_info=True)
        return None
    if len(data) > MAX_DOWNLOAD_BYTES:
        logger.warning("图片过大，丢弃：%s", url)
        return None
    if sniff_mime(data) is None:
        return None
    return save_bytes(data, base=base)
