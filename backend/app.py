# 为 Subsonic 认证计算 MD5 摘要。
import hashlib
import io
import ntpath
import zipfile
# 将结构化日志编码为 JSON。
import json
# 获取应用的日志记录器。
import logging
# 读取部署环境变量。
import os
import re
# 生成认证用的随机盐。
import secrets
import shutil
# 使用单调时钟计算耗时。
import time
# 将应用生命周期包装为异步上下文管理器。
from contextlib import asynccontextmanager
from pathlib import Path
# 生成唯一 ID。
from uuid import UUID, uuid4

# 异步调用 Navidrome。
import httpx
# 定义 HTTP 应用、错误和请求对象。
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
# 初始化 MCP 服务。
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
# 定义和校验歌单输入。
from pydantic import BaseModel, ConfigDict, Field
# 定义列类型并构造数据库连接和查询。
from sqlalchemy import JSON, Integer, String, Text, create_engine, select, text
# 按字段构建数据库地址。
from sqlalchemy.engine import URL
# 声明 ORM 模型并管理会话。
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# 接入生成任务模型和路由注册函数。
from generation import GenerationBase, configure_generation
from resume import router as resume_router, ResumeBase, configure_resume
from runtime_config import runtime_config


# 为歌单定义独立的 ORM 基类。
class Base(DeclarativeBase):
    # 沿用声明式基类的实现。
    pass


# 将歌单映射到数据库。
class Playlist(Base):
    # 指定歌单表名。
    __tablename__ = "playlists"
    # 使用 32 字符的字符串作为主键。
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 保存最长 120 字符的歌单名。
    name: Mapped[str] = mapped_column(String(120))
    # 以 JSON 保存有序的歌曲 ID。
    track_ids: Mapped[list[str]] = mapped_column(JSON)


class AudioAsset(Base):
    __tablename__ = "audio_assets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(16))
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column()
    caption: Mapped[str] = mapped_column(Text, default="")
    bpm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mood: Mapped[str] = mapped_column(String(120), default="")
    use_case: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(500), default="")
    license_status: Mapped[str] = mapped_column(String(24), default="unknown")
    lyrics: Mapped[str] = mapped_column(Text, default="")


class AudioTrainingDataset(Base):
    __tablename__ = "audio_training_datasets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    asset_ids: Mapped[list] = mapped_column(JSON)
    dataset_host_dir: Mapped[str] = mapped_column(String(1000))
    tensor_host_dir: Mapped[str] = mapped_column(String(1000))
    preprocess_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer)


class AudioTrainingRun(Base):
    __tablename__ = "audio_training_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    dataset_id: Mapped[str] = mapped_column(String(32), index=True)
    output_host_dir: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[int] = mapped_column(Integer)


class AudioAssetSearchInput(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=10, ge=1, le=30)


class AudioAssetMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tags: list[str] | None = Field(default=None, max_length=12)
    caption: str | None = Field(default=None, max_length=1000)
    bpm: int | None = Field(default=None, ge=30, le=300)
    mood: str | None = Field(default=None, max_length=120)
    use_case: str | None = Field(default=None, max_length=120)
    source: str | None = Field(default=None, max_length=500)
    license_status: str | None = None
    lyrics: str | None = Field(default=None, max_length=5000)


class AudioTrainingDatasetInput(BaseModel):
    asset_ids: list[str] = Field(min_length=1, max_length=100)


class AudioTrainingStartInput(BaseModel):
    dataset_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    train_epochs: int = Field(default=100, ge=1, le=1000)


def _asset_text(row: AudioAsset) -> str:
    return " ".join(part for part in [row.filename, row.caption, row.asset_type, row.mood, row.use_case,
                                      f"BPM {row.bpm}" if row.bpm else "", " ".join(row.tags), row.lyrics] if part)


def _index_audio_asset(row: AudioAsset) -> None:
    """把素材说明写入独立的 Qdrant 集合；沿用现有 BGE 文本向量，不加载音频模型。"""
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    collection = "music_audio_assets"
    embed_url = os.getenv("EMBEDDING_URL", "http://embedding:80").rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=5)) as client:
            collection_response = client.get(f"{base}/collections/{collection}")
            if collection_response.status_code == 404:
                created = client.put(f"{base}/collections/{collection}", json={"vectors": {"size": 512, "distance": "Cosine"}})
                created.raise_for_status()
            else:
                collection_response.raise_for_status()
            embedded = client.post(f"{embed_url}/embed", json={"inputs": [_asset_text(row)]})
            embedded.raise_for_status()
            vector = embedded.json()[0]
            point = {"points": [{"id": str(UUID(hex=row.id)), "vector": vector,
                                 "payload": {"user_id": row.user_id, "asset_id": row.id, "text": _asset_text(row)}}]}
            saved = client.put(f"{base}/collections/{collection}/points?wait=true", json=point)
            saved.raise_for_status()
    except (httpx.HTTPError, ValueError, IndexError) as exc:
        raise HTTPException(503, "素材已保存，但文字检索索引暂不可用；请稍后重试。") from exc


def _delete_asset_index(asset_id: str) -> None:
    try:
        base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
        with httpx.Client(timeout=10) as client:
            client.post(f"{base}/collections/music_audio_assets/points/delete?wait=true",
                        json={"points": [str(UUID(hex=asset_id))]}).raise_for_status()
    except httpx.HTTPError:
        logging.getLogger("music.assets").warning("audio_asset_index_delete_failed asset_id=%s", asset_id)


class MusicProject(Base):
    __tablename__ = "music_projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # job_id -> {decision, note}; JSON keeps review metadata with the project.
    items: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column()


# 定义创建歌单的请求体。
class PlaylistInput(BaseModel):
    # 校验名称长度为 1 到 120 个字符。
    name: str = Field(min_length=1, max_length=120)
    # 限制一次最多提交 500 个歌曲 ID。
    track_ids: list[str] = Field(max_length=500)


class MusicProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectReviewInput(BaseModel):
    decision: str = Field(pattern=r"^(pending|selected|rejected)$")
    note: str = Field(default="", max_length=500)


# 优先使用完整连接地址，否则连接名为 mysql 的容器服务。
db_url = os.getenv("DATABASE_URL")
# 在完整连接地址缺失时使用默认连接配置。
if not db_url:
    # 构建连接到 MySQL 容器的地址。
    db_url = URL.create(
        "mysql+pymysql",
        username="music",
        # 从环境变量读取 MySQL 密码。
        password=os.environ["MYSQL_PASSWORD"],
        host="mysql",
        database="music",
        # 使用 utf8mb4 支持完整的 Unicode 字符。
        query={"charset": "utf8mb4"},
    )
# 从连接池取出连接时先检查它是否仍然可用。
engine = create_engine(db_url, pool_pre_ping=True)
configure_resume(engine)
# 创建 MCP 服务，并把流式 HTTP 入口设在挂载根路径。
# 保留 Host 校验，同时允许同一 Docker 网络中的 DeerFlow 访问 API 服务名。
mcp = FastMCP(
    "Music Agent",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", "api:8000"],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)


# 让 yield 前后分别对应应用启动和关闭。
@asynccontextmanager
# 管理数据库表和 MCP 会话的生命周期。
async def lifespan(app: FastAPI):
    from sqlalchemy import inspect
    # 接收请求前创建缺失的表，并启动 MCP 会话管理器。
    # 创建缺失的歌单表。
    Base.metadata.create_all(engine)
    asset_columns = {column["name"] for column in inspect(engine).get_columns("audio_assets")}
    for name, definition in (
        ("caption", "TEXT NULL"), ("bpm", "INT NULL"), ("mood", "VARCHAR(120) NOT NULL DEFAULT ''"),
        ("use_case", "VARCHAR(120) NOT NULL DEFAULT ''"), ("source", "VARCHAR(500) NOT NULL DEFAULT ''"),
        ("license_status", "VARCHAR(24) NOT NULL DEFAULT 'unknown'"), ("lyrics", "TEXT NULL"),
    ):
        if name not in asset_columns:
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE audio_assets ADD COLUMN {name} {definition}"))
    with engine.begin() as connection:
        connection.execute(text("UPDATE audio_assets SET caption='' WHERE caption IS NULL"))
        connection.execute(text("UPDATE audio_assets SET lyrics='' WHERE lyrics IS NULL"))
        connection.execute(text("ALTER TABLE audio_assets MODIFY caption TEXT NOT NULL"))
        connection.execute(text("ALTER TABLE audio_assets MODIFY lyrics TEXT NOT NULL"))
    with Session(engine) as session:
        existing_assets = list(session.scalars(select(AudioAsset)))
        for asset in existing_assets:
            try:
                _index_audio_asset(asset)
            except HTTPException as exc:
                logging.getLogger("music.assets").warning("audio_asset_index_refresh_failed detail=%s", exc.detail)
                break
    # 创建缺失的生成任务表。
    GenerationBase.metadata.create_all(engine)
    MusicProject.metadata.create_all(engine)
    ResumeBase.metadata.create_all(engine)
    if "user_id" not in {column["name"] for column in inspect(engine).get_columns("music_generations")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE music_generations ADD COLUMN user_id VARCHAR(32) NULL"))
            connection.execute(text("CREATE INDEX ix_music_generations_user_id ON music_generations (user_id)"))
    generation_columns = {column["name"] for column in inspect(engine).get_columns("music_generations")}
    if "progress" not in generation_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE music_generations ADD COLUMN progress FLOAT NOT NULL DEFAULT 0"))
    if "progress_text" not in generation_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE music_generations ADD COLUMN progress_text VARCHAR(200) NOT NULL DEFAULT ''"))
    if "user_id" not in {column["name"] for column in inspect(engine).get_columns("playlists")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE playlists ADD COLUMN user_id VARCHAR(32) NULL"))
            connection.execute(text("CREATE INDEX ix_playlists_user_id ON playlists (user_id)"))
    if "user_id" not in {column["name"] for column in inspect(engine).get_columns("resume_chunks")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE resume_chunks ADD COLUMN user_id VARCHAR(32) NULL"))
            connection.execute(text("CREATE INDEX ix_resume_chunks_user_id ON resume_chunks (user_id)"))
    if "user_id" not in {column["name"] for column in inspect(engine).get_columns("creative_conversations")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE creative_conversations ADD COLUMN user_id VARCHAR(32) NULL"))
            connection.execute(text("CREATE INDEX ix_creative_conversations_user_id ON creative_conversations (user_id)"))
    if "storage_key" not in {column["name"] for column in inspect(engine).get_columns("resume_chunks")}:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE resume_chunks ADD COLUMN storage_key VARCHAR(512) NULL"))
    # 在应用运行期间维持 MCP 会话管理器。
    async with mcp.session_manager.run(), runtime_config.running():
        # 启动准备完成后让应用开始处理请求。
        yield
    # 服务关闭时释放数据库连接池。
    engine.dispose()


# 创建 API 应用并绑定生命周期函数。
app = FastAPI(title="Music Agent Workspaces", lifespan=lifespan)
web_dir = Path(__file__).resolve().parent / "web"
react_dir = Path(__file__).resolve().parent / "react-dist"
app.mount("/assets", StaticFiles(directory=web_dir), name="assets")
app.mount("/react-assets", StaticFiles(directory=react_dir), name="react-assets")


@app.get("/", include_in_schema=False)
def workspaces():
    return FileResponse(react_dir / "index.html")


@app.get("/music", include_in_schema=False)
def music_workspace():
    return FileResponse(react_dir / "index.html")


@app.get("/legacy-music", include_in_schema=False)
def legacy_music_workspace():
    return FileResponse(web_dir / "music.html")


@app.get("/resume-ui", include_in_schema=False)
def react_resume_workspace():
    return FileResponse(react_dir / "index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(web_dir / "login.html")


@app.api_route("/auth/{action}", methods=["GET", "POST"])
async def auth_proxy(action: str, request: Request):
    if action not in {"register", "login", "logout", "me"}:
        raise HTTPException(404, "Not found")
    upstream = os.getenv("AUTH_SERVICE_URL", "http://auth:8080")
    body = await request.body() if request.method == "POST" else None
    async with httpx.AsyncClient(timeout=15) as client:
        result = await client.request(request.method, f"{upstream}/auth/{action}", content=body, headers={"Content-Type": "application/json"} if body else None)
    headers = {}
    if "set-cookie" in result.headers:
        headers["set-cookie"] = result.headers["set-cookie"]
    return Response(content=result.content, status_code=result.status_code, media_type=result.headers.get("content-type", "application/json"), headers=headers)


# 注册生成端点并暴露 worker 使用的 Celery 应用。
celery = configure_generation(app, engine)
# 复用 Uvicorn 的日志记录器。
logger = logging.getLogger("uvicorn.error")


# 将日志函数注册为 HTTP 中间件。
@app.middleware("http")
# 为每个请求记录状态、路由和耗时。
async def log_request(request: Request, call_next):
    # 优先沿用网关生成的请求 ID；仅接受 32 位十六进制格式，避免日志注入。
    incoming_request_id = request.headers.get("X-Request-ID", "")
    request_id = incoming_request_id if re.fullmatch(r"[0-9a-f]{32}", incoming_request_id) else uuid4().hex
    request.state.request_id = request_id
    user_id = None
    # 记录请求开始时的单调时钟读数。
    start = time.monotonic()
    # 在下游提前抛错时按 500 记日志。
    status = 500
    # 处理请求，并在退出时统一记录日志。
    try:
        path = request.url.path
        public_path = path in {"/health", "/login", "/favicon.ico"} or path.startswith("/assets/") or path.startswith("/auth/") or path.startswith("/mcp")
        if not public_path:
            token = request.cookies.get("MUSIC_SESSION")
            if token:
                from sqlalchemy import text
                with Session(engine) as session:
                    user_id = session.execute(text(
                        "SELECT user_id FROM music_sessions WHERE token_hash=:token_hash AND expires_at>CURRENT_TIMESTAMP"
                    ), {"token_hash": hashlib.sha256(token.encode()).hexdigest()}).scalar_one_or_none()
            if not user_id:
                if path == "/" or path.endswith(".html"):
                    response = FileResponse(web_dir / "login.html")
                else:
                    response = Response(status_code=401, content='{"detail":"Authentication required."}', media_type="application/json")
                status = response.status_code
                response.headers["X-Request-ID"] = request_id
                return response
            request.state.user_id = user_id
        # 将请求交给后续处理链。
        response = await call_next(request)
        # 读取实际响应状态码。
        status = response.status_code
        # 通过响应头把请求 ID 提供给客户端。
        response.headers["X-Request-ID"] = request_id
        # 返回下游生成的响应。
        return response
    # 无论请求是否抛出异常都执行日志记录。
    finally:
        # 输出供 CLS 采集的结构化日志，请求 ID 可关联客户端响应。
        route = request.scope.get("route")
        # 将请求元数据编码为 JSON 并写入日志。
        logger.info(json.dumps({
            "event": "http_request",
            "request_id": request_id,
            "user_id": user_id,
            "method": request.method,
            "route": getattr(route, "path", None),
            "status": status,
            # 将请求耗时换算为毫秒并取整。
            "duration_ms": round((time.monotonic() - start) * 1000),
        }))


# 注册健康检查路由。
@app.get("/health")
# 响应服务探测。
def health():
    # 返回服务正常响应的标志。
    return {"status": "ok"}


# HTTP 接口和 DeerFlow 的 MCP 工具共用同一个搜索函数。
# 将搜索函数注册为 MCP 工具。
@mcp.tool()
# 注册 HTTP 歌曲搜索路由。
@app.get("/tracks/search")
async def search_tracks(q: str) -> list[dict]:
    """Search for songs by title, artist or album. Use returned IDs and playback links."""
    # 去掉搜索词首尾空白。
    q = q.strip()
    # 检查清理后的长度。
    if not q or len(q) > 200:
        # 以 422 响应提示允许的搜索词长度。
        raise HTTPException(422, "Enter a search term between 1 and 200 characters.")
    source = os.getenv("MUSIC_SEARCH_SOURCE", "itunes")
    if source == "itunes":
        return await search_itunes(q)
    if source != "navidrome":
        raise HTTPException(503, "Unknown music search source.")
    return await search_navidrome(q)


async def search_itunes(q: str) -> list[dict]:
    # 仅查询公开曲目元数据，播放入口使用平台返回的曲目链接。
    country = os.getenv("ITUNES_COUNTRY", "CN").strip().upper()
    countries = ("CN", "HK") if country == "CN" else (country,)
    try:
        async with httpx.AsyncClient(timeout=runtime_config.search_timeout) as client:
            for search_country in countries:
                response = await client.get("https://itunes.apple.com/search", params={
                    "term": q, "country": search_country,
                    "media": "music", "entity": "song", "limit": 25,
                })
                response.raise_for_status()
                tracks = response.json()["results"]
                if not isinstance(tracks, list):
                    raise HTTPException(502, "Music catalog returned an invalid result.")
                if tracks:
                    break
    except httpx.TimeoutException:
        raise HTTPException(504, "Music search timed out.") from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise HTTPException(429, "Music search is rate limited. Try again shortly.") from None
        raise HTTPException(502, "Music catalog is unavailable.") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(502, "Music catalog is unavailable.") from None
    results = {}
    for track in tracks:
        if not isinstance(track, dict) or type(track.get("trackId")) is not int:
            continue
        title = track.get("trackName")
        link = track.get("trackViewUrl")
        if not isinstance(title, str) or not isinstance(link, str):
            continue
        if not link.startswith(("https://music.apple.com/", "https://itunes.apple.com/")):
            continue
        # 为公开曲目加来源前缀，避免与已有 Navidrome ID 混淆。
        track_id = f"itunes:{track['trackId']}"
        milliseconds = track.get("trackTimeMillis")
        results[track_id] = {
            "id": track_id, "source": "itunes", "title": title,
            "artist": track.get("artistName"), "album": track.get("collectionName"),
            "duration": milliseconds // 1000 if type(milliseconds) is int else None,
            "playback_url": link,
        }
    return list(results.values())


async def search_navidrome(q: str) -> list[dict]:
    # 读取 Navidrome 用户名。
    username = os.getenv("NAVIDROME_USERNAME")
    # 读取 Navidrome 密码。
    password = os.getenv("NAVIDROME_PASSWORD")
    # 检查用户名和密码是否齐全。
    if not username or not password:
        # 以 503 响应提示先配置账号。
        raise HTTPException(503, "Configure the Navidrome account first.")
    # Subsonic 协议用随机盐和密码生成令牌，向 Navidrome 发送令牌与盐。
    # 生成本次认证使用的随机盐。
    salt = secrets.token_hex(16)
    # 用密码和盐计算 Subsonic 认证令牌。
    token = hashlib.md5((password + salt).encode()).hexdigest()
    # 读取服务地址并移除尾部斜杠。
    url = os.getenv("NAVIDROME_URL", "http://navidrome:4533").rstrip("/")
    # 将上游调用和响应解析纳入统一的错误处理。
    try:
        # 创建异步客户端，设置八秒超时并在用完后关闭。
        async with httpx.AsyncClient(timeout=runtime_config.search_timeout) as client:
            # search3 可返回多种结果，这里只请求歌曲，最多 25 首。
            response = await client.get(f"{url}/rest/search3", params={
                "query": q, "songCount": 25, "artistCount": 0, "albumCount": 0,
                # 传入用户名、令牌、随机盐和协议版本。
                "u": username, "t": token, "s": salt, "v": "1.16.1",
                # 标识客户端名称并要求 JSON 响应。
                "c": "music-agent", "f": "json",
            })
            # 将非成功 HTTP 状态转为异常。
            response.raise_for_status()
            # 从 JSON 响应中提取 Subsonic 响应体。
            result = response.json()["subsonic-response"]
    # 处理 HTTP 调用失败、解析失败或字段缺失。
    except (httpx.HTTPError, ValueError, KeyError):
        # 将上游故障转换为 502 响应。
        raise HTTPException(502, "Navidrome is unavailable.") from None
    # HTTP 请求成功后，还要检查 Subsonic 响应中的业务状态。
    if result.get("status") != "ok":
        # 将 Subsonic 业务失败转换为 502 响应。
        raise HTTPException(502, "Navidrome rejected the search.")
    # 仅返回歌曲的公开元数据。
    return [
        {key: song.get(key) for key in ("id", "title", "artist", "album", "duration")}
        # 缺少歌曲结果时使用空列表。
        for song in result.get("searchResult3", {}).get("song", [])
    ]


# 定义统一的歌单序列化格式。
def playlist_json(playlist: Playlist) -> dict:
    # 返回歌单 ID、名称和歌曲 ID 列表。
    return {"id": playlist.id, "name": playlist.name, "track_ids": playlist.track_ids}


# HTTP 和 MCP 使用相同的歌单查询与返回格式。
# 将歌单列表注册为 MCP 工具。
@app.get("/playlists")
# 读取并返回已保存的歌单。
def list_playlists(request: Request) -> list[dict]:
    return list_playlists_for_user(request.state.user_id)


def list_playlists_for_user(user_id: str | None) -> list[dict]:
    # 打开会话并在查询结束后关闭。
    with Session(engine) as session:
        # 执行查询并逐条转换为响应字典。
        return [playlist_json(row) for row in session.scalars(
            # 先按名称排序，同名时按 ID 排序。
            select(Playlist).where(Playlist.user_id == user_id).order_by(Playlist.name, Playlist.id)
        )]


# 注册创建歌单的路由，成功时返回 201。
def save_playlist(data: PlaylistInput, user_id: str | None) -> dict:
    # 去掉名称首尾空白。
    name = data.name.strip()
    # 检查清理后的名称是否为空。
    if not name:
        # 以 422 响应要求填写歌单名。
        raise HTTPException(422, "Enter a playlist name.")
    # 去掉重复歌曲，保留每首歌第一次出现的位置。
    playlist = Playlist(id=uuid4().hex, user_id=user_id, name=name, track_ids=list(dict.fromkeys(data.track_ids)))
    # 为保存操作打开数据库会话。
    with Session(engine) as session:
        # 将新歌单加入当前事务。
        session.add(playlist)
        # 在提交前构建响应字典。
        result = playlist_json(playlist)
        # 提交成功后才向调用方返回已保存的歌单。
        session.commit()
        # 返回提交成功的歌单。
        return result


@app.post("/playlists", status_code=201)
def save_playlist_http(data: PlaylistInput, request: Request) -> dict:
    return save_playlist(data, request.state.user_id)


@app.post("/audio-training/datasets", status_code=201)
def create_audio_training_dataset(data: AudioTrainingDatasetInput, request: Request) -> dict:
    user_id = request.state.user_id
    asset_ids = list(dict.fromkeys(data.asset_ids))
    with Session(engine) as session:
        assets = list(session.scalars(select(AudioAsset).where(AudioAsset.user_id == user_id, AudioAsset.id.in_(asset_ids))))
        if len(assets) != len(asset_ids):
            raise HTTPException(404, "部分素材不存在或不属于当前账号。")
        for asset in assets:
            if asset.asset_type != "original":
                raise HTTPException(422, "训练集只接收完整原始歌曲；stem/sample 可用于检索和混音。")
            if asset.license_status not in {"owned", "licensed", "public_domain"}:
                raise HTTPException(422, "训练集只能使用已确认授权的素材。")
            if not asset.caption.strip():
                raise HTTPException(422, f"请先为“{asset.filename}”补充声音描述，再建立训练集。")
        dataset_id = uuid4().hex
        container_dir = Path(os.getenv("ACESTEP_TRAINING_DATA_DIR", "/training-data")) / user_id / dataset_id
        host_dir = _training_host_path(user_id, dataset_id)
        container_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = Path(os.getenv("MUSIC_AUDIO_DIR", "/audio")) / "assets"
        total_bytes = 0
        has_vocals = False
        for asset in assets:
            source_path = audio_dir / f"{asset.id}{Path(asset.filename).suffix.lower()}"
            if not source_path.is_file():
                raise HTTPException(404, f"素材文件丢失：{asset.filename}")
            total_bytes += source_path.stat().st_size
            if total_bytes > 1024 * 1024 * 1024:
                raise HTTPException(413, "单次训练集最多 1 GB。")
            stem = asset.id
            shutil.copyfile(source_path, container_dir / f"{stem}{source_path.suffix.lower()}")
            (container_dir / f"{stem}.caption.txt").write_text(asset.caption.strip(), encoding="utf-8")
            metadata = {"caption": asset.caption.strip(), "bpm": asset.bpm, "keyscale": "", "timesignature": "4",
                        "language": "instrumental" if not asset.lyrics.strip() else "zh"}
            (container_dir / f"{stem}.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if asset.lyrics.strip():
                has_vocals = True
                (container_dir / f"{stem}.lyrics.txt").write_text(asset.lyrics.strip(), encoding="utf-8")
        try:
            scan = _acestep_request("POST", "/v1/dataset/scan", {
                "audio_dir": host_dir, "dataset_name": f"music_agent_{dataset_id[:8]}",
                "all_instrumental": not has_vocals,
            })
        except Exception:
            shutil.rmtree(container_dir, ignore_errors=True)
            raise
        if int(scan.get("num_samples") or 0) != len(assets):
            shutil.rmtree(container_dir, ignore_errors=True)
            raise HTTPException(502, "ACE-Step 扫描到的训练样本数量与所选素材不一致。")
        dataset = AudioTrainingDataset(id=dataset_id, user_id=user_id, asset_ids=asset_ids,
                                       dataset_host_dir=host_dir,
                                       tensor_host_dir=_training_host_path(user_id, dataset_id, "tensors"),
                                       created_at=int(time.time()))
        session.add(dataset)
        session.commit()
    return {"id": dataset_id, "sample_count": len(assets), "status": "scanned",
            "message": scan.get("message", "素材已整理并载入 ACE-Step。")}


@app.post("/audio-training/datasets/{dataset_id}/preprocess", status_code=202)
def preprocess_audio_training_dataset(dataset_id: str, request: Request) -> dict:
    with Session(engine) as session:
        dataset = session.get(AudioTrainingDataset, dataset_id)
        if dataset is None or dataset.user_id != request.state.user_id:
            raise HTTPException(404, "训练集不存在。")
        _ensure_acestep_model_initialized()
        response = _acestep_request("POST", "/v1/dataset/preprocess_async", {
            "output_dir": dataset.tensor_host_dir, "skip_existing": True,
        })
        task_id = response.get("task_id")
        if not task_id:
            raise HTTPException(502, "ACE-Step 没有返回预处理任务 ID。")
        dataset.preprocess_task_id = str(task_id)
        session.commit()
        return {"dataset_id": dataset.id, "task_id": task_id, "status": response.get("status", "running"),
                "message": response.get("message", "训练数据预处理中。")}


@app.get("/audio-training/datasets/{dataset_id}/preprocess")
def get_audio_training_preprocess_status(dataset_id: str, request: Request) -> dict:
    with Session(engine) as session:
        dataset = session.get(AudioTrainingDataset, dataset_id)
        if dataset is None or dataset.user_id != request.state.user_id:
            raise HTTPException(404, "训练集不存在。")
        if not dataset.preprocess_task_id:
            return {"dataset_id": dataset.id, "status": "not_started"}
        result = _acestep_request("GET", f"/v1/dataset/preprocess_status/{dataset.preprocess_task_id}")
        return {"dataset_id": dataset.id, **result}


@app.post("/audio-training/start", status_code=202)
def start_audio_training(data: AudioTrainingStartInput, request: Request) -> dict:
    user_id = request.state.user_id
    with Session(engine) as session:
        dataset = session.get(AudioTrainingDataset, data.dataset_id)
        if dataset is None or dataset.user_id != user_id:
            raise HTTPException(404, "训练集不存在。")
        if not dataset.preprocess_task_id:
            raise HTTPException(409, "请先完成训练张量预处理。")
        prep = _acestep_request("GET", f"/v1/dataset/preprocess_status/{dataset.preprocess_task_id}")
        if prep.get("status") != "completed":
            raise HTTPException(409, "训练集预处理尚未完成。")
        current = _acestep_request("GET", "/v1/training/status")
        if current.get("is_training"):
            raise HTTPException(409, "ACE-Step 当前已有训练任务运行。")
        output_host_dir = _training_host_path(user_id, data.dataset_id, "lokr")
        result = _acestep_request("POST", "/v1/training/start_lokr", {
            "tensor_dir": dataset.tensor_host_dir, "output_dir": output_host_dir,
            "train_epochs": data.train_epochs, "train_batch_size": 1, "gradient_accumulation": 4,
            "gradient_checkpointing": True,
        })
        run = AudioTrainingRun(id=uuid4().hex, user_id=user_id, dataset_id=dataset.id,
                               output_host_dir=output_host_dir, created_at=int(time.time()))
        session.add(run)
        session.commit()
        return {"id": run.id, "dataset_id": dataset.id, "output_dir": output_host_dir,
                "status": result.get("message", "训练已提交。")}


@app.get("/audio-training/runs/{run_id}")
def get_audio_training_status(run_id: str, request: Request) -> dict:
    with Session(engine) as session:
        run = session.get(AudioTrainingRun, run_id)
        if run is None or run.user_id != request.state.user_id:
            raise HTTPException(404, "训练任务不存在。")
        dataset = session.get(AudioTrainingDataset, run.dataset_id)
        state = _acestep_request("GET", "/v1/training/status")
        if state.get("is_training") and dataset and ntpath.normcase(str(state.get("tensor_dir", ""))) != ntpath.normcase(dataset.tensor_host_dir):
            return {"status": "此任务已结束；ACE-Step 正在运行另一项任务", "is_training": False,
                    "current_epoch": 0, "current_loss": None, "loadable": False}
        adapter_path = Path(os.getenv("ACESTEP_TRAINING_DATA_DIR", "/training-data")) / run.user_id / run.dataset_id / "lokr" / "lokr_weights.safetensors"
        state["loadable"] = not state.get("is_training") and adapter_path.is_file() and dataset is not None and \
            ntpath.normcase(str(state.get("tensor_dir", ""))) == ntpath.normcase(dataset.tensor_host_dir)
        return state


@app.post("/audio-training/runs/{run_id}/stop")
def stop_audio_training(run_id: str, request: Request) -> dict:
    with Session(engine) as session:
        run = session.get(AudioTrainingRun, run_id)
        if run is None or run.user_id != request.state.user_id:
            raise HTTPException(404, "训练任务不存在。")
        dataset = session.get(AudioTrainingDataset, run.dataset_id)
        state = _acestep_request("GET", "/v1/training/status")
        if not state.get("is_training") or dataset is None or ntpath.normcase(str(state.get("tensor_dir", ""))) != ntpath.normcase(dataset.tensor_host_dir):
            raise HTTPException(409, "该训练任务当前没有运行，无法停止其他训练任务。")
        return _acestep_request("POST", "/v1/training/stop", {})


@app.post("/audio-training/runs/{run_id}/load")
def load_audio_training_adapter(run_id: str, request: Request) -> dict:
    with Session(engine) as session:
        run = session.get(AudioTrainingRun, run_id)
        if run is None or run.user_id != request.state.user_id:
            raise HTTPException(404, "训练任务不存在。")
        state = _acestep_request("GET", "/v1/training/status")
        if state.get("is_training"):
            raise HTTPException(409, "训练仍在运行，完成后才能加载适配器。")
        dataset = session.get(AudioTrainingDataset, run.dataset_id)
        if dataset is None or ntpath.normcase(str(state.get("tensor_dir", ""))) != ntpath.normcase(dataset.tensor_host_dir):
            raise HTTPException(409, "ACE-Step 当前状态不对应这条训练记录，不能加载其适配器。")
        adapter_path = Path(os.getenv("ACESTEP_TRAINING_DATA_DIR", "/training-data")) / run.user_id / run.dataset_id / "lokr" / "lokr_weights.safetensors"
        if not adapter_path.is_file():
            raise HTTPException(404, "LoKr 权重文件尚未生成。")
        return _acestep_request("POST", "/v1/lora/load", {
            "lora_path": _training_host_path(run.user_id, run.dataset_id, "lokr", "lokr_weights.safetensors"),
        })


@app.get("/audio-assets")
def list_audio_assets(request: Request) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(AudioAsset).where(AudioAsset.user_id == request.state.user_id).order_by(AudioAsset.created_at.desc()))
        return [_audio_asset_json(row) for row in rows]


@app.post("/audio-assets", status_code=201)
async def upload_audio_asset(request: Request, file: UploadFile = File(...), asset_type: str = Form(...), tags: str = Form(""),
                             caption: str = Form(""), bpm: int | None = Form(None), mood: str = Form(""),
                             use_case: str = Form(""), source: str = Form(""), lyrics: str = Form(""),
                             license_status: str = Form("unknown")) -> dict:
    if asset_type not in {"original", "stem", "sample"}:
        raise HTTPException(422, "素材类型必须是 original、stem 或 sample。")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".wav", ".mp3", ".flac", ".aiff", ".aif", ".ogg"}:
        raise HTTPException(415, "仅支持 WAV、MP3、FLAC、AIFF 或 OGG 音频。")
    asset_id = uuid4().hex
    directory = Path(os.getenv("MUSIC_AUDIO_DIR", "/audio")) / "assets"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{asset_id}{suffix}"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 100 * 1024 * 1024:
                    raise HTTPException(413, "单个音频素材不能超过 100 MB。")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    clean_tags = list(dict.fromkeys(item.strip()[:32] for item in tags.split(",") if item.strip()))[:12]
    if license_status not in {"unknown", "owned", "licensed", "public_domain"}:
        target.unlink(missing_ok=True)
        raise HTTPException(422, "授权状态无效。")
    if bpm is not None and not 30 <= bpm <= 300:
        target.unlink(missing_ok=True)
        raise HTTPException(422, "BPM 必须在 30 到 300 之间。")
    safe_filename = Path((file.filename or f"asset{suffix}").replace("\\", "/")).name[:255]
    row = AudioAsset(id=asset_id, user_id=request.state.user_id,
                     filename=safe_filename, asset_type=asset_type,
                     tags=clean_tags, created_at=int(time.time()), caption=caption[:1000], bpm=bpm,
                     mood=mood[:120], use_case=use_case[:120], source=source[:500], license_status=license_status,
                     lyrics=lyrics[:5000])
    with Session(engine) as session:
        session.add(row)
        session.commit()
        try:
            _index_audio_asset(row)
        except HTTPException:
            session.delete(row)
            session.commit()
            target.unlink(missing_ok=True)
            raise
        result = _audio_asset_json(row)
        return result


def _audio_asset_json(row: AudioAsset, score: float | None = None) -> dict:
    result = {"id": row.id, "filename": row.filename, "type": row.asset_type, "tags": row.tags,
              "caption": row.caption, "bpm": row.bpm, "mood": row.mood, "use_case": row.use_case,
              "source": row.source, "license_status": row.license_status,
              "lyrics": row.lyrics,
              "audio_url": f"/audio-assets/{row.id}/audio", "created_at": row.created_at}
    if score is not None:
        result["score"] = score
    return result


@app.post("/audio-assets/search")
def search_audio_assets(data: AudioAssetSearchInput, request: Request) -> list[dict]:
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    embed_url = os.getenv("EMBEDDING_URL", "http://embedding:80").rstrip("/")
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=5)) as client:
            collection_response = client.get(f"{base}/collections/music_audio_assets")
            if collection_response.status_code == 404:
                created = client.put(f"{base}/collections/music_audio_assets", json={"vectors": {"size": 512, "distance": "Cosine"}})
                created.raise_for_status()
            else:
                collection_response.raise_for_status()
            embedded = client.post(f"{embed_url}/embed", json={"inputs": [f"为这个句子生成表示以用于检索相关文章：{data.query}"]})
            embedded.raise_for_status()
            found = client.post(f"{base}/collections/music_audio_assets/points/search", json={
                "vector": embedded.json()[0], "limit": data.limit, "with_payload": True,
                "filter": {"must": [{"key": "user_id", "match": {"value": request.state.user_id}}]},
            })
            found.raise_for_status()
            matches = found.json().get("result", [])
    except httpx.HTTPError as exc:
        raise HTTPException(503, "素材文字检索暂不可用，请稍后重试。") from exc
    ids = [item.get("payload", {}).get("asset_id") for item in matches]
    scores = {item.get("payload", {}).get("asset_id"): item.get("score") for item in matches}
    with Session(engine) as session:
        rows = {row.id: row for row in session.scalars(select(AudioAsset).where(
            AudioAsset.user_id == request.state.user_id, AudioAsset.id.in_(ids)))}
    return [_audio_asset_json(rows[asset_id], scores.get(asset_id)) for asset_id in ids if asset_id in rows]


@app.patch("/audio-assets/{asset_id}")
def update_audio_asset(asset_id: str, data: AudioAssetMetadataInput, request: Request) -> dict:
    if data.license_status is not None and data.license_status not in {"unknown", "owned", "licensed", "public_domain"}:
        raise HTTPException(422, "授权状态无效。")
    with Session(engine) as session:
        row = session.get(AudioAsset, asset_id)
        if row is None or row.user_id != request.state.user_id:
            raise HTTPException(404, "素材不存在。")
        values = data.model_dump(exclude_unset=True)
        values = {key: value for key, value in values.items() if value is not None or key == "bpm"}
        if "tags" in values:
            values["tags"] = list(dict.fromkeys(tag.strip()[:32] for tag in values["tags"] if tag.strip()))
        if "license_status" in values and values["license_status"] not in {"unknown", "owned", "licensed", "public_domain"}:
            raise HTTPException(422, "授权状态无效。")
        for key, value in values.items():
            setattr(row, key, value)
        _index_audio_asset(row)
        session.commit()
        session.refresh(row)
        return _audio_asset_json(row)


def _training_host_path(*parts: str) -> str:
    root = os.getenv("ACESTEP_TRAINING_HOST_DIR", "").strip()
    if not root:
        raise HTTPException(503, "尚未配置 ACESTEP_TRAINING_HOST_DIR，训练数据目录无法映射到本机 ACE-Step。")
    if ntpath.splitdrive(root)[0]:
        return ntpath.join(root, *parts)
    return str(Path(root).joinpath(*parts))


def _acestep_request(method: str, path: str, payload: dict | None = None, *, timeout_seconds: int = 60) -> dict | list:
    base_url = os.getenv("ACESTEP_API_URL", "http://host.docker.internal:8001").rstrip("/")
    headers = {}
    if os.getenv("ACESTEP_API_KEY"):
        headers["Authorization"] = f"Bearer {os.getenv('ACESTEP_API_KEY')}"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_seconds, connect=5)) as client:
            response = client.request(method, f"{base_url}{path}", headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, f"ACE-Step 训练接口不可用：{type(exc).__name__}") from exc
    if result.get("error"):
        raise HTTPException(502, str(result["error"])[:500])
    if result.get("code") not in (None, 200):
        raise HTTPException(502, str(result.get("error") or "ACE-Step 请求失败")[:500])
    return result.get("data", result)


def _ensure_acestep_model_initialized() -> None:
    """Training preprocess endpoints require the ACE-Step DiT model to be loaded."""
    inventory = _acestep_request("GET", "/v1/models")
    if isinstance(inventory, list):
        models = inventory
        default_model = None
    elif isinstance(inventory, dict):
        models = inventory.get("models") or []
        default_model = inventory.get("default_model")
    else:
        raise HTTPException(502, "ACE-Step 返回的模型列表格式无效。")

    model_info = [model for model in models if isinstance(model, dict)]
    if not default_model:
        default_entry = next((model for model in model_info if model.get("is_default")), None)
        loaded_entry = next((model for model in model_info if model.get("is_loaded")), None)
        selected_entry = default_entry or loaded_entry or (model_info[0] if model_info else None)
        if selected_entry:
            default_model = selected_entry.get("name") or selected_entry.get("id")

    if default_model and any(
        (model.get("name") or model.get("id")) == default_model and model.get("is_loaded")
        for model in model_info
    ):
        return
    if not default_model and any(model.get("is_loaded") for model in model_info):
        return
    payload = {"init_llm": False}
    if default_model:
        payload["model"] = default_model
    _acestep_request("POST", "/v1/init", payload, timeout_seconds=900)


@app.get("/audio-assets/{asset_id}/audio")
def get_audio_asset(asset_id: str, request: Request):
    with Session(engine) as session:
        row = session.get(AudioAsset, asset_id)
        if row is None or row.user_id != request.state.user_id:
            raise HTTPException(404, "素材不存在。")
        path = Path(os.getenv("MUSIC_AUDIO_DIR", "/audio")) / "assets" / f"{asset_id}{Path(row.filename).suffix.lower()}"
        if not path.is_file():
            raise HTTPException(404, "素材文件不存在。")
        return FileResponse(path, filename=row.filename)


@app.delete("/audio-assets/{asset_id}", status_code=204)
def delete_audio_asset(asset_id: str, request: Request):
    with Session(engine) as session:
        row = session.get(AudioAsset, asset_id)
        if row is None or row.user_id != request.state.user_id:
            raise HTTPException(404, "素材不存在。")
        (Path(os.getenv("MUSIC_AUDIO_DIR", "/audio")) / "assets" / f"{asset_id}{Path(row.filename).suffix.lower()}").unlink(missing_ok=True)
        session.delete(row)
        session.commit()
        _delete_asset_index(asset_id)


def project_payload(project: MusicProject) -> dict:
    items = []
    for job_id, review in project.items.items():
        try:
            job = app.state.get_generation(job_id, project.user_id)
        except HTTPException:
            continue
        items.append({"job": job, "decision": review.get("decision", "pending"), "note": review.get("note", "")})
    items.sort(key=lambda item: (item["job"].get("version", 1), item["job"]["id"]))
    return {"id": project.id, "name": project.name, "items": items, "created_at": project.created_at}


@app.get("/music-projects")
def list_music_projects(request: Request):
    with Session(engine) as session:
        projects = session.scalars(select(MusicProject).where(MusicProject.user_id == request.state.user_id).order_by(MusicProject.created_at.desc()))
        return [project_payload(project) for project in projects]


@app.post("/music-projects", status_code=201)
def create_music_project(data: MusicProjectInput, request: Request):
    name = data.name.strip()
    if not name:
        raise HTTPException(422, "请填写项目名称。")
    project = MusicProject(id=uuid4().hex, user_id=request.state.user_id, name=name, items={}, created_at=int(time.time()))
    with Session(engine) as session:
        session.add(project)
        session.commit()
        session.refresh(project)
        return project_payload(project)


@app.put("/music-projects/{project_id}/items/{job_id}")
def review_music_project_item(project_id: str, job_id: str, data: ProjectReviewInput, request: Request):
    job = app.state.get_generation(job_id, request.state.user_id)
    if job["status"] != "succeeded":
        raise HTTPException(409, "只有已完成的作品可以加入筛选项目。")
    with Session(engine) as session:
        project = session.get(MusicProject, project_id)
        if project is None or project.user_id != request.state.user_id:
            raise HTTPException(404, "项目不存在。")
        items = dict(project.items or {})
        items[job_id] = {"decision": data.decision, "note": data.note.strip()}
        project.items = items
        session.commit()
        session.refresh(project)
        return project_payload(project)


@app.delete("/music-projects/{project_id}/items/{job_id}", status_code=204)
def remove_music_project_item(project_id: str, job_id: str, request: Request):
    with Session(engine) as session:
        project = session.get(MusicProject, project_id)
        if project is None or project.user_id != request.state.user_id:
            raise HTTPException(404, "项目不存在。")
        items = dict(project.items or {})
        items.pop(job_id, None)
        project.items = items
        session.commit()


@app.get("/music-projects/{project_id}/export")
def export_music_project(project_id: str, request: Request):
    with Session(engine) as session:
        project = session.get(MusicProject, project_id)
        if project is None or project.user_id != request.state.user_id:
            raise HTTPException(404, "项目不存在。")
        chosen = [(job_id, review) for job_id, review in (project.items or {}).items() if review.get("decision") == "selected"]
        project_name = project.name
    if not chosen:
        raise HTTPException(409, "请先将至少一个候选作品标记为入选。")
    audio_dir = Path(os.getenv("MUSIC_AUDIO_DIR", "generated-audio"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        manifest = [f"项目：{project_name}", "", "入选作品："]
        for job_id, _ in chosen:
            job = app.state.get_generation(job_id, request.state.user_id)
            if job["status"] != "succeeded":
                continue
            path = audio_dir / f"{job_id}-{job['attempt']}.wav"
            if not path.is_file():
                path = path.with_suffix(".mp3")
            if path.is_file():
                safe_title = re.sub(r"[^\w\- ]", "_", job["theme"]).strip()[:60] or job_id
                bundle.write(path, f"{safe_title}-{job_id[:8]}{path.suffix}")
                review = next(review for selected_id, review in chosen if selected_id == job_id)
                manifest.append(f"- {job['theme']} | {safe_title}-{job_id[:8]}{path.suffix}" + (f" | 备注：{review.get('note', '')}" if review.get("note") else ""))
        if len(manifest) > 3:
            bundle.writestr("项目说明.txt", "\n".join(manifest))
    if not archive.getbuffer().nbytes:
        raise HTTPException(409, "入选作品没有可导出的音频文件。")
    archive.seek(0)
    return StreamingResponse(archive, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=music-project.zip", "X-Project-Name": project_name})


@mcp.tool(name="list_playlists")
def list_playlists_mcp() -> list[dict]:
    """List playlists created through the trusted music service integration."""
    return list_playlists_for_user(None)


# 将创建歌单注册为 MCP 工具。
@mcp.tool()
# 接收 MCP 客户端传入的名称和歌曲 ID。
def create_playlist(name: str, track_ids: list[str]) -> dict:
    """Save a playlist after the user asks to save it."""
    # MCP 入口复用 HTTP 接口的输入校验和保存逻辑。
    return save_playlist(PlaylistInput(name=name, track_ids=track_ids), None)


@mcp.tool()
def music_lyric_coach(messages: list[dict], mode: str = "draft") -> dict:
    """Write, revise, or critique song lyrics. Audio generation requires separate user confirmation."""
    if mode not in {"draft", "revise", "critique"}:
        raise HTTPException(422, "mode must be draft, revise, or critique.")
    if not messages or len(messages) > 20:
        raise HTTPException(422, "Provide 1 to 20 recent messages.")
    from generation import CreativeChatMessage
    clean_messages = [CreativeChatMessage.model_validate(item).model_dump() for item in messages]
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise HTTPException(503, "Configure DEEPSEEK_API_KEY to use the lyric coach.")
    instructions = {
        "draft": "Help the user write an original song and draft lyrics in the user's language.",
        "revise": "Revise the supplied song lyrics according to the user's feedback; preserve what they want to keep.",
        "critique": "Critique the supplied song lyrics constructively, focusing on imagery, rhythm, rhyme, and singability.",
    }[mode]
    with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
        response = client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-flash"),
                  "messages": [{"role": "system", "content": f"You are a Chinese songwriting assistant. {instructions} Do not submit music generation. Show lyrics in a readable, complete format and ask the user to confirm before audio creation."}, *clean_messages]},
        )
        response.raise_for_status()
    return {"reply": response.json()["choices"][0]["message"]["content"].strip()}


@mcp.tool()
def music_create_generation(theme: str, lyrics: str, style: str = "", duration_seconds: int = 30) -> dict:
    """Queue an ACE-Step vocal song using lyrics the user has confirmed."""
    from generation import GenerationInput
    data = GenerationInput(theme=theme, style=style, lyrics=lyrics, duration_seconds=duration_seconds)
    return app.state.create_generation(data)


@mcp.tool()
def music_create_instrumental(theme: str, style: str = "", instrument: str = "piano", duration_seconds: int = 30, generator: str = "ace-step") -> dict:
    """Queue ACE-Step or local instrumental music generation from an approved creative brief."""
    from generation import GenerationInput
    data = GenerationInput(theme=theme, style=style, instrument=[instrument], instrumental=True,
                           instrumental_generator=generator, duration_seconds=duration_seconds)
    return app.state.create_generation(data)


@mcp.tool()
def music_get_generation(job_id: str) -> dict:
    """Check the status and result URL for a music generation job."""
    return app.state.get_generation(job_id)


@mcp.tool()
def music_create_revision(parent_job_id: str, feedback: str) -> dict:
    """Create a full-song vocal or instrumental revision from user feedback."""
    return app.state.create_revision(parent_job_id, feedback)


@mcp.tool()
def music_list_versions(job_id: str) -> list[dict]:
    """List the original generation and its successive revisions."""
    return app.state.list_versions(job_id)


# 在同一个 FastAPI 应用中挂载供 DeerFlow 连接的 MCP 服务。
app.mount("/mcp", mcp.streamable_http_app())
app.include_router(resume_router)
