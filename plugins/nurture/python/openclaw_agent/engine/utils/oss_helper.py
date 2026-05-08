import os
import time
import socket
import mimetypes
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal, Union, BinaryIO
from io import BytesIO
import alibabacloud_oss_v2 as oss
from alibabacloud_oss_v2.models import PutObjectRequest


# 重试配置
MAX_RETRIES = 3
BASE_RETRY_DELAY = 2
MAX_RETRY_DELAY = 30
DNS_CHECK_TIMEOUT = 5
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

logger = logging.getLogger(__name__)

# OSS configuration. Credentials are loaded from environment variables
# (NURTURE_OSS_ACCESS_KEY_ID / NURTURE_OSS_ACCESS_KEY_SECRET). Set them
# in ~/.hermes/.env before running the device service. Empty values
# disable OSS upload — operations that need to upload screenshots
# (e.g. core.observe_screen) will fail until configured.
OSS_CONFIGS = {
    'dev': {
        'access_key_id': os.getenv('NURTURE_OSS_ACCESS_KEY_ID', ''),
        'access_key_secret': os.getenv('NURTURE_OSS_ACCESS_KEY_SECRET', ''),
        'endpoint': os.getenv('NURTURE_OSS_ENDPOINT', 'oss-cn-hangzhou.aliyuncs.com'),
        'region': os.getenv('NURTURE_OSS_REGION', 'cn-hangzhou'),
        'bucket_name': os.getenv('NURTURE_OSS_BUCKET', 'avatar-gui-data'),
        'url': os.getenv('NURTURE_OSS_URL', 'https://avatar-gui-data.oss-cn-hangzhou.aliyuncs.com'),
        'base_dir': os.getenv('NURTURE_OSS_BASE_DIR', 'mobile/dev'),
    },
    'prod': {
        'access_key_id': os.getenv('NURTURE_OSS_ACCESS_KEY_ID', ''),
        'access_key_secret': os.getenv('NURTURE_OSS_ACCESS_KEY_SECRET', ''),
        'endpoint': os.getenv('NURTURE_OSS_ENDPOINT', 'oss-cn-hangzhou.aliyuncs.com'),
        'region': os.getenv('NURTURE_OSS_REGION', 'cn-hangzhou'),
        'bucket_name': os.getenv('NURTURE_OSS_BUCKET', 'avatar-gui-data'),
        'url': os.getenv('NURTURE_OSS_URL', 'https://avatar-gui-data.oss-cn-hangzhou.aliyuncs.com'),
        'base_dir': os.getenv('NURTURE_OSS_BASE_DIR', 'mobile/prod'),
    },
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
    except:
        status['local_ip'] = 'unknown'
    
    return status


def _check_network_connectivity(host: str, port: int = 443, timeout: float = DNS_CHECK_TIMEOUT) -> tuple:
    """检查网络连通性
    
    Returns:
        (is_connected, error_msg)
    """
    try:
        # 先尝试DNS解析
        ip = socket.gethostbyname(host)
        logger.info(f"DNS解析成功: {host} -> {ip}")
        
        # 再尝试TCP连接
        socket.setdefaulttimeout(timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((host, port))
        sock.close()
        
        logger.info(f"网络检查通过: {host}:{port}")
        return (True, None)
    except socket.gaierror as e:
        error_msg = f"DNS解析失败: {host} - {e}"
        logger.warning(error_msg)
        # 记录网络状态
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
    """获取OSS配置"""
    # test 环境使用 dev 的 OSS 配置
    if env == 'test':
        env = 'dev'
    
    if env not in OSS_CONFIGS:
        raise ValueError(f"无效的环境: {env}，可选值: dev, prod, test")
    return OSS_CONFIGS[env]


def _get_client(env: str = 'dev'):
    """创建OSS客户端（配置超时）"""
    config = _get_config(env)
    cred = oss.credentials.StaticCredentialsProvider(
        config['access_key_id'],
        config['access_key_secret']
    )
    cfg = oss.config.load_default()
    cfg.credentials_provider = cred
    cfg.region = config['region']
    cfg.connect_timeout = CONNECT_TIMEOUT
    cfg.read_timeout = READ_TIMEOUT
    return oss.Client(cfg)


def _build_oss_key(filename: str, env: str = 'dev', business_dir: str = '') -> str:
    """
    构建OSS文件路径
    
    Args:
        filename: 文件名
        env: 环境
        business_dir: 业务目录（可选）
    
    Returns:
        完整的OSS路径
    """
    config = _get_config(env)
    base = config['base_dir']
    
    if business_dir:
        # 拼接业务目录: base_dir/business_dir/filename
        return f"{base}/{business_dir.strip('/')}/{filename}"
    else:
        # 仅使用base_dir: base_dir/filename
        return f"{base}/{filename}"


def _build_file_url(key: str, env: str = 'dev') -> str:
    """构建文件访问URL"""
    config = _get_config(env)
    return f"{config['url']}/{key}"


def upload_file(local_path: str, oss_filename: Optional[str] = None, env: str = 'dev', business_dir: str = '') -> str:
    """
    上传文件到OSS（支持所有文件类型，带重试机制和网络检查）
    
    Args:
        local_path: 本地文件路径
        oss_filename: OSS文件名（可选，默认使用本地文件名）
        env: 环境（'dev'或'prod'，默认'dev'）
        business_dir: 业务目录（可选，如'screenshots/2024-01-01'）
    
    Returns:
        文件访问URL
    """
    local_file = Path(local_path)
    if not local_file.exists():
        raise FileNotFoundError(f"文件不存在: {local_path}")
    
    if oss_filename is None:
        oss_filename = local_file.name
    
    config = _get_config(env)
    bucket = config['bucket_name']
    key = _build_oss_key(oss_filename, env, business_dir)
    
    # 获取OSS域名用于网络检查
    oss_host = f"{bucket}.{config['endpoint']}"
    
    logger.info(f"开始上传: {local_file.name} ({local_file.stat().st_size / 1024 / 1024:.2f}MB)")
    
    # 指数退避重试
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 网络连通性检查
            if attempt == 1:
                is_connected, error_msg = _check_network_connectivity(oss_host)
                if not is_connected:
                    logger.warning(f"网络预检失败: {error_msg}，尝试继续上传...")
            
            # 创建客户端并上传
            client = _get_client(env)
            mime, _ = mimetypes.guess_type(str(local_file))
            
            logger.info(f"[尝试 {attempt}/{MAX_RETRIES}] 上传到OSS: {key}")
            
            with open(local_file, "rb") as f:
                req = PutObjectRequest(
                    bucket=bucket,
                    key=key,
                    body=f,
                    content_type=mime or "application/octet-stream",
                )
                client.put_object(req)
            
            url = _build_file_url(key, env)
            logger.info(f"✓ 上传成功: {url}")
            return url
            
        except Exception as e:
            last_error = e
            error_str = str(e)
            
            # 判断是否为DNS/网络错误
            is_network_error = any(keyword in error_str.lower() for keyword in [
                'getaddrinfo failed', 'dns', 'resolve', 'network', 
                'connection', 'timeout', '11002'
            ])
            
            if attempt < MAX_RETRIES:
                # 指数退避：2s, 4s, 8s...
                delay = min(BASE_RETRY_DELAY * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
                
                if is_network_error:
                    logger.warning(f"[尝试 {attempt}/{MAX_RETRIES}] 网络错误: {error_str}")
                    logger.info(f"等待 {delay}秒 后重试...")
                else:
                    logger.warning(f"[尝试 {attempt}/{MAX_RETRIES}] 上传失败: {error_str}")
                    logger.info(f"等待 {delay}秒 后重试...")
                
                time.sleep(delay)
                
                # 重试前再次检查网络
                is_connected, check_error = _check_network_connectivity(oss_host)
                if not is_connected:
                    logger.warning(f"网络仍未恢复: {check_error}")
            else:
                logger.error(f"[尝试 {attempt}/{MAX_RETRIES}] 上传最终失败: {error_str}")
    
    # 所有重试都失败
    raise last_error


def upload_file_with_timestamp(local_path: str, env: str = 'dev', business_dir: str = '') -> str:
    """
    上传文件到OSS，自动添加时间戳（带重试机制）
    
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
    oss_filename = f"{original_name}_{timestamp}{file_ext}"
    
    logger.info(f"准备上传: {local_path} -> {oss_filename}")
    return upload_file(local_path, oss_filename, env, business_dir)


def upload_image(image_data: Union[bytes, BytesIO, 'PIL.Image.Image'], 
                 filename: Optional[str] = None, 
                 env: str = 'dev', 
                 business_dir: str = 'screenshots') -> str:
    """
    上传图片到OSS（支持多种输入格式）
    
    Args:
        image_data: 图片数据，支持：
            - bytes: 原始字节数据
            - BytesIO: 字节流
            - PIL.Image.Image: PIL图像对象
        filename: OSS文件名（可选，默认自动生成）
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
    
    # 生成文件名
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 毫秒级
        filename = f"screenshot_{timestamp}.png"
    
    key = _build_oss_key(filename, env, business_dir)
    oss_host = f"{bucket}.{config['endpoint']}"
    
    logger.info(f"开始上传图片: {filename}")
    
    # 重试机制
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if attempt == 1:
                is_connected, error_msg = _check_network_connectivity(oss_host)
                if not is_connected:
                    logger.warning(f"网络预检失败: {error_msg}，尝试继续上传...")
            
            client = _get_client(env)
            logger.info(f"[尝试 {attempt}/{MAX_RETRIES}] 上传到OSS: {key}")
            
            data_stream.seek(0)
            req = PutObjectRequest(
                bucket=bucket,
                key=key,
                body=data_stream,
                content_type=content_type,
            )
            client.put_object(req)
            
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
        key_prefix: OSS路径前缀
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
            key = _build_oss_key(key_prefix + rel, env)
            mime, _ = mimetypes.guess_type(str(fp))
            
            with open(fp, "rb") as f:
                req = PutObjectRequest(
                    bucket=bucket,
                    key=key,
                    body=f,
                    content_type=mime or "application/octet-stream",
                )
                client.put_object(req)
            
            url = _build_file_url(key, env)
            urls.append({'file': str(fp), 'url': url})
    
    return urls


def delete_oss_file(oss_filename: str, env: str = 'dev') -> bool:
    """
    删除OSS文件
    
    Args:
        oss_filename: OSS文件名
        env: 环境（'dev'或'prod'，默认'dev'）
    
    Returns:
        是否删除成功
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_oss_key(oss_filename, env)
        
        from alibabacloud_oss_v2.models import DeleteObjectRequest
        req = DeleteObjectRequest(bucket=bucket, key=key)
        client.delete_object(req)
        return True
    except Exception as e:
        print(f"删除失败: {e}")
        return False


def check_file_exists(oss_filename: str, env: str = 'dev') -> bool:
    """
    检查OSS文件是否存在
    
    Args:
        oss_filename: OSS文件名
        env: 环境（'dev'或'prod'，默认'dev'）
    
    Returns:
        文件是否存在
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_oss_key(oss_filename, env)
        
        from alibabacloud_oss_v2.models import HeadObjectRequest
        req = HeadObjectRequest(bucket=bucket, key=key)
        client.head_object(req)
        return True
    except:
        return False


def get_file_url(oss_filename: str, env: str = 'dev') -> str:
    """
    获取OSS文件访问URL
    
    Args:
        oss_filename: OSS文件名
        env: 环境（'dev'或'prod'，默认'dev'）
    
    Returns:
        文件访问URL
    """
    key = _build_oss_key(oss_filename, env)
    return _build_file_url(key, env)


def get_file_info(oss_filename: str, env: str = 'dev') -> dict:
    """
    获取OSS文件信息
    
    Args:
        oss_filename: OSS文件名
        env: 环境（'dev'或'prod'，默认'dev'）
    
    Returns:
        文件信息字典
    """
    try:
        config = _get_config(env)
        client = _get_client(env)
        bucket = config['bucket_name']
        key = _build_oss_key(oss_filename, env)
        
        from alibabacloud_oss_v2.models import HeadObjectRequest
        req = HeadObjectRequest(bucket=bucket, key=key)
        resp = client.head_object(req)
        
        return {
            'exists': True,
            'size': resp.content_length,
            'last_modified': resp.last_modified,
            'content_type': resp.content_type,
            'url': _build_file_url(key, env)
        }
    except:
        return {'exists': False}
