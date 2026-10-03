"""测试：/api/platforms/qq —— 设置页 QQ 开关的控制面（PR1）。

纯离线：FakeService 假扮 QQService（鸭子接口 start/stop/status），
config_dir 指向 tmp；不碰真 QQ、不起真服务。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.web.adapter import WebAdapter
from core.config.api import build_platforms_routes


class FakeService:
    def __init__(self, fail_detail: str = ""):
        self.calls: list[str] = []
        self._fail_detail = fail_detail

    async def start(self):
        self.calls.append("start")
        if self._fail_detail:
            return {"state": "failed", "detail": self._fail_detail, "connected": False}
        return {"state": "running", "detail": "", "connected": False}

    async def stop(self):
        self.calls.append("stop")
        return {"state": "disabled", "detail": "", "connected": False}

    def status(self):
        return {"state": "disabled", "detail": "", "connected": False}


@pytest.fixture
def make_client(tmp_path):
    def _make(service: FakeService | None = None):
        svc = service if service is not None else FakeService()
        a = WebAdapter(extra_routes=build_platforms_routes(svc, config_dir=tmp_path))
        return TestClient(a.app), svc, tmp_path
    return _make


def test_get_default_is_disabled(make_client):
    client, _svc, _ = make_client()
    r = client.get("/api/platforms/qq")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["enabled"] is False
    assert j["state"] == "disabled"


def test_post_enable_starts_and_persists(make_client):
    """开：先落 platforms.json（用户意图），再致动 service.start()。"""
    client, svc, cfg_dir = make_client()
    r = client.post("/api/platforms/qq", json={"enabled": True})
    assert r.status_code == 200
    j = r.json()
    assert j["enabled"] is True and j["state"] == "running"
    assert svc.calls == ["start"]
    saved = json.loads((cfg_dir / "platforms.json").read_text(encoding="utf-8"))
    assert saved["qq"]["enabled"] is True


def test_post_disable_stops(make_client):
    client, svc, _ = make_client()
    client.post("/api/platforms/qq", json={"enabled": True})
    r = client.post("/api/platforms/qq", json={"enabled": False})
    assert r.json()["enabled"] is False and r.json()["state"] == "disabled"
    assert svc.calls == ["start", "stop"]


def test_post_rejects_non_bool(make_client):
    client, svc, _ = make_client()
    r = client.post("/api/platforms/qq", json={"enabled": "yes"})
    assert r.status_code == 400
    assert svc.calls == []


def test_post_requires_json_content_type(make_client):
    client, _svc, _ = make_client()
    r = client.post("/api/platforms/qq", content="enabled=true",
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_post_rejects_cross_origin(make_client):
    client, _svc, _ = make_client()
    r = client.post("/api/platforms/qq", json={"enabled": True},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_start_failure_is_200_with_failed_state(make_client):
    """启动失败是**正常运行态**：200 + state=failed + detail——页面据此出告示条。"""
    client, _svc, _ = make_client(FakeService(fail_detail="端口可能被占用（uvicorn 退出码 3）"))
    r = client.post("/api/platforms/qq", json={"enabled": True})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["state"] == "failed"
    assert "端口可能被占用" in j["detail"]


def test_get_reflects_persisted_config(make_client):
    client, _svc, cfg_dir = make_client()
    (cfg_dir / "platforms.json").write_text(
        json.dumps({"qq": {"enabled": True}}), encoding="utf-8")
    r = client.get("/api/platforms/qq")
    assert r.json()["enabled"] is True
