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


def build_settings_routes(
    config_dir: Path | None = None,
) -> Callable[[FastAPI], None]:
    """造一个路由注册器，交给 WebAdapter 的 `extra_routes`。

    config_dir 只在测试里用（指向 tmp），生产不传 = 用默认 data/config/。
    """

    def _load(domain: str) -> Config:
        return Config.load(domain) if config_dir is None else Config.load(domain, config_dir / DOMAINS[domain])

    def _bad(msg: str, code: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": msg}, status_code=code)

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
            # 1) CSRF 防护：跨站表单 POST 也带 Cookie 但不带正确 Content-Type 之外的
            #    东西——这里双重把关：要求 application/json + Origin 与 Host 同源。
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
            if ctype != "application/json":
                return _bad("需要 Content-Type: application/json", code=415)
            origin = request.headers.get("origin")
            if origin:
                # 与 web/adapter.py 的 _origin_allowed 同一套解析（urlparse 取
                # hostname、剥掉端口）。此前这里用 split 保留端口、那边剥端口，
                # 两处口径不一致——反向代理重写 Host、或端口书写形态不同时会
                # 误判（评审 rev2）。
                host_header = request.headers.get("host") or ""
                if urlparse(origin).hostname != urlparse(f"//{host_header}").hostname:
                    return _bad("跨站请求被拒", code=403)
            # 2) 畸形 JSON 不能变成 500——归成 400。
            try:
                body = await request.json()
            except Exception:
                return _bad("请求体不是合法 JSON")
            if not isinstance(body, dict):
                return _bad("请求体必须是 JSON 对象")

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
                                 "note": "部分配置需重启 main.py 后生效"})

    return register
