# 开源项目致谢 / Open-source acknowledgements

Elren 的实现使用、集成或随发行包提供了下列开源项目的成果。
感谢各项目的作者、维护者和贡献者。下表区分源码中的组件、安装依赖与
发行包运行时；不表示所有组件的完整源码或二进制均包含在本仓库中，
也不表示上游项目参与、认可或为 Elren 提供担保。

## 主要项目与用途

| 项目 | 在 Elren 中的用途与集成方式 |
| --- | --- |
| OpenClaw | 外部 Agent/工具运行时集成，以及 `skills/openclaw-bundled` 中保留的技能资源；完整运行时由发行包提供。 |
| [Audiveris](https://github.com/Audiveris/audiveris) | 通过独立本地进程进行光学乐谱识别；源码仓库包含 Elren 的 JVM 引导代码，完整识谱运行时由发行包提供。 |
| [Tesseract tessdata](https://github.com/tesseract-ocr/tessdata) | 随仓库保留的英文、简体中文、繁体中文 OCR 数据，用于识谱运行时。 |
| [jianpu-ly](https://github.com/ssb22/jianpu-ly) | 随仓库保留的简谱到 LilyPond 转换器，支持带五线谱输出；Elren 的兼容适配代码单独维护。 |
| [GNU LilyPond](https://lilypond.org/) | 发行包中的离线乐谱排版程序，生成 SVG/PDF；不是 Elren 自研排版引擎。 |
| Playwright / Chromium | 浏览器自动化依赖及发行包中的浏览器运行时。 |
| PyAutoGUI / pywinauto / pywin32 / PyObjC | 桌面输入、窗口自动化和平台系统接口依赖，按操作系统使用。 |
| RapidOCR / ONNX Runtime | 本地 OCR 与模型推理依赖。 |
| FastAPI / Uvicorn / Pydantic / pydantic-settings | 本地服务、请求与配置校验。 |
| HTTPX / python-dotenv | 网络请求及环境配置加载。 |
| Pillow / qrcode | 图像处理及二维码生成。 |
| pypdf / python-docx / python-pptx / openpyxl / xlrd / striprtf / ReportLab | PDF、文档、演示文稿和表格处理依赖。 |
| edge-tts / lark-oapi / gradio-client | 语音及外部平台客户端集成；对应在线服务并不因此成为开源软件。 |
| PyCryptodome / psutil / pyperclip / tzdata | 密码学功能、系统信息、剪贴板与时区数据依赖。 |
| [KaTeX](https://katex.org/) | 随仓库保留的数学公式显示代码与字体。 |
| [Air Datepicker](https://github.com/t1m0n/air-datepicker) | 随仓库保留的日期、时间选择器及语言资源。 |
| Lucide / Feather | 界面图标成果；版权及许可保留于 `deepdesk/static/icons/LICENSE-lucide.txt`。 |
| CPython / Node.js / npm | 平台发行包中的语言与包管理运行时。 |
| Git / Git LFS / Git Credential Manager / dugite-native | macOS 便携 Git 工具链及凭据集成，具体构建以锁定的发行包为准。 |
| jq / ripgrep / GitHub CLI / FFmpeg / mcporter / xurl | 默认便携工具运行时中的命令行能力；具体版本见运行时锁文件。 |
| Gradle / Kotlin / Android 开源组件 | 移动端构建和平台依赖；应以实际解析的构建依赖为准。 |
| pytest / pytest-asyncio / Ruff / debugpy / Hatchling | 测试、静态检查、调试与 Python 构建依赖。 |

## 许可和来源说明

- 本声明是主要项目致谢，不是所有传递依赖的完整 SBOM，也不替代许可证全文。
- Python 直接依赖及可选基准依赖见 [pyproject.toml](pyproject.toml)；
  便携工具来源及校验值见 [launcher/tool-runtime.lock.json](launcher/tool-runtime.lock.json)。
- 已记录的版本、许可证位置、源码来源及待完成的发行审查见
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 保留随组件提供的版权、许可证及 `UPSTREAM.json`；第三方组件适用各自的许可，
  本项目的任何顶层许可均不能覆盖或替代它们。
- Microsoft WebView2 是 Windows 外壳使用的第三方 SDK/运行时，不在此声明中
  将其概括为开源项目；其 SDK 二进制再分发条款需要单独核对。
- 本声明不把使用远程模型 API 等同于获得或使用模型的开源权重，
  也不声称 Elren 使用了 Codex 或 Claude Code 的非公开源码。
- Elren 顶层许可证尚待项目所有者确定；致谢本身不构成开源授权。
