"""自动化操作辅助工具"""
import json
import os
import re
import signal
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Any
import uiautomator2 as u2


def get_max_volume(d: u2.Device, logger=None) -> int:
    """
    获取设备的最大媒体音量级别（Android 11+）
    
    Args:
        d: 设备实例
        logger: 日志记录器
    
    Returns:
        最大音量级别
    """
    try:
        result = d.shell("cmd media_session volume --stream 3 --get")
        
        # d.shell() 返回 ShellResponse 对象，需要获取输出
        out = result.output if hasattr(result, 'output') else str(result)
        
        if logger:
            logger.debug(f"get_max_volume 输出: {repr(out)}")
        
        # 解析输出: [V] volume is 5 in range [0..150]
        # 使用 re.DOTALL 让 . 匹配换行符
        m = re.search(r'range\s*\[0\.\.(\d+)\]', out, re.DOTALL)
        
        if m:
            max_vol = int(m.group(1))
            if logger:
                logger.debug(f"解析到最大音量: {max_vol}")
            return max_vol
        else:
            if logger:
                logger.warning(f"无法解析最大音量，使用默认值15")
            return 150
    except Exception as e:
        if logger:
            logger.error(f"获取最大音量失败: {e}")
        return 150


def set_media_volume(d: u2.Device, volume_percent: int, logger=None) -> bool:
    """
    设置媒体音量（Android 11+ 使用 cmd media_session）
    
    Args:
        d: 设备实例
        volume_percent: 音量百分比 (0-100)
        logger: 日志记录器
    
    Returns:
        是否设置成功
    """
    try:
        if not 0 <= volume_percent <= 100:
            raise ValueError(f"音量百分比必须在0-100之间，当前值: {volume_percent}")
        
        # 获取最大音量级别
        max_level = get_max_volume(d, logger)
        
        # 计算目标音量级别
        target_level = int(max_level * volume_percent / 100)
        
        # 设置音量
        d.shell(f"cmd media_session volume --stream 3 --set {target_level}")
        
        if logger:
            logger.info(f"设置媒体音量: {volume_percent}% (级别{target_level}/{max_level})")
        
        return True
    except Exception as e:
        if logger:
            logger.error(f"设置媒体音量失败: {e}")
        return False


def save_error_screenshot(d: u2.Device, error_msg: str = "", base_dir: Optional[Path] = None) -> Optional[str]:
    """
    保存错误截图
    
    Args:
        d: 设备实例
        error_msg: 错误信息
        base_dir: 基础目录
    
    Returns:
        截图路径
    """
    try:
        now = datetime.now()
        time_str = now.strftime("%Y%m%d_%H%M%S")
        
        if base_dir is None:
            base_dir = Path.cwd()
        
        err_img_dir = base_dir / "err_info_img"
        err_img_dir.mkdir(parents=True, exist_ok=True)
        
        img_filename = f"{time_str}.png"
        img_path = err_img_dir / img_filename
        
        d.screenshot(str(img_path))
        
        return str(img_path)
    except Exception as e:
        print(f"保存错误截图失败: {e}")
        return None


def save_screenshot(d: u2.Device, filename: str = "", base_dir: Optional[Path] = None) -> Optional[str]:
    """
    保存截图到指定目录
    
    Args:
        d: 设备实例
        filename: 文件名（不含扩展名）
        base_dir: 基础目录
    
    Returns:
        截图路径
    """
    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        
        if base_dir is None:
            base_dir = Path.cwd()
        
        img_dir = base_dir / date_str
        img_dir.mkdir(parents=True, exist_ok=True)
        
        if not filename:
            time_str = now.strftime("%H_%M_%S")
            filename = f"screenshot_{time_str}"
        
        img_path = img_dir / f"{filename}.png"
        d.screenshot(str(img_path))
        
        return str(img_path)
    except Exception as e:
        print(f"保存截图失败: {e}")
        return None


def save_json_data(data: Any, filepath: Path, encoding: str = 'utf-8', indent: int = 2) -> bool:
    """保存数据为JSON文件"""
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w', encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        
        return True
    except Exception as e:
        print(f"保存JSON数据失败: {e}")
        return False


def load_json_data(filepath: Path, encoding: str = 'utf-8', default: Any = None) -> Any:
    """从JSON文件加载数据"""
    try:
        if not filepath.exists():
            return default
        
        with open(filepath, 'r', encoding=encoding) as f:
            return json.load(f)
    except Exception as e:
        print(f"加载JSON数据失败: {e}")
        return default


def manage_scrcpy_recording(action: str, device_serial: str = None, video_path: str = None, 
                            process: subprocess.Popen = None, logger=None) -> Optional[subprocess.Popen]:
    """
    管理scrcpy录屏
    
    Args:
        action: "start"启动 或 "stop"停止
        device_serial: 设备序列号
        video_path: 视频保存路径
        process: scrcpy进程对象
        logger: 日志记录器
    
    Returns:
        启动时返回进程对象，停止时返回None
    """
    if action == "start":
        if not device_serial or not video_path:
            raise ValueError("启动录屏需要提供device_serial和video_path参数")
        
        # 音频录制（需要Android 11+和scrcpy 2.0+）
        scrcpy_cmd = [
            "scrcpy",
            "-s", device_serial,
            "--record", str(video_path),
            "--audio-source=output",  # 录制系统音频输出
            "--audio-codec=aac",
            "--audio-bit-rate=128K",
            "--no-video-playback"
        ]
        
        if logger:
            logger.info("启动scrcpy后台录屏...")
        
        scrcpy_process = subprocess.Popen(
            scrcpy_cmd,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        
        return scrcpy_process
    
    elif action == "stop":
        if not process:
            raise ValueError("停止录屏需要提供process参数")
        
        if logger:
            logger.info("停止scrcpy录屏...")
        
        if process.poll() is None:
            try:
                os.kill(process.pid, signal.CTRL_BREAK_EVENT)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if logger:
                    logger.warning("scrcpy未在5秒内退出，强制终止")
                process.kill()
                process.wait()
        
        return None
    
    else:
        raise ValueError(f"不支持的操作类型: {action}")

