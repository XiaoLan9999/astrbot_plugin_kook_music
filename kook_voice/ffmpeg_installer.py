import aiohttp
import aiofiles
import asyncio
import os
import zipfile
import tarfile
import platform
import shutil
import logging
from pathlib import Path

logger = logging.getLogger("astrbot")

async def check_and_install_ffmpeg(data_dir: Path) -> str:
    """
    检查系统中是否存在 ffmpeg，如果不存在，则根据系统环境自动下载并解压。
    返回可用的 ffmpeg 可执行文件路径。
    """
    # 1. 首先检查系统环境变量中是否已存在
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
        
    # 2. 检查本地插件数据目录中是否已经存在
    os_name = platform.system().lower()
    exe_name = "ffmpeg.exe" if os_name == "windows" else "ffmpeg"
    
    # 在 data_dir 的各级子目录下寻找
    for root, dirs, files in os.walk(data_dir):
        if exe_name in files:
            local_ffmpeg = os.path.join(root, exe_name)
            if os.access(local_ffmpeg, os.X_OK) or os_name == "windows":
                return local_ffmpeg
                
    # 3. 如果都没有，则开始自动下载
    logger.info("[KookMusic] 未在系统中检测到 FFmpeg，正在自动下载，可能需要几分钟，请耐心等待...")
    await _download_ffmpeg(data_dir, os_name)
    
    # 4. 下载解压完成后，再次搜索
    for root, dirs, files in os.walk(data_dir):
        if exe_name in files:
            local_ffmpeg = os.path.join(root, exe_name)
            # Linux/macOS 下给予执行权限
            if os_name != "windows":
                os.chmod(local_ffmpeg, 0o755)
            logger.info(f"[KookMusic] 成功获取内部 FFmpeg: {local_ffmpeg}")
            return local_ffmpeg
            
    logger.error("[KookMusic] 自动安装 FFmpeg 失败，将尝试直接调用。")
    return "ffmpeg"

async def _download_ffmpeg(data_dir: Path, os_name: str):
    machine = platform.machine().lower()
    
    # 确定下载链接和解压格式
    if os_name == "windows":
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        ext = ".zip"
    elif os_name == "linux":
        if "aarch64" in machine or "arm" in machine:
            url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz"
        else:
            url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        ext = ".tar.xz"
    elif os_name == "darwin": # macOS
        url = "https://evermeet.cx/ffmpeg/ffmpeg-6.0.zip"
        ext = ".zip"
    else:
        logger.warning(f"[KookMusic] 暂不支持自动下载 {os_name} 平台的 FFmpeg，请手动安装。")
        return
        
    archive_path = data_dir / f"ffmpeg_download{ext}"
    
    try:
        # 下载文件
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=600),  # 10分钟超时
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[KookMusic] 下载 FFmpeg 失败: HTTP {resp.status}")
                    return
                
                # 写入文件流
                async with aiofiles.open(archive_path, 'wb') as f:
                    while True:
                        chunk = await resp.content.read(8192)
                        if not chunk:
                            break
                        await f.write(chunk)
                        
        logger.info("[KookMusic] FFmpeg 压缩包下载完成，正在解压...")
        
        # 阻塞的解压操作放在线程池中运行
        await asyncio.to_thread(_extract_archive, archive_path, data_dir, ext)
        logger.info("[KookMusic] FFmpeg 解压配置完成！")
        
    except Exception as e:
        logger.error(f"[KookMusic] 下载或解压 FFmpeg 时发生异常: {e}")
    finally:
        # 清理压缩包文件
        if archive_path.exists():
            try:
                archive_path.unlink()
            except:
                pass

def _extract_archive(archive_path: Path, extract_dir: Path, ext: str):
    if ext == ".zip":
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    elif ext == ".tar.xz":
        with tarfile.open(archive_path, 'r:xz') as tar_ref:
            # 使用 filter='data' 防止路径遍历攻击（Python 3.12+）
            try:
                tar_ref.extractall(extract_dir, filter='data')
            except TypeError:
                # Python < 3.12 不支持 filter 参数
                tar_ref.extractall(extract_dir)
