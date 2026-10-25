# Gamdl Desktop Fork 1.1.5

[![Release](https://img.shields.io/badge/release-1.1.5-0a84ff)](https://github.com/Mrgu2/gu_music_downloader/releases)
[![License](https://img.shields.io/github/license/Mrgu2/gu_music_downloader)](https://github.com/Mrgu2/gu_music_downloader/blob/codex/alac-download/LICENSE)

<p align="center">
  <img src="assets/macos/app-icon-1024.png" alt="Desktop app icon" width="180" />
</p>

这是一个以桌面版为中心维护的下载工具 fork，基于上游 [Gamdl](https://github.com/glomatico/gamdl) 改编，由 [@Mrgu2](https://github.com/Mrgu2) 持续维护。

软件纯免费开源，无病毒，无额外广告。请确保你是从 GitHub [@Mrgu2](https://github.com/Mrgu2/gu_music_downloader) 下载该软件，以保证来源可核验。

## 项目定位

`Gamdl Desktop Fork 1.1.5` 的目标很明确：

- 提供开箱即用的桌面客户端，而不是只给命令行
- 默认覆盖最常用场景：歌曲、专辑、歌单、艺术家下载
- 支持本地 FFmpeg 转换页面，面向实际整理音乐库
- 保留上游 CLI 能力，方便高级用户继续脚本化使用

当前维护分支：

- Fork 仓库：[Mrgu2/gu_music_downloader](https://github.com/Mrgu2/gu_music_downloader)
- 当前修改分支：[codex/alac-download](https://github.com/Mrgu2/gu_music_downloader/tree/codex/alac-download)
- 上游项目：[glomatico/gamdl](https://github.com/glomatico/gamdl)

## 1.1.5 发布内容

正式版 `1.1.5` 这次继续发布 macOS 桌面包，并同时覆盖 Apple Silicon 与 Intel 机型：

- `Apple Music Downloader-arm64.dmg`
  - 面向 Apple Silicon Mac
  - 包含桌面应用和内置 `ffmpeg`
- `Apple Music Downloader-x86_64.dmg`
  - 面向 Intel Mac
  - 包含桌面应用和内置 `ffmpeg`

源码仓库仍然保留 CLI、Windows 启动器和开发环境，适合高级用户自行构建或二次修改。

本次版本主要修复部分 ALAC 歌曲下载时只报出内部音轨标识的问题，并补齐回归测试与双架构打包验证：

- 当播放列表音轨标识与资产元数据不一致时，按所选音频组匹配，修复 `'dktu2l6sm'` 等报错
- 补齐音轨标识缺失、元数据缺失和匹配不唯一时的处理，避免崩溃或任意使用其他音轨的元数据
- macOS 构建保留旧产物、强制装入当前源码，并修正系统显示的应用版本号
- 两个架构的正式应用均完成 ALAC 实际下载与完整解码验证
- 当前 release 不附带 Windows 现成安装包
- 详细更新及验证说明见 [1.1.5 发布说明](docs/release-1.1.5.md)

## 主要功能

### 下载

- 下载歌曲、专辑、歌单、艺术家
- 支持浏览器辅助登录
- 支持导入浏览器登录态
- 统一任务队列与日志页面
- 默认 AAC 可直接使用
- `ALAC / 杜比全景声` 继续走外部 wrapper
- 艺术家链接首版支持 `Top Songs / Main Albums / Singles / EPs / All Albums`
- 下载结束后支持对失败歌曲发起重试

### 下载整理与并发设置

在设置页可选择保留或舍弃文件夹结构。舍弃时，歌曲直接保存到输出目录，命名为 `[序号]歌名-专辑名-歌手`；歌单使用原歌单顺序，直接下载专辑时使用专辑曲目号。音频内嵌曲目标签仍保留专辑信息。封面使用 `cover-专辑名` 加原图片扩展名，同名专辑封面自动覆盖。

“下载后格式”可选保留原格式、FLAC、MP3；转换参数与转换菜单一致。“转换后保留原文件”控制是否同时保留新下载的原格式文件；关闭时只在转换成功后删除本任务新下载的原文件，失败时和已有原文件均保留。

下载、转换并发数分别支持 1–32，默认均为 8。歌单与专辑的歌曲信息准备、下载、转换可重叠执行；独立转换菜单也使用设置中的转换并发数。新设置仅影响后续新任务。

任务页分别显示信息准备、下载、转换的进度与当前歌曲。日志停在底部时自动跟随新记录，向上阅读时保持位置，刷新不会覆盖早期记录。

### 转换

- 左侧栏独立 `转换` 页面
- 支持选择单个文件或整个目录
- 支持选择输出目录
- 支持输出 `FLAC` 和 `MP3`
- 目录输入时保留原始目录结构
- 复制常见标签和封面
- 自动写入安全提示评论字段

转换策略：

- `FLAC`
  - 保持原采样率
  - 尽量保持原位宽
  - 保留曲目编号、曲目总数、碟片编号、碟片总数
- `MP3`
  - 保持原采样率
  - 使用高质量 `libmp3lame -q:a 0`
  - 对不兼容采样率直接报错，不偷偷重采样

### 诊断与安全

- 本地保存设置、日志和诊断信息
- About 页面显示 fork 来源、修改者和安全下载提示
- macOS 打包时优先使用内置 `ffmpeg`
- 缺失 `ffmpeg` 时会在界面中直接提示，不会默默失败

## 下载与安装

### macOS

1. 前往 [Releases](https://github.com/Mrgu2/gu_music_downloader/releases)
2. 下载当前 release 提供的 macOS dmg 安装包
3. 将 app 拖到 `Applications`
4. 首次打开如果被 Gatekeeper 阻止，在 `系统设置 > 隐私与安全性` 里点 `仍要打开`

macOS 包说明：

- 当前发布包内置 `ffmpeg`
- 当前 release 同时附带 Apple Silicon 和 Intel 构建
- 如需重新打包，请参考下方构建说明

### Windows

当前 `1.1.5` release 不附带 Windows 安装包。

Windows 包说明：

- 如需使用，请在 Windows 环境自行修改并编译
- 推荐直接使用 Claude Code 或 Codex 之类的 agent 协助处理 Windows 适配和打包
- 目前主要做过源码与构建级验证，建议保留源码仓库用于问题复现

## 使用说明

### 下载页面

1. 先使用浏览器辅助登录，或导入已登录浏览器的登录态
2. 粘贴一个或多个支持的链接
3. 如果包含艺术家链接，先选择要展开下载的艺术家内容
4. 确认输出目录和下载设置
5. 加入任务队列并在 `任务` 页面查看进度

登录安全说明：

- 桌面版只会把 Apple Music 登录态持久化到系统 keyring / Keychain
- 如果你的系统凭据存储不可用，界面会提示重新启用，而不会再把 token 明文写入本地文件

如果任务里有失败歌曲，`任务` 页面会提供“重试失败歌曲”入口，避免手动回去重新找原始链接。

### 转换页面

1. 选择输入模式：`文件` 或 `目录`
2. 选择输入路径
3. 选择输出目录
4. 选择输出格式：`FLAC` 或 `MP3`
5. 加入转换队列

评论字段会统一写入：

`软件纯免费开源，无病毒，无额外广告，请确保你是从 GitHub @Mrgu2 下载的该软件。`

## 构建说明

### macOS 本地构建

```bash
uv sync --extra desktop-build
bash scripts/build-macos-app.sh
```

如果你要构建 Intel mac 版本，至少要准备一份 `x86_64` 的 `ffmpeg`，然后显式指定目标架构：

```bash
cp /path/to/ffmpeg assets/macos/ffmpeg-x86_64
MACOS_ARCH=x86_64 bash scripts/build-macos-app.sh
```

说明：

- 脚本会为 `arm64` 和 `x86_64` 分别创建独立构建环境，默认目录分别是 `.venv-macos-arm64` 和 `.venv-macos-x86_64`
- `x86_64` 构建不会复用当前的 arm64 `.venv`
- 如果没有提供兼容目标架构的 `ffmpeg`，脚本会直接报错
- 构建会强制重装当前项目，确保同版本下的源码修复进入安装包
- 如默认输出目录已有应用或 DMG，脚本会保留文件并退出；再次构建请设置新的 `MACOS_DIST_DIR`，也可通过 `MACOS_BUILD_DIR` 指定工作目录

构建产物：

```text
dist/*.app
dist/*-arm64.dmg
dist/*-x86_64.dmg
```

### 本地更新时保留现有图标（macOS）

已安装应用时，可使用以下脚本先备份旧应用、构建当前源码，再保留原有应用图标及 Finder 自定义图标进行替换安装：

```bash
bash scripts/install-local-preserve-icon.sh
```

脚本会在安装前检查应用是否仍在运行，并校验新应用签名。

### Windows 构建

如果你是在 Windows 本地构建，可参考：

```bash
uv sync --extra desktop-build
uv run python -m unittest discover -s tests
uv run pyinstaller --noconfirm "<Windows spec 文件>"
```

## ALAC / Wrapper 快速说明

如果你要启用 `ALAC / 杜比全景声`，桌面版仍然依赖外部 wrapper。

首次安装时建议优先使用 [wrapper releases](https://github.com/WorldObservationLog/wrapper/releases) 里的 zip 包，不建议普通用户直接 clone 源码。先跑通再改，不要先改容器名、端口或目录。为了减少 Apple Silicon 机器踩坑，下面的构建和运行命令默认都直接带上 `--platform linux/amd64`。

普通用户直接复制这一段即可：

```bash
mkdir -p ~/wrapper-release && cd ~/wrapper-release
rm -rf rootfs wrapper Dockerfile wrapper.zip
curl -fsSL https://api.github.com/repos/WorldObservationLog/wrapper/releases/latest \
  | grep browser_download_url \
  | grep 'Wrapper.x86_64.*\.zip' \
  | cut -d '"' -f 4 \
  | xargs -n 1 curl -L -o wrapper.zip
unzip -o wrapper.zip
docker build --platform linux/amd64 -t wrapper-local .
```

如果上面失败，再执行诊断：

```bash
pwd
ls -l
test -f ./wrapper && echo "wrapper ok" || echo "wrapper missing"
test -d ./rootfs && echo "rootfs ok" || echo "rootfs missing"
test -f ./Dockerfile && echo "dockerfile ok" || echo "dockerfile missing"
ls -l wrapper.zip
```

如果这里出现 `wrapper missing`、`rootfs missing`、`dockerfile missing`、`wrapper.zip` 不存在，或者 `COPY ./wrapper /app: not found`，不要继续往后跑。先重新执行上面的自动下载命令。

首次登录示例：

注意：wrapper 当前的账号登录参数会把 Apple ID 凭据暴露在 shell history 和进程列表里。尽量只在临时 shell 里执行，不要把这条命令长期留在共享机器的历史记录中。

```bash
docker run --rm -it \
  --platform linux/amd64 \
  -v "$PWD/rootfs/data:/app/rootfs/data" \
  -e args="-L your_apple_id@example.com:your_password -F -H 0.0.0.0 -D 10022 -M 20022" \
  wrapper-local
```

长期运行示例：

注意：不要映射 wrapper 的 account 端口。wrapper 在容器里仍需要监听 `0.0.0.0`，但宿主机端口只绑定到 `127.0.0.1`，这样桌面版可访问，局域网其他主机不可访问。

再次强调：不要把 wrapper 的 account API 暴露到 `0.0.0.0` 或公网 / 局域网可达地址；桌面版默认只信任 `localhost / 127.0.0.1 / ::1`。

```bash
docker run -d \
  --platform linux/amd64 \
  --name wrapper-latest-10022 \
  -v "$PWD/rootfs/data:/app/rootfs/data" \
  -p 127.0.0.1:10022:10022 \
  -p 127.0.0.1:20022:20022 \
  -e args="-H 0.0.0.0 -D 10022 -M 20022" \
  wrapper-local
```

启动后自检：

```bash
docker ps --filter name=wrapper-latest-10022
nc -vz 127.0.0.1 10022
nc -vz 127.0.0.1 20022
docker logs --tail 30 wrapper-latest-10022
```

常见报错：

- `无法启用 Rosetta 2` / 镜像架构不匹配：确认运行命令里保留了 `--platform linux/amd64`
- `COPY ./wrapper /app: not found`：当前 release 解压目录不完整，先回到构建前自检
- `127.0.0.1:10022 connection refused`：wrapper 容器没在运行，先执行 `docker start wrapper-latest-10022`

如果 release 包下载失败，再回退到源码仓库方案：

```bash
git clone https://github.com/WorldObservationLog/wrapper.git
cd wrapper
```

## 开发与验证

常用本地验证命令：

```bash
uv run python -m unittest discover -s tests
uv run python -m unittest tests.test_web_gui
uv run python -m unittest tests.test_conversion
```

开发时启动桌面版：

```bash
uv run python -m gamdl.desktop_app
```

## CLI 说明

这个仓库仍然保留上游 `gamdl` CLI 能力，但当前 README 不再复制整份上游命令参考，避免桌面版文档被淹没。

如果你是高级用户，可以直接查看：

- 上游 CLI 项目：[glomatico/gamdl](https://github.com/glomatico/gamdl)
- 当前源码仓库：[Mrgu2/gu_music_downloader](https://github.com/Mrgu2/gu_music_downloader)

命令行入口仍然可用：

```bash
gamdl --help
```

## 本地数据目录

### macOS 桌面版

这里会保存在 macOS `Application Support` 下的当前桌面版目录中：

- 设置
- 登录态元数据
- 日志
- 诊断包
- 临时文件

说明：

- 真正的 Apple Music token 不应再以明文文件形式保存在这里
- 如果你看到旧版本遗留的 `.token` 文件，新版本启动时会自动删除

这套桌面版数据与 CLI 的配置文件分离，不会覆盖你自己的命令行配置。

## 致谢

- 上游项目：[glomatico/gamdl](https://github.com/glomatico/gamdl)
- 当前 fork 与桌面版维护者：[Mrgu2](https://github.com/Mrgu2)
- 外部 wrapper 项目：[WorldObservationLog/wrapper](https://github.com/WorldObservationLog/wrapper)
