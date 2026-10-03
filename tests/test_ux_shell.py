"""UX 批次地基：三页导航 + /settings 路由 + 控制面注入缝（UX-01）。

跟 test_webui.py 同一条纪律——页面是静态文件，只验契约（路由发得出去、
关键钩子存在），不验浏览器里的运行时行为。
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from core.adapter.web.adapter import WEBUI_DIR, WebAdapter
from core.event_bus import EventBus


def _page(name: str) -> str:
    path = WEBUI_DIR / name
    assert path.exists(), f"webui/{name} 不存在"
    return path.read_text(encoding="utf-8")


# ---------- /settings 路由 ----------


def test_settings_page_exists():
    assert (WEBUI_DIR / "settings.html").is_file()


def test_settings_redirects_to_the_page():
    a = WebAdapter()
    r = TestClient(a.app, follow_redirects=False).get("/settings")
    assert r.status_code in (307, 308)
    assert "settings.html" in r.headers["location"]


def test_settings_page_served():
    a = WebAdapter()
    r = TestClient(a.app).get("/static/settings.html")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# ---------- 三页共享导航 ----------


def test_index_has_full_nav_with_chat_active():
    page = _page("index.html")
    assert 'href="/settings"' in page
    assert 'href="/logs"' in page
    assert re.search(r'class="active"\s+href="/"', page), "聊天页没把「聊天」标为当前"


def test_logs_has_nav_with_logs_active():
    page = _page("logs.html")
    assert 'href="/"' in page
    assert 'href="/settings"' in page
    assert re.search(r'class="active"\s+href="/logs"', page), "日志页没把「日志」标为当前"


def test_settings_has_nav_with_settings_active():
    page = _page("settings.html")
    assert 'href="/"' in page
    assert 'href="/logs"' in page
    assert re.search(r'class="active"\s+href="/settings"', page), "设置页没把「设置」标为当前"


def test_all_pages_link_the_three_sections():
    for name in ("index.html", "logs.html", "settings.html"):
        page = _page(name)
        for label in ("聊天", "设置", "日志"):
            assert label in page, f"{name} 缺导航项：{label}"


# ---------- settings.html 的页面纪律 ----------


def test_settings_uses_textcontent_no_innerhtml():
    src = _page("settings.html")
    assert "textContent" in src, "设置页没用 textContent（XSS 纪律）"
    assert "innerHTML" not in src, "设置页不该塞 innerHTML"


def test_settings_makes_no_framework_or_cdn_calls():
    low = _page("settings.html").lower()
    for m in ("vue", "react", "jquery", "cdn.", "unpkg.com", "jsdelivr"):
        assert m not in low, f"设置页引了不该有的东西：{m!r}"


# ---------- 控制面注入缝 ----------


def test_extra_routes_injected_and_mounted():
    """注入 extra_routes 后，回调里挂的路由真的生效。"""

    def control(app: FastAPI) -> None:
        @app.get("/api/ping")
        async def ping() -> JSONResponse:  # noqa: D401
            return JSONResponse({"pong": True})

    a = WebAdapter(extra_routes=control)
    r = TestClient(a.app).get("/api/ping")
    assert r.status_code == 200
    assert r.json() == {"pong": True}


def test_no_extra_routes_means_no_control_route():
    """不注入就不多挂任何路由——裸适配器的行为跟以前完全一样。"""
    a = WebAdapter()
    paths = {getattr(r, "path", None) for r in a.app.routes}
    assert "/api/ping" not in paths


def test_extra_routes_do_not_break_ws():
    """控制面路由不能挡住 /ws——聊天照常。"""
    import asyncio

    def control(app: FastAPI) -> None:
        @app.get("/api/x")
        async def x() -> JSONResponse:
            return JSONResponse({})

    a = WebAdapter(extra_routes=control)
    with TestClient(a.app).websocket_connect("/ws?session_id=x") as ws:
        ws.send_json({"content": "还在吗"})
        got = asyncio.run(asyncio.wait_for(a.recv(), timeout=1))
    assert got.content == "还在吗"


def test_nav_does_not_introduce_framework_to_index_or_logs():
    for name in ("index.html", "logs.html"):
        low = _page(name).lower()
        for m in ("vue", "react", "jquery", "cdn.", "unpkg.com", "jsdelivr"):
            assert m not in low, f"{name} 引了不该有的东西：{m!r}"


# ---------- &newtale 重置会话（UX-02）----------


def test_newtale_command_defined():
    """&newtale 命令常量与判定函数存在。"""
    page = _page("index.html")
    assert "&newtale" in page, "页面没有 &newtale 命令"
    assert re.search(r"function\s+isResetCommand", page), "没有命令判定函数"


def test_newtale_sends_clear_action_not_content():
    """&newtale 必须复用 clear 控制帧（action=clear），而不是当聊天发 content。

    这是"会话号保持不变"的关键：clear 删消息、留会话行，session_id 不动。
    """
    page = _page("index.html")
    # 命令分支里发的是 action=clear
    assert re.search(r'action\s*:\s*"clear"', page), "&newtale 没有发 clear 帧"
    # 判定函数在 send 里被用上（否则命令形同虚设）
    assert re.search(r"isResetCommand\s*\(", page), "判定函数没被调用"


def test_newtale_mentioned_in_placeholder():
    """提示语不堆命令了（用户嫌长）——只留一句招呼，命令靠 &help 发现。"""
    page = _page("index.html")
    assert "&newtale" in page
    m = re.search(r'placeholder="([^"]*)"', page)
    assert m, "没有 placeholder"
    ph = m.group(1)
    assert "&newtale" not in ph, "提示语不该再堆命令了"
    assert len(ph) <= 20, f"提示语太长了：{ph!r}"


# ---------- &help 命令 ----------


def test_help_command_defined_and_listed():
    page = _page("index.html")
    assert re.search(r"function\s+isHelpCommand", page), "没有 &help 判定函数"
    assert re.search(r"function\s+showHelp", page), "没有帮助内容函数"
    assert "&newtale" in page, "帮助里应提到 &newtale"


def test_help_is_local_not_sent_to_server():
    """&help 是本地命令：不出气泡、不发 WS（在 send 里早返回）。"""
    page = _page("index.html")
    # showHelp 用系统提示（居中 pill），不调 addBubble
    assert re.search(r"function\s+showHelp[^}]*addSystemNote", page, re.S), \
        "帮助内容应该走 addSystemNote，不出气泡"


def test_commands_accept_both_prefixes():
    """& 和 / 前缀都认——省得用户记是哪个。"""
    page = _page("index.html")
    assert "commandOf" in page
    assert '"&"' in page and '"/"' in page, "应同时认 & 和 / 前缀"


# ---------- QQ 服务开关与抽屉（PR1）----------


def test_settings_has_qq_service_block():
    page = _page("settings.html")
    assert "/api/platforms/qq" in page, "设置页没有 QQ 开关的接口引用"
    assert re.search(r"function\s+buildQQDrawer", page), "没有 QQ 抽屉构建函数"
    assert re.search(r"function\s+postQQ", page), "没有 QQ 开关提交函数"


def test_settings_has_failure_banner_with_retry():
    """顶部黄色告示条：滚动字 + 重试按钮（"跳闸"提示）。"""
    page = _page("settings.html")
    assert 'id="banner"' in page
    assert 'id="bannerText"' in page
    assert 'id="bannerRetry"' in page
    assert "重试" in page


def test_settings_has_drawer_sections():
    page = _page("settings.html")
    assert re.search(r"function\s+makeDrawer", page), "没有抽屉构建函数"
    assert "drawer-head" in page and "drawer-body" in page
