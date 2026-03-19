"""
<<<<<<< Updated upstream
Videos API route (OpenAI-compatible create endpoint).
"""

import base64
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import orjson
from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.exceptions import UpstreamException, ValidationException
from app.services.grok.services.model import ModelService
from app.services.grok.services.video import VideoService
from app.services.grok.services.video_extend import VideoExtendService


router = APIRouter(tags=["Videos"])

VIDEO_MODEL_ID = "grok-imagine-1.0-video"
SIZE_TO_ASPECT = {
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1024": "1:1",
}
QUALITY_TO_RESOLUTION = {
    "standard": "480p",
    "high": "720p",
}


class VideoCreateRequest(BaseModel):
    """Supported create params only; unknown fields are ignored by design."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., description="Video prompt")
    model: Optional[str] = Field(VIDEO_MODEL_ID, description="Model id")
    size: Optional[str] = Field("1792x1024", description="Output size")
    seconds: Optional[int] = Field(6, description="Video length in seconds")
    quality: Optional[str] = Field("standard", description="Quality: standard/high")
    image_reference: Optional[Any] = Field(None, description="Structured image reference")
    input_reference: Optional[Any] = Field(None, description="Multipart input reference file")


class VideoExtendDirectRequest(BaseModel):
    """Direct extension params (non-OpenAI-compatible)."""

    model_config = ConfigDict(extra="ignore")

    prompt: str = Field(..., description="Prompt text mapped to message/originalPrompt")
    reference_id: str = Field(
        ..., description="Reference id mapped to extendPostId/originalPostId/parentPostId"
    )
    start_time: float = Field(..., description="Mapped to videoExtensionStartTime")
    ratio: str = Field("2:3", description="Mapped to aspectRatio")
    length: int = Field(6, description="Mapped to videoLength")
    resolution: str = Field("480p", description="Mapped to resolutionName")


def _raise_validation_error(exc: ValidationError) -> None:
    errors = exc.errors()
    if errors:
        first = errors[0]
        loc = first.get("loc", [])
        msg = first.get("msg", "Invalid request")
        code = first.get("type", "invalid_value")
        param_parts = [str(x) for x in loc if not (isinstance(x, int) or str(x).isdigit())]
        param = ".".join(param_parts) if param_parts else None
        raise ValidationException(message=msg, param=param, code=code)
    raise ValidationException(message="Invalid request", code="invalid_value")


def _extract_video_url(content: str) -> str:
    if not isinstance(content, str) or not content.strip():
        return ""

    md_match = re.search(r"\[video\]\(([^)\s]+)\)", content)
    if md_match:
        return md_match.group(1).strip()

    html_match = re.search(r"""<source[^>]+src=["']([^"']+)["']""", content)
    if html_match:
        return html_match.group(1).strip()

    url_match = re.search(r"""https?://[^\s"'<>]+""", content)
    if url_match:
        return url_match.group(0).strip().rstrip(".,)")

    return ""


def _normalize_model(model: Optional[str]) -> str:
    requested = (model or VIDEO_MODEL_ID).strip()
    if requested != VIDEO_MODEL_ID:
        raise ValidationException(
            message=f"The model `{VIDEO_MODEL_ID}` is required for video generation.",
            param="model",
            code="model_not_supported",
        )
    model_info = ModelService.get(requested)
    if not model_info or not model_info.is_video:
        raise ValidationException(
            message=f"The model `{requested}` is not supported for video generation.",
            param="model",
            code="model_not_supported",
        )
    return requested


def _normalize_size(size: Optional[str]) -> Tuple[str, str]:
    value = (size or "1792x1024").strip()
    aspect_ratio = SIZE_TO_ASPECT.get(value)
    if not aspect_ratio:
        raise ValidationException(
            message=f"size must be one of {sorted(SIZE_TO_ASPECT.keys())}",
            param="size",
            code="invalid_size",
        )
    return value, aspect_ratio


def _normalize_quality(quality: Optional[str]) -> Tuple[str, str]:
    value = (quality or "standard").strip().lower()
    resolution = QUALITY_TO_RESOLUTION.get(value)
    if not resolution:
        raise ValidationException(
            message=f"quality must be one of {sorted(QUALITY_TO_RESOLUTION.keys())}",
            param="quality",
            code="invalid_quality",
        )
    return value, resolution


def _normalize_seconds(seconds: Optional[int]) -> int:
    value = int(seconds or 6)
    if value < 6 or value > 30:
        raise ValidationException(
            message="seconds must be between 6 and 30",
            param="seconds",
            code="invalid_seconds",
        )
    return value


def _validate_reference_value(value: str, param: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    if candidate.startswith("data:"):
        return candidate
    raise ValidationException(
        message=f"{param} must be a URL or data URI",
        param=param,
        code="invalid_reference",
    )


def _parse_image_reference(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped[0] in {"{", "["}:
            try:
                value = orjson.loads(stripped)
            except orjson.JSONDecodeError:
                # allow plain url/data-uri in multipart text field as a practical fallback
                return _validate_reference_value(stripped, "image_reference")
        else:
            return _validate_reference_value(stripped, "image_reference")

    if not isinstance(value, dict):
        raise ValidationException(
            message=(
                "image_reference must be an object with exactly one of "
                "`image_url` or `file_id`"
            ),
            param="image_reference",
            code="invalid_reference",
        )

    image_url = value.get("image_url")
    file_id = value.get("file_id")
    image_url = image_url.strip() if isinstance(image_url, str) else ""
    file_id = file_id.strip() if isinstance(file_id, str) else ""

    has_image_url = bool(image_url)
    has_file_id = bool(file_id)
    if has_image_url == has_file_id:
        raise ValidationException(
            message="image_reference requires exactly one of image_url or file_id",
            param="image_reference",
            code="invalid_reference",
        )

    if has_file_id:
        raise ValidationException(
            message=(
                "image_reference.file_id is not supported in current reverse pipeline; "
                "please use image_reference.image_url or multipart input_reference"
            ),
            param="image_reference.file_id",
            code="unsupported_reference",
        )

    return _validate_reference_value(image_url, "image_reference.image_url")


async def _upload_to_data_uri(file: UploadFile, param: str) -> str:
    payload = await file.read()
    if not payload:
        raise ValidationException(
            message=f"{param} upload is empty",
            param=param,
            code="empty_file",
        )
    content_type = (file.content_type or "application/octet-stream").strip()
    encoded = base64.b64encode(payload).decode()
    return f"data:{content_type};base64,{encoded}"


async def _build_references_for_json(payload: BaseModel) -> List[str]:
    references: List[str] = []
    parsed_image_ref = _parse_image_reference(getattr(payload, "image_reference", None))
    if parsed_image_ref:
        references.append(parsed_image_ref)
    if getattr(payload, "input_reference", None) not in (None, ""):
        raise ValidationException(
            message="input_reference must be uploaded as multipart/form-data file",
            param="input_reference",
            code="invalid_reference",
        )
    return references


async def _build_payload_and_references_for_form(
    *,
    schema: type[BaseModel],
    prompt: Optional[str],
    model: Optional[str],
    size: Optional[str],
    seconds: Optional[int],
    quality: Optional[str],
    image_reference: Optional[str],
    input_reference: Optional[UploadFile],
) -> Tuple[BaseModel, List[str]]:
    try:
        payload = schema.model_validate(
            {
                "prompt": prompt,
                "model": model,
                "size": size,
                "seconds": seconds,
                "quality": quality,
                "image_reference": image_reference,
                "input_reference": None,
            }
        )
    except ValidationError as exc:
        _raise_validation_error(exc)

    references: List[str] = []
    if isinstance(input_reference, (UploadFile, StarletteUploadFile)):
        references.append(await _upload_to_data_uri(input_reference, "input_reference"))
    elif input_reference not in (None, ""):
        raise ValidationException(
            message="input_reference must be a file in multipart/form-data",
            param="input_reference",
            code="invalid_reference",
        )

    parsed_image_ref = _parse_image_reference(payload.image_reference)
    if parsed_image_ref:
        references.append(parsed_image_ref)
    return payload, references


def _multipart_create_schema(default_seconds: int) -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "default": VIDEO_MODEL_ID},
            "size": {"type": "string", "default": "1792x1024"},
            "seconds": {"type": "integer", "default": default_seconds},
            "quality": {"type": "string", "default": "standard"},
            "image_reference": {
                "type": "string",
                "description": "JSON string for image_reference object",
            },
            "input_reference": {"type": "string", "format": "binary"},
        },
    }


def _build_create_response(
    *,
    model: str,
    prompt: str,
    size: str,
    seconds: int,
    quality: str,
    url: str,
) -> Dict[str, Any]:
    ts = int(time.time())
    return {
        "id": f"video_{uuid.uuid4().hex[:24]}",
        "object": "video",
        "created_at": ts,
        "completed_at": ts,
        "status": "completed",
        "model": model,
        "prompt": prompt,
        "size": size,
        "seconds": str(seconds),
        "quality": quality,
        "url": url,
    }


async def _create_video_from_payload(
    payload: BaseModel,
    references: List[str],
    *,
    require_extension: bool = False,
) -> JSONResponse:
    prompt = (payload.prompt or "").strip()
    if not prompt:
        raise ValidationException(
            message="prompt is required",
            param="prompt",
            code="invalid_request_error",
        )

    model = _normalize_model(payload.model)
    size, aspect_ratio = _normalize_size(payload.size)
    quality, resolution = _normalize_quality(payload.quality)
    seconds = _normalize_seconds(payload.seconds)
    if require_extension and seconds <= 6:
        raise ValidationException(
            message="seconds must be between 7 and 30 for /video/extend",
            param="seconds",
            code="invalid_seconds",
        )

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    for ref in references:
        content.append({"type": "image_url", "image_url": {"url": ref}})

    result = await VideoService.completions(
        model=model,
        messages=[{"role": "user", "content": content}],
        stream=False,
        reasoning_effort=None,
        aspect_ratio=aspect_ratio,
        video_length=seconds,
        resolution=resolution,
        preset="custom",
    )

    choices = result.get("choices") if isinstance(result, dict) else None
    if not isinstance(choices, list) or not choices:
        raise UpstreamException("Video generation failed: empty result")

    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    rendered = msg.get("content", "") if isinstance(msg, dict) else ""
    video_url = _extract_video_url(rendered)
    if not video_url:
        raise UpstreamException("Video generation failed: missing video URL")

    return JSONResponse(
        content=_build_create_response(
            model=model,
            prompt=prompt,
            size=size,
            seconds=seconds,
            quality=quality,
            url=video_url,
        )
    )


@router.post(
    "/videos",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": VideoCreateRequest.model_json_schema()},
                "multipart/form-data": {"schema": _multipart_create_schema(6)},
            },
        }
    },
)
async def create_video(request: Request):
    """
    Videos create endpoint.
    Supports JSON and multipart/form-data using only reverse-supported params.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" in content_type:
        try:
            raw = await request.json()
        except ValueError:
            raise ValidationException(
                message=(
                    "Invalid JSON in request body. Please check for trailing commas or syntax errors."
                ),
                param="body",
                code="json_invalid",
            )
        if not isinstance(raw, dict):
            raise ValidationException(
                message="Request body must be a JSON object",
                param="body",
                code="invalid_request_error",
            )
        try:
            payload = VideoCreateRequest.model_validate(raw)
        except ValidationError as exc:
            _raise_validation_error(exc)
        references = await _build_references_for_json(payload)
        return await _create_video_from_payload(payload, references, require_extension=False)

    form = await request.form()
    payload, references = await _build_payload_and_references_for_form(
        schema=VideoCreateRequest,
        prompt=form.get("prompt"),
        model=form.get("model"),
        size=form.get("size"),
        seconds=form.get("seconds"),
        quality=form.get("quality"),
        image_reference=form.get("image_reference"),
        input_reference=form.get("input_reference"),
    )
    return await _create_video_from_payload(payload, references, require_extension=False)


@router.post(
    "/video/extend",
)
async def extend_video(request: VideoExtendDirectRequest):
    """
    Extension endpoint (non-OpenAI-compatible direct mapping).
    """
    result = await VideoExtendService.extend(
        prompt=request.prompt,
        reference_id=request.reference_id,
        start_time=request.start_time,
        ratio=request.ratio,
        length=request.length,
        resolution=request.resolution,
    )
    return JSONResponse(content=result)
=======
视频生成异步任务 API
Version: 1.5 (Image-to-Video Support)
"""

import asyncio
import json
import base64
from typing import Optional, Union, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Body, Request, UploadFile, File
from pydantic import BaseModel, Field

from app.core.auth import verify_api_key
from app.core.exceptions import AppException, ErrorType
from app.core.logger import logger
from app.services.token import get_token_manager
from app.services.grok.models.model import ModelService
from app.services.grok.services.video_task import (
    VideoTaskManager,
    TaskStatus,
    run_video_task,
)


router = APIRouter(prefix="/v1/videos", tags=["Videos"])


class VideoGenerationRequest(BaseModel):
    """视频生成请求"""
    model: str = Field("grok-video-1.0", description="模型名称")
    # 支持字符串，也支持 Chat-API 风格的列表
    prompt: Union[str, List[Dict[str, Any]]] = Field(..., description="视频生成提示词 (支持字符串或结构化内容)")
    aspect_ratio: Optional[str] = Field("16:9", description="宽高比: 16:9, 9:16, 1:1")
    video_length: Optional[int] = Field(6, description="视频时长(秒): 6, 10, 15")
    resolution: Optional[str] = Field("480p", description="分辨率")
    preset: Optional[str] = Field("normal", description="预设风格: normal, fun, spicy, custom")
    
    # 内部使用，不通过 API 直接传递，由 Router 处理
    _file_data: Optional[Dict[str, Any]] = None


class VideoTaskResponse(BaseModel):
    """视频任务响应"""
    task_id: str
    status: str
    created_at: Optional[int] = None
    progress: Optional[int] = None
    video_url: Optional[str] = None
    error: Optional[str] = None


@router.post("", dependencies=[Depends(verify_api_key)])
async def create_video_task_root(request: Request) -> VideoTaskResponse:
    """创建视频生成任务 (别名) - 兼容模式"""
    try:
        content_type = request.headers.get("content-type", "")
        body_bytes = await request.body()
        logger.info(f"DEBUG: Raw request body for /v1/videos: {body_bytes.decode('utf-8', errors='ignore')[:500]}")
        
        json_data = {}
        file_data = None
        
        if "application/json" in content_type:
            if not body_bytes:
                 raise HTTPException(status_code=400, detail="Empty JSON request body")
            json_data = await request.json()
        elif "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form_data = await request.form()
            json_data = {}
            for k, v in form_data.items():
                if isinstance(v, str):
                    json_data[k] = v
                elif isinstance(v, UploadFile) or type(v).__name__ == 'UploadFile':
                    logger.info(f"DEBUG: Found file field '{k}': {v.filename}")
                    # 优先处理 input_reference 或 file，或者如果还没抓到文件
                    if k in ["input_reference", "file"] or not file_data:
                        content = await v.read()
                        # 重置指针以防万一
                        await v.seek(0)
                        file_data = {
                            "filename": v.filename,
                            "content": content,
                            "content_type": v.content_type
                        }
                    # 也可以把 UploadFile 放入 json_data 以便 debug，但为了 Pydantic 验证通常不放
                    # json_data[k] = v 
                else:
                    logger.warning(f"DEBUG: Unknown type for field '{k}': {type(v)}")

            # Form Data 传过来的 prompt 可能是 JSON 字符串
            if "prompt" in json_data and isinstance(json_data["prompt"], str):
                 try:
                     parsed = json.loads(json_data["prompt"])
                     if isinstance(parsed, (list, dict)):
                        json_data["prompt"] = parsed
                 except:
                     pass
        else:
             # 尝试默认解析为 JSON
             try:
                 if body_bytes:
                     json_data = await request.json()
             except Exception:
                 pass
        
        # 兼容旧参数名
        if "seconds" in json_data and "video_length" not in json_data:
            json_data["video_length"] = json_data.pop("seconds")
        if "resolution_name" in json_data and "resolution" not in json_data:
            json_data["resolution"] = json_data.pop("resolution_name")

        if not json_data:
             # 如果只有文件没有参数，也可能报错，补一个默认 model
             if file_data:
                 json_data["model"] = "grok-video-1.0"
                 json_data["prompt"] = "Video generation from image"
             else:
                 logger.error(f"DEBUG: No json_data and no file_data. Body: {body_bytes[:100]}")
                 raise HTTPException(status_code=400, detail="Could not parse request body")

        logger.info(f"DEBUG: Parsed data keys: {list(json_data.keys())} (File captured: {bool(file_data)})")
        body = VideoGenerationRequest(**json_data)
        
        # 将文件数据挂载到 body 上 (使用私有字段)
        if file_data:
            body._file_data = file_data
        
        return await _create_video_task_impl(body)
    except Exception as e:
        logger.error(f"DEBUG: Failed to process request: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")


@router.post("/generations", dependencies=[Depends(verify_api_key)])
async def create_video_task(body: VideoGenerationRequest = Body(...)) -> VideoTaskResponse:
    """
    创建视频生成任务
    """
    return await _create_video_task_impl(body)


async def _create_video_task_impl(body: VideoGenerationRequest) -> VideoTaskResponse:
    """
    创建视频生成任务实现
    """
    from app.services.grok.services.assets import UploadService
    
    # 验证模型
    model_info = ModelService.get(body.model)
    if not model_info or not model_info.is_video:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{body.model}' is not a video model",
        )
    
    # -------------------------------------------------
    # 1. 提取 Prompt 文本
    # -------------------------------------------------
    prompt_text = ""
    external_image_url = None
    
    if isinstance(body.prompt, str):
        prompt_text = body.prompt
    elif isinstance(body.prompt, list):
        for block in body.prompt:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type == "text":
                    prompt_text += block.get("text", "")
                elif block_type == "image_url":
                    # 提取 image_url
                    url_obj = block.get("image_url", {})
                    if url_obj and "url" in url_obj:
                        external_image_url = url_obj["url"]
            elif isinstance(block, str):
                prompt_text += block
    
    # 无文本 Prompt 的容错 (纯图生视频可能没有 Prompt?)
    # 暂时还是要求有 Prompt，否则 Grok 可能报错
    if not prompt_text and not external_image_url and not body._file_data:
         # 如果真的没有任何输入
         raise HTTPException(status_code=400, detail="Prompt or Image is required")
    
    # -------------------------------------------------
    # 2. 获取 Token (用于上传和生成)
    # -------------------------------------------------
    token_mgr = await get_token_manager()
    await token_mgr.reload_if_stale()
    
    token = None
    for pool_name in ModelService.pool_candidates_for_model(body.model):
        token = token_mgr.get_token(pool_name)
        if token:
            break
    
    if not token:
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )

    # -------------------------------------------------
    # 3. 处理图片上传 (如果有)
    # -------------------------------------------------
    final_image_url = None
    upload_service = None
    
    try:
        # A. 优先处理上传的文件 (input_reference)
        if body._file_data:
            upload_service = UploadService()
            logger.info("Uploading attached file to Grok...")
            # 转换文件内容为 Base64 Data URI
            b64_content = base64.b64encode(body._file_data["content"]).decode()
            mime_type = body._file_data["content_type"]
            data_uri = f"data:{mime_type};base64,{b64_content}"

            _, file_uri = await upload_service.upload(
                file_input=data_uri,
                token=token
            )
            final_image_url = f"https://assets.grok.com/{file_uri}"
            logger.info(f"File uploaded: {final_image_url}")
            
        # B. 其次处理 Prompt 中的 image_url
        elif external_image_url:
            # 如果已经是 Grok Assets URL，直接使用
            if "assets.grok.com" in external_image_url:
                final_image_url = external_image_url
            else:
                # 如果是外部 URL，尝试下载并上传
                # TODO: 更好的方式是像 MessageExtractor 那样处理下载
                # 这里简单起见，暂时不主动下载外部 URL (需要引入 httpx 下载)
                # 假设用户传递的是可访问的 URL，或者 Grok 支持外部 URL
                # 此时我们先传递原 URL，如果 Grok 失败，那也没办法
                 final_image_url = external_image_url
                 logger.info(f"Using external image URL: {final_image_url}")

    except Exception as e:
        logger.error(f"Image upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload image: {str(e)}")
    finally:
        if upload_service:
            await upload_service.close()

    # -------------------------------------------------
    # 4. 创建任务
    # -------------------------------------------------
    manager = await VideoTaskManager.get_instance()
    task = await manager.create_task(
        model=body.model,
        prompt=prompt_text or "Video generation", # 如果只有图片没有 Prompt，给个默认值
        token=token,
        aspect_ratio=body.aspect_ratio or "16:9",
        video_length=body.video_length or 6,
        resolution=body.resolution or "480p",
        preset=body.preset or "normal",
        image_url=final_image_url,
    )
    
    # 启动后台任务
    asyncio.create_task(run_video_task(task))
    
    logger.info(f"Video generation task created: {task.task_id} (Image: {bool(final_image_url)})")
    
    return VideoTaskResponse(
        task_id=task.task_id,
        status=task.status.value,
        created_at=task.created_at,
    )


@router.get("/{task_id}", dependencies=[Depends(verify_api_key)])
async def get_video_task_root(task_id: str) -> VideoTaskResponse:
    """查询视频生成任务状态 (别名)"""
    return await _get_video_task_impl(task_id)


@router.get("/generations/{task_id}", dependencies=[Depends(verify_api_key)])
async def get_video_task(task_id: str) -> VideoTaskResponse:
    """
    查询视频生成任务状态
    """
    return await _get_video_task_impl(task_id)


async def _get_video_task_impl(task_id: str) -> VideoTaskResponse:
    """查询视频生成任务状态"""
    manager = await VideoTaskManager.get_instance()
    task = await manager.get_task(task_id)
    
    if not task:
        raise HTTPException(
            status_code=404,
            detail=f"Task '{task_id}' not found",
        )
    
    return VideoTaskResponse(**task.to_dict())
>>>>>>> Stashed changes


__all__ = ["router"]
