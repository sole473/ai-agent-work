"""Resume workspace with lightweight retrieval augmented generation."""
from datetime import datetime
import os
import re
from pathlib import PurePosixPath
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import String, Text, DateTime, JSON, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = None


def configure_resume(database):
    global engine
    engine = database

router = APIRouter(prefix="/resume", tags=["resume"])


class ResumeBase(DeclarativeBase):
    pass


class ResumeChunk(ResumeBase):
    __tablename__ = "resume_chunks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ResumeMatch(ResumeBase):
    __tablename__ = "resume_matches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)
    job_description: Mapped[str] = mapped_column(Text)
    analysis: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class MatchInput(BaseModel):
    job_description: str = Field(min_length=30, max_length=12000)


def split_chunks(text: str, size: int = 350, overlap: int = 50) -> list[str]:
    """按字符数切成有重叠的文本片段，不按句子、段落或 token 边界切分。"""
    # 合并换行和连续空白，避免排版空格占用片段长度。
    text = re.sub(r"\s+", " ", text).strip()
    # 每次前进 size - overlap 个字符，让相邻片段共享 overlap 个字符以保留边界上下文。
    return [text[i:i + size] for i in range(0, len(text), size - overlap) if text[i:i + size].strip()]


# BGE-small-zh-v1.5 输出 512 维向量；更换 embedding 模型时需同步调整此值及 Qdrant 集合。
VECTOR_SIZE = 512
# 限制每次送入 Embedding 服务的文本数，避免单次请求过大。
EMBED_BATCH_SIZE = 16
# 分批写入 Qdrant，控制单个 upsert 请求的体积。
POINT_BATCH_SIZE = 64
COLLECTION = os.getenv("QDRANT_COLLECTION", "resume_chunks")
# BGE 检索时把这段说明加在岗位查询前；材料片段本身不加该前缀。
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def _request_json(method: str, url: str, **kwargs):
    try:
        with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else {}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "RAG 检索服务暂不可用，请稍后重试。") from exc


def _ensure_collection():
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    url = f"{base}/collections/{COLLECTION}"
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5)) as client:
            response = client.get(url)
            if response.status_code == 200:
                return
            if response.status_code != 404:
                response.raise_for_status()
            created = client.put(url, json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}})
            if created.status_code not in (200, 201, 409):
                created.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(503, "无法连接 Qdrant，请检查服务状态。") from exc


def _embed(texts: list[str], *, query: bool = False) -> list[list[float]]:
    base = os.getenv("EMBEDDING_URL", "http://embedding:80").rstrip("/")
    embeddings = []
    for offset in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[offset:offset + EMBED_BATCH_SIZE]
        inputs = [f"{QUERY_INSTRUCTION}{text}" for text in batch] if query else batch
        result = _request_json("POST", f"{base}/embed", json={"inputs": inputs})
        if not isinstance(result, list) or len(result) != len(batch):
            raise HTTPException(503, "Embedding 服务返回了无效结果。")
        if any(not isinstance(vector, list) or len(vector) != VECTOR_SIZE for vector in result):
            raise HTTPException(503, "Embedding 向量维度与 Qdrant 配置不一致。")
        embeddings.extend(result)
    return embeddings


def _delete_chunks(rows: list[ResumeChunk]):
    if not rows:
        return
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        _request_json("POST", f"{base}/collections/{COLLECTION}/points/delete?wait=true", json={
            "points": [str(UUID(hex=row.id)) for row in rows]
        })
    except HTTPException:
        pass


def _upload_original_to_cos(raw: bytes, filename: str) -> str | None:
    """将简历原文件存入配置的 COS 桶；未配置桶时保留本地 RAG 行为。"""
    bucket = os.getenv("COS_BUCKET", "").strip()
    if not bucket:
        return None
    secret_id = os.getenv("COS_SECRET_ID", "").strip()
    secret_key = os.getenv("COS_SECRET_KEY", "").strip()
    if not secret_id or not secret_key:
        raise HTTPException(503, "COS 已配置桶名，但缺少 COS_SECRET_ID 或 COS_SECRET_KEY。")

    try:
        from qcloud_cos import CosConfig, CosS3Client
        config = CosConfig(
            Region=os.getenv("COS_REGION", "ap-guangzhou").strip(),
            SecretId=secret_id,
            SecretKey=secret_key,
            Token=os.getenv("COS_SESSION_TOKEN") or None,
        )
        client = CosS3Client(config)
        safe_name = PurePosixPath((filename or "resume").replace("\\", "/")).name
        safe_name = re.sub(r"[^\w.() -]", "_", safe_name, flags=re.UNICODE).strip(" .") or "resume"
        prefix = os.getenv("COS_PREFIX", "resume").strip("/") or "resume"
        key = f"{prefix}/{uuid4().hex}_{safe_name}"
        client.put_object(Bucket=bucket, Key=key, Body=raw)
        return key
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "简历原文件上传到 COS 失败，请检查桶地域、密钥和对象写入权限。") from exc


def _upsert_chunks(rows: list[ResumeChunk], user_id: str):
    _ensure_collection()
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    try:
        for offset in range(0, len(rows), POINT_BATCH_SIZE):
            batch = rows[offset:offset + POINT_BATCH_SIZE]
            vectors = _embed([row.content for row in batch])
            points = [
                {
                    "id": str(UUID(hex=row.id)),
                    "vector": vector,
                    "payload": {"user_id": user_id, "filename": row.filename},
                }
                for row, vector in zip(batch, vectors)
            ]
            _request_json("PUT", f"{base}/collections/{COLLECTION}/points?wait=true", json={"points": points})
    except Exception:
        _delete_chunks(rows)
        raise


def _search_chunks(vectors: list[list[float]], user_id: str, limit: int = 8) -> list[str]:
    _ensure_collection()
    base = os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")
    scores = {}
    for vector in vectors:
        result = _request_json(
            "POST",
            f"{base}/collections/{COLLECTION}/points/query",
            json={
                "query": vector,
                "limit": limit,
                "with_payload": True,
                "filter": {"must": [{"key": "user_id", "match": {"value": user_id}}]},
            },
        )
        for point in result.get("result", {}).get("points", []):
            point_id = UUID(point["id"]).hex
            scores[point_id] = max(scores.get(point_id, float("-inf")), point["score"])
    return [point_id for point_id, _ in sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]]


@router.get("", include_in_schema=False)
def resume_page():
    from pathlib import Path
    return FileResponse(Path(__file__).resolve().parent / "web" / "resume.html")


@router.post("/documents")
async def upload_document(request: Request, file: UploadFile = File(...)):
    name = (file.filename or "").lower()
    raw = await file.read(12_000_001)
    if len(raw) > 12_000_000:
        raise HTTPException(413, "文件不能超过 12 MB。")
    try:
        if name.endswith(".txt") or name.endswith(".md"):
            content = raw.decode("utf-8-sig")
        elif name.endswith(".pdf"):
            from pypdf import PdfReader
            import io
            content = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
        elif name.endswith(".docx"):
            from docx import Document
            import io
            content = "\n".join(p.text for p in Document(io.BytesIO(raw)).paragraphs)
        else:
            raise HTTPException(415, "目前支持 PDF、DOCX、TXT 和 MD 文件。")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "无法读取该文件，请确认文件未损坏且包含可提取文本。") from exc
    chunks = split_chunks(content)
    if not chunks:
        raise HTTPException(422, "文件中没有可检索的文本。")
    user_id = request.state.user_id
    storage_key = _upload_original_to_cos(raw, file.filename or "resume")
    rows = [ResumeChunk(id=uuid4().hex, user_id=user_id, filename=file.filename or "resume", storage_key=storage_key, content=chunk) for chunk in chunks]
    _upsert_chunks(rows, user_id)
    try:
        with Session(engine) as session:
            session.add_all(rows)
            session.commit()
    except Exception:
        _delete_chunks(rows)
        raise
    return {"filename": file.filename, "chunks_added": len(chunks), "stored_in_cos": storage_key is not None}


@router.get("/documents")
def list_documents(request: Request):
    with Session(engine) as session:
        rows = session.execute(
            select(ResumeChunk.filename, ResumeChunk.created_at)
            .where(ResumeChunk.user_id == request.state.user_id)
            .distinct()
            .order_by(ResumeChunk.created_at.desc())
        ).all()
    seen = set()
    docs = []
    for filename, created_at in rows:
        if filename not in seen:
            docs.append({"filename": filename, "created_at": created_at.isoformat()})
            seen.add(filename)
    return {"items": docs}


@router.get("/matches")
def list_matches(request: Request):
    with Session(engine) as session:
        rows = session.scalars(select(ResumeMatch).where(
            ResumeMatch.user_id == request.state.user_id
        ).order_by(ResumeMatch.created_at.desc()).limit(100)).all()
        return {"items": [{"id": row.id, "job_description": row.job_description,
                           "analysis": row.analysis, "sources": row.sources,
                           "created_at": row.created_at.isoformat()} for row in rows]}


@router.delete("/matches/{match_id}", status_code=204)
def delete_match(match_id: str, request: Request):
    with Session(engine) as session:
        row = session.get(ResumeMatch, match_id)
        if row is None or row.user_id != request.state.user_id:
            raise HTTPException(404, "找不到这条分析记录。")
        session.delete(row)
        session.commit()


@router.post("/match")
def match_job(data: MatchInput, request: Request):
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise HTTPException(503, "请配置 DEEPSEEK_API_KEY 后使用简历分析。")
    query_chunks = split_chunks(data.job_description)
    query_vectors = _embed(query_chunks, query=True)
    ids = _search_chunks(query_vectors, request.state.user_id)
    if not ids:
        raise HTTPException(409, "请先上传简历或经历材料，再进行岗位匹配。")
    with Session(engine) as session:
        rows = list(session.scalars(select(ResumeChunk).where(
            ResumeChunk.id.in_(ids), ResumeChunk.user_id == request.state.user_id
        )))
    by_id = {row.id: row for row in rows}
    ranked = [by_id[chunk_id] for chunk_id in ids if chunk_id in by_id]
    if not ranked:
        raise HTTPException(409, "请先上传简历或经历材料，再进行岗位匹配。")
    context = "\n\n".join(f"[材料：{row.filename}]\n{row.content}" for row in ranked)
    prompt = "你是简历顾问。只依据提供的个人材料分析岗位匹配度，指出匹配证据、差距和可改写建议；不得编造经历。明确区分材料事实与建议。\n\n个人材料（检索片段）：\n" + context + "\n\n岗位描述：\n" + data.job_description
    with httpx.Client(timeout=httpx.Timeout(90, connect=10)) as client:
        response = client.post("https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"},
            json={"model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"), "messages":[{"role":"user","content":prompt}]})
        response.raise_for_status()
    analysis = response.json()["choices"][0]["message"]["content"]
    sources = [{"filename": row.filename, "excerpt": row.content[:240]} for row in ranked]
    match = ResumeMatch(id=uuid4().hex, user_id=request.state.user_id,
                        job_description=data.job_description, analysis=analysis, sources=sources)
    with Session(engine) as session:
        session.add(match)
        session.commit()
    return {"id": match.id, "analysis": analysis, "sources": sources}
