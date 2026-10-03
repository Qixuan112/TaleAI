"""设置读写 API（UX-05）。

三条路由，挂在 FastAPI app 上（由 main 通过 `extra_routes` 注入，见
`WebAdapter`）——**适配器不认识 Config**，业务留在这一层，
守住 §22 import 单向：

    GET  /api/settings/fields?domain=config   字段定义（面板据此渲染表单）
    GET  /api/settings/values?domain=config   当前值（secrets 打码）
    POST /api/settings/values                 校验 + 原子写

**为什么表单由字段定义驱动**：`config/fields.py` 的 `FieldSpec` 就是为这个
存在的（§13「面板驱动，用户不碰 JSON」）。前端不硬编码字段——加一个配置项
只改 fields.py，面板自动出现。所以 fields 接口是这三条里最不该省的一条。

**安全**（§19-11 精神）：
- 写入只接受**已声明**的字段（fields_for(domain) 里有的 key），其余一律拒——
  防止有人拿这个接口往配置里塞任意键
- secrets 域的值**永远不回明文**，只回"已设置/未设置"
- 密钥留空提交 = 保持原值（不能让用户改别的字段时误清密钥）
"""

from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.config.fields import fields_for
from core.config.loader import DOMAINS, Config


# ---------- 点号路径读写 ----------


def _get_dotted(data: dict, key: str, default: Any = None) -> Any:
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_dotted(data: dict, key: str, value: Any) -> None:
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


# ---------- 类型归一 ----------


def _coerce(raw: Any, spec) -> Any:
    """把面板送来的字符串归一成配置该有的类型。

    - 字段默认值是 list（如 wake.words）→ 逗号拆成列表
      （fields.py 没有 list 类型，面板用逗号分隔的文本框，这是权宜之计）
    - number → int（能转 int 就 int，否则 float）
    - bool → 真布尔（面板可能送 "true"/"on"/true）
    其余原样。转换失败就原样返回，交给下游容错——不在这里抛。
    """
    if isinstance(spec.default, list):
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            return [x.strip() for x in raw.split(",") if x.strip()]
        return raw
    if spec.type == "number":
        try:
            f = float(raw)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return raw
    if spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"true", "1", "on", "yes", "是"}
    if raw is None:
        return ""
    return str(raw) if not isinstance(raw, str) else raw


def _bad(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


#: 保存成功后的"生效方式"说明（PR3 热重载）。按域**如实**说明——不许虚假
#: 宣传：多数项下一句对话即生效，个别项列出来（web.port 是监听端口，换端口
#: 只能重启；QQ 的地址/密钥在"下次开闸"读；persona.json 没有消费者，真正
#: 生效的是 persona.md；plugins.json 的读者排 M1-10，现在改了暂不生效）。
#: 注意：前端展示时已加「已保存。」前缀（settings.html），这里不重复写。
_APPLY_NOTES = {
    "config": "模型/网关/唤醒词等改动下一句对话即生效",
    "secrets": "密钥改动下一句对话即生效",
    "platforms": "web.port 需重启 main.py；QQ 的地址/密钥下次开闸（关→开）生效",
    "persona": "真正生效的是 data/config/persona.md，改完下一句对话即生效",
    "plugins": "插件启停面板排在 M1-10，当前改动暂不生效",
}


async def _guard_post_json(
    request: Request,
) -> tuple[dict | None, JSONResponse | None]:
    """POST JSON 接口的公共防线：Content-Type / CSRF（Origin 与 Host 同源）/
    畸形 JSON / 非对象体。settings 与 platforms 两组写接口共用一份——两处
    各写一份迟早漂移（CSRF 口径一处变、一处没变就是漏洞）。

    Origin 与 Host 都用 urlparse 取 hostname（剥端口），与 web/adapter.py 的
    `_origin_allowed` 同口径（评审 rev2）。返回 (body, None) 或 (None, 错误响应)。
    """
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
    if ctype != "application/json":
        return None, _bad("需要 Content-Type: application/json", code=415)
    origin = request.headers.get("origin")
    if origin:
        host_header = request.headers.get("host") or ""
        if urlparse(origin).hostname != urlparse(f"//{host_header}").hostname:
            return None, _bad("跨站请求被拒", code=403)
    # 畸形 JSON 不能变成 500——归成 400。
    try:
        body = await request.json()
    except Exception:
        return None, _bad("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return None, _bad("请求体必须是 JSON 对象")
    return body, None


def build_settings_routes(
    config_dir: Path | None = None,
) -> Callable[[FastAPI], None]:
    """造一个路由注册器，交给 WebAdapter 的 `extra_routes`。

    config_dir 只在测试里用（指向 tmp），生产不传 = 用默认 data/config/。
    """

    def _load(domain: str) -> Config:
        return Config.load(domain) if config_dir is None else Config.load(domain, config_dir / DOMAINS[domain])

    def register(app: FastAPI) -> None:
        @app.get("/api/settings/fields")
        async def fields(domain: str = "config") -> JSONResponse:
            try:
                specs = fields_for(domain)
            except ValueError as exc:
                return _bad(str(exc))
            return JSONResponse({
                "ok": True,
                "domain": domain,
                "fields": [
                    {
                        "key": s.key, "label": s.label, "type": s.type,
                        "default": s.default, "choices": list(s.choices),
                        "sensitive": s.sensitive, "help": s.help,
                    }
                    for s in specs
                ],
            })

        @app.get("/api/settings/values")
        async def values(domain: str = "config") -> JSONResponse:
            try:
                specs = fields_for(domain)
            except ValueError as exc:
                return _bad(str(exc))
            cfg = _load(domain)
            out: dict[str, Any] = {}
            for s in specs:
                v = _get_dotted(cfg.data, s.key, s.default)
                if s.sensitive:
                    # 密钥永远不回明文，只回"设没设"
                    out[s.key] = {"set": bool(v)}
                else:
                    out[s.key] = v
            return JSONResponse({"ok": True, "domain": domain, "values": out})

        @app.post("/api/settings/values")
        async def save(request: Request) -> JSONResponse:
            # 1) 防线（Content-Type / CSRF / 畸形 JSON / 非对象体）见
            #    _guard_post_json——与 platforms 写接口共用一份，防漂移。
            body, err = await _guard_post_json(request)
            if err is not None:
                return err

            domain = str(body.get("domain") or "config")
            incoming = body.get("values") or {}
            try:
                specs = fields_for(domain)
            except ValueError as exc:
                return _bad(str(exc))
            if not isinstance(incoming, dict):
                return _bad("values 必须是对象")

            by_key = {s.key: s for s in specs}
            unknown = [k for k in incoming if k not in by_key]
            if unknown:
                # 拒未知键——防止拿这个接口往配置里塞任意东西
                return _bad(f"不接受未声明的字段：{unknown}")

            cfg = _load(domain)
            changed = []
            for key, raw in incoming.items():
                spec = by_key[key]
                # 密钥留空 = 保持原值（改别的字段时不该误清密钥）
                if spec.sensitive and (raw == "" or raw is None):
                    continue
                _set_dotted(cfg.data, key, _coerce(raw, spec))
                changed.append(key)
            try:
                cfg.save()
            except Exception as exc:  # 磁盘满/权限——返回错误而不是 500 堆栈
                return _bad(f"写入失败：{exc}", code=500)
            return JSONResponse({"ok": True, "domain": domain, "changed": changed,
                                 "note": _APPLY_NOTES.get(domain, "改动已写入配置")})

    return register


def build_platforms_routes(
    service, config_dir: Path | None = None,
) -> Callable[[FastAPI], None]:
    """QQ 平台开关的路由（设置页"空气开关"的控制面）。

    service 由 main 注入——本模块不认识 QQService 的实现，只按约定调用
    start()/stop()/status()（鸭子接口，同 history_provider 那套；§22 import
    单向：config 层不 import adapter 层）。config_dir 只在测试里用。

    "失败"的语义：启动失败是**正常运行态**（端口被占/网关没起来），不是请求
    错误——返回 200 + state=failed + detail，让页面渲染告示条；只有配置写
    不进去才是 500。
    """

    def _load() -> Config:
        return (
            Config.load("platforms") if config_dir is None
            else Config.load("platforms", config_dir / DOMAINS["platforms"])
        )

    def register(app: FastAPI) -> None:
        @app.get("/api/platforms/qq")
        async def qq_status() -> JSONResponse:
            cfg = _load()
            enabled = bool(_get_dotted(cfg.data, "qq.enabled", False))
            return JSONResponse({"ok": True, "enabled": enabled, **service.status()})

        @app.post("/api/platforms/qq")
        async def qq_set(request: Request) -> JSONResponse:
            body, err = await _guard_post_json(request)
            if err is not None:
                return err
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return _bad("enabled 必须是 true / false")

            # 先落配置（用户意图），再致动——顺序反了的话：服务起来了但配置
            # 没写上，下次重启又回到旧意图。配置写失败直接 500，不假装成功。
            cfg = _load()
            _set_dotted(cfg.data, "qq.enabled", enabled)
            try:
                cfg.save()
            except Exception as exc:
                return _bad(f"配置写入失败：{exc}", code=500)

            status = await (service.start() if enabled else service.stop())
            return JSONResponse({"ok": True, "enabled": enabled, **status})

    return register
