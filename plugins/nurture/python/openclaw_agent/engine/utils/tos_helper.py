import os
import time
import socket
import mimetypes
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, BinaryIO
from io import BytesIO
import tos


# 重试配置
MAX_RETRIES = 3
BASE_RETRY_DELAY = 2
MAX_RETRY_DELAY = 30
DNS_CHECK_TIMEOUT = 5
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

logger = logging.getLogger(__name__)

# TOS配置
TOS_CONFIGS = {
    'dev': {
        'access_key': '',       # TODO: 填入火山引擎 AK
        'secret_key': '',       # TODO: 填入火山引擎 SK
        'endpoint': 'tos-cn-beijing.volces.com',
        'region': 'cn-beijing',
        'bucket_name': '',      # TODO: 填入 bucket 名称
        'url': '',              # TODO: 填入公网访问地址，如 https://{bucket}.tos-cn-beijing.volces.com
        'base_dir': 'mobile/dev'
    },
    'prod': {
        'access_key': '',
        'secret_key': '',
        'endpoint': 'tos-cn-beijing.volces.com',
        'region': 'cn-beijing',
        'bucket_name': '',
        'url': '',
        'base_dir': 'mobile/prod'
    }
}


def _get_network_status() -> dict:
    """获取网络状态信息"""
    status = {
        'hostname': socket.gethostname(),
        'dns_servers': [],
        'default_timeout': socket.getdefaulttimeout()
    }

    try:
        status['local_ip'] = socket.gethostbyname(socket.gethostname())
    except Exception:
        status['local_ip'] = 'unknown'

    return status


def _check_network_connectivity(host: str, port: int = 443, timeout: float = DNS_CHECK_TIMEOUT) -> tuple:
    """检查网络连通性

    Returns:
        (is_connected, error_msg)
    """
    try:
        ip = socket.gethostbyname(host)
        logger.info(f"DNS解析成功: {host} -> {ip}")

        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock.close()

        logger.info(f"网络检查通过: {host}:{port}")
        return (True, None)
    except socket.gaierror as e:
        error_msg = f"DNS解析失败: {host} - {e}"
        logger.warning(error_msg)
        net_status = _get_network_status()
        logger.warning(f"网络状态: {net_status}")
        return (False, error_msg)
    except socket.timeout as e:
        error_msg = f"连接超时: {host}:{port} - {e}"
        logger.warning(error_msg)
        return (False, error_msg)
    except Exception as e:
        error_msg = f"网络连接失败: {host}:{port} - {e}"
        logger.warning(error_msg)
        return (False, error_msg)


def _get_config(env: str = 'dev') -> dict:
    """获取TOS配置"""
    if env == 'test':
        env = 'dev'

    if env not in TOS_CONFIGS:
        raise ValueError(f"无效的环境: {env}，可选值: dev, prod, test")
    return TOS_CONFIGS[env]


def _get_client(env: str = 'dev') -> tos.TosClientV2:
    """创建TOS客户端"""
    config = _get_config(env)
    return tos.TosClientV2(
        ak=config['access_key'],
        sk=config['secret_key'],
        endpoint=config['endpoint'],
        region=config['region'],
        connection_time=CONNECT_TIMEOUT,
        socket_timeout=READ_TIMEOUT,
    )


def _build_tos_key(filename: str, env: str = 'dev', business_dir: str = '') -> str:
    """
    构建TOS文件路径

    Args:
        filename: 文件名
        env: 环境
        business_dir: 业务目录（可选）

    Returns:
        完整的TOS路径
    """
    config = _get_config(env)
    base = config['base_dir']

    if business_dir:
        return f"{base}/{business_dir.strip('/')}/{filename}"
    else:
        return f"{base}/{filename}"


def _build_file_url(key: str, env: str = 'dev') -> str:
    """构建文件访问URL"""
    config = _get_config(env)
    return f"{config['url']}/{key}"


def upload_file(local_path: str, tos_filename: Optional[str] = None, env: str = 'dev', business_dir: str = '') -> str:
    """
    上传文件到TOS（支持所有文件类型，带重试机制和网络检查）

    Args:
        local_path: 本地文件路径
        tos_filename: TOS文件名（可选，默认使用本地文件名）
        env: 环境（'dev'或'prod'，默认'dev'）
        business_dir: 业务目录（可选，如'screenshots/2024-01-01'）

    Returns:
        文件访问URL
    """
    local_file = Path(local_path)
    if not local_file.exists():
        raise FileNotFoundError(f"文件不存在: {local_path}")

    if tos_filename is None:
        tos_filename = local_file.name

    config = _get_config(env)
    bucket = config['bucket_name']
    key = _build_tos_key(tos_filename, env, business_dir)

    tos_host = config['endpoint']

    logger.info(f"开始上传: {local_file.name} ({local_file.stat().st_size / 1024 / 1024:.2f}MB)")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt == 1:
                is_connected, error_msg = _check_network_connectivity(tos_host)
                if not is_connected:
                    logger.warning(f"网络预检失败: {error_msg}，尝试继续上传...")

            client = _get_client(env)
            mime, _ = mimetypes.guess_type(str(local_file))

            logger.info(f"[尝试 {attempt}/{MAX_RETRIES}] 上传到TOS: {key}")

            client.put_object_from_file(
                bucket=bucket,
                key=key,
                file_path=str(local_file),
                content_type=mime or "application/octet-stream",
            )

            url = _build_file_url(key, env)
            logger.info(f"✓ 上传成功: {url}")
            return url

        except Exception as e:
            last_error = e
            error_str = str(e)

            is_network_error = any(keyword in error_str.lower() for keyword in [
                'getaddrinfo failed', 'dns', 'resolve', 'network',
                'connection', 'timeout', '11002'
            ])

            if attempt < MAX_RETRIES:
                delay = min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)

                if is_network_error:
                    logger.warning(f"[尝试 {attempt}/{MAX_RETRIES}] 网络错误: {error_str}")
                else:
                    logger.warning(f"[尝试 {attempt}/{MAX_RETRIES}] 上传失败: {error_str}")
                logger.info(f"等待 {delay}秒 后重试...")

                time.sleep(delay)

                is_connected, check_error = _check_network_connectivity(tos_host)
                if not is_connected:
                    logger.warning(f"网络仍未恢复: {check_error}")
            else:
                logger.error(f"[尝试 {attempt}/{MAX_RETRIES}] 上传最终失败: {error_str}")

    raise last_error


def upload_file_with_timestamp(local_path: str, env: str = 'dev', business_dir: str = '') -> str:
    """
    上传文件到TOS，自动添加时间戳（带重试机制）

    Args:
        local_path: 本地文件路径
        env: 环境（'dev'或'prod'，默认'dev'）
        business_dir: 业务目录（可选，如'screenshots'）

    Returns:
        文件访问URL
    """
    local_file = Path(local_path)
    if not local_file.exists():
        raise FileNotFoundError(f"文件不存在: {local_path}")

    file_ext = local_file.suffix
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    original_name = local_file.stem
    tos_filename = f"{original_name}_{timestamp}{file_ext}"

    logger.info(f"准备上传: {local_path} -> {tos_filename}")
    return upload_file(local_path, tos_filename, env, business_dir)


def upload_image(image_data: Union[bytes, BytesIO, 'PIL.Image.Image'],
                 filename: Optional[str] = None,
                 env: str = 'dev',
                 business_dir: str = 'screenshots') -> str:
    """
    上传图片到TOS（支持多种输入格式）

    Args:
        image_data: 图片数据，支持：
            - bytes: 原始字节数据
            - BytesIO: 字节流
            - PIL.Image.Image: PIL图像对象
        filename: TOS文件名（可选，默认自动生成）
        env: 环境（'dev'或'prod'，默认'dev'）
        business_dir: 业务目录（默认'screenshots'）

    Returns:
        文件访问URL
    """
    config = _get_config(env)
    bucket = config['bucket_name']

    # 处理不同类型的输入
    if hasattr(image_data, 'save'):  # PIL.Image.Image
        buffer = BytesIO()
        image_data.save(buffer, format='PNG')
        buffer.seek(0)
        data_stream = buffer
        content_type = 'image/png'
    elif isinstance(image_data, bytes):
        data_stream = BytesIO(image_data)
        content_type = 'image/png'
    elif isinstance(image_data, BytesIO):
        data_stream = image_data
        data_stream.seek(0)
        content_type = 'image/png'
    else:
        raise TypeError(f"不支持的图片数据类型: {type(image_data)}")

    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"screenshot_{timestamp}.png"

    key = _build_tos_key(filename, env, business_dir)
    tos_host = config['endpoint']

    logger.info(f"开始上传图片: {filename}")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt == 1:
                is_connected, error_msg = _check_network_connectivity(tos_host)
                if not is_connected:
                    logger.warning(f"网络预检失败: {error_msg}，尝试继续上传...")

            client = _get_client(env)
            logger.info(f"[尝试 {attempt}/{MAX_RETRIES}] 上传到TOS: {key}")

            data_stream.seek(0)
            client.put_object(
                bucket=bucket,
                key=key,
                content=data_stream,
                content_type=content_type,
            )

            url = _build_file_url(key, env)
            logger.info(f"✓ 图片上传成功: {url}")
            return url

        except Exception as e:
            last_error = e
            error_str = str(e)

            if attempt < MAX_RETRIES:
                delay = min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                logger.warning(f"[尝试 {attempt}/{MAX_RETRIES}] 上传失败: {error_str}")
                logger.info(f"等待 {delay}秒 后重试...")
                time.sleep(delay)
            else:
                logger.error(f"[尝试 {attempt}/{MAX_RETRIES}] 图片上传最终失败: {error_str}")

    raise last_error


def batch_upload_files(local_paths: list, env: str = 'dev', business_dir: str = '') -> list:
    """
    批量上传文件（带重试和详细日志）

    Args:
        local_paths: 本地文件路径列表
        env: 环境（'dev'或'prod'，默认'dev'）
        business_dir: 业务目录（可选）

    Returns:
        上传结果列表
    """
    results = []
    total = len(local_paths)

    logger.info(f"开始批量上传，共{total}个文件")

    for idx, local_path in enumerate(local_paths, 1):
        logger.info(f"[{idx}/{total}] 处理文件: {local_path}")
        try:
            url = upload_file_with_timestamp(local_path, env, business_dir)
            results.append({'path': local_path, 'url': url, 'success': True})
            logger.info(f"[{idx}/{total}] ✓ 上传成功")
        except Exception as e:
            logger.error(f"[{idx}/{total}] ✗ 上传失败: {e}")
            results.append({'path': local_path, 'error': str(e), 'success': False})

    success_count = sum(1 for r in results if r['success'])
    logger.info(f"批量上传完成: {success_count}/{total} 成功")

    return results


def upload_dir(local_dir: str, key_prefix: str = "", env: str = 'dev') -> list:
    """
    递归上传整个目录

    Args:
        local_dir: 本地目录路径
        key_prefix: TOS路径前缀
        env: 环境（'dev'或'prod'，默认'dev'）

    Returns:
        上传结果列表
    """
    config = _get_config(env)
    client = _get_client(env)
    bucket = config['bucket_name']
    local_path = Path(local_dir)
    urls = []

    for fp in local_path.rglob("*"):
        if fp.is_file():
            rel = fp.relative_to(local_path).as_posix()
            key = _build_tos_key(key_prefix + rel, env)
            mime, _ = mimetypes.guess_type(str(fp))

            client.put_object_from_file(
                bucket=bucket,
                key=key,
                file_path=str(fp),
                content_type=mime or "application/octet-stream",
            )

            url = _build_file_url(key, env)
            urls.append({'file': str(fp), 'url': url})

    return urls


def delete_tos_file(tos_filename: str, env: str = 'dev') -> bool:
    """
    删除TOS文件

    Args:
        tos_filename: TOS文件名
        env: 环境（'dev'或'prod'，默认'dev'）

    Returns:
        是否删除成功
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_tos_key(tos_filename, env)

        client.delete_object(bucket=bucket, key=key)
        return True
    except Exception as e:
        logger.error(f"删除失败: {e}")
        return False


def check_file_exists(tos_filename: str, env: str = 'dev') -> bool:
    """
    检查TOS文件是否存在

    Args:
        tos_filename: TOS文件名
        env: 环境（'dev'或'prod'，默认'dev'）

    Returns:
        文件是否存在
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_tos_key(tos_filename, env)

        client.head_object(bucket=bucket, key=key)
        return True
    except Exception:
        return False


def get_file_url(tos_filename: str, env: str = 'dev') -> str:
    """
    获取TOS文件访问URL

    Args:
        tos_filename: TOS文件名
        env: 环境（'dev'或'prod'，默认'dev'）

    Returns:
        文件访问URL
    """
    key = _build_tos_key(tos_filename, env)
    return _build_file_url(key, env)


def get_file_info(tos_filename: str, env: str = 'dev') -> dict:
    """
    获取TOS文件信息

    Args:
        tos_filename: TOS文件名
        env: 环境（'dev'或'prod'，默认'dev'）

    Returns:
        文件信息字典
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_tos_key(tos_filename, env)

        resp = client.head_object(bucket=bucket, key=key)

        return {
            'exists': True,
            'size': resp.content_length,
            'last_modified': resp.last_modified,
            'content_type': resp.content_type,
            'url': _build_file_url(key, env)
        }
    except Exception:
        return {'exists': False}
