import boto3
import os
import tomllib
from botocore.config import Config
from botocore.exceptions import ClientError
import logging

# 启用 boto3 调试日志
boto3.set_stream_logger('botocore', level=logging.DEBUG)

def load_config():
    try:
        with open("data/config.toml", "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        print("data/config.toml not found.")
        return None

def get_aliyun_config(config):
    # 优先从 aliyun section 读取
    aliyun_conf = config.get("aliyun", {})
    if not aliyun_conf:
        # Fallback to r2 if it's actually configured for aliyun (legacy/misconfiguration check)
        r2_conf = config.get("r2", {})
        if r2_conf.get("provider") == "aliyun":
            return r2_conf
    return aliyun_conf

def test_aliyun_upload():
    config = load_config()
    if not config:
        return

    conf = get_aliyun_config(config)
    print(f"Loaded Config: {conf}")

    endpoint_url = conf.get("endpoint_url")
    access_key_id = conf.get("access_key_id")
    secret_access_key = conf.get("secret_access_key")
    bucket_name = conf.get("bucket_name")

    if not all([endpoint_url, access_key_id, secret_access_key, bucket_name]):
        print("Missing required Aliyun configuration.")
        return

    # 模拟 user 的修复逻辑
    region_name = "us-west-1" # 假设是这个，或者从 endpoint 解析
    if "oss-" in endpoint_url:
        import re
        match = re.search(r"oss-([a-z0-9-]+)\.aliyuncs\.com", endpoint_url)
        if match:
             region_name = match.group(1)
    
    print(f"Region: {region_name}")

    # 当前代码中的配置逻辑
    addressing_style = "virtual"
    s3_config = {"addressing_style": addressing_style}
    # 尝试复现：禁用 payload signing
    s3_config["payload_signing_enabled"] = False 

    print(f"Boto3 Config: {s3_config}")

    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            s3=s3_config,
        ),
        region_name=region_name,
    )

    test_content = b"This is a test file for aliyun upload."
    key = "debug_upload_test.txt"

    print(f"Attempting to upload {len(test_content)} bytes to {bucket_name}/{key}...")

    try:
        client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=test_content,
            ContentType="text/plain",
        )
        print("Upload Successful!")
    except ClientError as e:
        print(f"Boto3 ClientError: {e}")
    except Exception as e:
        print(f"General Error: {e}")

if __name__ == "__main__":
    test_aliyun_upload()
