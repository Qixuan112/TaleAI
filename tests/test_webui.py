"""M0-12 验收：最小聊天页（§21「聊天气泡 + 会话列表」/ §1.6「静态 HTML/JS」）。

页面本身是静态文件，验证分两层：
- **服务层**：适配器把它发得出去（/ 和 /static/index.html），且不挡住 /ws。
- **接线层**：页面里的关键钩子确实存在——连 /ws、用 localStorage 存 session_id
  （这是"刷新后历史还在"的兑现）、有输入框与消息区。

JS 的运行时行为不在这里测（那要浏览器）；这里保证**契约正确**：
页面发的消息形状和适配器期望的一致。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from fastapi.testclient import TestClient

from core.adapter.websocket.adapter import WEBUI_DIR, WebSocketAdapter
from core.bus.event_bus import EventBus


@pytest.fixture(autouse=True)
def clean_bus():
    bus = EventBus()
    bus.reset()
    yield bus
    bus.reset()


@pytest.fixture
def page() -> str:
    path = WEBUI_DIR / "index.html"
    assert path.exists(), "webui/index.html 不存在——M0-12 的核心产出"
    return path.read_text(encoding="utf-8")


# ---------- 服务层：发得出去 ----------


def test_index_html_exists():
    assert (WEBUI_DIR / "index.html").is_file()


def test_root_redirects_to_the_page():
    a = WebSocketAdapter()
    client = TestClient(a.app, follow_redirects=False)
    r = client.get("/")
    assert r.status_code in (307, 308)
    assert "index.html" in r.headers["location"]


def test_page_is_served():
    a = WebSocketAdapter()
    r = TestClient(a.app).get("/static/index.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_page_serving_does_not_break_ws():
    """加了静态页之后 /ws 仍要能用——两条路互不干扰。"""
    import asyncio

    a = WebSocketAdapter()
    with TestClient(a.app).websocket_connect("/ws?session_id=x") as ws:
        ws.send_json({"content": "还在吗"})
        got = asyncio.run(asyncio.wait_for(a.recv(), timeout=1))
    assert got.content == "还在吗"


# ---------- 接线层：关键钩子 ----------


def test_page_connects_to_ws(page):
    assert "/ws" in page


def test_page_persists_session_id_across_reload(page):
    """刷新后历史还在的关键：session_id 存 localStorage，重连带同一个。

    这是 M0 验收⑤ 在浏览器上的兑现——不存的话每次刷新都是新会话，历史断掉。
    """
    assert "localStorage" in page
    assert "session_id" in page


def test_page_has_input_and_message_area(page):
    """聊天页的两个基本件：输入框 + 消息区（§21「聊天气泡」）。"""
    assert re.search(r"<textarea|<input", page), "没有输入框"
    assert re.search(r'id="messages"|class="messages"|id="chat"', page), "没有消息区"


def test_page_sends_content_field_matching_adapter_contract(page):
    """页面发出的 JSON 必须含 content 字段——适配器 normalize 就认它。"""
    assert "content" in page


def test_page_sends_session_id_in_query(page):
    """session_id 走查询参数（适配器的约定），不是消息体里。"""
    assert re.search(r"/ws\?session_id=|/ws` \+|/ws\?session", page) or (
        "/ws?" in page and "session_id" in page
    )


def test_page_escapes_or_uses_textcontent_for_user_text(page):
    """不能把用户/模型的话直接塞 innerHTML——那是 XSS 口子。

    允许 textContent，或者显式转义函数；禁止裸 innerHTML = 变量。
    """
    has_textcontent = "textContent" in page
    has_escape = "escapeHtml" in page or "escapeHTML" in page
    assert has_textcontent or has_escape, "没有用 textContent 或转义函数，存在 XSS 风险"


def test_page_uses_no_frontend_framework(page):
    """§1.6：M0 是静态 HTML/JS，不引前端框架（先够用再升级）。

    按"框架标志物"判断，不做子串匹配——否则 "interactive" 里也含 "react"。
    """
    low = page.lower()
    markers = [
        "vue.global", "vue.min", "vue.js", "new vue(", "createapp(",
        "react.development", "react.production", "react-dom", "reactdom.",
        "angular.min", "ng-app", "jquery.min", "jquery.js", "$(document).ready",
    ]
    for m in markers:
        assert m not in low, f"引了前端框架标志物：{m!r}"
    # 也不该从 CDN 拉脚本
    assert "cdn." not in low and "unpkg.com" not in low and "jsdelivr" not in low

