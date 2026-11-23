# 记录生成任务的运行和失败信息。
import json
import logging
import time
# 读取供应商凭据、模型及存储配置。
import os
import mimetypes
import shutil
import subprocess
import tempfile
from typing import Literal
# 使用路径对象操作音频文件。
from pathlib import Path
# 生成任务唯一 ID。
from uuid import uuid4

# 调用歌词与音乐供应商的 HTTP 接口。
import httpx
# 创建并注册 Celery 后台任务。
from celery import Celery
# 捕获任务超过软时限的异常。
from celery.exceptions import SoftTimeLimitExceeded
from kombu import Exchange, Queue
# 声明 API、HTTP 错误及查询参数。
from fastapi import FastAPI, HTTPException, Query, Request
# 把磁盘上的音频作为 HTTP 响应发送。
from fastapi.responses import FileResponse, StreamingResponse
from instrumental import render_instrumental
# 捕获消息代理投递失败。
from kombu.exceptions import OperationalError
# 声明生成参数并校验字段组合。
from pydantic import BaseModel, ConfigDict, Field, model_validator
# 定义列类型并构建查询和更新。
from sqlalchemy import JSON, Float, Integer, String, select, text, update
# 声明传入的数据库引擎类型。
from sqlalchemy.engine import Engine
# 声明 ORM 模型并管理会话。
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

# 创建生成模块专用的日志记录器。
logger = logging.getLogger("music.generation")

Instrument = Literal["piano", "guzheng", "music_box", "guitar", "synth_pad", "violin", "strings", "flute", "bass", "drums", "organ", "harp", "trumpet", "saxophone", "choir"]
CreativeModel = Literal["deepseek-flash", "deepseek-v4-pro"]


# 为生成任务定义独立的 ORM 基类。
class GenerationBase(DeclarativeBase):
    # 沿用声明式基类的实现。
    pass


# 将生成任务映射到数据库。
class Generation(GenerationBase):
    # 指定生成任务表名。
    __tablename__ = "music_generations"
    # 使用 32 字符的任务 ID 作为主键。
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 存储当前任务状态。
    status: Mapped[str] = mapped_column(String(16))
    # 存储当前尝试次数。
    attempt: Mapped[int] = mapped_column(Integer)
    # 保存原始生成参数供 worker 和重试读取。
    request: Mapped[dict] = mapped_column(JSON)
    # 保存可为空的错误说明。
    error: Mapped[str | None] = mapped_column(String(200))
    # 保存 ACE-Step 实际回报的 0 到 1 进度及阶段文字。
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    progress_text: Mapped[str] = mapped_column(String(200), default="")


class CreativeConversation(GenerationBase):
    __tablename__ = "creative_conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 允许旧版对话在迁移后暂时保持无主状态；新建对话仍由认证用户归属。
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    messages: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[int] = mapped_column(Integer)


# 定义音乐生成请求的输入模型。
class GenerationInput(BaseModel):
    # 清理字符串首尾空白并拒绝未知字段。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    # 要求主题长度为 1 到 500 个字符。
    theme: str = Field(min_length=1, max_length=500)
    # 曲风可省略，最长 200 个字符。
    style: str = Field(default="", max_length=200)
    # 歌词可省略，最长 3000 个字符。
    lyrics: str = Field(default="", max_length=3000)
    # 默认不强制生成纯音乐。
    instrumental: bool = False
    # 纯音乐可调用 ACE-Step，也可保留本地 MIDI 渲染路径。
    instrumental_generator: Literal["local", "ace-step"] = "local"
    # 指定本地纯音乐合成使用的音色。
    instrument: list[Instrument] = Field(default_factory=lambda: ["piano"], max_length=15)
    focus_instrument: Instrument = "piano"
    support_level: Literal["soft", "balanced", "full"] = "balanced"
    # 纯音乐编曲方式；显式选择模型时禁止静默回退成本地规则。
    arrangement_model: Literal["local", "deepseek-flash", "deepseek-v4-pro"] = "deepseek-flash"
    # 默认关闭自动代写歌词。
    draft_lyrics: bool = False
    # 固定时长为 10 到 120 秒；人声歌曲可设为 None 交由 ACE-Step 自动决定。
    duration_seconds: int | None = Field(default=30, ge=10, le=120)
    revision_feedback: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=12)
    reference_audio_asset_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    mix_asset_ids: list[str] = Field(default_factory=list, max_length=4)
    mix_gain: float = Field(default=0.25, ge=0.05, le=1.0)
    mix_start_seconds: int = Field(default=0, ge=0, le=120)
    parent_job_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    version: int = Field(default=1, ge=1)

    # 在字段校验完成后检查歌词选项。
    @model_validator(mode="after")
    # 校验纯音乐、自带歌词和代写之间的约束。
    def validate_lyrics(self):
        # 人声歌曲不使用纯音乐编制；纯音乐则至少需要一种乐器。
        if self.instrumental and not self.instrument:
            raise ValueError("Instrumental tracks require at least one instrument.")
        if self.instrumental and self.duration_seconds is None:
            raise ValueError("Instrumental tracks require a fixed duration.")
        # 检查纯音乐请求是否同时携带歌词或代写选项。
        if self.instrumental and (self.lyrics or self.draft_lyrics):
            # 拒绝纯音乐请求中的歌词设置。
            raise ValueError("Instrumental tracks cannot include lyrics.")
        # 检查是否同时提供歌词并要求代写。
        if self.lyrics and self.draft_lyrics:
            # 要求在自带歌词和自动代写中选一种。
            raise ValueError("Provide lyrics or request a draft.")
        if self.instrumental and self.reference_audio_asset_id:
            raise ValueError("ACE-Step reference audio can only be used for vocal generation.")
        if self.instrumental and self.instrumental_generator == "ace-step" and self.mix_asset_ids:
            raise ValueError("Stem/sample mixing is only available for local instrumental generation.")
        if self.instrumental and self.mix_asset_ids and self.mix_start_seconds >= self.duration_seconds:
            raise ValueError("Mix start time must be shorter than the instrumental track duration.")
        if not self.instrumental and self.mix_asset_ids:
            raise ValueError("Stem/sample mixing is only available for local instrumental generation.")
        if len(set(self.mix_asset_ids)) != len(self.mix_asset_ids):
            raise ValueError("Do not select the same mixing asset more than once.")
        if any(len(asset_id) != 32 or any(char not in "0123456789abcdef" for char in asset_id) for asset_id in self.mix_asset_ids):
            raise ValueError("Mixing asset IDs must be 32-character hexadecimal IDs.")
        # 返回通过校验的请求模型。
        return self


def generation_input_from_request(request_data: dict) -> GenerationInput:
    # 内部生成器标记不属于公开请求参数，读取数据库任务时先移除。
    return GenerationInput.model_validate({key: value for key, value in request_data.items() if key != "_generator"})


def mix_local_audio(base_audio: bytes, asset_paths: list[Path], *, gain: float, start_seconds: int, duration_seconds: int) -> bytes:
    """Mix selected user-owned stems/samples over the local renderer's WAV result."""
    if not asset_paths:
        return base_audio
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("当前镜像缺少 FFmpeg，无法混入 stem/sample。请重建 Worker 镜像。")
    with tempfile.TemporaryDirectory(prefix="music-mix-") as directory:
        root = Path(directory)
        base_path = root / "base.wav"
        output_path = root / "mixed.wav"
        base_path.write_bytes(base_audio)
        inputs = [ffmpeg, "-y", "-i", str(base_path)]
        for path in asset_paths:
            inputs.extend(["-i", str(path)])
        filters = ["[0:a]aresample=44100,aformat=channel_layouts=stereo[base]"]
        delay_ms = start_seconds * 1000
        for index in range(1, len(asset_paths) + 1):
            filters.append(f"[{index}:a]aresample=44100,aformat=channel_layouts=stereo,volume={gain},adelay={delay_ms}|{delay_ms}[asset{index}]")
        labels = "".join(f"[asset{index}]" for index in range(1, len(asset_paths) + 1))
        filters.append(f"[base]{labels}amix=inputs={len(asset_paths) + 1}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[out]")
        inputs.extend(["-filter_complex", ";".join(filters), "-map", "[out]", "-t", str(duration_seconds),
                       "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(output_path)])
        subprocess.run(inputs, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
        return output_path.read_bytes()


class GenerationTagsInput(BaseModel):
    """User-editable catalog tags for a generated work."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    tags: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def validate_tags(self):
        normalized = []
        for tag in self.tags:
            tag = tag.strip()
            if not tag or len(tag) > 32:
                raise ValueError("Tags must contain 1 to 32 characters.")
            if tag.casefold() not in {item.casefold() for item in normalized}:
                normalized.append(tag)
        self.tags = normalized
        return self


class GenerationRevisionInput(BaseModel):
    feedback: str = Field(min_length=1, max_length=1000)
    lyrics: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def validate_feedback(self):
        self.feedback = self.feedback.strip()
        if not self.feedback:
            raise ValueError("Please describe what to change.")
        return self


class CreativeChatMessage(BaseModel):
    """一条发送给创作助手的对话消息。"""

    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=5000)


class CreativeChatInput(BaseModel):
    """创作助手的最近对话，浏览器仅保留有限轮次。"""

    model_config = ConfigDict(extra="forbid")
    messages: list[CreativeChatMessage] = Field(min_length=1, max_length=20)
    session_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    chat_type: Literal["song", "instrumental"] = "instrumental"
    # 用户可分别控制更长推演和服务端联网检索。
    deep_think: bool = False
    web_search: bool = False
    # 留空时使用服务器配置的默认模型。
    model: CreativeModel | None = None


class CreativeBrief(BaseModel):
    """创作助手整理出的纯音乐方案或已确认歌曲歌词方案。"""

    model_config = ConfigDict(extra="forbid")
    theme: str = Field(min_length=1, max_length=500)
    style: str = Field(default="", max_length=200)
    instrument: Instrument = "piano"
    duration_seconds: int | None = Field(default=30, ge=10, le=120)
    lyrics: str = Field(default="", max_length=3000)


# 请求供应商生成音乐并返回音频字节。
def render_ace_step(client: httpx.Client, data: GenerationInput, lyrics: str, reference_path: Path | None = None, progress_callback=None) -> bytes:
    base_url = os.getenv("ACESTEP_API_URL", "http://host.docker.internal:8001").rstrip("/")
    headers = {}
    api_key = os.getenv("ACESTEP_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "prompt": ", ".join(part for part in (data.style, data.theme,
                                                    ("乐器：" + "、".join(data.instrument)) if data.instrumental else "",
                                                    f"修改要求：{data.revision_feedback}" if data.revision_feedback else "") if part),
        # ACE-Step 的 release_task 用 [inst] 标记纯音乐，不需要歌词。
        "lyrics": "[inst]" if data.instrumental else lyrics,
        "vocal_language": "unknown" if data.instrumental else "zh",
        "audio_format": "mp3",
        # 自动时长需要 5Hz LM 根据歌词/结构推断时长和生成代码。
        "thinking": data.duration_seconds is None,
    }
    if data.duration_seconds is not None:
        payload["audio_duration"] = data.duration_seconds
    if reference_path is not None:
        mime = mimetypes.guess_type(reference_path.name)[0] or "application/octet-stream"
        with reference_path.open("rb") as reference_file:
            response = client.post(f"{base_url}/release_task", headers=headers, data=payload,
                                   files={"reference_audio": (reference_path.name, reference_file, mime)})
    else:
        response = client.post(f"{base_url}/release_task", headers=headers, json=payload)
    response.raise_for_status()
    task_id = response.json().get("data", {}).get("task_id")
    if not task_id:
        raise ValueError("ACE-Step did not return a task ID.")
    deadline = time.monotonic() + float(os.getenv("ACESTEP_TASK_TIMEOUT", "3600"))
    while time.monotonic() < deadline:
        result_response = client.post(f"{base_url}/query_result", headers=headers, json={"task_id_list": [task_id]})
        result_response.raise_for_status()
        items = result_response.json().get("data", [])
        item = next((entry for entry in items if entry.get("task_id") == task_id), None)
        if item and item.get("status") == 2:
            failure_detail = ""
            try:
                failure_items = json.loads(item.get("result") or "[]")
                if failure_items and isinstance(failure_items[0], dict):
                    failure_detail = failure_items[0].get("error") or failure_items[0].get("status_message") or ""
            except (TypeError, json.JSONDecodeError):
                pass
            failure_detail = str(failure_detail).strip()[:300]
            logger.warning("ace_step_task_failed task_id=%s detail=%s", task_id, failure_detail or "no detail returned")
            raise ValueError(f"ACE-Step generation failed: {failure_detail or 'no detail returned'}")
        if item and progress_callback:
            # 从 ACE-Step 的 query_result 读取模型真实推理进度和阶段。
            progress = None
            # query_result 顶层的 progress_text 是服务最近日志，不一定属于当前任务；只读取任务结果内的阶段信息。
            stage = "ACE-Step 正在推理"
            try:
                progress_items = json.loads(item.get("result") or "[]")
                if progress_items and isinstance(progress_items[0], dict):
                    progress = progress_items[0].get("progress")
                    stage = progress_items[0].get("progress_text") or progress_items[0].get("stage") or stage
            except (TypeError, json.JSONDecodeError):
                pass
            if progress is not None:
                try:
                    progress_callback(max(0.0, min(0.99, float(progress))), str(stage)[:200])
                except (TypeError, ValueError):
                    logger.debug("ace_step_invalid_progress task_id=%s progress=%r", task_id, progress)
                except Exception:
                    # 进度记录失败不应中断 ACE-Step 音频生成。
                    logger.exception("ace_step_progress_update_failed task_id=%s", task_id)
        if item and item.get("status") == 1:
            details = json.loads(item.get("result") or "[]")
            result_item = details[0] if details and isinstance(details[0], dict) else {}
            file_value = result_item.get("file")
            if not file_value or (isinstance(file_value, str) and not file_value.strip()):
                result_keys = sorted(result_item.keys())
                provider_detail = result_item.get("error") or result_item.get("status_message") or ""
                provider_detail = str(provider_detail).strip().replace("\n", " ")[:160]
                model_info = ",".join(str(result_item.get(key) or "") for key in ("dit_model", "lm_model")).strip(",")[:120]
                stage = str(result_item.get("stage") or "")[:100]
                logger.warning(
                    "ace_step_completed_without_audio task_id=%s status=%s result_count=%s result_keys=%s "
                    "file_type=%s file_empty=%s file_length=%s stage=%s progress=%s models=%s provider_detail=%s env=%s",
                    task_id, item.get("status"), len(details) if isinstance(details, list) else 0,
                    result_keys, type(file_value).__name__, not bool(file_value),
                    len(file_value) if isinstance(file_value, str) else None,
                    stage, result_item.get("progress"), model_info, provider_detail[:300], result_item.get("env"),
                )
                model_text = f"；模型：{model_info}" if model_info else ""
                stage_text = f"；阶段：{stage}" if stage else ""
                detail_text = f"；供应商信息：{provider_detail}" if provider_detail else ""
                raise ValueError(
                    f"ACE-Step 已完成但未返回音频文件；供应商任务 ID：{task_id}；"
                    f"file 类型：{type(file_value).__name__}{model_text}{stage_text}{detail_text}。"
                )
            audio_url = file_value
            audio_response = client.get(audio_url if audio_url.startswith("http") else f"{base_url}{audio_url}", headers=headers)
            audio_response.raise_for_status()
            if not audio_response.content:
                raise ValueError("ACE-Step returned an empty audio file.")
            return audio_response.content
        time.sleep(3)
    raise TimeoutError("ACE-Step generation timed out.")
    # 读取用户提交的歌词。
    lyrics = data.lyrics
    # 仅在用户选择代写时请求歌词服务。
    if data.draft_lyrics:
        # DeepSeek 只在用户要求代写歌词时参与，音频由音乐模型生成。
        response = client.post(
            "https://api.deepseek.com/chat/completions",
            # 在请求头附带 DeepSeek 密钥。
            headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
            json={
                # 读取配置的歌词模型，缺省时使用内置模型名。
                "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                "messages": [
                    # 要求模型只写简短原创歌词，并沿用主题的语言。
                    {"role": "system", "content": "Write short original song lyrics only. Use the language of the user's theme."},
                    # 将主题、曲风和时长交给歌词模型。
                    {"role": "user", "content": f"Theme: {data.theme}\nStyle: {data.style}\nDuration: {data.duration_seconds}s"},
                ],
            },
        )
        # 将歌词服务的非成功状态转为异常。
        response.raise_for_status()
        # 提取第一条回复，清理空白并截取前 3000 字符。
        lyrics = response.json()["choices"][0]["message"]["content"].strip()[:3000]
        # 检查代写结果是否为空。
        if not lyrics:
            # 将空歌词交给任务的失败处理流程。
            raise ValueError("DeepSeek returned empty lyrics.")

    return render_ace_step(client, data, lyrics)


# 初始化生成任务并将相关路由注册到 API。
def configure_generation(app: FastAPI, engine: Engine) -> Celery:
    # 根据消息代理地址初始化 Celery。
    celery = Celery("music", broker=os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//"))
    # 新任务进入带死信处理的 quorum 队列；旧队列继续接收升级前的积压任务。
    task_exchange = Exchange("music.tasks", type="direct", durable=True)
    dead_exchange = Exchange("music.dead", type="direct", durable=True)
    celery.conf.update(
        task_default_queue="music.generate",
        task_default_exchange="music.tasks",
        task_default_exchange_type="direct",
        task_default_routing_key="music.generate",
        task_default_delivery_mode="persistent",
        task_routes={"music.generate": {"queue": "music.generate"}},
        task_create_missing_queues=False,
        task_queues=(
            Queue("music.generate.dead", dead_exchange, routing_key="music.generate.dead", durable=True,
                  queue_arguments={"x-queue-type": "quorum"}),
            Queue("music.generate", task_exchange, routing_key="music.generate", durable=True,
                  queue_arguments={
                      "x-queue-type": "quorum",
                      "x-dead-letter-exchange": "music.dead",
                      "x-dead-letter-routing-key": "music.generate.dead",
                      # 使用 quorum 队列的确认式死信转发，避免目标队列暂时不可用时丢消息。
                      "x-dead-letter-strategy": "at-least-once",
                      "x-overflow": "reject-publish",
                      # 达到五次投递上限后由 RabbitMQ 移入死信队列。
                      "x-delivery-limit": 5,
                  }),
            # 保留 Celery 默认队列，让升级前尚未处理的任务继续被 Worker 消费。
            Queue("celery", Exchange("celery", type="direct", durable=True), routing_key="celery", durable=True),
        ),
    )
    # 读取生成音频的保存目录。
    audio_dir = Path(os.getenv("MUSIC_AUDIO_DIR", "generated-audio"))

    def creative_chat_model():
        # 创作对话单独配置模型，联网搜索依赖该模型的工具支持。
        configured = os.getenv("DEEPSEEK_CHAT_MODEL", "deepseek-flash")
        return configured if configured in ("deepseek-flash", "deepseek-v4-pro") else "deepseek-flash"

    # 按任务 ID、尝试次数和原状态有条件地更新任务。
    def transition(job_id, attempt, current, target, error=None):
        # 原子更新避免重复投递或并发重试触发两次付费生成。
        with Session(engine) as session:
            # 执行数据库更新并保留受影响行数。
            result = session.execute(
                update(Generation)
                # 只有指定尝试仍处于预期状态时才更新。
                .where(Generation.id == job_id, Generation.attempt == attempt, Generation.status == current)
                # 写入目标状态和对应的错误说明。
                .values(
                    status=target,
                    error=error,
                    progress=1.0 if target == "succeeded" else Generation.progress,
                    progress_text=("已完成" if target == "succeeded" else ("生成失败" if target == "failed" else Generation.progress_text)),
                )
            )
            # 提交状态更新。
            session.commit()
            # 恰好更新一行才视为转换成功。
            return result.rowcount == 1

    # 将 ACE-Step 的实际进度写入任务记录，供网页轮询展示。
    def update_progress(job_id: str, attempt: int, progress: float, progress_text: str):
        with Session(engine) as session:
            session.execute(
                update(Generation)
                .where(Generation.id == job_id, Generation.attempt == attempt, Generation.status == "running")
                .values(progress=max(0.0, min(0.99, progress)), progress_text=progress_text[:200])
            )
            session.commit()

    # 将函数注册为 Celery 任务。
    @celery.task(
        name="music.generate",
        # 将任务实例传给函数以读取投递信息。
        bind=True,
        # 任务状态由应用数据库管理，因此关闭 Celery 结果存储。
        ignore_result=True,
        # 执行结束后才确认消息。
        acks_late=True,
        # worker 丢失时允许消息代理重新投递。
        reject_on_worker_lost=True,
        # 到软时限后抛出可捕获的异常。
        soft_time_limit=3660,
        # 到硬时限后终止超时任务。
        time_limit=3690,
    )
    # 领取并执行指定的生成尝试。
    def generate(task, job_id: str, attempt: int):
        # 只有成功从排队切换到运行才能开始调用付费接口。
        if not transition(job_id, attempt, "queued", "running"):
            # RabbitMQ 重投已领取的任务时，检查产物，避免自动重复调用付费接口。
            if (task.request.delivery_info or {}).get("redelivered"):
                # 检查上一次运行是否已留下完整音频。
                if audio_path(job_id, attempt).is_file():
                    # 音频已落盘时恢复成功状态。
                    transition(job_id, attempt, "running", "succeeded")
                # 无完整音频时将这次尝试视为中断。
                else:
                    # 标记任务失败，供用户主动重试。
                    transition(job_id, attempt, "running", "failed", "Worker interrupted. Retry to generate again.")
            # 结束本次处理，避免重复生成。
            return
        user_id = None
        asset_paths: dict[str, Path] = {}
        # 定位当前尝试的最终音频。
        path = audio_path(job_id, attempt)
        # 生成同目录下的临时文件路径。
        temporary = path.with_suffix(".tmp")
        # 将参数读取、生成和落盘纳入统一的错误处理。
        try:
            # 打开会话读取已保存的参数。
            with Session(engine) as session:
                # 从任务记录加载参数并重新校验。
                job = session.get(Generation, job_id)
                user_id = job.user_id if job is not None else None
                data = generation_input_from_request(job.request)
                selected_ids = list(dict.fromkeys(data.mix_asset_ids + ([data.reference_audio_asset_id] if data.reference_audio_asset_id else [])))
                for asset_id in selected_ids:
                    asset = session.execute(text("SELECT filename, license_status FROM audio_assets WHERE id=:asset_id AND user_id=:user_id"),
                                            {"asset_id": asset_id, "user_id": user_id}).mappings().first()
                    if asset is None:
                        raise ValueError("A selected audio asset is no longer available to this account.")
                    if asset["license_status"] not in {"owned", "licensed", "public_domain"}:
                        raise ValueError("A selected audio asset no longer has confirmed usage rights.")
                    asset_path = audio_dir / "assets" / f"{asset_id}{Path(asset['filename']).suffix.lower()}"
                    if not asset_path.is_file():
                        raise ValueError("A selected audio asset file is missing.")
                    asset_paths[asset_id] = asset_path
            if data.instrumental and data.instrumental_generator == "local":
                path = path.with_suffix(".wav")
                render_style = data.style
                if data.revision_feedback:
                    render_style = f"{render_style}\nRevision feedback: {data.revision_feedback}".strip()
                audio, arrangement_source = render_instrumental(
                    data.theme, render_style, data.duration_seconds, data.instrument,
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    job_id=job_id,
                    arrangement_model=data.arrangement_model,
                    focus_instrument=data.focus_instrument,
                    support_level=data.support_level,
                )
                audio = mix_local_audio(audio, [asset_paths[item] for item in data.mix_asset_ids],
                                        gain=data.mix_gain, start_seconds=data.mix_start_seconds,
                                        duration_seconds=data.duration_seconds)
            else:
                if data.instrumental:
                    path = path.with_suffix(".mp3")
                with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
                    audio = render_ace_step(
                        client, data, data.lyrics,
                        reference_path=asset_paths.get(data.reference_audio_asset_id) if data.reference_audio_asset_id else None,
                        progress_callback=lambda progress, stage: update_progress(job_id, attempt, progress, stage),
                    )
            # 创建存储目录及所需父目录。
            audio_dir.mkdir(parents=True, exist_ok=True)
            # 先将音频写入临时文件。
            temporary.write_bytes(audio)
            if data.instrumental and data.instrumental_generator == "local":
                # 先写来源，再发布成品，任务恢复时也能读取同一次尝试的来源。
                path.with_suffix(".source").write_text(arrangement_source, encoding="ascii")
            # 用完整的临时文件替换最终路径。
            temporary.replace(path)
        # 处理供应商、文件、数据解析和软超时错误。
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError, AttributeError, TypeError,
                subprocess.SubprocessError, SoftTimeLimitExceeded) as exc:
            # 尝试删除可能残留的临时文件。
            try:
                # 删除临时文件，允许文件已经不存在。
                temporary.unlink(missing_ok=True)
            # 清理失败时仍继续处理原任务的失败。
            except OSError:
                # 记录未能清理的任务 ID 和尝试次数。
                logger.warning("generation_cleanup_failed job_id=%s attempt=%s", job_id, attempt)
            # 在上游 HTTP 状态异常时提取状态码。
            provider_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            # 保留有限长度的失败详情，便于直接从任务卡和 Worker 日志诊断。
            failure_detail = (str(exc).strip() or "Music generation failed.")[:200]
            logger.warning("generation_failed job_id=%s user_id=%s attempt=%s error=%s provider_status=%s detail=%s",
                           job_id, user_id, attempt, type(exc).__name__, provider_status, failure_detail)
            # 把具体失败原因写入任务卡。
            transition(job_id, attempt, "running", "failed", failure_detail)
            # 失败后结束任务。
            return
        # 完成落盘后将当前尝试标记为成功。
        transition(job_id, attempt, "running", "succeeded")

    # 计算指定任务尝试的音频路径。
    def audio_path(job_id: str, attempt: int) -> Path:
        # 每次重试使用独立文件，避免旧 worker 覆盖新结果。
        wave_path = audio_dir / f"{job_id}-{attempt}.wav"
        if wave_path.is_file():
            return wave_path
        return wave_path.with_suffix(".mp3")

    # 将生成任务转换为公开 API 字段。
    def serialize(job: Generation) -> dict:
        source = None
        if job.status == "succeeded" and job.request["instrumental"] and job.request.get("instrumental_generator", "local") == "local":
            try:
                source = audio_path(job.id, job.attempt).with_suffix(".source").read_text(encoding="ascii")
            except (OSError, UnicodeError):
                pass  # 旧作品没有编曲来源记录。
        # 构造任务状态响应。
        return {
            "id": job.id, "status": job.status, "attempt": job.attempt,
            "user_id": job.user_id,
            "theme": job.request["theme"], "style": job.request["style"],
            "instrumental": job.request["instrumental"],
            "lyrics": job.request.get("lyrics", ""),
            "generator": job.request.get("_generator", "旧版人声生成服务" if not job.request["instrumental"] else "local"),
            # 兼容音色字段加入前创建的任务。
            "instrument": job.request.get("instrument", ["piano"]),
            "arrangement_model": job.request.get("arrangement_model", "local"),
            "arrangement_source": source,
                "duration_seconds": job.request.get("duration_seconds", 30),
            "parent_job_id": job.request.get("parent_job_id"),
            "version": job.request.get("version", 1),
            "revision_feedback": job.request.get("revision_feedback", ""),
            "tags": job.request.get("tags", []),
            "reference_audio_asset_id": job.request.get("reference_audio_asset_id"),
            "mix_asset_ids": job.request.get("mix_asset_ids", []),
            "progress": job.progress or 0.0,
            "progress_text": job.progress_text or "",
            "error": job.error,
            # 仅在任务成功后提供音频地址。
            "audio_url": f"/generations/{job.id}/audio" if job.status == "succeeded" else None,
        }

    # 根据任务 ID 查询并序列化当前状态。
    def snapshot(job_id, user_id):
        # 为单条任务查询打开会话。
        with Session(engine) as session:
            # 按主键读取任务记录。
            job = session.get(Generation, job_id)
            # 判断记录是否存在。
            if job is None:
                # 任务不存在时返回 404。
                raise HTTPException(404, "Generation not found.")
            if job.user_id != user_id:
                raise HTTPException(404, "Generation not found.")
            # 返回查询到的任务状态。
            return serialize(job)

    # 投递任务并读取投递后的状态。
    def enqueue(job_id, attempt, user_id):
        # 捕获消息发布阶段的异常。
        try:
            # 投递指定尝试并关闭发布端自动重试。
            generate.apply_async(args=(job_id, attempt), retry=False)
        # 处理消息代理不可用的情况。
        except OperationalError:
            # 将仍在排队的尝试标记为失败。
            transition(job_id, attempt, "queued", "failed", "Queue unavailable. Retry when it is back.")
        # 重新读取数据库中最新的任务状态。
        return snapshot(job_id, user_id)

    # 检查本次请求需要使用的供应商凭据。
    def check_credentials(draft_lyrics, instrumental, instrumental_generator="local"):
        if not instrumental or instrumental_generator == "ace-step":
            try:
                with httpx.Client(timeout=1.5) as client:
                    health = client.get(os.getenv("ACESTEP_API_URL", "http://host.docker.internal:8001").rstrip("/") + "/health")
                    if not health.is_success:
                        raise HTTPException(503, "ACE-Step API is not ready.")
            except httpx.HTTPError:
                raise HTTPException(503, "Cannot reach ACE-Step API. Start your deployed ACE-Step service first.") from None
        # 仅在开启代写时检查歌词供应商密钥。
        if draft_lyrics and not os.getenv("DEEPSEEK_API_KEY"):
            # 缺少代写所需的密钥时返回 503。
            raise HTTPException(503, "Configure DEEPSEEK_API_KEY to draft lyrics.")

    def check_arrangement_credentials(data):
        if data.instrumental and data.instrumental_generator == "local" and data.arrangement_model != "local" and not os.getenv("DEEPSEEK_API_KEY"):
            raise HTTPException(503, "所选模型编曲需要配置 DEEPSEEK_API_KEY。")

    @app.get("/generations/config")
    def generation_config():
        ace_step_available = False
        try:
            with httpx.Client(timeout=1.5) as client:
                health = client.get(os.getenv("ACESTEP_API_URL", "http://host.docker.internal:8001").rstrip("/") + "/health")
                ace_step_available = health.is_success
        except httpx.HTTPError:
            pass
        return {
            "vocal_generation_available": ace_step_available,
            "ace_step_available": ace_step_available,
            "instrumental_generation_available": True,
            "ace_step_instrumental_available": ace_step_available,
        }

    @app.get("/creative-chat/config")
    # 只返回可公开的助手能力和模型名称，不返回任何密钥。
    def creative_chat_config():
        return {
            "model": creative_chat_model(),
            "deep_think": True,
            "web_search": True,
        }

    @app.get("/creative-conversations")
    def list_creative_conversations(request: Request):
        with Session(engine) as session:
            rows = session.scalars(
                select(CreativeConversation).where(CreativeConversation.user_id == request.state.user_id)
                .order_by(CreativeConversation.updated_at.desc())
            ).all()
            return [{"id": row.id, "title": row.title, "updated_at": row.updated_at} for row in rows]

    @app.post("/creative-conversations", status_code=201)
    def create_creative_conversation(request: Request):
        conversation = CreativeConversation(
            id=uuid4().hex, user_id=request.state.user_id, title="新对话", messages=[], updated_at=int(time.time())
        )
        result = {"id": conversation.id, "title": conversation.title, "messages": []}
        with Session(engine) as session:
            session.add(conversation)
            session.commit()
        return result

    @app.get("/creative-conversations/{conversation_id}")
    def get_creative_conversation(conversation_id: str, request: Request):
        with Session(engine) as session:
            conversation = session.get(CreativeConversation, conversation_id)
            if conversation is None or conversation.user_id != request.state.user_id:
                raise HTTPException(404, "Conversation not found.")
            return {"id": conversation.id, "title": conversation.title, "messages": conversation.messages or []}

    @app.delete("/creative-conversations/{conversation_id}", status_code=204)
    def delete_creative_conversation(conversation_id: str, request: Request):
        with Session(engine) as session:
            conversation = session.get(CreativeConversation, conversation_id)
            if conversation is None or conversation.user_id != request.state.user_id:
                raise HTTPException(404, "Conversation not found.")
            session.delete(conversation)
            session.commit()

    @app.post("/creative-chat")
    # 以 SSE 流式返回 DeepSeek 回复，末尾附带结构化创作方案。
    def creative_chat(data: CreativeChatInput, request: Request):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise HTTPException(503, "Configure DEEPSEEK_API_KEY to use the creative assistant.")
        model = data.model or creative_chat_model()
        if data.web_search and model != "deepseek-flash":
            raise HTTPException(422, "Web search is available with DeepSeek Flash in the creative assistant.")
        if data.session_id:
            with Session(engine) as session:
                conversation = session.get(CreativeConversation, data.session_id)
                if conversation is None or conversation.user_id != request.state.user_id:
                    raise HTTPException(404, "Conversation not found.")
        if data.chat_type == "instrumental":
            instructions = """You are a Chinese music creative assistant for instrumental tracks. Discuss the user's intent naturally and ask one focused question when essential information is missing. When enough information exists, provide a concise recommendation and a ready-to-use creation brief.
First write the user-facing reply in Chinese. Then, only when a usable brief is ready, append a new line containing exactly <MUSIC_BRIEF> followed by one compact JSON object with theme, style, instrument (piano|guzheng|music_box|guitar|synth_pad), and duration_seconds (10 to 120). If no brief is ready, do not include the marker."""
        else:
            instructions = """You are a Chinese songwriting assistant. Help the user develop an original song, including theme, style, structure, and lyrics. Ask one focused question when essential details are missing. Show complete lyrics clearly and invite the user to revise or confirm them. Once the user has approved the lyrics and enough music details are known, provide a ready-to-use brief.
First write the user-facing reply in Chinese. Then, only when the user has clearly approved a lyric draft and a usable song brief is ready, append a new line containing exactly <MUSIC_BRIEF> followed by one compact JSON object with theme, style, duration_seconds (10 to 120, or null when the user wants ACE-Step to determine the duration automatically), and lyrics (the exact approved lyrics). Do not generate audio or suggest that lyrics are confirmed without the user's confirmation. If no brief is ready, do not include the marker."""
        messages = [{"role": "system", "content": instructions}]
        messages.extend(message.model_dump() for message in data.messages)
        request_data = {
            "model": model,
            "thinking": {"type": "enabled" if data.deep_think else "disabled"},
            "stream": True,
            "messages": messages,
        }
        if data.web_search:
            # 搜索由 DeepSeek 服务端执行，浏览器与服务器都不会直接访问搜索结果网站。
            request_data["tools"] = [{"type": "deepseek_web_search"}]
        def events():
            marker = "<MUSIC_BRIEF>"
            buffered = ""
            reply_parts = []
            brief = None
            reading_brief = False
            brief_text = ""
            try:
                with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
                    with client.stream(
                        "POST", "https://api.deepseek.com/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}"}, json=request_data,
                    ) as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if not line or not line.startswith("data:"):
                                continue
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                delta = json.loads(raw)["choices"][0].get("delta", {}).get("content", "")
                            except (ValueError, KeyError, IndexError, TypeError):
                                continue
                            if not delta:
                                continue
                            if reading_brief:
                                brief_text += delta
                                continue
                            buffered += delta
                            if marker in buffered:
                                visible, structured = buffered.split(marker, 1)
                                if visible:
                                    reply_parts.append(visible)
                                    yield "data: " + json.dumps({"type": "delta", "text": visible}, ensure_ascii=False) + "\n\n"
                                brief_text = structured
                                reading_brief = True
                                buffered = ""
                                continue
                            safe_length = max(0, len(buffered) - len(marker) + 1)
                            if safe_length:
                                visible, buffered = buffered[:safe_length], buffered[safe_length:]
                                reply_parts.append(visible)
                                yield "data: " + json.dumps({"type": "delta", "text": visible}, ensure_ascii=False) + "\n\n"
                if reading_brief:
                    try:
                        parsed = json.loads(brief_text.strip())
                        brief = CreativeBrief.model_validate(parsed).model_dump()
                    except (ValueError, TypeError):
                        brief = None
                if buffered:
                    reply_parts.append(buffered)
                    yield "data: " + json.dumps({"type": "delta", "text": buffered}, ensure_ascii=False) + "\n\n"
                reply = "".join(reply_parts)
                if data.session_id and reply:
                    with Session(engine) as session:
                        conversation = session.get(CreativeConversation, data.session_id)
                        if conversation is not None and conversation.user_id == request.state.user_id:
                            stored = list(conversation.messages or [])
                            if data.messages and data.messages[-1].role == "user":
                                stored.append(data.messages[-1].model_dump())
                            stored.append({"role": "assistant", "content": reply[:5000]})
                            conversation.messages = stored[-200:]
                            if conversation.title == "新对话" and data.messages:
                                conversation.title = data.messages[-1].content[:80]
                            conversation.updated_at = int(time.time())
                            session.commit()
                yield "data: " + json.dumps({"type": "done", "brief": brief}, ensure_ascii=False) + "\n\n"
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                logger.warning("creative_chat_stream_failed request_id=%s user_id=%s reason=%s provider_status=%s",
                               request.state.request_id, request.state.user_id, type(exc).__name__, status)
                yield "data: " + json.dumps({"type": "error", "message": "创作助手暂时不可用，请稍后重试。"}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 注册创建生成任务的路由，接收成功时返回 202。
    # 持久化新任务并投递首次尝试。
    def persist_generation(data: GenerationInput, user_id=None):
        if data.draft_lyrics:
            raise HTTPException(422, "Use the songwriting conversation to draft and confirm lyrics before creating a song.")
        if not data.instrumental and not data.lyrics:
            raise HTTPException(422, "Vocal songs require confirmed lyrics from the songwriting conversation.")
        # 在写入任务前检查所需的供应商密钥。
        check_credentials(data.draft_lyrics, data.instrumental, data.instrumental_generator)
        check_arrangement_credentials(data)
        selected_ids = list(dict.fromkeys(data.mix_asset_ids + ([data.reference_audio_asset_id] if data.reference_audio_asset_id else [])))
        for asset_id in selected_ids:
            with Session(engine) as session:
                asset = session.execute(text("SELECT license_status FROM audio_assets WHERE id=:asset_id AND user_id=:user_id"),
                                         {"asset_id": asset_id, "user_id": user_id}).mappings().first()
            if asset is None:
                raise HTTPException(404, "所选素材不存在或不属于当前账号。")
            if asset["license_status"] not in {"owned", "licensed", "public_domain"}:
                raise HTTPException(422, "请先在素材库确认该素材的使用授权，再用于生成或混音。")
        # 生成任务唯一 ID。
        job_id = uuid4().hex
        # 为创建任务打开数据库会话。
        with Session(engine) as session:
            # 写入请求参数，初始状态为排队、尝试次数为 1。
            request_data = data.model_dump()
            request_data["_generator"] = "local" if data.instrumental and data.instrumental_generator == "local" else "ACE-Step"
            session.add(Generation(id=job_id, user_id=user_id, status="queued", attempt=1, request=request_data))
            # 在发布消息之前提交任务记录。
            session.commit()
        # 投递已保存任务的第一次尝试。
        return enqueue(job_id, 1, user_id)

    @app.post("/generations", status_code=202)
    def create_generation(data: GenerationInput, request: Request):
        return persist_generation(data, request.state.user_id)

    app.state.create_generation = persist_generation

    def create_revision(parent_job_id: str, feedback: str, user_id=None, lyrics: str | None = None):
        if not feedback.strip() or len(feedback) > 1000:
            raise HTTPException(422, "Feedback must contain 1 to 1000 characters.")
        with Session(engine) as session:
            parent = session.get(Generation, parent_job_id)
            if parent is None:
                raise HTTPException(404, "Generation not found.")
            if parent.user_id != user_id:
                raise HTTPException(404, "Generation not found.")
            if parent.status != "succeeded":
                raise HTTPException(409, "Wait for the source generation to succeed before revising it.")
            payload = dict(parent.request)
            payload.pop("_generator", None)
            next_version = max((item["version"] for item in list_versions(parent.id, user_id)), default=parent.request.get("version", 1)) + 1
            payload.update({
                "revision_feedback": feedback.strip(),
                "parent_job_id": parent.id,
                "version": next_version,
            })
            if lyrics is not None:
                if parent.request.get("instrumental", False):
                    raise HTTPException(422, "Instrumental revisions do not accept lyrics.")
                payload["lyrics"] = lyrics.strip()
        return persist_generation(GenerationInput.model_validate(payload), user_id=user_id)

    app.state.create_revision = create_revision

    def list_versions(job_id: str, user_id=None):
        with Session(engine) as session:
            current = session.get(Generation, job_id)
            if current is None:
                raise HTTPException(404, "Generation not found.")
            if current.user_id != user_id:
                raise HTTPException(404, "Generation not found.")
            jobs = list(session.scalars(select(Generation).where(Generation.user_id == user_id).order_by(Generation.id)))
            by_id = {job.id: job for job in jobs}
            root = current
            seen = set()
            while root.request.get("parent_job_id") in by_id and root.id not in seen:
                seen.add(root.id)
                root = by_id[root.request["parent_job_id"]]
            chain = {root.id}
            changed = True
            while changed:
                changed = False
                for job in jobs:
                    if job.request.get("parent_job_id") in chain and job.id not in chain:
                        chain.add(job.id)
                        changed = True
            return sorted((serialize(by_id[item]) for item in chain), key=lambda item: (item["version"], item["id"]))

    app.state.list_versions = list_versions

    def get_generation(job_id: str, user_id=None):
        return snapshot(job_id, user_id)

    # 注册任务列表路由。
    @app.get("/generations")
    # 按任务 ID 游标分页。
    def list_generations(
        request: Request,
        # 默认每页 20 条，允许 1 到 100 条。
        limit: int = Query(20, ge=1, le=100),
        # 将游标限制为 32 位小写十六进制字符串。
        after: str | None = Query(None, pattern=r"^[0-9a-f]{32}$"),
    ):
        # 以不可变的任务 ID 分页，任务状态变化时也不会改变排序。
        # 多查一条记录判断是否还有下一页。
        statement = select(Generation).where(Generation.user_id == request.state.user_id).order_by(Generation.id).limit(limit + 1)
        # 有游标时缩小查询范围。
        if after:
            # 仅查询 ID 大于游标的任务。
            statement = statement.where(Generation.id > after)
        # 打开数据库会话执行分页查询。
        with Session(engine) as session:
            # 读取当前页及用于检测下一页的额外记录。
            jobs = list(session.scalars(statement))
            # 构造分页响应。
            return {
                # 只返回当前页规定数量的任务。
                "items": [serialize(job) for job in jobs[:limit]],
                # 还有下一页时用本页最后一条任务的 ID 作为游标。
                "next_cursor": jobs[limit - 1].id if len(jobs) > limit else None,
            }

    # 注册单任务查询路由。
    @app.get("/generations/{job_id}")
    # 接收路径中的任务 ID。
    def get_generation_http(job_id: str, request: Request):
        # 返回指定任务的当前状态。
        return snapshot(job_id, request.state.user_id)

    app.state.get_generation = get_generation

    @app.get("/generations/{job_id}/versions")
    def get_generation_versions(job_id: str, request: Request):
        return list_versions(job_id, request.state.user_id)

    @app.post("/generations/{job_id}/revisions", status_code=202)
    def create_generation_revision(job_id: str, data: GenerationRevisionInput, request: Request):
        return create_revision(job_id, data.feedback, request.state.user_id, data.lyrics)

    @app.patch("/generations/{job_id}/tags")
    def update_generation_tags(job_id: str, data: GenerationTagsInput, request: Request):
        with Session(engine) as session:
            job = session.get(Generation, job_id)
            if job is None or job.user_id != request.state.user_id:
                raise HTTPException(404, "Generation not found.")
            updated_request = dict(job.request)
            updated_request["tags"] = data.tags
            job.request = updated_request
            session.commit()
            return serialize(job)

    # 注册重试路由，接收成功时返回 202。
    @app.post("/generations/{job_id}/retry", status_code=202)
    # 把符合条件的失败任务重新排队。
    def retry_generation(job_id: str, request: Request):
        # 为读取和更新任务打开同一数据库会话。
        with Session(engine) as session:
            # 按 ID 读取待重试任务。
            job = session.get(Generation, job_id)
            # 检查任务是否存在。
            if job is None:
                # 任务不存在时返回 404。
                raise HTTPException(404, "Generation not found.")
            if job.user_id != request.state.user_id:
                raise HTTPException(404, "Generation not found.")
            # 检查任务是否处于失败状态。
            if job.status != "failed":
                # 以 409 拒绝重试其他状态的任务。
                raise HTTPException(409, "Only failed generations can be retried.")
            # 根据原始请求检查重试所需的密钥。
            check_credentials(job.request["draft_lyrics"], job.request.get("instrumental", False), job.request.get("instrumental_generator", "local"))
            check_arrangement_credentials(generation_input_from_request(job.request))
            # 递增尝试次数以区分新旧结果。
            attempt = job.attempt + 1
            # 使用带条件的数据库更新争取重试资格。
            result = session.execute(
                update(Generation)
                # 仅在状态和尝试次数均未被其他请求更改时更新。
                .where(Generation.id == job_id, Generation.status == "failed", Generation.attempt == attempt - 1)
                # 将新尝试设为排队并清除上一轮错误。
                .values(status="queued", attempt=attempt, error=None, progress=0.0, progress_text="")
            )
            # 提交重试状态。
            session.commit()
            # 检查是否有并发请求已抢先更新。
            if result.rowcount != 1:
                # 更新未命中时以 409 报告任务已被重试。
                raise HTTPException(409, "Generation was already retried.")
        # 在事务提交后投递新的尝试。
        return enqueue(job_id, attempt, request.state.user_id)

    # 注册生成音频的读取路由。
    @app.get("/generations/{job_id}/audio")
    # 定位并返回成功生成的音频。
    def get_generation_audio(job_id: str, request: Request):
        # 查询任务状态及其当前尝试次数。
        job = snapshot(job_id, request.state.user_id)
        # 按当前尝试次数定位音频。
        path = audio_path(job["id"], job["attempt"])
        # 当前文件不存在时尝试旧版文件命名。
        if not path.is_file():
            path = audio_dir / f"{job['id']}.mp3"  # 兼容已生成的旧版作品。
        # 要求任务已成功且音频文件存在。
        if job["status"] != "succeeded" or not path.is_file():
            # 音频当前不可用时返回 404。
            raise HTTPException(404, "Audio is not available.")
        # 按文件格式发送音频，并指示客户端不缓存。
        media_type = "audio/wav" if path.suffix == ".wav" else "audio/mpeg"
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, no-store"})

    # 返回配置好的 Celery 应用供 worker 加载。
    return celery
