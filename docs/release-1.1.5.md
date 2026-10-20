Apple Music Downloader 1.1.5

更新内容
- 修复部分 ALAC 歌曲下载失败、日志只显示 `'dktu2l6sm'` 或类似内部标识的问题：当播放列表中的音轨标识与音频资产元数据不一致时，现在会按当前音频组匹配对应元数据。
- 补齐相关兼容处理：音轨标识缺失时仍可匹配音频组；元数据缺失、匹配不唯一或缺少 key IDs 时记录原因，避免直接崩溃或任意选取其他音轨的元数据。
- 新增 ALAC 元数据回归测试，覆盖正常匹配、标识不一致及主要异常边界。
- 改进 macOS 打包流程：保留已有构建产物，支持独立构建目录，并强制装入当前项目源码，防止旧构建环境遗漏修复。
- 修正 macOS 系统中的应用版本信息，使其与项目版本 1.1.5 保持一致。

说明
- 当前 release 对应分支：`codex/alac-download`
- 当前 release 包含两个 macOS 安装包：`Apple Music Downloader-arm64.dmg` 和 `Apple Music Downloader-x86_64.dmg`
- `arm64` 面向 Apple Silicon Mac，`x86_64` 面向 Intel Mac
- 两个安装包都包含桌面应用和内置 `ffmpeg`
- ALAC / 杜比全景声仍需要可用的外部 wrapper 和有效 Apple Music 订阅
- 与前版一样，不附带 Windows 安装包
- 当前安装包未经过 Apple 公证；首次启动若被系统阻止，可在“系统设置 > 隐私与安全性”中选择“仍要打开”

验证
- 全套 281 项源码回归测试通过，锁文件和差异检查通过。
- 两个正式安装包均完成架构、应用版本、签名完整性、DMG 校验和内容一致性检查，包内包含本次修复。
- 使用两个正式打包应用分别下载 `Everybody!!`，均成功 1、跳过 0、错误 0；输出为 ALAC、24-bit / 48 kHz、183.4 秒，完整解码通过。
- Intel 构建在 Apple Silicon Mac 的 Rosetta 环境完成启动及下载测试，尚未在实体 Intel Mac 上验收。
