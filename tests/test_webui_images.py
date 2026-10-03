"""UX-07：WebUI 图片（上传端点 + WS images 帧 + 前端钩子）。

- /api/upload：收图落盘、回文件名；拒空/拒超大/拒非图
- /api/img/{name}：取回图片
- WebAdapter.normalize：消息帧里的 images 透传 + 上限
- index.html：有附件入口、粘贴、缩略图、历史带图渲染的钩子
"""

import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core import image_store
from core.adapter.web.adapter import WEBUI_DIR, WebAdapter
from core.image_api import build_upload_routes

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(image_store, "DEFAULT_DIR", tmp_path / "img")
    a = WebAdapter(extra_routes=build_upload_routes())
    return TestClient(a.app)


# ---------- 上传端点 ----------


def test_upload_accepts_png(client):
    r = client.post("/api/upload", content=PNG_1PX,
                    headers={"Content-Type": "image/png"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["name"].endswith(".png")


def test_upload_rejects_non_image(client):
    r = client.post("/api/upload", content=b"hello",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_upload_rejects_empty(client):
    r = client.post("/api/upload", content=b"")
    assert r.status_code == 400


def test_upload_rejects_oversize(client, monkeypatch):
    monkeypatch.setattr(image_store, "MAX_UPLOAD_BYTES", 10)
    r = client.post("/api/upload", content=PNG_1PX,
                    headers={"Content-Type": "image/png"})
    assert r.status_code == 413


def test_uploaded_image_is_served_back(client, tmp_path):
    r = client.post("/api/upload", content=PNG_1PX,
                    headers={"Content-Type": "image/png"})
    name = r.json()["name"]
    got = client.get("/api/img/" + name)
    assert got.status_code == 200
    assert got.content == PNG_1PX


def test_missing_image_404(client):
    assert client.get("/api/img/nope.png").status_code == 404


def test_img_path_traversal_blocked(client):
    """不能用 .. 之类的名字读到目录外的文件。"""
    r = client.get("/api/img/..%2F..%2Fsessions.db")
    assert r.status_code == 404


# ---------- WS 帧：images ----------


def test_ws_normalize_passes_images():
    a = WebAdapter()
    m = a.normalize({"content": "看图", "session_id": "web:x", "images": ["a.png", "b.png"]})
    assert m.images == ["a.png", "b.png"]


def test_ws_normalize_caps_image_count():
    a = WebAdapter()
    many = [f"{i}.png" for i in range(10)]
    m = a.normalize({"content": "x", "images": many})
    assert len(m.images) == image_store.MAX_IMAGES_PER_MESSAGE


def test_ws_normalize_ignores_bad_images_field():
    a = WebAdapter()
    assert a.normalize({"content": "x", "images": "notalist"}).images == []
    assert a.normalize({"content": "x"}).images == []


# ---------- 前端钩子 ----------


def _page(name="index.html"):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


def test_page_has_attach_and_paste_hooks():
    p = _page()
    assert "paste" in p, "没有粘贴监听"
    assert "api/upload" in p, "没有上传调用"
    assert 'id="file"' in p and "image/*" in p, "没有文件选择入口"


def test_page_sends_images_in_frame():
    p = _page()
    assert "images: images" in p or "images:images" in p.replace(" ", ""), "帧里没带 images"


def test_page_renders_images_via_src_attribute():
    """图用 <img src> 显示，且是 createElement 造的（不塞 innerHTML）。"""
    p = _page()
    assert 'createElement("img")' in p
    assert "api/img/" in p


def test_page_declares_image_limit():
    p = _page()
    assert "最多 4 张" in p or ">= 4" in p
