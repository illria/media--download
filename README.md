# Media Download Hub

一个适合个人服务器使用的轻量流媒体解析、下载、音频提取与直播录制面板。

项目按以下服务器规格设计：

- Ubuntu 22.04 x86_64
- 2 核 CPU
- 约 1GB 内存
- 约 30GB 磁盘
- 本机监听 `127.0.0.1:19190`
- 单个 Docker 容器
- 不包含 Nginx、Caddy 或 Cloudflare Tunnel

> 仅处理你有权访问、保存或转换的媒体。项目不会绕过或解密 Widevine、FairPlay 等 DRM，也不会绕过付费墙和账号权限。

## 主要功能

- 中文 Web UI，首次打开时初始化管理员密码。
- yt-dlp 负责公开点播、社交媒体、字幕、封面和音频提取。
- Streamlink 负责公开直播、HLS、DASH 和 m3u8 录制。
- FFmpeg 负责音视频合并和格式处理。
- 链接先解析，再选择视频、音频或直播任务。
- 支持最高分辨率、音频格式、字幕语言和时间段。
- SQLite 持久化任务、设置和下载记录。
- 单任务队列，支持进度、取消、失败重试和诊断日志。
- Cookie 文件加密保存，运行任务时临时解密。
- 支持 HTTP、HTTPS、SOCKS5、SOCKS5H 代理。
- 自动检查磁盘空间并按保留时间清理完成文件。
- 拒绝 `file://`、localhost、内网、链路本地和云 metadata 地址。
- 检测到 DRM 时明确拒绝，不进行解密。

## 一键部署

服务器需要先安装 Docker Engine 和 Docker Compose 插件。

```bash
git clone https://github.com/illria/media--download.git
cd media--download
cp .env.example .env
docker compose up -d --build
```

Koofr 是可选挂载。默认宿主机路径是 `/mnt/koofr`，项目目录是
`/mnt/koofr/Media-Download`。未挂载 Koofr 时，Compose 会使用一个空的可选绑定目录，
不会阻止镜像构建；任务下载和本地保留功能仍可正常使用，Koofr 操作会提示未挂载。

如果 Koofr 实际挂载在其他宿主机路径，复制 `.env.example` 为 `.env` 后修改：

```dotenv
KOOFR_HOST_PATH=/实际的宿主机挂载路径
```

容器会根据 `KOOFR_CONTAINER_PATH`、`KOOFR_SUBPATH` 和 `KOOFR_ROOT` 自动检查当前可用目录；
挂载完成后无需重新构建镜像。

查看运行状态：

```bash
docker compose ps
docker compose logs -f --tail=100
curl http://127.0.0.1:19190/api/health
```

项目默认只绑定本机回环地址：

```text
http://127.0.0.1:19190
```

不会直接把 `19190` 暴露到公网。

## Cloudflare Tunnel

Cloudflare Tunnel 由你自己单独安装和管理，不属于本项目。

Tunnel 服务地址填写：

```text
http://127.0.0.1:19190
```

## 首次登录

默认 `.env` 不填写管理员密码。第一次打开页面时会要求设置至少 8 位密码。

系统会自动生成并保存：

```text
data/database/secret.key
data/database/cookie-encryption.key
```

这两个文件需要和数据库一起备份。丢失 Cookie 加密密钥后，已保存的 Cookie 将无法解密。

也可以提前编辑 `.env`：

```dotenv
ADMIN_PASSWORD=替换为高强度密码
SECRET_KEY=
COOKIE_ENCRYPTION_KEY=
```

不要把 `.env` 或 `data/` 提交到 GitHub。

## 使用流程

1. 打开“新建任务”。
2. 粘贴 YouTube、Bilibili、TikTok、X、Twitch 或公开流媒体链接。
3. 可选择已保存的 Cookie 账号。
4. 点击“解析链接”。
5. 查看标题、平台、时长、格式、字幕、直播和 DRM 状态。
6. 选择视频、仅音频或直播录制。
7. 设置清晰度、音频格式、字幕语言和时间段。
8. 加入队列，在任务中心查看进度。
9. 完成后从媒体库下载到电脑。

## Cookie

建议上传浏览器扩展导出的 Netscape `cookies.txt`。

- 单个文件最大 2MB。
- Cookie 会使用 Fernet 加密。
- 页面不会回显 Cookie 原文。
- 任务运行时只在临时目录短暂解密。

## 默认资源限制

```text
下载并发：1
最大单文件：5GB
默认最高分辨率：1080P
最长普通视频：180 分钟
最长直播录制：120 分钟
最低可用磁盘：5GB
完成文件保留：24 小时
容器内存限制：850MB
```

这些设置可以在 Web UI 的“系统设置”中修改。

## 数据目录

```text
data/
├── database/     SQLite 数据库和持久化密钥
├── downloads/    已完成媒体
├── temp/         下载和后处理临时文件
├── cookies/      加密 Cookie
└── home/         容器运行时缓存
```

## 更新

```bash
cd media--download
git pull
docker compose up -d --build
```

站点接口和反爬规则会持续变化，遇到解析失败时应先重新构建镜像，获取新版 yt-dlp 和 Streamlink。

## 停止与卸载

停止但保留数据：

```bash
docker compose down
```

删除容器和全部数据：

```bash
docker compose down
rm -rf data
```

## 支持范围

系统通过 yt-dlp 和 Streamlink 的现有解析器覆盖大量公开站点，包括但不限于：

- YouTube / YouTube Live
- Bilibili / Bilibili Live
- TikTok
- X / Twitter
- Instagram
- Facebook
- Vimeo
- SoundCloud
- Twitch
- 公开 HLS、DASH、m3u8
- 普通网页嵌入媒体

站点网页、接口、签名和登录策略会变化，因此不能保证任何站点永久可用。

## 明确不支持

- Netflix、Disney+、Prime Video 等 DRM 内容解密。
- 绕过付费墙、登录权限或访问控制。
- 公开匿名下载站和高并发多人服务。
- 用户自定义 Shell 命令或任意服务器输出路径。
- 本机、内网、云 metadata 和服务器文件访问。

## 安全说明

- 默认只监听 `127.0.0.1:19190`。
- API 需要管理员令牌。
- 密码使用 scrypt 哈希保存。
- Cookie 使用 Fernet 加密。
- 子进程使用参数列表启动，不把用户输入拼接进 Shell。
- 下载路径由后端固定生成。
- URL 在创建任务前进行 DNS 和公网 IP 检查。

本项目面向个人私有部署，不建议向不受信任的用户开放。
