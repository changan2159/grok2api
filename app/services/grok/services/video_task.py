"""
视频生成异步任务管理器
"""

import asyncio
import time
import uuid
from typing import Dict, Optional
from dataclasses import dataclass, field
from enum import Enum

from app.core.logger import logger


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class VideoTask:
    """视频任务"""
    task_id: str
    model: str
    prompt: str
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    video_url: Optional[str] = None
    error: Optional[str] = None
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))
    
    # 生成参数
    aspect_ratio: str = "16:9"
    video_length: int = 6
    resolution: str = "480p"
    preset: str = "normal"
    image_url: Optional[str] = None
    
    # 内部使用
    token: str = ""

    def __post_init__(self):
        """确保 status 是枚举类型"""
        if isinstance(self.status, str):
            try:
                self.status = TaskStatus(self.status)
            except ValueError:
                # 如果是未知的状态字符串，默认为 PENDING 或保持原样
                pass

    def to_dict(self) -> dict:
        """转换为 API 响应"""
        result = {
            "task_id": self.task_id,
            "status": self.status.value,
            "created_at": self.created_at,
        }
        
        if self.status == TaskStatus.PROCESSING:
            result["progress"] = self.progress
        elif self.status == TaskStatus.COMPLETED:
            result["video_url"] = self.video_url
        elif self.status == TaskStatus.FAILED:
            result["error"] = self.error
            
        return result


class VideoTaskManager:
    """视频任务管理器（单例）"""
    
    _instance: Optional["VideoTaskManager"] = None
    _lock = asyncio.Lock()
    
    def __init__(self):
        # 移除内存存储 self._tasks
        from app.core.storage import StorageFactory
        self.storage = StorageFactory.get_storage()
        self._task_expiry = 3600  # 任务过期时间（秒）
    
    @classmethod
    async def get_instance(cls) -> "VideoTaskManager":
        """获取单例实例"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    async def create_task(
        self,
        model: str,
        prompt: str,
        token: str,
        aspect_ratio: str = "16:9",
        video_length: int = 6,
        resolution: str = "480p",
        preset: str = "normal",
        image_url: Optional[str] = None,
    ) -> VideoTask:
        """创建新任务"""
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task = VideoTask(
            task_id=task_id,
            model=model,
            prompt=prompt,
            token=token,
            aspect_ratio=aspect_ratio,
            video_length=video_length,
            resolution=resolution,
            preset=preset,
            image_url=image_url,
        )
        
        # 保存到存储
        await self.storage.save_video_task(task_id, task.__dict__)
        
        logger.info(f"Video task created: {task_id}")
        return task
    
    async def get_task(self, task_id: str) -> Optional[VideoTask]:
        """获取任务"""
        data = await self.storage.load_video_task(task_id)
        if not data:
            return None
        
        # 恢复为对象
        return VideoTask(**data)
    
    async def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        progress: Optional[int] = None,
        video_url: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """更新任务状态"""
        # 先加载
        data = await self.storage.load_video_task(task_id)
        if not data:
            return
        
        task = VideoTask(**data)
        
        if status is not None:
            task.status = status
        if progress is not None:
            task.progress = progress
        if video_url is not None:
            task.video_url = video_url
        if error is not None:
            task.error = error
        task.updated_at = int(time.time())
        
        # 保存更新
        await self.storage.save_video_task(task_id, task.__dict__)
        
        logger.debug(f"Task {task_id} updated: status={task.status}, progress={task.progress}")
    
    async def cleanup_expired(self):
        """清理过期任务"""
        await self.storage.cleanup_expired_video_tasks(self._task_expiry)


async def run_video_task(task: VideoTask):
    """执行视频生成任务"""
    from app.services.grok.services.media import VideoService
    from app.services.grok.processors import VideoCollectProcessor
    from app.services.grok.services.r2_storage import R2StorageService
    from app.services.grok.services.assets import DownloadService
    
    manager = await VideoTaskManager.get_instance()
    
    try:
        await manager.update_task(task.task_id, status=TaskStatus.PROCESSING, progress=10)
        
        # 生成视频
        service = VideoService()
        if task.image_url:
            response = await service.generate_from_image(
                token=task.token,
                prompt=task.prompt,
                image_url=task.image_url,
                aspect_ratio=task.aspect_ratio,
                video_length=task.video_length,
                resolution=task.resolution,
                preset=task.preset,
            )
        else:
            response = await service.generate(
                token=task.token,
                prompt=task.prompt,
                aspect_ratio=task.aspect_ratio,
                video_length=task.video_length,
                resolution_name=task.resolution,
                preset=task.preset,
            )
        
        await manager.update_task(task.task_id, progress=30)
        
        # 处理响应
        processor = VideoCollectProcessor(task.model, task.token)
        result = await processor.process(response)
        
        await manager.update_task(task.task_id, progress=80)
        
        # 提取视频 URL
        video_url = None
        if result and "choices" in result:
            for choice in result.get("choices", []):
                content = choice.get("message", {}).get("content", "")
                if "https://" in content and (".mp4" in content or "video" in content):
                    # 提取 URL
                    import re
                    urls = re.findall(r'https://[^\s<>"\']+\.mp4[^\s<>"\']*', content)
                    if urls:
                        video_url = urls[0]
                        break
        
        if not video_url:
            # 尝试从 result 中提取
            video_url = result.get("video_url") if isinstance(result, dict) else None
        
        if video_url:
            await manager.update_task(
                task.task_id,
                status=TaskStatus.COMPLETED,
                progress=100,
                video_url=video_url,
            )
            logger.info(f"Video task completed: {task.task_id}")
        else:
            await manager.update_task(
                task.task_id,
                status=TaskStatus.FAILED,
                error="Failed to extract video URL",
            )
            
    except Exception as e:
        logger.error(f"Video task failed: {task.task_id}, error={e}")
        await manager.update_task(
            task.task_id,
            status=TaskStatus.FAILED,
            error=str(e),
        )


__all__ = ["VideoTaskManager", "VideoTask", "TaskStatus", "run_video_task"]
