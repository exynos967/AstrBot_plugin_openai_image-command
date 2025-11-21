import aiohttp
import asyncio
import aiofiles
import base64
import uuid
import re
from datetime import datetime, timedelta
from pathlib import Path
from astrbot.api import logger


class ImageGeneratorState:
    """图像生成器状态管理类，用于处理并发安全"""
    def __init__(self):
        self.last_saved_image = {"url": None, "path": None}
        self.api_key_index = 0
        self._lock = asyncio.Lock()
    
    async def get_next_api_key(self, api_keys):
        """获取下一个可用的API密钥"""
        async with self._lock:
            if not api_keys or not isinstance(api_keys, list):
                raise ValueError("API密钥列表不能为空")
            current_key = api_keys[self.api_key_index % len(api_keys)]
            return current_key
    
    async def rotate_to_next_api_key(self, api_keys):
        """轮换到下一个API密钥"""
        async with self._lock:
            if api_keys and isinstance(api_keys, list) and len(api_keys) > 1:
                self.api_key_index = (self.api_key_index + 1) % len(api_keys)
                logger.info(f"已轮换到下一个API密钥，当前索引: {self.api_key_index}")
    
    async def update_saved_image(self, url, path):
        """更新保存的图像信息"""
        async with self._lock:
            self.last_saved_image = {"url": url, "path": path}
    
    async def get_saved_image_info(self):
        """获取最后保存的图像信息"""
        async with self._lock:
            return self.last_saved_image["url"], self.last_saved_image["path"]


# 全局状态管理实例
_state = ImageGeneratorState()

# 响应文本中图片信息的匹配模式
_DATA_URL_PATTERN = re.compile(
    r"(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)"
)
_HTTP_URL_PATTERN = re.compile(r"(https?://[^\s)]+)")


async def cleanup_old_images(data_dir=None):
    """
    清理超过15分钟的图像文件
    
    Args:
        data_dir (Path): 数据目录路径，如果为None则使用当前脚本目录
    """
    try:
        # 如果没有传入data_dir，使用当前脚本目录
        if data_dir is None:
            script_dir = Path(__file__).parent.parent
            data_dir = script_dir
        
        images_dir = data_dir / "images"

        if not images_dir.exists():
            return

        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=15)

        # 查找images目录下的所有图像文件（兼容不同前缀）
        image_patterns = [
            "gemini_image_*.png",
            "gemini_image_*.jpg",
            "gemini_image_*.jpeg",
            "openai_image_*.png",
            "openai_image_*.jpg",
            "openai_image_*.jpeg",
        ]

        for pattern in image_patterns:
            for file_path in images_dir.glob(pattern):
                try:
                    # 获取文件的修改时间
                    file_mtime = datetime.fromtimestamp(file_path.stat().st_mtime)

                    # 如果文件超过15分钟，删除它
                    if file_mtime < cutoff_time:
                        file_path.unlink()
                        logger.info(f"已清理过期图像: {file_path}")

                except Exception as e:
                    logger.warning(f"清理文件 {file_path} 时出错: {e}")

    except Exception as e:
        logger.error(f"图像清理过程出错: {e}")


async def save_base64_image(base64_string, image_format="png", data_dir=None):
    """
    保存base64图像数据到images文件夹

    Args:
        base64_string (str): base64编码的图像数据
        image_format (str): 图像格式
        data_dir (Path): 数据目录路径，如果为None则使用当前脚本目录

    Returns:
        bool: 是否保存成功
    """
    try:
        # 如果没有传入data_dir，使用当前脚本目录
        if data_dir is None:
            script_dir = Path(__file__).parent.parent
            data_dir = script_dir
        
        images_dir = data_dir / "images"
        # 确保images目录存在
        images_dir.mkdir(exist_ok=True)
        
        # 先清理旧图像
        await cleanup_old_images(data_dir)

        # 解码 base64 数据
        image_data = base64.b64decode(base64_string)

        # 生成唯一文件名（使用时间戳和UUID避免冲突）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_id = str(uuid.uuid4())[:8]
        image_path = images_dir / f"gemini_image_{timestamp}_{unique_id}.{image_format}"

        # 保存图像文件
        async with aiofiles.open(image_path, "wb") as f:
            await f.write(image_data)

        # 获取绝对路径
        abs_path = str(image_path.absolute())
        file_url = f"file://{abs_path}"

        # 更新状态
        await _state.update_saved_image(file_url, str(image_path))

        logger.info(f"图像已保存到: {abs_path}")
        logger.debug(f"文件大小: {len(image_data)} bytes")

        return True

    except base64.binascii.Error as e:
        logger.error(f"Base64 解码失败: {e}")
        return False
    except Exception as e:
        logger.error(f"保存图像文件失败: {e}")
        return False


async def get_next_api_key(api_keys):
    """
    获取下一个可用的API密钥
    
    Args:
        api_keys (list): API密钥列表
        
    Returns:
        str: 当前可用的API密钥
    """
    return await _state.get_next_api_key(api_keys)


async def rotate_to_next_api_key(api_keys):
    """
    轮换到下一个API密钥
    
    Args:
        api_keys (list): API密钥列表
    """
    await _state.rotate_to_next_api_key(api_keys)


async def get_saved_image_info():
    """
    获取最后保存的图像信息

    Returns:
        tuple: (image_url, image_path)
    """
    return await _state.get_saved_image_info()


async def generate_image_openai(
    prompt,
    api_key,
    model: str = "gpt-image-1",
    image_size: str = "1024x1024",
    input_images=None,
    api_base=None,
    api_format: str = "openai",
    max_retry_attempts: int = 3,
):
    """
    使用 OpenAI 兼容的 Chat Completions 接口生成图像。

    Args:
        prompt (str): 图像生成提示词
        api_key (str | list[str]): OpenAI API 密钥或密钥列表。
            - 传入单个字符串时按原行为使用该 key 并重试；
            - 传入列表时将在失败时按顺序轮换到下一个 key。
        model (str): 使用的图像模型，如 gpt-image-1
        image_size (str): 图像尺寸，例如 1024x1024
        input_images (list): 参考图像 base64 列表，会以 data:image/...;base64 形式作为 image_url 传入
        api_base (str): OpenAI API Base，例如 https://api.openai.com
        max_retry_attempts (int): 最大重试次数

    Returns:
        tuple: (image_url, image_path) 或 (None, None) 表示失败
    """
    mode = (api_format or "openai").lower()

    # 规范化为密钥列表，支持单 key 与多 key
    if isinstance(api_key, list):
        api_keys = [k for k in (str(k).strip() for k in api_key or []) if k]
    elif api_key:
        api_keys = [str(api_key).strip()]
    else:
        api_keys = []

    if not api_keys:
        logger.error("generate_image_openai: 未提供 OpenAI API 密钥")
        return None, None

    base_url = (api_base or "https://api.openai.com").rstrip("/")
    # OpenAI 兼容：/v1/chat/completions；Gemini 兼容：拼接通用 v1beta 路径
    if mode == "openai":
        url = f"{base_url}/v1/chat/completions"
    else:
        # 如果 base_url 已经是完整 v1beta 地址则直接使用，否则按通用 Gemini 路径拼接
        if "v1beta" in base_url:
            url = base_url
        else:
            # 例如：https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-vision:generateContent
            url = f"{base_url}/v1beta/models/{model}:generateContent"

    if mode == "openai":
        # OpenAI Chat Completions 兼容格式：messages
        message_content = []
        message_content.append(
            {
                "type": "text",
                "text": (
                    f"Generate an image based on the following description, and respond with either a direct image URL "
                    f"or a single data:image/*;base64,... string.\n\nDescription: {prompt}"
                ),
            }
        )

        if input_images:
            for base64_image in input_images:
                if not base64_image.startswith("data:image/"):
                    base64_image = f"data:image/png;base64,{base64_image}"
                message_content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": base64_image,
                        },
                    }
                )

        if len(message_content) == 1:
            content_field = message_content[0]["text"]
        else:
            content_field = message_content

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": content_field,
                }
            ],
        }
    else:
        # Gemini contents 兼容格式：contents
        parts = [{"text": prompt}]
        if input_images:
            for base64_image in input_images:
                if not base64_image.startswith("data:image/"):
                    base64_image = f"data:image/png;base64,{base64_image}"
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": "image/png",
                            "data": base64_image.split(",", 1)[-1],
                        }
                    }
                )

        payload = {
            "model": model,
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
        }

    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for retry_attempt in range(max_retry_attempts):
            try:
                # 每次重试前选择当前要使用的 key
                current_key = await get_next_api_key(api_keys)
                headers = {
                    "Authorization": f"Bearer {current_key}",
                    "Content-Type": "application/json",
                }

                if retry_attempt > 0:
                    delay = min(2 ** retry_attempt, 10)
                    logger.info(
                        f"OpenAI 图像生成重试 {retry_attempt + 1}/{max_retry_attempts}，"
                        f"使用第 {retry_attempt + 1} 次尝试的密钥，等待 {delay} 秒..."
                    )
                    await asyncio.sleep(delay)

                async with session.post(url, json=payload, headers=headers) as response:
                    data = await response.json()

                    if retry_attempt == 0:
                        logger.debug(f"OpenAI Chat API 响应状态: {response.status}")
                        if isinstance(data, dict):
                            logger.debug(f"OpenAI Chat API 响应数据键: {list(data.keys())}")

                    if response.status == 200 and "choices" in data and data["choices"]:
                        choice = data["choices"][0]
                        message = choice.get("message", {})
                        content = message.get("content")

                        image_url = None
                        image_format = "png"
                        base64_string = None

                        # content 可能是字符串或列表
                        if isinstance(content, str):
                            text = content.strip()
                            # 1）直接是 data:image/...;base64,...
                            if text.startswith("data:image/"):
                                try:
                                    header, base64_part = text.split(",", 1)
                                    image_format = header.split("/")[1].split(";")[0]
                                    base64_string = base64_part
                                except Exception as e:
                                    logger.warning(f"解析 data URL 失败: {e}")
                            # 2）直接是 http(s) 图片 URL
                            elif text.startswith("http://") or text.startswith("https://"):
                                image_url = text
                            else:
                                # 3）兼容 Markdown 等包装形式，例如：
                                #    ![image](data:image/png;base64,xxx)
                                data_match = _DATA_URL_PATTERN.search(text)
                                if data_match:
                                    candidate = data_match.group(1)
                                    try:
                                        header, base64_part = candidate.split(",", 1)
                                        image_format = header.split("/")[1].split(";")[0]
                                        base64_string = base64_part
                                    except Exception as e:
                                        logger.warning(f"解析嵌入文本中的 data URL 失败: {e}")
                                else:
                                    # 再尝试从文本中提取 http(s) 图片 URL
                                    url_match = _HTTP_URL_PATTERN.search(text)
                                    if url_match:
                                        image_url = url_match.group(1)
                        elif isinstance(content, list):
                            # 查找 image_url 类型内容
                            for item in content:
                                if isinstance(item, dict) and item.get("type") in {"image_url", "output_image"}:
                                    url_obj = item.get("image_url") or item.get("url")
                                    if isinstance(url_obj, dict):
                                        candidate = url_obj.get("url")
                                    else:
                                        candidate = url_obj
                                    if isinstance(candidate, str) and candidate:
                                        if candidate.startswith("data:image/"):
                                            try:
                                                header, base64_part = candidate.split(",", 1)
                                                image_format = header.split("/")[1].split(";")[0]
                                                base64_string = base64_part
                                            except Exception as e:
                                                logger.warning(f"解析 image_url data URL 失败: {e}")
                                        else:
                                            image_url = candidate
                                            break

                        # 如果响应中没有找到图片信息，尝试兼容 data[] 风格（部分网关可能复用 images/generations 返回结构）
                        if not image_url and not base64_string and "data" in data and data["data"]:
                            item0 = data["data"][0]
                            if "url" in item0:
                                image_url = item0["url"]
                            elif "b64_json" in item0:
                                base64_string = item0["b64_json"]
                                image_format = "png"

                        # 保存图片
                        if image_url:
                            async with session.get(image_url) as img_response:
                                if img_response.status == 200:
                                    script_dir = Path(__file__).parent.parent
                                    images_dir = script_dir / "images"
                                    images_dir.mkdir(exist_ok=True)

                                    await cleanup_old_images(script_dir)

                                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                    unique_id = str(uuid.uuid4())[:8]
                                    image_path = images_dir / f"openai_image_{timestamp}_{unique_id}.png"

                                    async with aiofiles.open(image_path, "wb") as f:
                                        await f.write(await img_response.read())

                                    abs_path = str(image_path.absolute())
                                    file_url = f"file://{abs_path}"

                                    await _state.update_saved_image(file_url, str(image_path))
                                    logger.info(f"OpenAI Chat 成功生成图像: {abs_path}")
                                    return file_url, str(image_path)
                                else:
                                    logger.error(f"下载 OpenAI Chat 图像失败: {image_url}")

                        if base64_string:
                            if await save_base64_image(base64_string, image_format):
                                logger.info("OpenAI Chat 成功生成图像 (base64 格式)")
                                return await get_saved_image_info()

                        logger.info("OpenAI Chat API 调用成功，但未找到图像数据")
                        return None, None

                    # 非 200 状态处理
                    error_msg = None
                    if isinstance(data, dict):
                        error_msg = data.get("error", {}).get("message", f"HTTP {response.status}")
                    else:
                        error_msg = f"HTTP {response.status}"

                    logger.warning(
                        f"OpenAI Chat API 错误 (重试 {retry_attempt + 1}/{max_retry_attempts}): {error_msg}"
                    )
                    # 遇到错误时尝试切换到下一个 key
                    await rotate_to_next_api_key(api_keys)

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(
                    f"OpenAI Chat API 网络请求失败 (重试 {retry_attempt + 1}/{max_retry_attempts}): {e}"
                )
                # 网络类错误同样尝试轮换 key，以防单个 key 被限流
                await rotate_to_next_api_key(api_keys)
            except Exception as e:
                logger.error(f"调用 OpenAI Chat API 时发生异常: {e}")
                # 未预期异常也尝试轮换 key，但不中断循环
                await rotate_to_next_api_key(api_keys)

    logger.error("OpenAI Chat API 调用失败，已达到最大重试次数")
    return None, None


async def generate_image(prompt, api_key, model="stabilityai/stable-diffusion-3-5-large", seed=None, image_size="1024x1024"):
    """
    已废弃：旧的 SiliconFlow 图像生成接口。

    为兼容历史调用保留空壳实现，统一返回失败并记录日志。
    """
    logger.warning(
        "generate_image 已废弃，请使用 generate_image_openai，并通过兼容网关接入第三方服务。"
    )
    return None, None
