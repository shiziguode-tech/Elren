# Elren
A local-first desktop AI agent for developers.

Elren 是面向开发者的本地桌面 AI Agent，包含多模型对话、工具调用、
代码与文件操作、浏览器和桌面操作，以及可选的文档、乐谱与消息平台集成。
模型服务和外部平台需要用户自己的账户、密钥及网络连接。

> 本项目采用 [Elren 源码可见许可证 1.0](LICENSE)，不是 OSI 定义的开源项目。
> 允许商业开发使用、收费安装维护、收费托管及免费完整修复版；
> 软件副本转售及其他衍生竞品受限，具体以正式许可全文为准。
> 第三方组件保留原许可；[发行审查待办](LICENSE_PENDING.md)仍未全部完成。

## 源码目录

- `deepdesk/`：Agent、工具、服务端和前端静态文件。
- `launcher/`、`start.ps1`：Windows 启动器与构建脚本。
- `macos/`：macOS 桌面与打包源码。
- `mobile/`：移动端源码。
- `plugins/`、`skills/`：随项目提供的扩展。
- `tests/`：自动化测试。
- `work/`：少量构建所需的 MCP 源码与离线 OCR 模型，不是完整运行时。

## 从源码运行

源码目录与可直接运行的发行包不同。开发环境需要自行安装 Python 及项目依赖；
完整打包还需要对应平台的编译工具和附加运行时。

在已安装 Python 的开发环境中：

```text
python -m venv .venv
```

Windows PowerShell 激活环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS 激活环境：

```sh
source .venv/bin/activate
```

然后安装并运行：

```text
python -m pip install -e ".[dev]"
elren
```

macOS 完整构建使用 `macos/build-macos-app.sh`，要求 Apple Silicon、Python 3.12、
Xcode Command Line Tools、兼容的 Node.js，以及脚本检查的其他构建资源。
Windows 构建脚本位于 `launcher/`。此源码副本尚未在全新机器上完成完整构建验收。
核心 CI 在 push／PR 时运行 Windows、Linux 离线回归及 Windows 启动器编译；
Android 工作流在相关文件发生 push／PR 变更时运行；完整 macOS 构建仅手动触发。
macOS 工作流会准备哈希锁定的 Audiveris 和 OCR 模型输入；新增流程的云端完整构建仍待验证。

## 参与项目与安全报告

提交修改前请阅读 [贡献指南](CONTRIBUTING.md)。漏洞请按
[安全报告说明](SECURITY.md)私下联系维护者，不要公开密钥或个人数据。

## 配置与隐私

可参考 `.env.example` 或应用设置配置服务；不要把实际密钥写进源码或提交到 Git。
不要提交聊天数据库、密钥保险库、个人记忆、截图、日志或用户输出。
代码可能运行本地命令或操作桌面，使用前请审核工具权限，备份重要文件。

## 测试

```text
python -m pytest -q
```

部分测试需要 Windows/macOS、浏览器或可选运行时；通过单元测试不代表所有
硬件、桌面软件、模型线路和真实操作场景均已通过验收。
内部基准跑分脚本和数据集不在此源码副本中，相应测试在缺少这些资源时明确跳过，
不将跳过计作通过。

## 发行包

经过审核的公共安装包应发布到 GitHub Releases，不放入源码提交。
个人版包含个人资料，禁止作为公共 Release 附件。
当前原生构建未完成商业代码签名和 Apple 公证；不要把系统提示绕过描述成安全保证。

## 第三方组件

Elren 使用并集成了多个开源项目的成果，并非所有底层能力均由本项目独立实现。
感谢这些项目的作者、维护者和贡献者。主要项目与用途见
[开源项目致谢](ACKNOWLEDGEMENTS.md)，版本、许可与发行包注意事项见
[第三方组件声明](THIRD_PARTY_NOTICES.md)和各组件随附的许可证。
第三方组件保留各自的许可和版权声明，不受顶层许可重新授权。
