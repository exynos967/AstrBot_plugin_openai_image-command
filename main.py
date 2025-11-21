import asyncio
import time
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, sp
from astrbot.api.all import Image, Plain
from astrbot.core.message.components import Reply
from .utils.ttp import generate_image_openai
from .utils.file_send_server import send_file


@register("astrbot_plugin_openai_image-command", "薄暝", "使用 OpenAI 的图片接口生成图片", "2.1.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)

        # OpenAI 配置
        self.openai_api_keys = self._normalize_api_keys(config.get("openai_api_key"))
        self.openai_api_base = config.get("openai_api_base", "https://api.openai.com").strip()

        # 接口格式：openai（messages）或 gemini（contents）
        self.api_format = str(config.get("api_format", "openai")).strip().lower() or "openai"

        # 模型配置 - 优先从配置文件加载，全局配置会在命令中覆盖
        self.model_name = config.get("model_name", "gpt-image-1").strip()

        # 重试配置
        self.max_retry_attempts = config.get("max_retry_attempts", 3)

        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")

        # 群过滤配置（模式 + 名单）
        self.group_filter_mode = str(config.get("group_filter_mode", "none") or "none").strip().lower()
        self.group_filter_list = self._normalize_id_list(config.get("group_filter_list"))

        # 限流配置（按群）
        self.rate_limit_max_calls_per_group = int(config.get("rate_limit_max_calls_per_group", 0) or 0)
        self.rate_limit_period_seconds = int(config.get("rate_limit_period_seconds", 60) or 60)

        # 限流状态：group_id -> (window_start_ts, count)
        self._rate_limit_state = {}
        self._rate_limit_lock = asyncio.Lock()

        # 标记是否已经加载过全局配置
        self._global_config_loaded = False

    async def _load_global_config(self):
        """异步加载全局配置"""
        if self._global_config_loaded:
            return

        try:
            plugin_config = await sp.global_get("gemini-25-image-openrouter", {})

            # 如果全局配置中有设置，则覆盖当前配置
            if "model_name" in plugin_config:
                self.model_name = str(plugin_config["model_name"]).strip() or self.model_name
                logger.info(f"从全局配置加载 model_name: {self.model_name}")

            if "openai_api_key" in plugin_config:
                self.openai_api_keys = self._normalize_api_keys(plugin_config["openai_api_key"])
                logger.info("从全局配置加载 openai_api_key 列表")

            if "openai_api_base" in plugin_config:
                self.openai_api_base = str(plugin_config["openai_api_base"]).strip() or self.openai_api_base
                logger.info(f"从全局配置加载 openai_api_base: {self.openai_api_base}")

            if "max_retry_attempts" in plugin_config:
                try:
                    self.max_retry_attempts = int(plugin_config["max_retry_attempts"])
                    logger.info(f"从全局配置加载 max_retry_attempts: {self.max_retry_attempts}")
                except (TypeError, ValueError):
                    logger.warning("全局配置中的 max_retry_attempts 非法，使用本地配置")

            if "api_format" in plugin_config:
                fmt = str(plugin_config["api_format"]).strip().lower()
                if fmt in {"openai", "gemini"}:
                    self.api_format = fmt
                    logger.info(f"从全局配置加载 api_format: {self.api_format}")
                else:
                    logger.warning(f"全局配置中的 api_format={fmt} 非法，保持当前值 {self.api_format}")

            self._global_config_loaded = True
        except Exception as e:
            logger.error(f"加载全局配置失败: {e}")
            self._global_config_loaded = True  # 即使失败也标记为已加载，避免重复尝试

    async def send_image_with_callback_api(self, image_path: str) -> Image:
        """
        优先使用callback_api_base发送图片，失败则退回到本地文件发送

        Args:
            image_path (str): 图片文件路径

        Returns:
            Image: 图片组件
        """
        callback_api_base = self.context.get_config().get("callback_api_base")
        if not callback_api_base:
            logger.info("未配置callback_api_base，使用本地文件发送")
            return Image.fromFileSystem(image_path)

        logger.info(f"检测到配置了callback_api_base: {callback_api_base}")
        try:
            image_component = Image.fromFileSystem(image_path)
            download_url = await image_component.convert_to_web_link()
            logger.info(f"成功生成下载链接: {download_url}")
            return Image.fromURL(download_url)
        except (IOError, OSError) as e:
            logger.warning(f"文件操作失败: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)
        except (ConnectionError, TimeoutError) as e:
            logger.warning(f"网络连接失败: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)
        except Exception as e:
            logger.error(f"发送图片时出现未预期的错误: {e}，将退回到本地文件发送")
            return Image.fromFileSystem(image_path)

    async def _generate_image_via_provider(self, prompt: str, input_images: list | None):
        """
        根据当前 provider_type 调用对应的图像生成提供商。

        Returns:
            tuple[str | None, str | None]: (image_url, image_path)
        """
        # 确保使用最新的全局配置
        await self._load_global_config()

        if not self.openai_api_keys:
            logger.error("未配置 openai_api_key，无法生成图像")
            return None, None

        api_base = self.openai_api_base or "https://api.openai.com"
        logger.info(
            f"使用 {self.api_format} 格式的兼容接口生成图像，model={self.model_name}, api_base={api_base}"
        )

        return await generate_image_openai(
            prompt,
            api_key=self.openai_api_keys,
            model=self.model_name,
            input_images=input_images,
            api_base=api_base,
            api_format=self.api_format,
            max_retry_attempts=self.max_retry_attempts,
        )

    @staticmethod
    def _normalize_api_keys(value) -> list[str]:
        """
        将配置中的 openai_api_key 规范化为字符串列表。

        支持以下输入形式：
        - 单个字符串（可能包含逗号分隔的多个 key）
        - 字符串列表
        - 其他可转为字符串的元素列表
        """
        if value is None:
            return []

        keys: list[str] = []

        # 字符串形式：支持用逗号分隔多个 key，兼容旧配置
        if isinstance(value, str):
            for part in value.split(","):
                k = part.strip()
                if k:
                    keys.append(k)
            return keys

        # 列表形式：逐项转为字符串并去空白
        if isinstance(value, list):
            for item in value:
                if item is None:
                    continue
                k = str(item).strip()
                if k:
                    keys.append(k)

        return keys

    @staticmethod
    def _normalize_id_list(value) -> list[str]:
        """
        将配置中的群号列表规范化为字符串列表。

        支持：
        - 单个字符串（可用逗号分隔多个群号）
        - 列表（元素会被转为字符串）
        """
        if value is None:
            return []

        ids: list[str] = []

        if isinstance(value, str):
            for part in value.split(","):
                gid = part.strip()
                if gid:
                    ids.append(gid)
            return ids

        if isinstance(value, list):
            for item in value:
                if item is None:
                    continue
                gid = str(item).strip()
                if gid:
                    ids.append(gid)

        return ids

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        """
        判断当前事件所在群是否允许使用插件指令。

        通过 group_filter_mode + group_filter_list 控制：
        - mode = whitelist: 仅名单内群允许；
        - mode = blacklist: 名单内群禁止；
        - mode = none 或其他: 不做群过滤。
        """
        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None

        # 私聊或无法获取群号时不做限制
        if not group_id:
            return True

        gid = str(group_id)
        mode = self.group_filter_mode or "none"

        if mode == "whitelist":
            allowed = gid in self.group_filter_list
            if not allowed:
                logger.info(f"群 {gid} 不在白名单中，忽略指令")
            return allowed

        if mode == "blacklist":
            if gid in self.group_filter_list:
                logger.info(f"群 {gid} 命中黑名单，忽略指令")
                return False
            return True

        # none 或未知值：不做过滤
        if mode not in {"none", "whitelist", "blacklist"}:
            logger.warning(f"未知的 group_filter_mode={mode}，按 none 处理")
        return True

    async def _check_and_consume_rate_limit(self, event: AstrMessageEvent) -> bool:
        """
        检查并消耗当前群的限流配额。

        返回 True 表示允许本次调用；False 表示已达到上限。
        """
        if self.rate_limit_max_calls_per_group <= 0 or self.rate_limit_period_seconds <= 0:
            return True

        group_id = None
        try:
            group_id = event.get_group_id()
        except AttributeError:
            group_id = None

        # 仅对群聊做限流，私聊不受限
        if not group_id:
            return True

        gid = str(group_id)
        now = time.time()

        async with self._rate_limit_lock:
            window_start, count = self._rate_limit_state.get(gid, (now, 0))

            # 窗口过期则重置
            if now - window_start >= self.rate_limit_period_seconds:
                window_start, count = now, 0

            if count >= self.rate_limit_max_calls_per_group:
                logger.info(
                    f"群 {gid} 在 {self.rate_limit_period_seconds}s 周期内达到限流上限 "
                    f"{self.rate_limit_max_calls_per_group}，拒绝本次调用"
                )
                return False

            count += 1
            self._rate_limit_state[gid] = (window_start, count)
            return True

    @filter.command("生图")
    async def generate_image_command(
        self,
        event: AstrMessageEvent,
        image_description: str = "",
    ):
        """纯文本生图指令 `/生图`，专注于根据文字描述生成图片。"""
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return
        # NAP 文件转发配置
        nap_server_address = self.nap_server_address
        nap_server_port = self.nap_server_port

        if not image_description:
            # 如果没有显式参数，则从整条消息中提取指令后的文本
            raw = getattr(event, "message_str", "") or ""
            # message_str 形如 "生图 小猫咪"，去掉指令名
            parts = raw.strip().split(" ", 1)
            if len(parts) == 2:
                image_description = parts[1].strip()
            else:
                image_description = ""

        if not image_description:
            yield event.plain_result("请提供要生成图像的文字描述，例如：/生图 一只坐在键盘上的橙色猫，赛博朋克风格。")
            return

        # 生图指令忽略消息中的图片，仅使用文本提示词
        input_images: list = []

        # 调用生成图像的函数
        try:
            image_url, image_path = await self._generate_image_via_provider(
                image_description,
                input_images=input_images,
            )

            if not image_url or not image_path:
                # 生成失败，发送错误消息
                error_chain = [Plain("图像生成失败，请检查API配置和网络连接。")]
                yield event.chain_result(error_chain)
                return

            # 处理文件传输和图片发送
            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=nap_server_address, PORT=nap_server_port)

            # 使用新的发送方法，优先使用callback_api_base
            image_component = await self.send_image_with_callback_api(image_path)
            chain = [image_component]
            yield event.chain_result(chain)
            return

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致图像生成失败: {e}")
            error_chain = [Plain(f"网络连接错误，图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)
            return
        except ValueError as e:
            logger.error(f"参数错误导致图像生成失败: {e}")
            error_chain = [Plain(f"参数错误，图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)
            return
        except Exception as e:
            logger.error(f"图像生成过程出现未预期的错误: {e}")
            error_chain = [Plain(f"图像生成失败: {str(e)}")]
            yield event.chain_result(error_chain)
            return

    @filter.command("手办化")
    async def figure_transform(self, event: AstrMessageEvent):
        """将用户提供的图片转换为手办效果

        使用方法：发送图片并使用 /手办化 指令
        """
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return
        # 检查消息中是否包含图片
        input_images = []
        if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
            for comp in event.message_obj.message:
                if isinstance(comp, Image):
                    try:
                        base64_data = await comp.convert_to_base64()
                        input_images.append(base64_data)
                    except (IOError, ValueError, OSError) as e:
                        logger.warning(f"转换图片到base64失败: {e}")
                    except Exception as e:
                        logger.error(f"处理图片时出现未预期的错误: {e}")
                elif isinstance(comp, Reply):
                    # 处理引用消息中的图片
                    if comp.chain:
                        for reply_comp in comp.chain:
                            if isinstance(reply_comp, Image):
                                try:
                                    base64_data = await reply_comp.convert_to_base64()
                                    input_images.append(base64_data)
                                    logger.info("从引用消息中获取到图片")
                                except (IOError, ValueError, OSError) as e:
                                    logger.warning(f"转换引用消息中的图片到base64失败: {e}")
                                except Exception as e:
                                    logger.error(f"处理引用消息中的图片时出现未预期的错误: {e}")

        # 检查是否找到图片
        if not input_images:
            yield event.plain_result(
                "请提供一张图片以进行手办化处理！\n发送图片后使用 /手办化 指令，或者回复包含图片的消息并使用 /手办化 指令。"
            )
            return

        logger.info(f"开始手办化处理，使用了 {len(input_images)} 张图片")

        # 使用专门的手办化提示词
        figure_prompt = """Please accurately transform the main subject in this image into a realistic, masterpiece-quality 1/7 scale PVC figure.

Specific Requirements:
1. **Figure Creation**: Convert the subject into a high-quality PVC figure with obvious three-dimensional depth and the characteristic glossy finish of PVC material
2. **Packaging Box Design**: Place an exquisite packaging box beside the figure. The front of the box should have a large transparent window displaying the original image, along with brand logos, product name, barcode, and detailed specification panels
3. **Display Base**: The figure should be placed on a round, transparent plastic base with visible thickness
4. **Background Setup**: Place a computer monitor in the background, with the screen displaying the ZBrush 3D modeling process of this figure
5. **Indoor Scene**: Set the entire scene in an indoor environment with appropriate lighting effects

Technical Requirements:
- Maintain the exact characteristics, expressions, and poses from the original image
- The figure must have obvious three-dimensional effects and must never appear flat
- PVC material texture should be clearly visible and realistic
- Avoid any cartoon outline strokes
- If the original image is not full-body, complete it as a full-body figure
- Character proportions should be natural and coordinated (head not too large, legs not too short)
- For animal figures, reduce fur realism to make it more statue-like rather than the real creature
- Pay attention to perspective relationships with near objects appearing larger and distant objects smaller
- No outer outline lines should be present

Please ensure the final result looks like a real commercial figure product that could exist in the market."""

        try:
            image_url, image_path = await self._generate_image_via_provider(
                figure_prompt,
                input_images=input_images,
            )

            if not image_url or not image_path:
                error_chain = [Plain("手办化处理失败，请检查API配置和网络连接。")]
                yield event.chain_result(error_chain)
                return

            # 处理文件传输和图片发送
            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=self.nap_server_address, PORT=self.nap_server_port)

            # 发送处理结果
            image_component = await self.send_image_with_callback_api(image_path)
            result_chain = [Plain("✨ 手办化处理完成！"), image_component]
            yield event.chain_result(result_chain)

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致手办化处理失败: {e}")
            error_chain = [Plain(f"网络连接错误，手办化处理失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except ValueError as e:
            logger.error(f"参数错误导致手办化处理失败: {e}")
            error_chain = [Plain(f"参数错误，手办化处理失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except Exception as e:
            logger.error(f"手办化处理过程出现未预期的错误: {e}")
            error_chain = [Plain(f"手办化处理失败: {str(e)}")]
            yield event.chain_result(error_chain)

    @filter.command("改图")
    async def edit_image_command(
        self,
        event: AstrMessageEvent,
        edit_description: str = "",
        use_reference_images: str = "true",
    ):
        """改图指令 `/改图`，专注于基于用户提供或引用的图片进行修改。

        使用示例：
        - 发送图片并输入：`/改图 把这张图改成赛博朋克风格`
        - 回复一条包含图片的消息并输入：`/改图 给这张图加上蓝色霓虹背景`
        """
        if not self._is_group_allowed(event):
            return

        if not await self._check_and_consume_rate_limit(event):
            yield event.plain_result("本群本周期内的插件调用次数已达上限，请稍后再试。")
            return
        use_reference = str(use_reference_images).lower() in {"true", "1", "yes", "y"}

        # NAP 文件转发配置
        nap_server_address = self.nap_server_address
        nap_server_port = self.nap_server_port

        if not edit_description:
            raw = getattr(event, "message_str", "") or ""
            parts = raw.strip().split(" ", 1)
            if len(parts) == 2:
                edit_description = parts[1].strip()
            else:
                edit_description = ""

        # 收集参考图片
        input_images: list = []
        if use_reference:
            if hasattr(event, "message_obj") and event.message_obj and hasattr(event.message_obj, "message"):
                for comp in event.message_obj.message:
                    if isinstance(comp, Image):
                        try:
                            base64_data = await comp.convert_to_base64()
                            input_images.append(base64_data)
                        except (IOError, ValueError, OSError) as e:
                            logger.warning(f"转换当前消息中的图片到base64失败: {e}")
                        except Exception as e:
                            logger.error(f"处理当前消息中的图片时出现未预期的错误: {e}")
                    elif isinstance(comp, Reply):
                        if comp.chain:
                            for reply_comp in comp.chain:
                                if isinstance(reply_comp, Image):
                                    try:
                                        base64_data = await reply_comp.convert_to_base64()
                                        input_images.append(base64_data)
                                        logger.info("从引用消息中获取到图片")
                                    except (IOError, ValueError, OSError) as e:
                                        logger.warning(f"转换引用消息中的图片到base64失败: {e}")
                                    except Exception as e:
                                        logger.error(f"处理引用消息中的图片时出现未预期的错误: {e}")

        if not input_images:
            yield event.plain_result("请先发送一张图片，或回复包含图片的消息后再使用 /改图 指令。")
            return

        if not edit_description:
            edit_description = "请在保持主体内容不变的前提下，对这张图片进行美化。"

        logger.info(f"改图指令使用了 {len(input_images)} 张图片")

        try:
            image_url, image_path = await self._generate_image_via_provider(
                edit_description,
                input_images=input_images,
            )

            if not image_url or not image_path:
                error_chain = [Plain("改图失败，请检查 OpenAI API 配置和网络连接。")]
                yield event.chain_result(error_chain)
                return

            if self.nap_server_address and self.nap_server_address != "localhost":
                image_path = await send_file(image_path, HOST=nap_server_address, PORT=nap_server_port)

            image_component = await self.send_image_with_callback_api(image_path)
            result_chain = [Plain("✨ 改图完成！"), image_component]
            yield event.chain_result(result_chain)

        except (ConnectionError, TimeoutError) as e:
            logger.error(f"网络连接错误导致改图失败: {e}")
            error_chain = [Plain(f"网络连接错误，改图失败: {str(e)}")]
            yield event.chain_result(error_chain)
        except Exception as e:
            logger.error(f"改图过程出现未预期的错误: {e}")
            error_chain = [Plain(f"改图失败: {str(e)}")]
            yield event.chain_result(error_chain)
