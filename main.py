import json
import time
import os
import pathlib
import asyncio
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain

# Try to import get_astrbot_data_path, fallback if not available
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None

@register("helloworld", "YourName", "一个消息审核插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # Setup warnings file path
        if get_astrbot_data_path:
            self.data_dir = get_astrbot_data_path() / "plugin_data" / "helloworld"
        else:
            # Fallback for older versions or if import fails
            # Assuming we are running from AstrBot root
            self.data_dir = pathlib.Path(os.getcwd()) / "data" / "plugin_data" / "helloworld"

        if not self.data_dir.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)

        self.warns_file = self.data_dir / "warns.json"

    def _write_warning(self, warn_entry):
        warns = []
        if self.warns_file.exists():
            try:
                with open(self.warns_file, "r", encoding="utf-8") as f:
                    warns = json.load(f)
            except json.JSONDecodeError:
                pass

        warns.append(warn_entry)

        with open(self.warns_file, "w", encoding="utf-8") as f:
            json.dump(warns, f, ensure_ascii=False, indent=2)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        # Ignore messages if they are empty
        message_str = event.message_str
        if not message_str:
            return

        # Ignore bot's own messages
        sender_id = event.get_sender_id()
        if hasattr(event.message_obj, "self_id") and sender_id == event.message_obj.self_id:
            return

        # Get admin list from config
        admin_qq_list = self.config.get("admin_qq_list", [])
        if not isinstance(admin_qq_list, list):
            # Fallback if user configured a single string by mistake, though schema enforces list
            if isinstance(admin_qq_list, str):
                admin_qq_list = [admin_qq_list]
            else:
                admin_qq_list = []

        # Ignore admin messages to prevent loops if admin triggers it
        if sender_id in admin_qq_list:
            return

        provider_id = self.config.get("llm_provider")
        if not provider_id:
            # No LLM provider configured, skip moderation
            return

        prompt = f"""You are a content safety moderator. Analyze the following user message.
Check for:
1. Severe negative emotions (suicide, self-harm, homicide tendencies).
2. Inappropriate content (illegal acts, pornography, extreme violence).

Message: "{message_str}"

Respond ONLY with a JSON object in this format (no markdown):
{{
  "alert": true,
  "category": "suicide" | "homicide" | "illegal" | "porn" | "violence" | "other",
  "reason": "Explain the reason in Chinese (Simplified)"
}}
OR
{{
  "alert": false
}}
"""

        try:
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )

            response_text = llm_resp.completion_text.strip()
            # Handle potential markdown code blocks
            if response_text.startswith("```"):
                lines = response_text.splitlines()
                if lines[0].strip().startswith("```"):
                    lines = lines[1:]
                if lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                response_text = "\n".join(lines)

            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse LLM response: {response_text}")
                return

            if result.get("alert"):
                # Log warning
                warn_entry = {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "qq": sender_id,
                    "nickname": event.get_sender_name(),
                    "content": message_str,
                    "category": result.get("category"),
                    "reason": result.get("reason")
                }

                # Write to file asynchronously
                await asyncio.to_thread(self._write_warning, warn_entry)

                # Notify Admins
                # Try to get platform name from event, default to aiocqhttp
                platform_name = "aiocqhttp"
                if hasattr(event, "platform") and event.platform:
                    platform_name = event.platform.platform_name

                msg_content = (f"⚠️ 消息审核警告\n"
                               f"时间: {warn_entry['time']}\n"
                               f"用户: {warn_entry['nickname']}({warn_entry['qq']})\n"
                               f"内容: {warn_entry['content']}\n"
                               f"类型: {warn_entry['category']}\n"
                               f"原因: {warn_entry['reason']}")

                for admin_qq in admin_qq_list:
                    if not admin_qq:
                        continue
                    umo = f"{platform_name}:private:{admin_qq}"
                    await self.context.send_message(umo, [Plain(msg_content)])

        except Exception as e:
            logger.error(f"Error in moderation plugin: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # Note: We do NOT stop event propagation here.
        # AstrBot will continue to process the message (e.g., reply to the user if applicable).
