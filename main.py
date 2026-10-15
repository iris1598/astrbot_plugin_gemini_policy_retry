"""Gemini 平台政策与安全拦截自动重试插件。

当模型报错包含 "The model output violates Gemini platform policy."
或 "The model output failed Gemini platform safety checks." 时，
自动在用户消息中追加一条临时合规提示（不写入会话历史 / 不落盘），
引导模型输出合规内容，并自动重试 1 次。
"""

import copy
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star

try:  # >= v4.5.7 agent 重构后可用
    from astrbot.core.agent.message import Message, TextPart

    _HAS_TEXT_PART = True
except ImportError:  # pragma: no cover - 旧版本降级
    Message = None
    TextPart = None
    _HAS_TEXT_PART = False

GEMINI_POLICY_ERRORS = (
    "The model output violates Gemini platform policy.",
    "The model output failed Gemini platform safety checks.",
)

DEFAULT_HINT = (
    "[系统提示：请以安全合规的措辞直接回答，保持原有人设语气，严禁提及规则或道歉。]"
)


def _is_gemini_policy_error(exc: Exception) -> bool:
    err_str = str(exc)
    return any(err in err_str for err in GEMINI_POLICY_ERRORS)


class GeminiPolicyRetryPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        # 按照开发文档读取配置项 retry_prompt，未配置时回退到 DEFAULT_HINT
        hint_cfg = self.config.get("retry_prompt") or self.config.get("hint")
        self.hint: str = (
            hint_cfg if isinstance(hint_cfg, str) and hint_cfg.strip() else DEFAULT_HINT
        ).strip()
        self._provider_change_registered = False

        # 兼容 WebUI 动态加载/重载：实例化时立即尝试包装现有 Provider 并注册变更监听
        self._init_plugin()

    def _init_plugin(self) -> None:
        self._wrap_all_providers()
        if not self._provider_change_registered:
            try:
                self.context.provider_manager.register_provider_change_hook(
                    self._on_provider_change
                )
                self._provider_change_registered = True
            except Exception as e:
                logger.warning(
                    f"[gemini_policy_retry] 注册 Provider 变更钩子失败: {e}"
                )

    # ---------------- Provider 包装 ----------------

    def _wrap_all_providers(self) -> int:
        """包装当前所有聊天 Provider，返回本次新包装的数量。"""
        count = 0
        try:
            provider_insts = self.context.provider_manager.provider_insts
        except Exception as e:
            logger.warning(f"[gemini_policy_retry] 获取 Provider 列表失败: {e}")
            return 0
        for provider in list(provider_insts):
            if self._wrap_provider(provider):
                count += 1
        if count:
            logger.info(
                f"[gemini_policy_retry] 已为 {count} 个聊天 Provider 启用政策拦截自动重试"
            )
        return count

    def _patch_call_args(self, args: tuple, kwargs: dict) -> tuple[tuple, dict]:
        """向请求参数中注入临时合规提示词（不落盘），同时兼容 contexts 与 prompt 模式。"""
        new_args = list(args)
        new_kwargs = dict(kwargs)

        # 1. 尝试从 kwargs 或 args 中定位 contexts (Agent 模式核心)
        contexts = None
        in_kwargs_contexts = False
        in_args_contexts_idx = None

        if "contexts" in new_kwargs and new_kwargs["contexts"] is not None:
            contexts = new_kwargs["contexts"]
            in_kwargs_contexts = True
        elif len(new_args) > 5 and new_args[5] is not None:
            contexts = new_args[5]
            in_args_contexts_idx = 5

        if contexts:
            # 浅拷贝 contexts 列表，避免直接修改 AgentRunner 的 run_context.messages
            new_contexts = list(contexts)
            last_msg = new_contexts[-1]

            # 优先将提示词追加在最后一条 user 消息末尾，避免产生连续的 user 消息
            if Message and isinstance(last_msg, Message) and last_msg.role == "user":
                last_msg = last_msg.model_copy(deep=True)
                if isinstance(last_msg.content, str):
                    last_msg.content = f"{last_msg.content}\n\n{self.hint}"
                elif isinstance(last_msg.content, list):
                    if _HAS_TEXT_PART and TextPart:
                        hint_part = TextPart(text=self.hint).mark_as_temp()
                    else:
                        hint_part = {
                            "type": "text",
                            "text": self.hint,
                            "_no_save": True,
                        }
                    last_msg.content = list(last_msg.content) + [hint_part]
                new_contexts[-1] = last_msg
            elif isinstance(last_msg, dict) and last_msg.get("role") == "user":
                last_msg = copy.deepcopy(last_msg)
                c = last_msg.get("content")
                if isinstance(c, str):
                    last_msg["content"] = f"{c}\n\n{self.hint}"
                elif isinstance(c, list):
                    last_msg["content"] = list(c) + [
                        {"type": "text", "text": self.hint, "_no_save": True}
                    ]
                new_contexts[-1] = last_msg
            else:
                # 尾部不是 user 消息（例如 tool 结果后），追加一条临时 user 提示消息
                if (
                    Message
                    and _HAS_TEXT_PART
                    and TextPart
                    and (not new_contexts or isinstance(new_contexts[0], Message))
                ):
                    hint_msg = Message(
                        role="user",
                        content=[TextPart(text=self.hint).mark_as_temp()],
                    )
                else:
                    hint_msg = {
                        "role": "user",
                        "content": self.hint,
                        "_no_save": True,
                    }
                new_contexts.append(hint_msg)

            if in_kwargs_contexts:
                new_kwargs["contexts"] = new_contexts
            elif in_args_contexts_idx is not None:
                new_args[in_args_contexts_idx] = new_contexts
            return tuple(new_args), new_kwargs

        # 2. 如果没有 contexts，检查 prompt 模式
        prompt = None
        in_kwargs_prompt = False
        in_args_prompt_idx = None

        if "prompt" in new_kwargs and new_kwargs["prompt"] is not None:
            prompt = new_kwargs["prompt"]
            in_kwargs_prompt = True
        elif len(new_args) > 0 and new_args[0] is not None:
            prompt = new_args[0]
            in_args_prompt_idx = 0

        if prompt is not None:
            if _HAS_TEXT_PART and TextPart:
                parts = list(new_kwargs.get("extra_user_content_parts") or [])
                parts.append(TextPart(text=self.hint).mark_as_temp())
                new_kwargs["extra_user_content_parts"] = parts
            else:
                patched_prompt = f"{prompt}\n\n{self.hint}"
                if in_kwargs_prompt:
                    new_kwargs["prompt"] = patched_prompt
                elif in_args_prompt_idx is not None:
                    new_args[in_args_prompt_idx] = patched_prompt

        return tuple(new_args), new_kwargs

    def _wrap_provider(self, provider) -> bool:
        if provider is None or getattr(
            provider, "_gemini_policy_retry_wrapped", False
        ):
            return False

        orig_text_chat = getattr(provider, "text_chat", None)
        orig_text_chat_stream = getattr(provider, "text_chat_stream", None)
        if not orig_text_chat or not orig_text_chat_stream:
            return False

        async def text_chat_with_retry(*args, **kwargs):
            try:
                return await orig_text_chat(*args, **kwargs)
            except Exception as e:
                if not _is_gemini_policy_error(e):
                    raise
                logger.warning(
                    "[gemini_policy_retry] 检测到 Gemini 平台政策/安全拦截，"
                    "附加合规提示后自动重试 1 次..."
                )
                patched_args, patched_kwargs = self._patch_call_args(args, kwargs)
                return await orig_text_chat(*patched_args, **patched_kwargs)

        def text_chat_stream_with_retry(*args, **kwargs):
            async def _gen():
                yielded = False
                try:
                    async for chunk in orig_text_chat_stream(*args, **kwargs):
                        # 若已有面向用户的正文输出，标记为已输出（避免重复生成）
                        if getattr(chunk, "completion_text", None) or (
                            getattr(chunk, "result_chain", None)
                            and getattr(chunk.result_chain, "chain", None)
                        ):
                            yielded = True
                        yield chunk
                    return
                except Exception as e:
                    if not _is_gemini_policy_error(e) or yielded:
                        raise
                logger.warning(
                    "[gemini_policy_retry] 检测到 Gemini 平台政策/安全拦截，"
                    "附加合规提示后自动重试 1 次..."
                )
                patched_args, patched_kwargs = self._patch_call_args(args, kwargs)
                async for chunk in orig_text_chat_stream(
                    *patched_args, **patched_kwargs
                ):
                    yield chunk

            return _gen()

        provider.text_chat = text_chat_with_retry
        provider.text_chat_stream = text_chat_stream_with_retry
        # 保存原始方法，便于插件卸载时还原
        provider._gemini_policy_retry_wrapped = True
        provider._gemini_policy_retry_orig_text_chat = orig_text_chat
        provider._gemini_policy_retry_orig_text_chat_stream = orig_text_chat_stream
        return True

    def _unwrap_all_providers(self) -> None:
        try:
            provider_insts = self.context.provider_manager.provider_insts
        except Exception:
            return
        for provider in list(provider_insts):
            if not getattr(provider, "_gemini_policy_retry_wrapped", False):
                continue
            try:
                provider.text_chat = provider._gemini_policy_retry_orig_text_chat
                provider.text_chat_stream = (
                    provider._gemini_policy_retry_orig_text_chat_stream
                )
            except Exception as e:
                logger.warning(f"[gemini_policy_retry] 还原 Provider 失败: {e}")
            finally:
                provider._gemini_policy_retry_wrapped = False

    def _on_provider_change(self, provider_id, provider_type, umo) -> None:
        """Provider 增删/更新后，包装新增的 Provider。"""
        try:
            self._wrap_all_providers()
        except Exception as e:
            logger.warning(f"[gemini_policy_retry] 处理 Provider 变更失败: {e}")

    # ---------------- 生命周期 ----------------

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 初始化完成后包装所有 Provider，并监听后续变更。"""
        self._init_plugin()

    async def terminate(self):
        """插件卸载/停用时还原 Provider 的原始方法并清理变更监听。"""
        self._unwrap_all_providers()
        try:
            hooks = getattr(
                self.context.provider_manager, "_provider_change_hooks", None
            )
            if isinstance(hooks, list) and self._on_provider_change in hooks:
                hooks.remove(self._on_provider_change)
                self._provider_change_registered = False
        except Exception as e:
            logger.warning(
                f"[gemini_policy_retry] 注销 Provider 变更钩子失败: {e}"
            )
