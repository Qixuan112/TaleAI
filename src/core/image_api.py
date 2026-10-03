"""图片上传端点（UX-07）。

`POST /api/upload`：前端把图片（粘贴或选择）POST 上来 → 落 `data/temp/img/`
→ 返回文件名。前端再把这文件名放进 WS 消息帧的 `images` 里。

**为什么走 HTTP 而不是 WS 塞 base64**：大图塞进 WS JSON 会卡、会顶爆消息帧，
而且没有大小/类型校验的机会。HTTP 上传能干净地做校验 + 返回错误码。

**为什么收原始请求体而不是 multipart**：省掉 `python-multipart` 依赖。
前端 `fetch(body: file)` 直接发原始字节、Content-Type 就是图片类型，
后端 `await request.body()` 读出来即可——一图一请求，够用且更简单。

跟设置 API 一样由 main 通过 `extra_routes` 注入，适配器不认识这些业务。
"""

from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from core import image_store


def build_upload_routes() -> Callable[[FastAPI], None]:
    def register(app: FastAPI) -> None:
        @app.post("/api/upload")
        async def upload(request: Request) -> JSONResponse:
            data = await request.body()
            if not data:
                return JSONResponse({"ok": False, "error": "空文件"}, status_code=400)
            if len(data) > image_store.MAX_UPLOAD_BYTES:
                mb = image_store.MAX_UPLOAD_BYTES / 1024 / 1024
                return JSONResponse(
                    {"ok": False, "error": f"图片超过 {mb:.0f}MB"}, status_code=413)
            if image_store.sniff_mime(data) is None:
                return JSONResponse(
                    {"ok": False, "error": "不是图片（png/jpg/gif/webp）"},
                    status_code=400)
            try:
                name = image_store.save_bytes(data)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": f"保存失败：{exc}"},
                                    status_code=500)
            return JSONResponse({"ok": True, "name": name})

        # 图片本身要通过 /static 之外的路径取回给网页显示。
        # 挂 /api/img/{name} 读 data/temp/img/——**不**走 webui 静态目录
        # （那会把运行时可写的图混进前端源码目录）。
        @app.get("/api/img/{name}")
        async def get_image(name: str):
            path = image_store.DEFAULT_DIR / name
            # 只认目录内的直接子文件，防目录穿越
            if path.name != name or not path.is_file():
                return JSONResponse({"ok": False, "error": "没有这张图"},
                                    status_code=404)
            return FileResponse(str(path))

    return register
