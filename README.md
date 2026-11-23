# Music Agent

## 环境划分

- 本机开发环境使用 `.env.local`，Compose 项目名为 `music-agent-local`，数据卷独立保存。
- Ubuntu 服务器作为测试环境，使用 `.env.test`，Compose 项目名保持 `music-agent`，沿用现有容器和数据卷。
- 两个环境都使用同一份 `compose.yaml` 和代码；密钥文件不会打包或提交。

## 简历原文件保存到腾讯云 COS

简历工作台上传文件时，API 会在提取文本并建立索引前，把原文件保存到 `COS_BUCKET` 对应桶的 `COS_PREFIX/` 目录。当前默认值为桶 `rag-1344102550`、地域 `ap-guangzhou`、目录 `resume/`。不配置 `COS_BUCKET` 时只使用现有本地 RAG 存储；配置桶名后必须同时配置 SecretId 和 SecretKey，否则上传请求会返回配置错误。

在本机，把 `COS_BUCKET`、`COS_REGION`、`COS_PREFIX`、`COS_SECRET_ID` 和 `COS_SECRET_KEY` 写入项目根目录的 `.env.local`；Ubuntu 测试服务器则写入部署目录的 `.env.test`。Compose 会把这两个环境文件提供给 API。不要把密钥写入 `.env.example`、代码或聊天，也不要提交 `.env.local` / `.env.test`。给腾讯云子用户配置该桶 `resume/*` 对象写入权限即可。

## 平台账号与双后端

平台前端不显示注册入口；后端提供 `POST /auth/register`，可由 Apifox 等 API 客户端创建账号。注册用户名需为 3 至 80 位字母、数字、点、下划线或连字符；密码按 UTF-8 编码为 8 至 72 字节。登录用户名不区分大小写，密码使用 BCrypt 哈希保存；登录会话由 Spring Boot 签发随机 Cookie，数据库只保存会话令牌的 SHA-256 摘要。部署 HTTPS 环境时，在对应环境文件中设置 `SESSION_COOKIE_SECURE=true`；仅本机 HTTP 开发环境使用 `false`。

Spring Boot 位于 `auth-service/`，负责管理员登录、登出和会话验证；Python FastAPI 继续负责音乐创作与生成。两者共用 Music Agent 的 MySQL 库。网页通过 Gateway 同源 `/auth/*` 路由访问 Spring Boot，登录 Cookie 使用 `HttpOnly` 和 `SameSite=Lax`。

`gateway-service/` 是对外 HTTP 入口，负责将 `/auth/*` 转给 Spring Boot，将工作台和音乐 API 转给 Python，并在转发前验证登录会话。验证通过后，Gateway 会覆盖 `X-Authenticated-User` 身份头；限流计数由独立 Redis 保存，登录接口按来源 IP 限流，已登录请求按用户限流。API 不映射宿主机端口，外部的 Nginx/HTTPS 反向代理应转发至 Gateway 的宿主机回环端口 `127.0.0.1:8000`。

如需将网站限制为仅固定公网 IP 访问，可将 `deploy/nginx/music-agent-ip-allowlist.conf` include 到服务器上 Music Agent 网站对应的 Nginx `server {}` 配置块中。当前白名单为 `119.123.207.29`；修改服务器上的 Nginx 配置并重新加载后才会生效。如果网站前面还有 CDN 或其他代理，需要先让 Nginx 正确识别客户端真实 IP。

## RabbitMQ 生成任务死信队列

新生成任务使用持久化 quorum 队列 `music.generate`。消息达到五次投递上限后，RabbitMQ 会通过 `music.dead` 死信交换机将其转入持久化 quorum 队列 `music.generate.dead`。死信队列不会自动重放；应先排查消息或 Worker 故障，再决定是否手动重新投递。死信转发采用 at-least-once 确认方式。

已捕获的音乐供应商错误、超时和生成失败仍由应用写入 MySQL 并显示为 `failed`，不进入死信队列；用户通过“重试”创建下一次任务尝试。死信主要用于消息在 Worker 处理前反复异常投递等情况。升级时 Worker 仍监听旧的 `celery` 队列，以完成升级前尚未处理的消息；新任务使用 `music.generate`。

生成任务和歌单按登录用户隔离。升级时，已有任务和歌单保留为空用户归属，普通账号不会自动获得这些旧数据。DeerFlow MCP 仍是服务间入口，目前只处理无主数据，不代表网页用户身份。

## 更新 Ubuntu 测试环境

每次发布都从本机打包部署文件，上传到 Ubuntu，然后在服务器现有的 Compose 项目目录解包和重建。下面的命令适用于已经启动过的部署。

### 1. 在本机 PowerShell 打包并上传

以下使用当前的 SSH 登录地址。如果服务器地址变了，更新 `$server`。

```powershell
# 进入本机项目目录，打包时使用这里的相对路径。
Set-Location 'B:\music-agent'

# 设置 SSH 用户和服务器地址。
$server = 'ubuntu@120.53.14.254'
# 在 Windows 临时目录里指定压缩包的保存路径。
$archive = Join-Path $env:TEMP 'music-agent-release.tar.gz'

# -czf 创建 gzip 压缩包；排除缓存和所有实际环境密钥文件。
# 当前仓库使用统一的 .env.example 模板，没有单独的本地/测试环境示例文件。
tar.exe -czf $archive --exclude=__pycache__ --exclude='*.pyc' --exclude=.env --exclude=.env.local --exclude=.env.test compose.yaml gateway-service auth-service backend frontend README.md .env.example
# 外部命令退出码为 0 表示成功，打包失败就停止。
if ($LASTEXITCODE -ne 0) { throw '打包失败' }

# 通过 SSH 把压缩包上传到 ubuntu 用户的主目录（~）。
scp $archive "${server}:~/music-agent-release.tar.gz"
# 检查 scp 的退出码，上传失败就显示错误。
if ($LASTEXITCODE -ne 0) { throw '上传失败' }
```

### 2. 在 Ubuntu 服务器部署

命令从现有 API 容器读取部署目录。压缩包不包含 `.env.test`，服务器继续使用自己的测试密钥；Compose 项目名显式设为 `music-agent`，以沿用原有数据卷和容器。

首次切换前，若服务器原来使用 `.env`，请在服务器部署目录把它复制为 `.env.test` 并确认凭据正确。复制保留原 `.env`，不会更改或删除现有配置；之后测试环境只读取 `.env.test`。

```bash
# sudo 用管理员权限访问 Docker。--format 只输出容器标签中的部署目录。
# && 表示前一步成功后才继续执行下一步。
deploy_dir=$(sudo docker inspect music-agent-api-1 \
  --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}') &&
# 确认部署目录有值，再进入该目录。
test -n "$deploy_dir" &&
cd "$deploy_dir" &&
# 确认 Compose 配置和服务器测试环境变量文件都存在。
test -f compose.yaml &&
test -f .env.test &&
# -xzf 解开 gzip 压缩包，把新版部署文件写入当前目录。
sudo tar -xzf "$HOME/music-agent-release.tar.gz" &&
# 构建 API 和 worker 镜像。BUILDKIT_PROGRESS=plain 让下载进度和错误逐行输出。
sudo env BUILDKIT_PROGRESS=plain docker compose --env-file .env.test -p music-agent build gateway auth api worker &&
# -d 让服务在后台运行，--no-build 使用刚构建的镜像。
sudo docker compose --env-file .env.test -p music-agent up -d --no-build &&
# 查看容器状态，确认 api 和 worker 都显示 Up。
sudo docker compose --env-file .env.test -p music-agent ps
```

如果需要重新安装 Python 依赖，看到 `Looking in indexes:` 后跟着 `https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple` 就表示构建正在使用清华 PyPI 镜像。如果安装步骤显示 `CACHED`，说明直接复用了已装好依赖的镜像层。

### 3. 验证搜索

等 API 启动后，在服务器上执行：

```bash
# --get 发送 GET 请求，--data-urlencode 把中文词编码为查询参数。
# -fsS 让 curl 遇到 HTTP 错误时失败，隐藏进度但保留错误提示。
curl -fsS --get 'http://127.0.0.1:8000/tracks/search' \
  --data-urlencode 'q=七里香'
```

## 用 Nacos 调整搜歌超时

在 Nacos 的 `public` 命名空间、`DEFAULT_GROUP` 分组发布 `music-agent.yaml`。

```yaml
search:
  timeout_seconds: 12
```

在 Music Agent 的 `.env` 中设置 `NACOS_URL=http://music-agent-nacos:8848` 及 `NACOS_USERNAME`、`NACOS_PASSWORD`。给账号配置该条目的读取权限。

如果 Nacos 是单独启动的 Docker 容器，先用 `sudo docker network ls` 确认 Music Agent 的 Compose 网络名。假设它是 `music-agent_default`，运行：

```bash
# 把独立运行的 Nacos 容器加入 Music Agent 的 Docker 网络。
sudo docker network connect music-agent_default music-agent-nacos
# 重建并在后台启动 api，--no-deps 跳过它的依赖服务。
sudo docker compose up -d --build --no-deps api
# 查看 api 最近 50 行日志。
sudo docker compose logs --tail 50 api
```

API 启动后每约 30 秒读取一次配置，支持 1 至 30 秒。配置有效时，后续 Apple 和 Navidrome 搜索会使用新值。更新失败时继续使用上一次有效值，首次读取前使用默认的 8 秒。更新日志为 `nacos_search_timeout_updated`，读取失败日志为 `nacos_config_refresh_failed`。

工作台的「找歌」默认查询 Apple 的公开音乐目录，点击结果可跳转到曲目页面。默认地区是中国大陆（`ITUNES_COUNTRY=CN`），大陆区返回空列表时会自动补查香港区。有自有曲库时，可设置 `MUSIC_SEARCH_SOURCE=navidrome` 并填写 Navidrome 连接参数。

## 接入 DeerFlow

启动 Music Agent 后，将 [MCP 配置示例](deerflow/extensions_config.example.json) 中的 `music`、`music-playlists`、`music-instrumental` 和 `music-songwriting` 条目合并到 DeerFlow 根目录的 `extensions_config.json`。把 [歌单 Skill](deerflow/skills/music-playlists/SKILL.md)、[纯音乐创作 Skill](deerflow/skills/music-instrumental/SKILL.md) 和 [歌词歌曲创作 Skill](deerflow/skills/music-songwriting/SKILL.md) 分别复制到 DeerFlow 的 `skills/custom/` 下对应目录。

- 两边都使用 Docker 时，将运行 DeerFlow Agent 的容器加入 Music Agent 的 Compose 默认网络。用 `docker network ls` 确认网络名，再运行 `docker network connect <网络名> <DeerFlow Agent 容器名>`。配置使用 `http://api:8000/mcp/`。
- DeerFlow 直接运行在同一台宿主机时，将 MCP 地址改为 `http://127.0.0.1:8000/mcp/`。

启动或重启 DeerFlow 后，在对话中搜索一首已知歌曲，确认后保存成歌单，再让 Agent 列出歌单。MCP 入口应在宿主机回环地址或受信任的 Docker 网络中使用。

音乐生成后端使用 FastAPI 接收请求，MySQL 保存任务状态，RabbitMQ 把任务交给 Celery。DeerFlow 可调用 `music-instrumental` Skill 协助确定主题、曲风和乐器，再由 Worker 使用 ACE-Step 生成纯音乐；也可选择本地 MIDI 与 FluidSynth 路径。配置 DeepSeek 密钥时，本地路径还可让模型规划逐小节编曲。

人声歌曲通过用户自部署的 ACE-Step API 生成。创作助手先与用户完成歌词并等待确认，再把确认歌词提交给 ACE-Step；工作台可将已确认授权的素材作为 ACE-Step 参考音频。纯音乐可将选定素材按音量和起始时间混入本地编曲成品。素材库支持 WAV、MP3、FLAC、AIFF、OGG，单文件上限 100 MB，文件保存在共享音频卷。作品支持账号内自定义标签；工作台还可建立音乐项目、筛选候选、记录备注并打包下载入选作品。

每个成功生成的版本都可以提交文字修改意见，创建保留基础设定的新版本；人声歌曲还可以在同一处修改歌词。工作台会保留同一作品的历史版本和各版试听。这个过程是重新生成整首歌曲，不是对已有波形做局部修补；失败任务的“重试原参数”会原样重跑，不会应用新意见。满意后可在生成记录中将该版本确认归档到项目，项目导出只包含标记入选的作品。

素材库的文字 RAG 使用现有 BGE-small-zh-v1.5 与 Qdrant，按描述、标签、BPM、情绪及场景检索；不会下载或加载 CLAP 模型，也不提供按音频波形相似度检索。CLAP 可作为以后可选组件加入。素材可记录来源、描述、歌词与授权状态；训练集只接收已确认授权、有描述的完整原始歌曲，不接受 stem/sample。

音乐工作台的训练区会把所选原始歌曲和标注复制到 `models/ACE-Step-1.5/music-agent-data/{user_id}/{dataset_id}/`，载入 ACE-Step 训练数据集，调用其预处理 API，再提交 LoKr 训练。训练完成并产生权重文件后，可从工作台加载该 LoKr 适配器供之后 ACE-Step 生成人声歌曲使用。训练集副本、预处理张量和适配器会占用额外磁盘空间；删除素材库原件不会自动清理训练副本。Windows 本机默认路径对应 `B:/music-agent/models/ACE-Step-1.5/music-agent-data`；项目位置不同，需在 `.env.local` 设置 `ACESTEP_TRAINING_HOST_DIR` 为 ACE-Step 进程可读的同一宿主机目录。该目录由 Compose 映射到 API/Worker 的 `/training-data`。训练依赖 ACE-Step 当前服务提供数据扫描、预处理、LoKr 及适配器加载 API；本机 4 GB 显存配置下，预处理或训练可能因显存不足失败。训练权重保存在每个数据集目录的 `lokr/` 子目录。CLAP 音频/文字联合嵌入暂不安装，相关“按声音相似度检索”作为后续可选计划记录；当前素材检索只用已有 BGE 文本嵌入。

## 

本地版本通过 Docker Compose 启动，包含 Gateway、身份服务、限流 Redis、Python API、后台 Worker、MySQL、RabbitMQ、Qdrant 和本地 Embedding 服务。Windows 用户需先安装并启动 Docker Desktop，并确认 Docker Desktop 已进入运行状态。简历 RAG 使用本地 BGE-small-zh-v1.5；模型目录被 Git 忽略，新环境首次启动前需要下载模型。

在本机 Windows PowerShell 中按顺序执行以下命令。首次启动会下载基础镜像并构建 Java/Python 镜像，可能需要几分钟。

本地 Gateway 默认绑定 `127.0.0.1:8000`。如果该端口已被另一套 Music Agent 或其他程序占用，可在 `.env.local` 中设置 `GATEWAY_HOST_PORT=8081`，然后通过 `http://127.0.0.1:8081/` 访问。`.env.test` 未设置该变量时仍使用默认端口 8000。

```powershell
# 进入项目目录；后续命令都在此目录执行。
Set-Location 'B:\music-agent'

# 安装 Hugging Face 下载命令；如果系统已安装 huggingface_hub，可跳过。
python -m pip install -U huggingface_hub

# 下载约 100 MB 的 BGE-small-zh-v1.5 模型文件到 Compose 挂载目录。
hf download BAAI/bge-small-zh-v1.5 --local-dir models/BAAI-bge-small-zh-v1.5 --exclude pytorch_model.bin

# 从本机示例创建独立配置；已存在时保留已有配置。
if (-not (Test-Path .env.local)) { Copy-Item .env.local.example .env.local }

# 用记事本打开配置文件，至少确认 MYSQL_PASSWORD 非空；按需填写模型服务密钥。
notepad .env.local

# 检查 Docker CLI 是否能连接正在运行的 Docker Desktop；成功时会显示版本信息。
docker version
if ($LASTEXITCODE -ne 0) { throw 'Docker Desktop 尚未就绪，请启动 Docker Desktop 后重试。' }

# 校验 Compose 配置和必填变量；成功时会输出解析后的服务配置。
docker compose --env-file .env.local -p music-agent-local config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose 配置有误，请检查 .env 中的 MYSQL_PASSWORD。' }

# 构建当前代码的镜像并在后台启动 Gateway、身份服务、API、Worker、Qdrant、Embedding 服务及其依赖。
docker compose --env-file .env.local -p music-agent-local up -d --build
if ($LASTEXITCODE -ne 0) { throw '启动失败，请查看下面的故障排查命令。' }

# 查看服务状态；gateway、auth、api、worker、qdrant、embedding、mysql、rabbitmq 和 gateway-redis 应运行，数据库/Redis 健康检查需等待片刻。
docker compose --env-file .env.local -p music-agent-local ps
```

启动成功后，在浏览器打开 `http://127.0.0.1:8081/` 进入平台登录页，注册或登录后进入音乐工作台。API 文档位于 `http://127.0.0.1:8081/docs`。本项目的 `.env.local` 已将 Gateway 映射到 8081，以避开旧 `music-agent-api-1` 容器占用的 8000 端口。

如果页面打不开或容器没有运行，在同一个 PowerShell 项目目录查看启动日志：

```powershell
# 查看最近 100 行服务日志，重点检查 gateway、auth、api、worker、mysql、rabbitmq 和 gateway-redis 的报错。
docker compose --env-file .env.local -p music-agent-local logs --tail 100
```

修改 Python 代码后重新构建并启动：

```powershell
# 重新构建应用镜像并在后台应用变更。
docker compose --env-file .env.local -p music-agent-local up -d --build
```

停止本地服务但保留数据库、队列和音频数据：

```powershell
# 停止并移除容器和 Compose 网络；不加 -v 会保留命名卷中的数据。
docker compose --env-file .env.local -p music-agent-local down
```

纯音乐表单默认使用 ACE-Step 模型；也可切换到本地程序合成。ACE-Step 模式将主题、曲风和选定乐器交给模型，并以 `[inst]` 标记无歌词纯音乐；本地模式用 MIDI 与 FluidSynth 渲染 WAV。选择本地模式并配置 `DEEPSEEK_API_KEY` 后，还可让 DeepSeek 规划逐小节编曲，此模式会产生 DeepSeek 调用费用；密钥留空时使用本地规则。两种模式都要求指定时长。
打开 `http://127.0.0.1:8081/docs`，可用以下请求测试 `POST /generations`。

```json
{
  "theme": "雨夜回家",
  "style": "华语民谣，木吉他",
  "lyrics": "路灯在雨里轻轻摇，回家的路有你的微笑",
  "duration_seconds": 30
}
```

生成纯音乐时设置 `instrumental: true`，并省略歌词；`instrumental_generator` 可设为 `ace-step` 或 `local`（API 默认 `local`，工作台默认 `ace-step`）。人声歌曲须先通过创作对话完成并确认歌词，再将歌词与主题、曲风一起提交。`duration_seconds` 可设为 10 到 120 的整数；人声歌曲也可设为 `null`，此时 Music Agent 不向 ACE-Step 发送固定时长，并启用 ACE-Step 的 LM 按歌词和结构推断时长。此模式要求 ACE-Step 已加载 5Hz LM；项目的 4 GB 显存启动配置默认关闭 LM，使用自动时长前需要调整 ACE-Step 的模型加载配置。纯音乐必须指定时长。ACE-Step 纯音乐和人声歌曲都会由 Worker 提交到模型 API；本地纯音乐则由 Worker 合成 WAV。

用 `GET /generations/{id}` 查询任务状态。成功时打开返回的 `audio_url` 试听。失败时通过 `POST /generations/{id}/retry` 再试一次；重试会再次占用本机模型算力，纯音乐若使用 DeepSeek 编曲还会再次调用 DeepSeek。音频保存在 Compose 的共享卷中。

工作台可通过 `GET /generations?limit=20` 获取任务和作品。翻页时将返回的 `next_cursor` 作为 `after` 参数。

当前配置用于本地联调，API 绑定在本机回环地址。Worker 中断后，RabbitMQ 若重新投递任务，服务会检查这一轮是否已写完音频。有成品则恢复成功状态，否则标记失败，等用户决定是否重试。

## 在本机同时运行 ACE-Step 与 Music Agent

ACE-Step 1.5 以本机 HTTP API 运行，由 Music Agent Worker 通过 Docker 主机网关访问。RTX 3050 Ti 4 GB 显存使用 2B Turbo、关闭语言模型并启用 CPU 卸载。首次启动会自动下载模型文件，官方模型文件约需 10 GB 磁盘空间；下载和生成速度取决于网络和电脑配置。

Music Agent 人声歌曲和 ACE-Step 纯音乐都使用 ACE-Step。创作助手先写词，用户确认后再提交人声歌曲任务；纯音乐请求则使用 `[inst]` 标记并包含所选乐器。Worker 负责轮询 ACE-Step 并将完成的音频保存到 Music Agent 音频卷。ACE-Step API 需要监听 `0.0.0.0:8001`，Music Agent 容器通过 `http://host.docker.internal:8001` 访问。

### 日常一键启动

首次安装好 BGE Embedding 模型、ACE-Step 和 `.env.local` 后，日常在本机 Windows PowerShell 中执行下面两行即可启动全部服务和模型。Music Agent 容器在后台启动；ACE-Step API 会在新开的 PowerShell 窗口中运行，首次运行可能还会下载 ACE-Step 模型文件。不要关闭 ACE-Step 窗口，否则人声歌曲生成会无法连接模型。

```powershell
# 切换到 Music Agent 项目目录，保证脚本能找到配置和模型文件。
Set-Location 'B:\music-agent'

# 启动 Compose 服务并在新 PowerShell 窗口启动 ACE-Step API。
powershell -ExecutionPolicy Bypass -File .\start-local.ps1
```

脚本会检查 `.env.local`、本地 BGE 模型、ACE-Step 安装和 Docker Desktop。启动后访问 `http://127.0.0.1:8081/`。停止 Music Agent 容器可按本文前面的 `docker compose ... down` 命令操作；停止 ACE-Step 则在它的窗口按 `Ctrl+C`。

如果 ACE-Step API 启用了 API Key，请在 Music Agent 的 `.env.local` 中设置同一个 `ACESTEP_API_KEY`，并在启动 ACE-Step 的 PowerShell 窗口中设置相同环境变量；未启用鉴权的本机 API 可留空。

### 首次安装 ACE-Step

以下命令在本机 Windows PowerShell 中执行。项目代码和虚拟环境保存在 Git 忽略的 `models/ACE-Step-1.5` 目录，不会混入 Music Agent 的 Python 环境。

```powershell
# 进入 Music Agent 项目目录。
Set-Location 'B:\music-agent'

# 首次安装时克隆 ACE-Step 官方仓库；目录已存在时跳过克隆。
if (-not (Test-Path 'models/ACE-Step-1.5/pyproject.toml')) { git clone https://github.com/ACE-Step/ACE-Step-1.5.git models/ACE-Step-1.5 }

# 进入 ACE-Step 独立目录。
Set-Location 'B:\music-agent\models\ACE-Step-1.5'

# 安装项目内独立保存的 uv；已安装时跳过。
if (-not (Test-Path '.bootstrap/bin/uv.exe')) { python -m pip install --target .bootstrap uv }

# 将当前 PowerShell 窗口的网络超时调到 5 分钟，最多重试 10 次，并允许等待安装锁 10 分钟。
$env:UV_HTTP_TIMEOUT = '300'
$env:UV_HTTP_RETRIES = '10'
$env:UV_LOCK_TIMEOUT = '600'

# 安装 ACE-Step 依赖和 CUDA 版 PyTorch；省略语言模型专用的加速包以适配 4 GB 显存配置。
.\.bootstrap\bin\uv.exe sync --frozen --python 3.12 --no-install-package nano-vllm --no-install-package flash-attn
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败；请检查网络后再重试。' }
```

如果依赖下载因网络超时中断，先确认旧的安装命令已结束，再在同一个 ACE-Step 目录重跑 `uv.exe sync` 那一行。不要同时运行两个安装命令，以免争用 `.venv` 环境锁。

### 两个窗口启动

窗口一在本机 Windows PowerShell 中运行 ACE-Step API。保持窗口打开；API 就绪后健康检查地址为 `http://127.0.0.1:8001/health`。首次启动还会下载模型文件。

```powershell
# 进入 ACE-Step 独立目录。
Set-Location 'B:\music-agent\models\ACE-Step-1.5'

# 选择 2B Turbo 模型，并在低显存电脑上启用 CPU 卸载、关闭额外语言模型。
$env:ACESTEP_CONFIG_PATH = 'acestep-v15-turbo'
$env:ACESTEP_OFFLOAD_TO_CPU = 'true'
$env:ACESTEP_INIT_LLM = 'false'

# 启动供 Docker 容器访问的 ACE-Step API，端口 8001 是 ACE-Step API 默认端口。
.\.venv\Scripts\acestep-api.exe --host 0.0.0.0 --port 8001
```

窗口二在另一个本机 Windows PowerShell 中启动 Music Agent。若已按前文启动过本地 Compose 服务，可以跳过 `up`，直接访问平台或查看服务状态。

```powershell
# 进入 Music Agent 项目目录。
Set-Location 'B:\music-agent'

# 构建并在后台启动 Gateway、身份服务、API、Worker 和依赖服务。
docker compose --env-file .env.local -p music-agent-local up -d --build
if ($LASTEXITCODE -ne 0) { throw 'Music Agent 启动失败；请查看本地启动日志。' }

# 查看各容器状态；主要服务应显示为运行中。
docker compose --env-file .env.local -p music-agent-local ps
```

Music Agent 工作台地址是 `http://127.0.0.1:8081/`。ACE-Step API 地址是 `http://127.0.0.1:8001/`；Compose 会把 `host.docker.internal` 指向宿主机。关闭 ACE-Step 时在窗口一按 `Ctrl+C`；Music Agent 的停止命令见上文“停止本地服务”。

## Android 与 iOS 原生客户端

原生客户端位于 [mobile](mobile/README.md)，使用 Expo React Native 同时构建 Android 与 iOS。它连接已部署的 HTTPS Music Agent 服务，包含创作助手、模型与辅助能力选择、纯音乐生成、作品记录和原生音频试听。
