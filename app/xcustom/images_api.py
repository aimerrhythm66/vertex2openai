"""
OpenAI /v1/images/generations 兼容层（bad-woman/vertex2openai 插件，不改上游核心代码）

本项目对外只有 GET /v1/models 与 POST /v1/chat/completions。很多客户端的
生图功能硬编码调 POST /v1/images/generations，因此必然 404。

本模块把该端点补上：把请求转成对本服务自身 POST /v1/chat/completions 的一次调用
（走本地回环），再把返回的 Markdown data URL 解析成 OpenAI 规范的 b64_json。

放置位置（镜像内为 /app/xcustom/images_api.py）：
    app/xcustom/__init__.py
    app/xcustom/images_api.py

挂载（在 app/main.py 末尾追加）：
    from xcustom.images_api import router as _images_router
    app.include_router(_images_router)
"""

import os
import re
import time
from math import gcd
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

# 与项目其它模块保持一致的平级导入。
# 服务以 WORKDIR /app + `uvicorn main:app` 启动，sys.path 是 /app，
# 因此 auth.py 必须写成 `import auth`；写成 `from app.auth import ...` 会 ModuleNotFoundError。
from auth import get_api_key

router = APIRouter()

DEFAULT_IMAGE_MODEL = os.environ.get("DEFAULT_IMAGE_MODEL", "gemini-3-pro-image")
SELF_TIMEOUT = float(os.environ.get("IMAGES_SELF_TIMEOUT", "600"))

_IMAGE_MIME_RE = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)")
_BARE_B64_RE = re.compile(r"base64,([A-Za-z0-9+/=]{200,})")
_PLAIN_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")


class ImageGenerationRequest(BaseModel):
    model: Optional[str] = None
    prompt: Optional[str] = None
    n: Optional[int] = 1
    size: Optional[str] = None             # 例 "2048x2048"，用于推导 aspect_ratio + image_size
    quality: Optional[str] = None          # 忽略（Gemini 无此概念）
    style: Optional[str] = None            # 忽略
    response_format: Optional[str] = None  # "b64_json"（默认）或 "url"
    user: Optional[str] = None
    image_size: Optional[str] = None       # 本代理扩展：1K / 2K / 4K
    aspect_ratio: Optional[str] = None     # 本代理扩展：如 "3:4"
    model_config = ConfigDict(extra="allow")


def _self_base_url() -> str:
    explicit = os.environ.get("SELF_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{os.environ.get('PORT', '7860')}"


def _extract_prompt(body: ImageGenerationRequest) -> str:
    if body.prompt and body.prompt.strip():
        return body.prompt.strip()
    raw = (body.model_extra or {}).get("messages") or []
    chunks: List[str] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            chunks.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _aspect_and_size(size: Optional[str], image_size: Optional[str], aspect_ratio: Optional[str]):
    ar, isz = aspect_ratio, image_size
    if size and not ar:
        m = re.fullmatch(r"\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*", size)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w > 0 and h > 0:
                g = gcd(w, h)
                ar = f"{w // g}:{h // g}"
                if not isz:
                    longest = max(w, h)
                    isz = "4K" if longest >= 3000 else ("2K" if longest >= 1500 else "1K")
    return ar, isz


def _iter_text_parts(payload: Dict[str, Any]) -> List[str]:
    parts: List[str] = []
    for ch in payload.get("choices") or []:
        msg = ch.get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    parts.append(p["text"])
    return parts


def _pick_image(texts: List[str]) -> Optional[Dict[str, Optional[str]]]:
    for t in texts:
        m = _IMAGE_MIME_RE.search(t or "")
        if m:
            return {"b64": re.sub(r"\s+", "", m.group(2)), "mime": m.group(1)}
    for t in texts:
        m = _BARE_B64_RE.search(t or "")
        if m:
            return {"b64": m.group(1), "mime": None}
    for t in texts:
        s = (t or "").strip()
        if _PLAIN_B64_RE.fullmatch(s):
            return {"b64": re.sub(r"\s+", "", s), "mime": None}
    return None


@router.post("/v1/images/generations")
async def images_generations(
    fastapi_request: Request,
    body: ImageGenerationRequest,
    api_key: str = Depends(get_api_key),
):
    prompt = _extract_prompt(body)
    if not prompt:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "prompt is required (or provide messages)",
                               "type": "invalid_request_error", "code": "missing_prompt"}},
        )

    model = (body.model or DEFAULT_IMAGE_MODEL).strip()
    if model.startswith("models/"):
        model = model[len("models/"):]

    ar, isz = _aspect_and_size(body.size, body.image_size, body.aspect_ratio)

    chat_body: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,   # 生图在本项目里是"假流式"整块返回，非流式最省事
        "n": 1,
    }
    if isz:
        chat_body["image_size"] = isz
    if ar:
        chat_body["aspect_ratio"] = ar
    for k, v in (body.model_extra or {}).items():
        if k in ("messages", "stream", "n", "model"):
            continue
        chat_body.setdefault(k, v)

    url = f"{_self_base_url()}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": fastapi_request.headers.get("authorization", f"Bearer {api_key}"),
    }

    try:
        async with httpx.AsyncClient(timeout=SELF_TIMEOUT) as client:
            resp = await client.post(url, json=chat_body, headers=headers)
    except Exception as e:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"self-call to {url} failed: {e}",
                               "type": "server_error", "code": "self_call_failed"}},
        )

    try:
        payload = resp.json()
    except Exception:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"non-JSON upstream response (HTTP {resp.status_code})",
                               "type": "server_error", "code": "bad_upstream",
                               "upstream_text": resp.text[:2000]}},
        )

    if isinstance(payload.get("error"), dict):
        return JSONResponse(status_code=resp.status_code if resp.status_code >= 400 else 502,
                            content={"error": payload["error"]})

    texts = _iter_text_parts(payload)
    found = _pick_image(texts)
    if not found:
        return JSONResponse(
            status_code=502,
            content={"error": {
                "message": "upstream returned no image data (模型可能未放行，或命中安全策略)",
                "type": "server_error",
                "code": "no_image",
                "upstream_content_preview": ("\n".join(texts))[:2000],
            }},
        )

    b64 = found["b64"]
    item: Dict[str, Any] = {"revised_prompt": prompt}
    if (body.response_format or "b64_json").lower() == "url":
        item["url"] = f"data:{found['mime'] or 'image/png'};base64,{b64}"
    else:
        item["b64_json"] = b64

    return JSONResponse(
        status_code=200,
        content={
            "created": int(time.time()),
            "data": [item],
            "model": model,
            "size": body.size,
            "aspect_ratio": ar,
            "image_size": isz,
        },
    )
