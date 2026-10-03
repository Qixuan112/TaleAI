"""UX-05：设置读写 API（fields 驱动表单 + 校验 + 原子写）。

契约要点：
- fields 接口是表单的输入契约（前端据此渲染，不硬编码字段名）
- values 接口回当前值，**secrets 打码**（只回 set/未 set，永不回明文）
- save 只收**已声明**字段；密钥留空=保持原值；写入原子
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.web.adapter import WebAdapter
from core.config import api as cfg_api
from core.config.api import _coerce, _get_dotted, _set_dotted


# ---------- 点号路径读写 ----------


def test_dotted_get_set_roundtrip():
    d = {"llm": {"model": "m"}}
    assert _get_dotted(d, "llm.model") == "m"
    _set_dotted(d, "llm.base_url", "http://x")
    assert d["llm"]["base_url"] == "http://x"
    _set_dotted(d, "wake.scope", "all")
    assert d["wake"]["scope"] == "all"


def test_dotted_get_missing_returns_default():
    assert _get_dotted({}, "a.b.c", "dflt") == "dflt"


# ---------- 类型归一 ----------


class _Spec:
    def __init__(self, type="text", default=None):
        self.type = type
        self.default = default


def test_coerce_number():
    assert _coerce("10", _Spec("number", 0)) == 10
    assert _coerce("2.5", _Spec("number", 0)) == 2.5


def test_coerce_bool():
    assert _coerce("true", _Spec("bool", False)) is True
    assert _coerce("on", _Spec("bool", False)) is True
    assert _coerce("false", _Spec("bool", False)) is False
    assert _coerce(True, _Spec("bool", False)) is True


def test_coerce_list_from_comma_string():
    assert _coerce("塔利, 小塔", _Spec("text", ["塔利"])) == ["塔利", "小塔"]
    assert _coerce("塔利", _Spec("text", ["塔利"])) == ["塔利"]


def test_coerce_text_passthrough():
    assert _coerce("hello", _Spec("text", "")) == "hello"


# ---------- 路由：fields / values / save ----------


@pytest.fixture
def client(tmp_path):
    """把设置路由挂在一个裸适配器上，config_dir 指向 tmp。"""
    a = WebAdapter(extra_routes=cfg_api.build_settings_routes(config_dir=tmp_path))
    return TestClient(a.app)


def test_fields_returns_declared_specs(client):
    r = client.get("/api/settings/fields?domain=config")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    keys = {f["key"] for f in body["fields"]}
    assert {"llm.base_url", "llm.model", "wake.words", "wake.scope"} <= keys
    # 面板要用的元信息都在
    one = next(f for f in body["fields"] if f["key"] == "wake.scope")
    assert one["type"] == "select"
    assert "group" in one["choices"]


def test_fields_unknown_domain_errors(client):
    r = client.get("/api/settings/fields?domain=nope")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_values_masks_secrets(client):
    """密钥域只回 set 状态，绝不回明文。"""
    r = client.get("/api/settings/values?domain=secrets")
    assert r.status_code == 200
    v = r.json()["values"]
    assert v["llm.api_key"] == {"set": False}   # 未设置


def test_save_writes_and_reads_back(client, tmp_path):
    r = client.post("/api/settings/values", json={
        "domain": "config",
        "values": {"llm.model": "gpt-x", "wake.words": "塔利, 小塔", "wake.scope": "all"},
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 写回后再读，值应生效
    v = client.get("/api/settings/values?domain=config").json()["values"]
    assert v["llm.model"] == "gpt-x"
    assert v["wake.words"] == ["塔利", "小塔"]   # 逗号串归一成列表
    assert v["wake.scope"] == "all"


def test_save_rejects_unknown_key(client):
    r = client.post("/api/settings/values", json={
        "domain": "config", "values": {"evil.key": "x"},
    })
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_save_empty_secret_keeps_old_value(client):
    """密钥留空提交 = 不改（改别的字段时不该误清密钥）。"""
    # 先设一个密钥
    client.post("/api/settings/values", json={
        "domain": "secrets", "values": {"llm.api_key": "sk-123"},
    })
    assert client.get("/api/settings/values?domain=secrets").json()["values"]["llm.api_key"] == {"set": True}
    # 再提交空 → 应保持
    client.post("/api/settings/values", json={
        "domain": "secrets", "values": {"llm.api_key": ""},
    })
    assert client.get("/api/settings/values?domain=secrets").json()["values"]["llm.api_key"] == {"set": True}


def test_save_persists_to_disk(client, tmp_path):
    client.post("/api/settings/values", json={
        "domain": "config", "values": {"bot.name": "初念"},
    })
    import json
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["bot"]["name"] == "初念"


def test_settings_routes_absent_without_injection():
    """不注入 extra_routes 就不挂设置路由——裸适配器行为不变。"""
    a = WebAdapter()
    paths = {getattr(r, "path", None) for r in a.app.routes}
    assert "/api/settings/fields" not in paths


# ---------- 兔老师审查：写接口加固（2026-10-03）----------


def test_save_rejects_non_json_content_type(client):
    """非 application/json → 415（挡跨站表单 POST）。"""
    r = client.post("/api/settings/values",
                    content='domain=config&values=x',
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_save_rejects_cross_origin(client):
    """Origin 与 Host 不同源 → 403（CSRF 防护）。"""
    r = client.post("/api/settings/values",
                    json={"domain": "config", "values": {}},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_save_allows_origin_port_variant(client):
    """Origin 与 Host 只有端口书写形态差异（同主机名）→ 放行。

    反向代理会重写 Host（可能去掉端口）；解析口径必须与 web/adapter.py 的
    _origin_allowed 对齐——两边都 urlparse 取 hostname（评审 rev2）。
    真正的跨站仍被拒（上一条用例）。
    """
    r = client.post("/api/settings/values",
                    json={"domain": "config", "values": {"bot.name": "塔利"}},
                    headers={"Origin": "http://testserver:8000"})
    assert r.status_code == 200


def test_save_rejects_malformed_json_as_400_not_500(client):
    """畸形 JSON 归 400，不是 500 堆栈。"""
    r = client.post("/api/settings/values",
                    content="{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_save_rejects_non_object_body(client):
    r = client.post("/api/settings/values",
                    content="[1,2,3]",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_save_allows_json_no_origin(client):
    """没有 Origin（脚本/curl 客户端）→ 放行（同源策略只约束浏览器）。"""
    r = client.post("/api/settings/values",
                    json={"domain": "config", "values": {"bot.name": "塔利"}})
    assert r.status_code == 200
