# astrbot_plugin_gemini_policy_retry

当模型因 Gemini 平台政策或内容安全策略被拦截时（例如报错 **The model output violates Gemini platform policy.** 或 **The model output failed Gemini platform safety checks.**），自动在用户消息后追加一条**不会落盘**的临时提示，引导模型输出合规内容，并**自动重试 1 次**。

## 工作原理

AstrBot 内置的重试（`request_max_retries`）只针对传输层错误；Gemini 内容拦截是在响应解析阶段抛出的普通异常，不会被框架自动重试。本插件在 Provider 调用层进行包装：

1. **自动挂载与热重载兼容**：插件初始化（`__init__`）及 AstrBot 加载完毕（`on_astrbot_loaded`）时包装所有聊天 Provider 的 `text_chat` / `text_chat_stream` 方法，并通过 `register_provider_change_hook` 动态跟踪后续新增或切换的 Provider；在插件卸载时（`terminate`）完整注销钩子并还原方法。
2. **全场景临时合规提示注入**：捕获到 Gemini 政策/安全拦截异常后：
   - **Agent 模式（主流 `contexts` 上下文）**：拷贝上下文并将临时合规提示安全注入到用户消息末尾；
   - **Prompt 模式**：通过 `extra_user_content_parts` 或提示词追加合规提示。
   该修改仅存在于本次重试的请求 Payload 中，不会写回 Agent 的 `run_context.messages` 或数据库，**完全不落盘**。
3. **安全单次重试**：使用原始方法自动重试 **1 次**；重试仍失败则按原逻辑抛出异常（交由框架的 fallback / 错误提示流程）。
4. **流式正文保护**：流式模式下，如果拦截发生前已经输出了面向用户的正文内容，则不重试（避免已发内容重复），直接抛出原异常；若仅输出了思考过程（thinking）被拦截，则正常进入重试。

## 安装

将整个 `astrbot_plugin_gemini_policy_retry` 文件夹放入 AstrBot 的 `data/plugins/` 目录，然后在 WebUI 中重载插件即可。

## 自定义提示词

支持以下两种方式自定义合规提示词：
1. **AstrBot 管理面板可视化配置（推荐）**：在 WebUI 的插件配置界面中，直接编辑“合规重试提示词”并保存即可生效，无需修改代码。
2. **代码级修改**：编辑 `main.py` 顶部的 `DEFAULT_HINT` 常量。
