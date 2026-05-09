"""
通过 ADB 将视频推送到手机相册目录
"""

import os
import subprocess
import sys

def push_video_to_phone(video_path, device_id=None):
    """
    将视频推送到手机相册目录
    
    Args:
        video_path: 视频文件路径
        device_id: 设备ID，None 表示默认设备
    """
    if not os.path.exists(video_path):
        print(f"错误: 文件不存在 {video_path}")
        return False
    
    video_name = os.path.basename(video_path)
    target_dir = "/sdcard/DCIM/Camera"
    target_path = f"{target_dir}/{video_name}"
    
    print(f"开始推送视频: {video_name}")
    print(f"目标路径: {target_path}")
    
    device_arg = f"-s {device_id}" if device_id else ""
    
    # 1. 创建目标目录
    print("\n1. 创建目标目录...")
    cmd = f"adb {device_arg} shell mkdir -p {target_dir}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"警告: {result.stderr}")
    
    # 2. 推送文件
    print("\n2. 推送文件到手机...")
    cmd = f'adb {device_arg} push "{video_path}" {target_path}'
    print(f"执行命令: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"错误: 推送失败")
        print(result.stderr)
        return False
    
    print(result.stdout)
    
    # 3. 触发媒体扫描，让相册能识别新文件
    print("\n3. 触发媒体扫描...")
    cmd = f"adb {device_arg} shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{target_path}"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if result.returncode == 0:
        print("✓ 媒体扫描触发成功")
    else:
        print("警告: 媒体扫描触发失败，可能需要手动刷新相册")
        print(result.stderr)
    
    print(f"\n✓ 视频已成功推送到手机相册")
    print(f"  文件位置: {target_path}")
    print(f"  打开相册即可查看")
    
    return True


if __name__ == "__main__":
    # 视频文件路径
    video_path = r"D:\gitclone\operation-engine\全族流放，我靠种植发家致富了_1002_1697_6.mp4.mp4"
    
    # 设备ID（如果有多个设备连接，需要指定）
    device_id = "192.168.18.92:33639"  # None 表示默认设备
    
    print("=" * 70)
    print("将视频推送到手机相册")
    print("=" * 70)
    
    success = push_video_to_phone(video_path, device_id)
    
    if success:
        print("\n" + "=" * 70)
        print("推送完成！")
        print("=" * 70)
    else:
        print("\n推送失败")
        sys.exit(1)
