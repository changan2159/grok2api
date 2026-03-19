"""
Cloudflare R2 存储服务

基于 boto3 的 S3 兼容 API 实现 R2 对象存储
"""

import hashlib
import time
from pathlib import Path
from typing import Optional, Tuple


import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_config
from app.core.logger import logger


class R2StorageService:
    """Cloudflare R2 存储服务"""

    _instance: Optional["R2StorageService"] = None
    _client = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def _get_provider_config(cls) -> dict:
        """获取当前 Provider 的配置"""
        provider = get_config("r2.provider", "r2")
        if provider == "aliyun":
            section = "aliyun"
        elif provider == "s3":
            section = "s3"
        else:
            section = "r2"

        return {
            "provider": provider,
            "section": section,
            "endpoint_url": get_config(f"{section}.endpoint_url", ""),
            "access_key_id": get_config(f"{section}.access_key_id", ""),
            "secret_access_key": get_config(f"{section}.secret_access_key", ""),
            "bucket_name": get_config(f"{section}.bucket_name", ""),
            "public_url": get_config(f"{section}.public_url", ""),
            "path_prefix": get_config(f"{section}.path_prefix", "grok2api"),
            "account_id": get_config("r2.account_id", "") if section == "r2" else "",
        }

    @classmethod
    def is_enabled(cls) -> bool:
        """检查 R2 (或兼容存储) 是否启用"""
        enabled = get_config("r2.enabled", False)
        if not enabled:
            return False

        conf = cls._get_provider_config()
        # 检查必要配置
        if not conf["access_key_id"] or not conf["secret_access_key"] or not conf["bucket_name"]:
            return False
            
        # R2 特有检查
        if conf["provider"] == "r2" and not conf["account_id"]:
             return False

        return True

    @classmethod
    def _get_region_from_endpoint(cls, endpoint_url: str) -> str:
        """从 Endpoint 解析 Region (主要用于 Aliyun)"""
        if "aliyuncs.com" in endpoint_url:
            # https://oss-cn-hangzhou.aliyuncs.com -> cn-hangzhou
            # https://oss-us-west-1.aliyuncs.com -> us-west-1
            import re
            match = re.search(r"oss-([a-z0-9-]+)\.aliyuncs\.com", endpoint_url)
            if match:
                return match.group(1)
        return "auto"

    @classmethod
    def _get_client(cls):
        """获取 S3 客户端（单例）"""
        if cls._client is not None:
            return cls._client

        conf = cls._get_provider_config()

        endpoint_url = conf["endpoint_url"]
        if not endpoint_url and conf["provider"] == "r2":
             endpoint_url = f"https://{conf['account_id']}.r2.cloudflarestorage.com"

        region_name = cls._get_region_from_endpoint(endpoint_url)
        
        # Aliyun OSS 需要使用 virtual addressing style
        is_aliyun = "aliyuncs.com" in endpoint_url
        addressing_style = "virtual" if is_aliyun else "auto"
        
        # 针对 Aliyun 使用 V2 签名 (s3) 以彻底避开 V4 的 chunked/payload hash 问题
        signature_version = "s3" if is_aliyun else "s3v4"

        cls._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=conf["access_key_id"],
            aws_secret_access_key=conf["secret_access_key"],
            config=Config(
                signature_version=signature_version,
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": addressing_style},
            ),
            region_name=region_name,
        )

        logger.info(f"Storage client initialized: {conf['provider']} @ {endpoint_url} (region: {region_name}, style: {addressing_style})")
        return cls._client

    @classmethod
    def generate_key(cls, file_path: Path, media_type: str = "image") -> str:
        """
        生成 R2 对象键

        Args:
            file_path: 本地文件路径
            media_type: 媒体类型 (image/video)

        Returns:
            R2 对象键，格式: {prefix}/{type}/{hash}_{timestamp}{ext}
        """
        conf = cls._get_provider_config()
        prefix = conf["path_prefix"]

        # 生成唯一文件名
        file_hash = hashlib.md5(str(file_path).encode()).hexdigest()[:12]
        timestamp = int(time.time() * 1000)
        ext = file_path.suffix.lower() or ".bin"

        return f"{prefix}/{media_type}/{file_hash}_{timestamp}{ext}"

    @classmethod
    async def upload(
        cls,
        file_path: Path,
        key: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: str = "image",
    ) -> Optional[str]:
        """
        上传文件到 R2

        Args:
            file_path: 本地文件路径
            key: R2 对象键（可选，自动生成）
            content_type: MIME 类型
            media_type: 媒体类型

        Returns:
            R2 公开 URL 或 None
        """
        if not cls.is_enabled():
            return None

        if not file_path.exists():
            logger.error(f"R2 upload failed: file not found {file_path}")
            return None

        try:
            client = cls._get_client()
            conf = cls._get_provider_config()
            bucket_name = conf["bucket_name"]

            # 生成对象键
            if key is None:
                key = cls.generate_key(file_path, media_type)

            # 推断 MIME 类型
            if content_type is None:
                ext = file_path.suffix.lower()
                content_type = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                    ".mp4": "video/mp4",
                    ".webm": "video/webm",
                }.get(ext, "application/octet-stream")

            # 针对 Aliyun OSS 特殊处理：使用 bytes 上传避免 aws-chunked
            # 其他存储 (R2, S3) 继续使用流式上传以节省内存
            is_aliyun = "aliyuncs.com" in conf["endpoint_url"]

            if is_aliyun:
                with open(file_path, "rb") as f:
                    file_content = f.read()
                
                client.put_object(
                    Bucket=bucket_name,
                    Key=key,
                    Body=file_content,
                    ContentType=content_type,
                )
            else:
                with open(file_path, "rb") as f:
                    client.put_object(
                        Bucket=bucket_name,
                        Key=key,
                        Body=f,
                        ContentType=content_type,
                    )

            # 生成公开 URL
            public_url = cls.get_public_url(key)
            logger.info(f"Storage upload success ({'aliyun' if is_aliyun else 's3/r2'}): {key} -> {public_url}")

            return public_url

        except ClientError as e:
            logger.error(f"Storage upload failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Storage upload error: {e}")
            return None

    @classmethod
    def get_public_url(cls, key: str) -> str:
        """
        获取 R2 公开访问 URL

        Args:
            key: R2 对象键

        Returns:
            公开 URL
        """
        conf = cls._get_provider_config()
        public_url = conf["public_url"].rstrip("/")

        if not public_url:
            # 如果没有配置公开 URL，尝试自动推断
            if conf["provider"] == "r2":
                 public_url = f"https://{conf['bucket_name']}.r2.dev"
            elif "aliyuncs.com" in conf["endpoint_url"]:
                # Aliyun OSS 默认公网地址: https://{bucket}.{endpoint_domain}
                endpoint = conf["endpoint_url"].replace("https://", "").replace("http://", "")
                public_url = f"https://{conf['bucket_name']}.{endpoint}"
            else:
                 # 其他 S3，尝试使用 endpoint/bucket 风格或者虚拟主机风格
                 pass

        return f"{public_url}/{key}"

    @classmethod
    async def delete(cls, key: str) -> bool:
        """
        删除 R2 对象

        Args:
            key: R2 对象键

        Returns:
            是否删除成功
        """
        if not cls.is_enabled():
            return False

        try:
            client = cls._get_client()
            conf = cls._get_provider_config()
            bucket_name = conf["bucket_name"]

            client.delete_object(Bucket=bucket_name, Key=key)
            logger.info(f"R2 delete success: {key}")
            return True

        except ClientError as e:
            logger.error(f"R2 delete failed: {e}")
            return False
        except Exception as e:
            logger.error(f"R2 delete error: {e}")
            return False

    @classmethod
    def reset_client(cls):
        """重置客户端（配置更改后调用）"""
        cls._client = None


    @classmethod
    def test_connection(cls, config_data: dict) -> Tuple[bool, str]:
        """
        测试连接
        
        Args:
            config_data: 前端传来的配置字典 (包含 provider, endpoint_url, access_key_id 等)
            
        Returns:
            (success, message)
        """
        provider = config_data.get("provider", "r2")
        section = provider # r2, aliyun, s3
        
        # 提取配置 (注意：前端传来的 keys 可能是扁平的或者嵌套的，取决于实现。
        # 这里假设 config_data 是整个 config 对象，或者我们从前端只传相关部分。
        # 为了通用，假设传入的是包含 provider 和对应 section 字段的字典)
        
        # 更加健壮的方式：直接从 config_data 读取
        # 如果前端传的是整个 config json: {"r2": {...}, "aliyun": {...}, "r2.provider": "aliyun"}
        # 无论如何，我们需要按照 provider 提取
        
        access_key_id = config_data.get("access_key_id", "")
        secret_access_key = config_data.get("secret_access_key", "")
        bucket_name = config_data.get("bucket_name", "")
        endpoint_url = config_data.get("endpoint_url", "")
        account_id = config_data.get("account_id", "")
        
        if not access_key_id or not secret_access_key or not bucket_name:
            return False, "缺少必要参数 (Access Key, Secret Key, Bucket Name)"

        if provider == "r2":
            if not account_id:
                return False, "R2 需要 Account ID"
            if not endpoint_url:
                endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        
        if not endpoint_url:
             return False, "缺少 Endpoint URL"

        if not endpoint_url.startswith("http"):
             endpoint_url = f"https://{endpoint_url}"

        region_name = cls._get_region_from_endpoint(endpoint_url)
        
        # Aliyun OSS 需要使用 virtual addressing style
        is_aliyun = "aliyuncs.com" in endpoint_url
        addressing_style = "virtual" if is_aliyun else "auto"

        # 针对 Aliyun 使用 V2 签名 (s3)
        signature_version = "s3" if is_aliyun else "s3v4"

        try:
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                config=Config(
                    signature_version=signature_version,
                    retries={"max_attempts": 2, "mode": "standard"},
                    connect_timeout=10,
                    read_timeout=10,
                    s3={"addressing_style": addressing_style},
                ),
                region_name=region_name,
            )
            
            # 策略：即使 HeadBucket 失败(403)，也尝试上传文件。
            # 因为很多 RAM 用户只有 PutObject 权限，没有 List/Head 权限。
            head_success = False
            head_error_msg = ""
            
            try:
                client.head_bucket(Bucket=bucket_name)
                head_success = True
            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code")
                if error_code == "404":
                    return False, f"连接失败：Bucket '{bucket_name}' 不存在。"
                elif error_code == "403":
                    head_error_msg = f"无法列出/读取 Bucket (403)，尝试测试写入权限..."
                else:
                    head_error_msg = f"Bucket 检查失败: {str(e)}"

            # 如果 HeadBucket 成功，直接返回
            if head_success:
                return True, f"连接成功！Bucket '{bucket_name}' 存在且可访问。(Region: {region_name})"

            # 如果 HeadBucket 失败，尝试上传测试文件
            try:
                test_key = f"connection_test_{int(time.time())}.txt"
                client.put_object(
                    Bucket=bucket_name,
                    Key=test_key,
                    Body=b"test",
                    ContentType="text/plain"
                )
                # 清理
                try:
                    client.delete_object(Bucket=bucket_name, Key=test_key)
                except:
                    pass
                
                return True, f"连接成功！拥有写入权限 (注意：List权限受限，可能无法在后台浏览文件，但不影响上传使用)。(Region: {region_name})"
            except Exception as e:
                real_error = str(e)
                if "403" in real_error:
                     return False, f"连接失败：无权访问 Bucket '{bucket_name}' (403 Forbidden)。请检查 AccessKey 的 OSS 权限。 (Region: {region_name})"
                return False, f"连接失败：{real_error} (之前 HeadBucket 错误: {head_error_msg})"

        except Exception as e:
            return False, f"连接失败: {str(e)}"

__all__ = ["R2StorageService"]
