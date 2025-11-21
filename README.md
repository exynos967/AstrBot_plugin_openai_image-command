# AstrBot OpenAI 图像生成插件

基于 OpenAI Images API 的 AstrBot 图像生成插件，使用 OpenAI 官方图片模型（如 `gpt-image-1`）生成高质量图像。

## 功能特点

- 🖼️ **OpenAI 官方生图支持**: 使用 OpenAI Images API（如 `gpt-image-1`）生成图片
- ️ **参考图片支持（占位，当前忽略）**: 插件仍会尝试从消息中收集图片，但在当前 OpenAI 实现中仅记录日志，不参与生成
- ♻️ **智能重试机制**: 支持可配置的自动重试，提高请求成功率和稳定性
- 🚀 **异步处理**: 基于 asyncio 的高性能异步图像生成
- 🔗 **智能文件传输**: 支持本地和远程服务器的文件传输
- 🧹 **自动清理**: 自动清理超过15分钟的历史图像文件
- 🛡️ **错误处理**: 完善的异常处理和错误提示
- 🌐 **多语言支持**: 自动将中文提示词翻译为英文

## 安装配置

### 1. 获取 API Key

前往 [OpenAI](https://platform.openai.com/) 创建 API Key。

### 2. 配置参数

#### 通过Web界面配置

1. 访问AstrBot的Web管理界面
2. 进入"插件管理"页面
3. 找到插件  
4. 点击"配置"按钮进行可视化配置

#### 配置参数说明

- **openai_api_key**: OpenAI API 密钥列表。支持配置一个或多个 Key：
  - 如只需一个 Key，可在配置界面仅填写单项；
  - 如需多 Key 轮换或分账号使用，可填写多项。插件内部会将旧版本中单字符串配置自动转换为列表。
- **openai_api_base**: OpenAI API Base URL，默认 `https://api.openai.com`
- **model_name**: 使用的 OpenAI 图像模型名称，例如 `gpt-image-1` 或其他支持 `/v1/images/generations` 的模型
- **max_retry_attempts**: 每个请求的最大重试次数（默认：3 次，推荐 2-5 次）
- **nap_server_address**: NAP cat 服务地址（同服务器填写 `localhost`）
- **nap_server_port**: 文件传输端口（默认 3658）
- **group_filter_mode**: 群过滤模式，可选 `none` / `whitelist` / `blacklist`：
  - `none`: 不做群过滤（默认）；
  - `whitelist`: 仅名单内群允许使用插件指令；
  - `blacklist`: 名单内群不会响应插件指令。
- **group_filter_list**: 群过滤名单（字符串列表）。结合 `group_filter_mode` 使用：
  - 当为 `whitelist` 时，列表作为白名单；
  - 当为 `blacklist` 时，列表作为黑名单；
  - `none` 时此列表不生效。
- **rate_limit_max_calls_per_group**: 单群在一个限流周期内允许调用插件指令的最大次数。设置为 `0` 表示不启用限流。
- **rate_limit_period_seconds**: 限流周期长度（秒），与 `rate_limit_max_calls_per_group` 搭配使用，默认 `60` 秒。

## 技术实现

### 核心组件

- **main.py**: 插件主要逻辑，继承自 AstrBot 的 Star 类
- **utils/ttp.py**: OpenAI Images API 调用和图像处理逻辑
- **utils/file_send_server.py**: 文件传输服务器通信

### 支持的模型

插件支持配置多种 OpenAI 图像生成模型，包括但不限于：

- `gpt-image-1`
- 其他支持 `/v1/images/generations` 接口的 OpenAI 模型

您可以在插件的配置文件中的 `model_name` 字段指定要使用的模型。

## 文件结构

```
AstrBot_plugin_gemini2.5image-openrouter/
├── main.py                 # 插件主文件
├── metadata.yaml          # 插件元数据
├── _conf_schema.json      # 配置模式定义
├── utils/
│   ├── ttp.py            # openai兼容 API 调用
│   └── file_send_server.py # 文件传输工具
├── images/               # 生成的图像存储目录
├── LICENSE              # 许可证文件
└── README.md           # 项目说明文档
```

## 错误处理

插件包含完善的错误处理机制：

- **API 调用失败处理**: 记录详细的 OpenAI API 错误信息
- **Base64 图像解码错误处理**: 自动检测和修复格式问题
- **参考图片处理异常捕获**: 当参考图片转换失败时的回退机制
- **文件传输异常捕获**: 网络传输失败时的错误提示
- **自动清理失败处理**: 清理历史文件时的异常保护
- **详细的错误日志输出**: 便于调试和问题定位
