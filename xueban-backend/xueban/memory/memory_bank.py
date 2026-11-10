"""Xueban 多模态记忆库：单文件、嵌入式、可追溯的 RAG 后端。

建议放置：``xueban/memory/memory_bank.py``。
本文件不修改前端、不替换现有 KnowledgePack，也不在 import 时启动服务或读取 API Key。
Python >= 3.10；核心文本/CSV/SQLite 检索只依赖标准库，其他能力按需懒加载。

===============================================================================
1. 最小接入（只调用 Python API）
===============================================================================

    from xueban.memory.memory_bank import MemoryBank

    bank = MemoryBank()                       # .xueban/knowledge/{pack_id}/
    pack = bank.create_pack("销售知识库")
    bank.ingest_text(pack["pack_id"], "Q3 华东续费率为 91%。", source_name="续费结论")
    bank.ingest_file(pack["pack_id"], "/data/销售明细.xlsx")
    result = bank.query(pack["pack_id"], "华东续费率", top_k=5)
    # result == {"data": {"hits": [...]}, "meta": {...}}；hits 含 text/source/score。

生产环境必须注入真正的 Embedder 才有语义召回；未注入时明确退化为 BM25 + 结构排序：

    from xueban.memory.memory_bank import (
        MemoryBank, SentenceTransformerEmbedder, CrossEncoderReranker,
        OpenAIVisionDescriber,
    )
    bank = MemoryBank(
        embedder=SentenceTransformerEmbedder("BAAI/bge-m3"),
        reranker=CrossEncoderReranker("BAAI/bge-reranker-v2-m3"),
        vision=OpenAIVisionDescriber(
            base_url="https://api.openai.com/v1",
            api_key=os.environ["OPENAI_API_KEY"],
            model="your-vision-model",
        ),
    )

===============================================================================
2. 可选 FastAPI 接入（与 Xueban 主文件低耦合）
===============================================================================

    from xueban.memory.memory_bank import MemoryBank, create_memory_router
    memory_bank = MemoryBank(embedder=my_embedder, vision=my_vision)
    app.include_router(create_memory_router(memory_bank))

路由前缀为 /api/memory-bank，可经现有 Node proxy-fallback 转发；不会与
/api/knowledge 或 /api/memory 冲突。核心方法是同步、事务式的，HTTP 层用
asyncio.to_thread 避免阻塞事件循环。文件上传走 multipart bytes，不信任客户端路径。

===============================================================================
3. 数据结构与为什么不是“一个大 JSON”
===============================================================================

每个 pack 目录包含：
    memory_bank.json       包配置、schema 版本、索引统计（不复制全部正文）
    memory_bank.sqlite     业务数据与索引，WAL、外键、事务、级联删除
    blobs/aa/{sha256}.ext  原始文件/抽取图片，内容寻址、不可变、可校验、可去重

SQLite 逻辑结构：
    sources   原始来源 + SHA256 + 文件名 + 版本 + supersedes + is_latest + 状态
    units     原子知识单元：段落、表格行、图片、公式/其他；保存结构 JSON 和血缘
    parents   给生成器的完整上下文，保存标题路径、页码、sheet、行列等
    chunks    给检索器的小块；保存 parent_id、原文偏移、float32 向量和模型签名
    links     单元间顺序/描述等确定性关系，不依赖 LLM 静默造边
    chunks_fts FTS5 全文索引；中英文统一转成英文词 + 中文 bigram 后计算 BM25

同名文件的新内容会成为新版本；新版本成功入库后旧版本才退出默认检索。
原始文件只存一份，结构化单元保留字段和单元格，父块和子块承担不同职责。
因此既能精确查“合同编号/金额/型号”，又能返回完整章节、表头或图片上下文。

===============================================================================
4. 检索与文献落地方式
===============================================================================

结构优先分块 -> 小子块多路召回 -> RRF 融合 -> 可选交叉编码器重排 ->
近重复抑制 -> parent_id 去重 -> 返回父上下文与精确 matched_text/血缘。

* RAG-Anything：非文本内容不是孤立附件；图片保存视觉描述/OCR/邻近上下文，
  表格保留表头、行、sheet，所有模态统一成 KnowledgeUnit 并保留原始内容引用。
* LightRAG/A-Mem：保留单元关系和语义扩展接口；本文件只写确定性结构边，
  不在入库时暗中调用 LLM 生成可能错误的“事实关系”。
* Adaptive-RAG/CoRAG：retrieve 接受 query_variants，复杂问题可由上层 Agent
  分解后多路召回；简单查询不强制多轮，不在存储层绑定某个 LLM。
* RAGChecker：evaluate_retrieval 分离 context precision、source recall、F1、
  Recall@K、MRR、nDCG；回答的 claim-level F1/faithfulness 仍应由生成评测层完成。

重叠只用于同一父块内的小文本块，不跨标题、页面、sheet 或表格行硬切。
默认不是“语义分块”：后者需要额外模型且边界不可复现；当前方案先保证结构、
可解释性与稳定召回。分块尺寸是字符预算而非某模型的 token 数，便于中文与模型迁移。

===============================================================================
5. 技术栈、理由与边界
===============================================================================

* SQLite + WAL + FTS5：项目已有 SQLite 使用习惯；零额外服务、事务可靠、BM25
  与元数据/版本在同一存储中，适合桌面端、Docker 和单实例 FastAPI。
* float32 BLOB + NumPy 精确余弦：不做 ANN 近似，避免召回损失；默认缓存向量矩阵。
  适合约数万至十万级小块（实际取决于维度、内存和延迟目标）。更大规模应把
  _dense_search 换成 Qdrant/Milvus/pgvector，公共 ingest/retrieve API 无需改变。
* openpyxl / python-docx / PyMuPDF：分别保留 Excel、Word、PDF 的结构与位置；
  均为可选懒加载，缺依赖时给出安装提示，不让整个 Xueban 启动失败。
* OpenAI-compatible / sentence-transformers / CrossEncoder：模型可本地也可远程，
  BYOK 与供应商选择留给主项目；接口注入避免把存储层锁死到一家模型。
* Tesseract 或视觉模型：扫描件和独立图片的文字/语义入口。两者都未配置时，
  图片不会用文件名冒充内容，而是明确报 VISION_REQUIRED。
* FastAPI 仅 create_memory_router 内可选导入；核心模块不要求安装 FastAPI。

支持：txt/md/markdown/log、csv、json/jsonl、html/htm、xlsx/xlsm、docx、pdf、
png/jpg/jpeg/webp/bmp/tif/tiff。旧 doc/xls、加密 PDF 需先转换/解密。
不执行宏、公式、脚本或嵌入对象。默认限制文件、页数、图片数、表格行数并检查
OOXML zip bomb；但商业生产仍需在网关做鉴权、配额、病毒扫描、审计和磁盘加密。
本文件的 SQLite 不是 SQLCipher，原始 blob 也未应用层加密。

依赖示例（按需要安装，不必全装）：
    pip install numpy sentence-transformers
    pip install openpyxl python-docx pymupdf pillow pytesseract
    pip install fastapi httpx
系统 OCR 还需安装 Tesseract；生产视觉理解更推荐注入 VisionDescriber。
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import unicodedata
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import (
    Any,
    BinaryIO,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)
from uuid import uuid4

logger = logging.getLogger(__name__)

_SCHEMA = "xueban.memory_bank/1"
_DB_VERSION = 1
_DEFAULT_ROOT = Path(os.getenv("XUEBAN_KNOWLEDGE_DIR", ".xueban/knowledge"))
_PACK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SHA_RE = re.compile(r"^[a-f0-9]{64}$")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-/#]*", re.UNICODE)
_IDENTIFIER_RE = re.compile(r"\b(?=[A-Za-z0-9_.\-/]{3,}\b)(?=\w*\d)[A-Za-z0-9_.\-/]+\b")
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log"}
_SUPPORTED_EXTENSIONS = (
    _TEXT_EXTENSIONS
    | _IMAGE_EXTENSIONS
    | {".csv", ".json", ".jsonl", ".html", ".htm", ".xlsx", ".xlsm", ".docx", ".pptx", ".pdf"}
)


# ---------------------------------------------------------------------------
# Public errors and configuration
# ---------------------------------------------------------------------------


class MemoryBankError(RuntimeError):
    """Safe, machine-readable module error; FastAPI adapter maps it to HTTP."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


class PackNotFound(MemoryBankError):
    def __init__(self, pack_id: str) -> None:
        super().__init__("PACK_NOT_FOUND", f"Knowledge pack not found: {pack_id}", status=404)


class SourceNotFound(MemoryBankError):
    def __init__(self, source_id: str) -> None:
        super().__init__("SOURCE_NOT_FOUND", f"Knowledge source not found: {source_id}", status=404)


@dataclass(frozen=True)
class MemoryConfig:
    """All limits are deliberately explicit; values are characters, not model tokens."""

    child_size: int = 420
    child_overlap: int = 64
    parent_size: int = 1800
    max_table_rows_per_parent: int = 24
    max_file_bytes: int = 64 * 1024 * 1024
    max_pdf_pages: int = 500
    max_images_per_document: int = 48
    max_table_rows: int = 200_000
    max_ooxml_uncompressed_bytes: int = 256 * 1024 * 1024
    candidate_multiplier: int = 6
    rrf_k: int = 60
    bm25_weight: float = 1.0
    vector_weight: float = 1.0
    embedding_batch_size: int = 32
    vector_cache_max_chunks: int = 100_000
    rerank_candidates: int = 40
    near_duplicate_threshold: float = 0.92

    def __post_init__(self) -> None:
        if self.child_size < 64 or self.parent_size < self.child_size:
            raise ValueError("Require 64 <= child_size <= parent_size")
        if not 0 <= self.child_overlap < self.child_size:
            raise ValueError("Require 0 <= child_overlap < child_size")
        if self.max_file_bytes <= 0 or self.max_table_rows <= 0:
            raise ValueError("File and table limits must be positive")
        if self.candidate_multiplier < 1 or self.rrf_k < 1:
            raise ValueError("Retrieval candidate settings must be positive")


@dataclass
class _Unit:
    kind: str
    text: str
    group_key: str
    context: Dict[str, Any]
    data: Dict[str, Any] = field(default_factory=dict)
    raw_sha256: str = ""
    raw_mime: str = ""
    raw_data: bytes = field(default=b"", repr=False)
    unit_id: str = field(default_factory=lambda: uuid4().hex)
    parent_id: str = ""


@dataclass
class _Parent:
    source_id: str
    ordinal: int
    text: str
    kind: str
    context: Dict[str, Any]
    units: List[_Unit]
    parent_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class _Child:
    parent_id: str
    source_id: str
    ordinal: int
    start_char: int
    end_char: int
    text: str
    kind: str
    context: Dict[str, Any]
    unit_ids: List[str]
    chunk_id: str = field(default_factory=lambda: uuid4().hex)
    embedding: Optional[Sequence[float]] = None


@dataclass
class _VectorCacheEntry:
    revision: int
    ids: List[str]
    matrix: Any
    created_at: float


# ---------------------------------------------------------------------------
# Replaceable model ports (dependency inversion; no vendor imports at module load)
# ---------------------------------------------------------------------------


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class VisionDescriber(Protocol):
    def describe(
        self, image_bytes: bytes, mime_type: str, context: str
    ) -> Union[str, Mapping[str, Any]]: ...


class Reranker(Protocol):
    def rerank(self, query: str, texts: Sequence[str]) -> Sequence[float]: ...


class SentenceTransformerEmbedder:
    """Local multilingual embeddings; recommended default model: BAAI/bge-m3."""

    def __init__(self, model_name: str = "BAAI/bge-m3", device: Optional[str] = None) -> None:
        self.name = f"sentence-transformers:{model_name}"
        self._model_name = model_name
        self._device = device
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise MemoryBankError(
                    "DEPENDENCY_MISSING",
                    "Install sentence-transformers to use SentenceTransformerEmbedder",
                    status=500,
                ) from exc
            self._model = SentenceTransformer(self._model_name, device=self._device)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return []
        return self._load().encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        ).tolist()

    def embed_query(self, text: str) -> Sequence[float]:
        return self.embed_documents([text])[0]


class OpenAICompatibleEmbedder:
    """OpenAI-compatible /embeddings adapter (also works with compatible local gateways)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 60.0,
        batch_size: int = 32,
    ) -> None:
        self.name = f"openai-compatible:{model}"
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._batch_size = max(1, batch_size)

    def _request(self, texts: Sequence[str]) -> List[List[float]]:
        try:
            import httpx
        except ImportError as exc:
            raise MemoryBankError(
                "DEPENDENCY_MISSING", "Install httpx to use OpenAICompatibleEmbedder", status=500
            ) from exc
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        output: List[List[float]] = []
        try:
            # 本机代理变量可能不适用于外部服务；Embedding 按用户配置的地址直连。
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                for start in range(0, len(texts), self._batch_size):
                    batch = list(texts[start : start + self._batch_size])
                    response = client.post(
                        f"{self._base_url}/embeddings",
                        headers=headers,
                        json={"model": self._model, "input": batch},
                    )
                    response.raise_for_status()
                    rows = sorted(response.json()["data"], key=lambda row: row["index"])
                    output.extend([list(map(float, row["embedding"])) for row in rows])
        except Exception as exc:
            if isinstance(exc, MemoryBankError):
                raise
            raise MemoryBankError(
                "EMBEDDING_FAILED", f"Embedding request failed: {type(exc).__name__}: {exc}", status=502
            ) from exc
        return output

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return self._request(texts) if texts else []

    def embed_query(self, text: str) -> Sequence[float]:
        return self._request([text])[0]


class CrossEncoderReranker:
    """Optional precision stage; BGE v2 m3 is suitable for multilingual business text."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: Optional[str] = None) -> None:
        self._model_name = model_name
        self._device = device
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise MemoryBankError(
                    "DEPENDENCY_MISSING", "Install sentence-transformers to use reranking", status=500
                ) from exc
            self._model = CrossEncoder(self._model_name, device=self._device)
        return self._model

    def rerank(self, query: str, texts: Sequence[str]) -> Sequence[float]:
        if not texts:
            return []
        scores = self._load().predict([(query, text) for text in texts])
        return [float(score) for score in scores]


class OpenAIVisionDescriber:
    """Vision-to-retrieval-text adapter; output must distinguish OCR from inference."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 90.0,
        max_tokens: int = 1200,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_tokens = max_tokens

    def describe(self, image_bytes: bytes, mime_type: str, context: str) -> Mapping[str, Any]:
        try:
            import httpx
        except ImportError as exc:
            raise MemoryBankError(
                "DEPENDENCY_MISSING", "Install httpx to use OpenAIVisionDescriber", status=500
            ) from exc
        mime_type = mime_type if mime_type.startswith("image/") else "image/png"
        encoded = base64.b64encode(image_bytes).decode("ascii")
        prompt = (
            "你是商业知识库的多模态解析器。只描述图中可见事实，不猜测。"
            "返回 JSON：description（详细、可检索的中文描述）、ocr_text（尽量逐字保留数字、"
            "编号、表格与单位，没有则为空）、tags（短字符串数组）。"
            "若文字无法辨认，明确写无法辨认，不得补造。文档邻近上下文仅供定位："
            + context[:1600]
        )
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
                ],
            }],
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(f"{self._base_url}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            raise MemoryBankError(
                "VISION_FAILED", f"Vision request failed: {type(exc).__name__}: {exc}", status=502
            ) from exc
        try:
            cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
            parsed = json.loads(cleaned)
            return parsed if isinstance(parsed, dict) else {"description": str(parsed)}
        except (ValueError, TypeError):
            return {"description": str(content), "ocr_text": "", "tags": []}


class TesseractVisionDescriber:
    """Offline OCR fallback, not a semantic image captioner."""

    def describe(self, image_bytes: bytes, mime_type: str, context: str) -> Mapping[str, Any]:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:
            raise MemoryBankError(
                "DEPENDENCY_MISSING", "Install pillow and pytesseract for OCR", status=500
            ) from exc
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                text = pytesseract.image_to_string(image, lang=os.getenv("MEMORY_OCR_LANG", "eng"))
        except Exception as exc:
            raise MemoryBankError(
                "OCR_FAILED",
                f"Tesseract OCR failed; install the binary and language data: {type(exc).__name__}: {exc}",
                status=500,
            ) from exc
        return {"description": "", "ocr_text": text, "tags": ["ocr"]}


class RapidOcrVisionDescriber:
    """Offline OCR via RapidOCR/ONNXRuntime; ships its own Chinese+English models.

    Unlike Tesseract it needs no system binary, so image ingestion works out of
    the box. Output is literal OCR text only — it never invents a description.
    """

    def __init__(self) -> None:
        self._engine: Any = None

    def _engine_or_raise(self) -> Any:
        if self._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise MemoryBankError(
                    "DEPENDENCY_MISSING",
                    "Install rapidocr-onnxruntime for offline OCR",
                    status=500,
                ) from exc
            self._engine = RapidOCR()
        return self._engine

    def describe(self, image_bytes: bytes, mime_type: str, context: str) -> Mapping[str, Any]:
        try:
            import numpy as np
            from PIL import Image
        except ImportError as exc:
            raise MemoryBankError(
                "DEPENDENCY_MISSING", "Install pillow and numpy for offline OCR", status=500
            ) from exc
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                array = np.array(image.convert("RGB"))
            result, _elapsed = self._engine_or_raise()(array)
        except MemoryBankError:
            raise
        except Exception as exc:
            raise MemoryBankError(
                "OCR_FAILED", f"Offline OCR failed: {type(exc).__name__}: {exc}", status=500
            ) from exc
        lines = [
            str(item[1]).strip()
            for item in (result or [])
            if isinstance(item, (list, tuple)) and len(item) > 1 and str(item[1]).strip()
        ]
        return {"description": "", "ocr_text": "\n".join(lines), "tags": ["ocr"]}


# ---------------------------------------------------------------------------
# Small deterministic helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load_json(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def _validate_pack_id(pack_id: str) -> None:
    if not isinstance(pack_id, str) or not _PACK_ID_RE.fullmatch(pack_id):
        raise MemoryBankError("INVALID_PACK_ID", "pack_id must be a 32-character lowercase UUID hex")


def _validate_sha(value: str) -> None:
    if not _SHA_RE.fullmatch(value):
        raise MemoryBankError("INVALID_BLOB_ID", "Blob id must be a SHA-256 hex string")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _fts_terms(text: str) -> List[str]:
    """Mixed English/number tokens and Chinese bigrams; avoids unicode61 CJK limitation."""
    terms: List[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if word.strip("._-/#"):
            terms.append(word[:128])
    for run in _CJK_RUN_RE.findall(text):
        run = run[:256]
        if len(run) == 1:
            terms.append(run)
        else:
            terms.extend(run[i : i + 2] for i in range(len(run) - 1))
            if len(run) <= 12:
                terms.append(run)
    return list(dict.fromkeys(terms))


def _fts_text(text: str) -> str:
    return " ".join(_fts_terms(text))


def _fts_query(text: str) -> str:
    terms = _fts_terms(text)
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:96])


def _lexical_tokens(text: str) -> set[str]:
    return set(_fts_terms(text))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _normalize_vector(vector: Sequence[float]) -> List[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if not values or norm == 0 or not math.isfinite(norm):
        raise MemoryBankError("INVALID_EMBEDDING", "Embedder returned an empty or invalid vector", status=500)
    return [value / norm for value in values]


def _vector_blob(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _safe_suffix(filename: str) -> str:
    # Accept both a filename and an already-normalized extension such as '.png'.
    if re.fullmatch(r"\.[a-z0-9]{1,10}", filename.lower()):
        return filename.lower()
    suffix = Path(filename).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"


def _read_limited(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise MemoryBankError("FILE_TOO_LARGE", f"File exceeds {limit} bytes", status=413)
    return data


def _check_ooxml(data: bytes, max_uncompressed_bytes: int = 256 * 1024 * 1024) -> None:
    """Reject path traversal, excessive expansion and excessive member counts."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000:
                raise MemoryBankError("OOXML_LIMIT", "OOXML contains too many members", status=413)
            total = sum(info.file_size for info in infos)
            compressed = max(1, sum(info.compress_size for info in infos))
            if total > max_uncompressed_bytes or total / compressed > 250:
                raise MemoryBankError("OOXML_LIMIT", "OOXML expansion ratio or size is unsafe", status=413)
            for info in infos:
                path = Path(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise MemoryBankError("OOXML_UNSAFE", "OOXML contains an unsafe member path")
    except zipfile.BadZipFile as exc:
        raise MemoryBankError("INVALID_OOXML", "Invalid OOXML/ZIP document") from exc


def _paragraphs(text: str) -> List[str]:
    return [_clean_text(part) for part in re.split(r"\n\s*\n", text) if _clean_text(part)]


def _text_units(text: str, *, context: Optional[Dict[str, Any]] = None, group: str = "正文") -> List[_Unit]:
    """Markdown/heading-aware paragraphs; heading path is repeated into retrieval text."""
    units: List[_Unit] = []
    headings: List[Tuple[int, str]] = []
    buffer: List[str] = []

    def flush() -> None:
        nonlocal buffer
        content = _clean_text("\n".join(buffer))
        buffer = []
        if not content:
            return
        path = [title for _, title in headings]
        prefix = " > ".join(path)
        enriched = f"章节：{prefix}\n{content}" if prefix else content
        data = dict(context or {})
        data["heading_path"] = path
        units.append(_Unit("text", enriched, group + "::" + prefix, data, data={"heading_path": path}))

    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, match.group(2).strip()))
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()
    return units


def _row_text(source_name: str, sheet: str, row_number: int, headers: Sequence[str], values: Sequence[Any]) -> str:
    pairs = []
    for index, value in enumerate(values):
        if value is None or str(value).strip() == "":
            continue
        header = str(headers[index]).strip() if index < len(headers) and str(headers[index]).strip() else f"列{index + 1}"
        pairs.append(f"{header}: {value}")
    return f"来源：{source_name}｜工作表：{sheet}｜行：{row_number}\n" + "｜".join(pairs)


def _modality_boost(query: str, kind: str) -> float:
    query_lower = query.lower()
    table_cues = ("表", "表格", "excel", "sheet", "行", "列", "csv", "table")
    image_cues = ("图", "图片", "图像", "照片", "截图", "流程图", "chart", "figure", "image")
    if any(cue in query_lower for cue in table_cues) and kind == "table_row":
        return 1.12
    if any(cue in query_lower for cue in image_cues) and kind == "image":
        return 1.15
    return 1.0


def _sliding_spans(text: str, size: int, overlap: int) -> List[Tuple[int, int]]:
    """Bounded windows that prefer paragraph/sentence boundaries and always advance."""
    if not text:
        return []
    if len(text) <= size:
        return [(0, len(text))]
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + size)
        end = hard_end
        if hard_end < len(text):
            floor = start + max(1, size // 2)
            candidates = [
                text.rfind(separator, floor, hard_end)
                for separator in ("\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ")
            ]
            boundary = max(candidates)
            if boundary >= floor:
                end = boundary + (2 if hard_end - boundary >= 2 else 1)
                end = min(end, hard_end)
        end = max(end, start + 1)
        spans.append((start, end))
        if end >= len(text):
            break
        next_start = max(0, end - overlap)
        if next_start <= start:
            next_start = start + max(1, size - overlap)
        start = next_start
    return spans


def _flatten_json(value: Any, prefix: str = "", output: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    output = {} if output is None else output
    if len(output) >= 500:
        return output
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten_json(item, path, output)
    elif isinstance(value, list):
        if len(value) <= 30 and all(not isinstance(item, (dict, list)) for item in value):
            output[prefix] = value
        else:
            for index, item in enumerate(value):
                _flatten_json(item, f"{prefix}[{index}]", output)
    else:
        output[prefix or "value"] = value
    return output


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style", "noscript", "template"}:
            self.skip_depth += 1
        elif tag in {"p", "div", "section", "article", "h1", "h2", "h3", "tr", "li", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "section", "article", "h1", "h2", "h3", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return _clean_text("".join(self.parts))


class MemoryBank:
    """High-cohesion storage/retrieval service. One instance is safe for thread-based FastAPI use."""

    def __init__(
        self,
        root: Union[str, Path] = _DEFAULT_ROOT,
        *,
        config: Optional[MemoryConfig] = None,
        embedder: Optional[Embedder] = None,
        vision: Optional[VisionDescriber] = None,
        reranker: Optional[Reranker] = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config or MemoryConfig()
        self.embedder = embedder
        self.vision = vision
        self.reranker = reranker
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._vector_cache: Dict[str, _VectorCacheEntry] = {}
        self._vector_cache_guard = threading.Lock()

    # -- paths, locks and persistence -------------------------------------

    def _lock(self, pack_id: str) -> threading.RLock:
        with self._locks_guard:
            if pack_id not in self._locks:
                self._locks[pack_id] = threading.RLock()
            return self._locks[pack_id]

    def _pack_dir(self, pack_id: str) -> Path:
        _validate_pack_id(pack_id)
        return self.root / pack_id

    def _manifest_path(self, pack_id: str) -> Path:
        return self._pack_dir(pack_id) / "memory_bank.json"

    def _db_path(self, pack_id: str) -> Path:
        return self._pack_dir(pack_id) / "memory_bank.sqlite"

    def _load_manifest(self, pack_id: str) -> Dict[str, Any]:
        path = self._manifest_path(pack_id)
        if not path.exists():
            raise PackNotFound(pack_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MemoryBankError("PACK_CORRUPT", f"Cannot read pack manifest: {exc}", status=500) from exc
        if payload.get("schema") != _SCHEMA or payload.get("pack_id") != pack_id:
            raise MemoryBankError("PACK_SCHEMA_MISMATCH", "Unsupported or inconsistent pack manifest", status=500)
        return payload

    def _save_manifest(self, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = _now()
        _atomic_write(
            self._manifest_path(manifest["pack_id"]),
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        )

    def _connect(self, pack_id: str, *, initialize: bool = False) -> sqlite3.Connection:
        path = self._db_path(pack_id)
        if not initialize and not path.exists():
            raise PackNotFound(pack_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        if initialize:
            self._initialize_db(connection)
        return connection

    def _initialize_db(self, connection: sqlite3.Connection) -> None:
        existing_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if existing_version not in (0, _DB_VERSION):
            connection.close()
            raise MemoryBankError(
                "DB_SCHEMA_MISMATCH", f"Unsupported database version: {existing_version}", status=500
            )
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                supersedes TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
                is_latest INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error_message TEXT NOT NULL DEFAULT '',
                unit_count INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(filename, version);
            CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(raw_sha256);
            CREATE INDEX IF NOT EXISTS idx_sources_latest ON sources(is_latest, status);

            CREATE TABLE IF NOT EXISTS parents (
                parent_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                context_json TEXT NOT NULL,
                unit_ids_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_parents_source ON parents(source_id, ordinal);

            CREATE TABLE IF NOT EXISTS units (
                unit_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                parent_id TEXT REFERENCES parents(parent_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                group_key TEXT NOT NULL,
                context_json TEXT NOT NULL,
                data_json TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL DEFAULT '',
                raw_mime TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_units_source ON units(source_id, ordinal);
            CREATE INDEX IF NOT EXISTS idx_units_parent ON units(parent_id);

            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL REFERENCES parents(parent_id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                kind TEXT NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL,
                text TEXT NOT NULL,
                context_json TEXT NOT NULL,
                unit_ids_json TEXT NOT NULL,
                embedding BLOB,
                embedding_dim INTEGER,
                embedding_model TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
            CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED,
                index_text,
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TABLE IF NOT EXISTS links (
                src_unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
                dst_unit_id TEXT NOT NULL REFERENCES units(unit_id) ON DELETE CASCADE,
                relation TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY(src_unit_id, dst_unit_id, relation)
            );
            INSERT OR IGNORE INTO meta(key, value) VALUES ('revision', '0');
            PRAGMA user_version = 1;
            """
        )
        connection.commit()

    @contextmanager
    def _db(self, pack_id: str) -> Iterator[sqlite3.Connection]:
        connection = self._connect(pack_id)
        try:
            yield connection
        finally:
            connection.close()

    def _invalidate_vector_cache(self, pack_id: str) -> None:
        with self._vector_cache_guard:
            self._vector_cache.pop(pack_id, None)

    def _bump_revision(self, connection: sqlite3.Connection) -> None:
        connection.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")

    def _store_blob(self, pack_id: str, data: bytes, suffix: str, expected_sha: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha and digest != expected_sha:
            raise MemoryBankError("BLOB_CHECKSUM_MISMATCH", "Blob checksum mismatch", status=500)
        target = self._pack_dir(pack_id) / "blobs" / digest[:2] / f"{digest}{_safe_suffix(suffix)}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, temp_name = tempfile.mkstemp(prefix=".blob.", dir=str(target.parent))
            temp = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
        return digest

    def get_blob(self, pack_id: str, sha256: str) -> Tuple[bytes, str]:
        """Read an immutable original/extracted blob without exposing arbitrary filesystem paths."""
        self._load_manifest(pack_id)
        _validate_sha(sha256)
        folder = self._pack_dir(pack_id) / "blobs" / sha256[:2]
        matches = list(folder.glob(f"{sha256}.*")) if folder.exists() else []
        if not matches:
            raise MemoryBankError("BLOB_NOT_FOUND", f"Blob not found: {sha256}", status=404)
        path = matches[0]
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    # -- pack/source public API -------------------------------------------

    def create_pack(
        self,
        name: str,
        description: str = "",
        *,
        pack_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        pack_id = pack_id or uuid4().hex
        _validate_pack_id(pack_id)
        if not name or not name.strip():
            raise MemoryBankError("INVALID_PACK_NAME", "Pack name cannot be empty")
        with self._lock(pack_id):
            if self._manifest_path(pack_id).exists():
                raise MemoryBankError("PACK_EXISTS", f"Pack already exists: {pack_id}", status=409)
            now = _now()
            manifest = {
                "schema": _SCHEMA,
                "pack_id": pack_id,
                "name": name.strip(),
                "description": description,
                "status": "ready",
                "retrieval_profile": {
                    "mode": "hybrid" if self.embedder else "keyword",
                    "top_k": 5,
                    "child_size": self.config.child_size,
                    "child_overlap": self.config.child_overlap,
                    "parent_size": self.config.parent_size,
                },
                "embedding_model": getattr(self.embedder, "name", "") if self.embedder else "",
                "metadata": dict(metadata or {}),
                "stats": {"sources": 0, "units": 0, "parents": 0, "chunks": 0, "bytes": 0},
                "created_at": now,
                "updated_at": now,
            }
            self._pack_dir(pack_id).mkdir(parents=True, exist_ok=True)
            connection = self._connect(pack_id, initialize=True)
            connection.close()
            self._save_manifest(manifest)
            return manifest

    def ensure_pack(self, pack_id: str, name: str = "KnowledgePack") -> Dict[str, Any]:
        """Attach to an existing Xueban pack_id, creating only the sidecar bank if absent."""
        try:
            return self._load_manifest(pack_id)
        except PackNotFound:
            return self.create_pack(name, pack_id=pack_id)

    def list_packs(self) -> List[Dict[str, Any]]:
        packs: List[Dict[str, Any]] = []
        for path in self.root.glob("*/memory_bank.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("schema") == _SCHEMA:
                    packs.append(payload)
            except (OSError, ValueError):
                logger.warning("Skipping corrupt memory bank manifest: %s", path)
        return sorted(packs, key=lambda item: item.get("created_at", ""), reverse=True)

    def get_pack(self, pack_id: str) -> Dict[str, Any]:
        manifest = self._load_manifest(pack_id)
        manifest["stats"] = self.stats(pack_id)
        return manifest

    def update_pack(
        self,
        pack_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Atomically update display fields without touching sources or the index."""
        with self._lock(pack_id):
            manifest = self._load_manifest(pack_id)
            if name is not None:
                name = name.strip()
                if not name:
                    raise MemoryBankError("INVALID_PACK_NAME", "Pack name cannot be empty")
                manifest["name"] = name
            if description is not None:
                manifest["description"] = description
            self._save_manifest(manifest)
            return self.get_pack(pack_id)

    def stats(self, pack_id: str) -> Dict[str, int]:
        self._load_manifest(pack_id)
        with self._db(pack_id) as connection:
            row = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM sources WHERE status = 'done') AS sources,
                  (SELECT COUNT(*) FROM units) AS units,
                  (SELECT COUNT(*) FROM parents) AS parents,
                  (SELECT COUNT(*) FROM chunks) AS chunks,
                  COALESCE((SELECT SUM(size_bytes) FROM sources WHERE status = 'done'), 0) AS bytes
                """
            ).fetchone()
            return {key: int(row[key]) for key in row.keys()}

    def delete_pack(self, pack_id: str) -> None:
        """Delete only this module's sidecar files; preserve legacy pack.json/graph/index files."""
        self._load_manifest(pack_id)
        with self._lock(pack_id):
            for path in (
                self._db_path(pack_id),
                self._pack_dir(pack_id) / "memory_bank.sqlite-wal",
                self._pack_dir(pack_id) / "memory_bank.sqlite-shm",
                self._manifest_path(pack_id),
            ):
                path.unlink(missing_ok=True)
            shutil.rmtree(self._pack_dir(pack_id) / "blobs", ignore_errors=True)
            self._invalidate_vector_cache(pack_id)
            try:
                self._pack_dir(pack_id).rmdir()
            except OSError:
                pass

    def list_sources(self, pack_id: str, *, include_history: bool = False) -> List[Dict[str, Any]]:
        self._load_manifest(pack_id)
        where = "" if include_history else "WHERE is_latest = 1 OR status = 'failed'"
        with self._db(pack_id) as connection:
            rows = connection.execute(
                f"SELECT * FROM sources {where} ORDER BY created_at DESC"
            ).fetchall()
            return [self._source_dict(row) for row in rows]

    def get_source(self, pack_id: str, source_id: str) -> Dict[str, Any]:
        with self._db(pack_id) as connection:
            row = connection.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
            if row is None:
                raise SourceNotFound(source_id)
            return self._source_dict(row)

    def _source_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "source_id": row["source_id"],
            "filename": row["filename"],
            "source_name": row["filename"],
            "extension": row["extension"],
            "mime_type": row["mime_type"],
            "checksum": row["raw_sha256"],
            "size_bytes": row["size_bytes"],
            "ingest_status": "done" if row["status"] == "done" else row["status"],
            "version": row["version"],
            "supersedes": row["supersedes"],
            "is_latest": bool(row["is_latest"]),
            "metadata": _load_json(row["metadata_json"], {}),
            "warnings": _load_json(row["warnings_json"], []),
            "error_message": row["error_message"],
            "unit_count": row["unit_count"],
            "chunk_count": row["chunk_count"],
            "imported_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def delete_source(self, pack_id: str, source_id: str) -> None:
        with self._lock(pack_id), self._db(pack_id) as connection:
            row = connection.execute(
                "SELECT filename, raw_sha256, is_latest FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if row is None:
                raise SourceNotFound(source_id)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))
            if row["is_latest"]:
                previous = connection.execute(
                    """SELECT source_id FROM sources
                       WHERE filename = ? AND status = 'done'
                       ORDER BY version DESC LIMIT 1""",
                    (row["filename"],),
                ).fetchone()
                if previous:
                    connection.execute(
                        "UPDATE sources SET is_latest = 1, updated_at = ? WHERE source_id = ?",
                        (_now(), previous["source_id"]),
                    )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM sources WHERE raw_sha256 = ?", (row["raw_sha256"],)
            ).fetchone()[0]
            self._bump_revision(connection)
            connection.commit()
            self._invalidate_vector_cache(pack_id)
            if not remaining:
                folder = self._pack_dir(pack_id) / "blobs" / row["raw_sha256"][:2]
                for path in folder.glob(f"{row['raw_sha256']}.*") if folder.exists() else []:
                    path.unlink(missing_ok=True)
            self._refresh_manifest(pack_id, connection)

    def _refresh_manifest(self, pack_id: str, connection: sqlite3.Connection) -> None:
        manifest = self._load_manifest(pack_id)
        manifest["stats"] = self.stats(pack_id)
        failed = connection.execute("SELECT COUNT(*) FROM sources WHERE status = 'failed'").fetchone()[0]
        processing = connection.execute("SELECT COUNT(*) FROM sources WHERE status = 'processing'").fetchone()[0]
        manifest["status"] = "indexing" if processing else ("degraded" if failed else "ready")
        self._save_manifest(manifest)

    # -- parsing -----------------------------------------------------------

    def _default_vision_describer(self) -> VisionDescriber:
        """Prefer Tesseract when its binary is installed, else bundled RapidOCR."""
        try:
            import pytesseract  # noqa: F401

            return TesseractVisionDescriber()
        except Exception:
            pass
        try:
            import rapidocr_onnxruntime  # noqa: F401

            return RapidOcrVisionDescriber()
        except Exception:
            # 保留原错误语义：没有可用 OCR 时明确报 DEPENDENCY_MISSING。
            return TesseractVisionDescriber()

    def _describe_image(self, data: bytes, mime_type: str, context: str, group: str) -> _Unit:
        if self.vision is None:
            describer: VisionDescriber = self._default_vision_describer()
        else:
            describer = self.vision
        result = describer.describe(data, mime_type, context)
        if isinstance(result, str):
            result = {"description": result, "ocr_text": "", "tags": []}
        description = _clean_text(str(result.get("description", "")))
        ocr_text = _clean_text(str(result.get("ocr_text", "")))
        tags = [str(tag) for tag in result.get("tags", []) if str(tag).strip()][:30]
        if not description and not ocr_text:
            raise MemoryBankError(
                "VISION_REQUIRED",
                "Image produced no searchable content; configure a VisionDescriber or OCR language data",
                status=422,
            )
        text = "类型：图片\n"
        if context:
            text += f"邻近上下文：{context[:1800]}\n"
        if description:
            text += f"可见内容：{description}\n"
        if ocr_text:
            text += f"图中文字（OCR）：{ocr_text}\n"
        if tags:
            text += "标签：" + "、".join(tags)
        digest = hashlib.sha256(data).hexdigest()
        return _Unit(
            "image",
            text.strip(),
            group,
            {"context": context[:1800]},
            data={"vision": dict(result), "caption": description, "ocr_text": ocr_text, "tags": tags},
            raw_sha256=digest,
            raw_mime=mime_type,
            raw_data=data,
        )

    def _try_describe_image(self, data: bytes, mime_type: str, context: str, group: str) -> Optional[_Unit]:
        """Best-effort image understanding for images embedded in documents.

        A document must still ingest when no vision backend is configured: the
        image simply contributes nothing instead of failing the whole file.
        """
        try:
            return self._describe_image(data, mime_type, context, group)
        except MemoryBankError:
            return None

    def _parse_csv(self, filename: str, data: bytes) -> List[_Unit]:
        text = _decode_text(data)
        try:
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.reader(io.StringIO(text), dialect)
        try:
            headers = [str(value).strip() for value in next(reader)]
        except StopIteration:
            return []
        units: List[_Unit] = []
        for row_number, row in enumerate(reader, start=2):
            if len(units) >= self.config.max_table_rows:
                raise MemoryBankError("TABLE_LIMIT", "CSV exceeds max_table_rows", status=413)
            if not any(str(value).strip() for value in row):
                continue
            values: Dict[str, Any] = {}
            for index, value in enumerate(row):
                key = headers[index] if index < len(headers) and headers[index] else f"列{index + 1}"
                values[key] = value
            units.append(_Unit(
                "table_row",
                _row_text(filename, "CSV", row_number, headers, row),
                "CSV::main",
                {"sheet": "CSV", "row": row_number},
                data={"sheet": "CSV", "row": row_number, "headers": headers, "values": values},
            ))
        return units

    def _parse_excel(self, filename: str, data: bytes) -> List[_Unit]:
        _check_ooxml(data, self.config.max_ooxml_uncompressed_bytes)
        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install openpyxl to parse xlsx/xlsm", status=500) from exc
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        units: List[_Unit] = []
        try:
            for sheet in workbook.worksheets:
                headers: List[str] = []
                for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    values = list(row)
                    if not any(value is not None and str(value).strip() for value in values):
                        continue
                    if not headers:
                        headers = [str(value).strip() if value is not None else "" for value in values]
                        continue
                    if len(units) >= self.config.max_table_rows:
                        raise MemoryBankError("TABLE_LIMIT", "Workbook exceeds max_table_rows", status=413)
                    record = {
                        headers[index] if index < len(headers) and headers[index] else f"列{index + 1}": value
                        for index, value in enumerate(values)
                        if value is not None and str(value).strip()
                    }
                    units.append(_Unit(
                        "table_row",
                        _row_text(filename, sheet.title, row_number, headers, values),
                        f"EXCEL::{sheet.title}",
                        {"sheet": sheet.title, "row": row_number},
                        data={"sheet": sheet.title, "row": row_number, "headers": headers, "values": record},
                    ))
        finally:
            workbook.close()
        return units

    def _parse_json(self, filename: str, data: bytes, jsonl: bool = False) -> List[_Unit]:
        text = _decode_text(data)
        try:
            if jsonl:
                records = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                parsed = json.loads(text)
                records = parsed if isinstance(parsed, list) else [parsed]
        except ValueError as exc:
            raise MemoryBankError("INVALID_JSON", f"Invalid JSON/JSONL: {exc}") from exc
        units: List[_Unit] = []
        for index, record in enumerate(records):
            flattened = _flatten_json(record)
            body = "｜".join(f"{key}: {value}" for key, value in flattened.items())
            units.append(_Unit(
                "record",
                f"来源：{filename}｜记录：{index + 1}\n{body}",
                "JSON::records",
                {"record_index": index + 1},
                data={"record_index": index + 1, "record": record, "flattened": flattened},
            ))
        return units

    def _parse_docx(self, filename: str, data: bytes) -> List[_Unit]:
        _check_ooxml(data, self.config.max_ooxml_uncompressed_bytes)
        try:
            from docx import Document
            from docx.table import Table
            from docx.text.paragraph import Paragraph
            from docx.oxml.table import CT_Tbl
            from docx.oxml.text.paragraph import CT_P
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install python-docx to parse docx", status=500) from exc
        document = Document(io.BytesIO(data))
        units: List[_Unit] = []
        headings: List[Tuple[int, str]] = []

        def add_paragraph(paragraph: Any) -> None:
            text = _clean_text(paragraph.text)
            if not text:
                return
            style = str(paragraph.style.name if paragraph.style else "")
            heading = re.match(r"(?:Heading|标题)\s*([1-6])", style, re.I)
            if heading:
                level = int(heading.group(1))
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, text))
                return
            path = [title for _, title in headings]
            enriched = f"章节：{' > '.join(path)}\n{text}" if path else text
            units.append(_Unit(
                "text", enriched, "DOCX::" + " > ".join(path),
                {"heading_path": path}, data={"heading_path": path},
            ))

        for element in document.element.body.iterchildren():
            if isinstance(element, CT_P):
                add_paragraph(Paragraph(element, document))
            elif isinstance(element, CT_Tbl):
                table = Table(element, document)
                rows = [[_clean_text(cell.text) for cell in row.cells] for row in table.rows]
                if not rows:
                    continue
                headers = rows[0]
                for row_number, values in enumerate(rows[1:], start=2):
                    if not any(values):
                        continue
                    units.append(_Unit(
                        "table_row",
                        _row_text(filename, "Word表格", row_number, headers, values),
                        "DOCX::table",
                        {"sheet": "Word表格", "row": row_number},
                        data={"row": row_number, "headers": headers, "values": dict(zip(headers, values))},
                    ))

        seen_images: set[str] = set()
        for relationship in document.part.rels.values():
            if not relationship.reltype.endswith("/image") or relationship.is_external:
                continue
            part = relationship.target_part
            blob = part.blob
            digest = hashlib.sha256(blob).hexdigest()
            if digest in seen_images:
                continue
            seen_images.add(digest)
            if len(seen_images) > self.config.max_images_per_document:
                break
            context = "；".join(title for _, title in headings) or f"Word 文档 {filename}"
            unit = self._try_describe_image(blob, part.content_type, context, "DOCX::images")
            if unit is not None:
                units.append(unit)
        return units

    def _parse_pptx(self, filename: str, data: bytes) -> List[_Unit]:
        _check_ooxml(data, self.config.max_ooxml_uncompressed_bytes)
        try:
            from pptx import Presentation
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install python-pptx to parse pptx", status=500) from exc
        try:
            presentation = Presentation(io.BytesIO(data))
        except Exception as exc:
            raise MemoryBankError("INVALID_PPTX", f"Cannot open PPTX: {exc}") from exc
        units: List[_Unit] = []
        image_count = 0
        seen_images: set[str] = set()
        for index, slide in enumerate(presentation.slides, start=1):
            group = f"PPTX::slide-{index}"
            texts: List[str] = []
            title = ""
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    text = _clean_text("\n".join(p.text for p in shape.text_frame.paragraphs))
                    if text:
                        texts.append(text)
                        if not title:
                            title = text.splitlines()[0][:80]
                if getattr(shape, "has_table", False):
                    rows = [[_clean_text(cell.text) for cell in row.cells] for row in shape.table.rows]
                    if rows:
                        headers = rows[0]
                        for row_number, values in enumerate(rows[1:], start=2):
                            if not any(values):
                                continue
                            units.append(_Unit(
                                "table_row",
                                _row_text(filename, f"幻灯片{index}表格", row_number, headers, values),
                                group + "::table",
                                {"sheet": f"幻灯片{index}", "row": row_number},
                                data={"row": row_number, "headers": headers, "values": dict(zip(headers, values))},
                            ))
                if getattr(shape, "shape_type", None) == 13 and hasattr(shape, "image"):
                    if image_count >= self.config.max_images_per_document:
                        continue
                    try:
                        blob = shape.image.blob
                        digest = hashlib.sha256(blob).hexdigest()
                        if digest in seen_images:
                            continue
                        seen_images.add(digest)
                        image_count += 1
                        unit = self._try_describe_image(
                            blob, getattr(shape.image, "content_type", "") or "image/png",
                            title or f"{filename} 第 {index} 页", group + "::images",
                        )
                        if unit is not None:
                            units.append(unit)
                    except Exception as exc:
                        logger.warning("Skipping unreadable PPTX image on slide %s: %s", index, exc)
            if texts:
                units.append(_Unit(
                    "text", f"幻灯片 {index}" + (f"（{title}）" if title else "") + "：\n" + "\n".join(texts),
                    group, {"slide": index, "title": title}, data={"slide": index, "title": title},
                ))
            if getattr(slide, "has_notes_slide", False):
                notes_frame = getattr(slide.notes_slide, "notes_text_frame", None)
                notes = _clean_text(notes_frame.text) if notes_frame is not None else ""
                if notes:
                    units.append(_Unit(
                        "text", f"幻灯片 {index} 备注：{notes}", group + "::notes", {"slide": index},
                        data={"slide": index, "notes": True},
                    ))
        return units

    def _parse_pdf(self, filename: str, data: bytes) -> List[_Unit]:
        try:
            import pymupdf
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install pymupdf to parse PDF", status=500) from exc
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise MemoryBankError("INVALID_PDF", f"Cannot open PDF (encrypted/corrupt): {exc}") from exc
        units: List[_Unit] = []
        image_count = 0
        seen_images: set[str] = set()
        try:
            if document.needs_pass:
                raise MemoryBankError("ENCRYPTED_PDF", "Encrypted PDF must be decrypted before ingest")
            if len(document) > self.config.max_pdf_pages:
                raise MemoryBankError("PDF_PAGE_LIMIT", "PDF exceeds max_pdf_pages", status=413)
            for page_index, page in enumerate(document):
                page_number = page_index + 1
                blocks = page.get_text("blocks", sort=True)
                page_text = _clean_text("\n\n".join(
                    str(block[4]) for block in blocks if len(block) > 4 and str(block[4]).strip()
                ))
                page_group = f"PDF::page-{page_number}"
                for paragraph in _paragraphs(page_text):
                    units.append(_Unit(
                        "text", f"页码：{page_number}\n{paragraph}", page_group,
                        {"page": page_number}, data={"page": page_number},
                    ))
                for image_info in page.get_images(full=True):
                    if image_count >= self.config.max_images_per_document:
                        break
                    xref = image_info[0]
                    try:
                        extracted = document.extract_image(xref)
                        blob = extracted["image"]
                        if len(blob) < 4096 or extracted.get("width", 0) < 128 or extracted.get("height", 0) < 128:
                            continue
                        digest = hashlib.sha256(blob).hexdigest()
                        if digest in seen_images:
                            continue
                        seen_images.add(digest)
                        image_count += 1
                        context = f"{filename} 第 {page_number} 页。{page_text[:1200]}"
                        unit = self._try_describe_image(
                            blob, extracted.get("ext", "png") and f"image/{extracted.get('ext', 'png')}",
                            context, page_group + "::images",
                        )
                        if unit is None:
                            continue
                        unit.context["page"] = page_number
                        unit.data["page"] = page_number
                        units.append(unit)
                    except Exception as exc:
                        logger.warning("Skipping unreadable PDF image on page %s: %s", page_number, exc)
                if not page_text and image_count < self.config.max_images_per_document:
                    # Scanned page: render and OCR/describe the whole page.
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1.7, 1.7), alpha=False)
                    blob = pixmap.tobytes("png")
                    unit = self._try_describe_image(
                        blob, "image/png", f"{filename} 的扫描页，第 {page_number} 页", page_group + "::scan",
                    )
                    if unit is None:
                        units.append(_Unit(
                            "text",
                            f"页码：{page_number}\n（扫描页：当前未配置 OCR / 视觉模型，无法提取文字内容）",
                            page_group,
                            {"page": page_number},
                            data={"page": page_number, "scanned": True},
                        ))
                        continue
                    unit.context["page"] = page_number
                    unit.data["page"] = page_number
                    units.append(unit)
                    image_count += 1
        finally:
            document.close()
        return units

    def _parse_units(self, filename: str, data: bytes, mime_type: str) -> List[_Unit]:
        suffix = _safe_suffix(filename)
        if suffix not in _SUPPORTED_EXTENSIONS:
            raise MemoryBankError(
                "UNSUPPORTED_FORMAT",
                f"Unsupported extension {suffix}; supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}",
                status=415,
            )
        if suffix in _TEXT_EXTENSIONS:
            return _text_units(_decode_text(data), group=f"TEXT::{filename}")
        if suffix in {".html", ".htm"}:
            parser = _HTMLText()
            parser.feed(_decode_text(data))
            parser.close()
            return _text_units(parser.text(), group=f"HTML::{filename}")
        if suffix == ".csv":
            return self._parse_csv(filename, data)
        if suffix in {".xlsx", ".xlsm"}:
            return self._parse_excel(filename, data)
        if suffix == ".docx":
            return self._parse_docx(filename, data)
        if suffix == ".pptx":
            return self._parse_pptx(filename, data)
        if suffix == ".pdf":
            return self._parse_pdf(filename, data)
        if suffix in {".json", ".jsonl"}:
            return self._parse_json(filename, data, jsonl=suffix == ".jsonl")
        if suffix in _IMAGE_EXTENSIONS:
            return [self._describe_image(data, mime_type or mimetypes.guess_type(filename)[0] or "image/png", filename, "IMAGE")]
        raise MemoryBankError("UNSUPPORTED_FORMAT", f"Unsupported extension: {suffix}", status=415)

    # -- structure-aware parent/child construction ------------------------

    def _build_parents(self, source_id: str, units: List[_Unit]) -> List[_Parent]:
        parents: List[_Parent] = []
        current: List[_Unit] = []
        current_length = 0

        def flush() -> None:
            nonlocal current, current_length
            if not current:
                return
            kinds = {unit.kind for unit in current}
            kind = current[0].kind if len(kinds) == 1 else "mixed"
            context = dict(current[0].context)
            context["locations"] = [unit.data for unit in current]
            parent = _Parent(
                source_id, len(parents), "\n\n".join(unit.text for unit in current),
                kind, context, current,
            )
            for unit in current:
                unit.parent_id = parent.parent_id
            parents.append(parent)
            current = []
            current_length = 0

        for unit in units:
            if unit.kind == "text" and len(unit.text) > self.config.parent_size:
                flush()
                for start, end in _sliding_spans(unit.text, self.config.parent_size, 0):
                    part = _Unit(
                        unit.kind, unit.text[start:end], unit.group_key, dict(unit.context),
                        data={**unit.data, "unit_char_start": start, "unit_char_end": end},
                        raw_sha256=unit.raw_sha256, raw_mime=unit.raw_mime,
                    )
                    parent = _Parent(source_id, len(parents), part.text, "text", dict(part.context), [part])
                    part.parent_id = parent.parent_id
                    parents.append(parent)
                continue
            is_table = unit.kind == "table_row"
            same_group = current and current[0].group_key == unit.group_key
            same_kind = current and current[0].kind == unit.kind
            too_many_rows = is_table and len(current) >= self.config.max_table_rows_per_parent
            too_long = current_length + len(unit.text) + 2 > self.config.parent_size
            if current and (not same_group or not same_kind or too_many_rows or too_long or unit.kind == "image"):
                flush()
            current.append(unit)
            current_length += len(unit.text) + 2
            if unit.kind == "image":
                flush()
        flush()
        return parents

    def _build_children(self, parents: List[_Parent]) -> List[_Child]:
        children: List[_Child] = []
        for parent in parents:
            if parent.kind == "table_row":
                cursor = 0
                for unit in parent.units:
                    start = parent.text.find(unit.text, cursor)
                    if start < 0:
                        start = cursor
                    end = start + len(unit.text)
                    cursor = end
                    children.append(_Child(
                        parent.parent_id, parent.source_id, len(children), start, end,
                        unit.text, unit.kind, dict(parent.context), [unit.unit_id],
                    ))
                continue
            spans = _sliding_spans(parent.text, self.config.child_size, self.config.child_overlap)
            for start, end in spans:
                children.append(_Child(
                    parent.parent_id, parent.source_id, len(children), start, end,
                    parent.text[start:end].strip(), parent.kind, dict(parent.context),
                    [unit.unit_id for unit in parent.units],
                ))
        return [child for child in children if child.text]

    def _embed_children(self, pack_id: str, children: List[_Child]) -> str:
        if not self.embedder:
            return ""
        signature = getattr(self.embedder, "name", self.embedder.__class__.__name__)
        manifest = self._load_manifest(pack_id)
        configured = manifest.get("embedding_model", "")
        if configured and configured != signature:
            raise MemoryBankError(
                "EMBEDDING_MODEL_MISMATCH",
                f"Pack uses {configured}, but MemoryBank received {signature}; use the original model or reindex a new pack",
                status=409,
            )
        expected_dim: Optional[int] = None
        for start in range(0, len(children), self.config.embedding_batch_size):
            batch = children[start : start + self.config.embedding_batch_size]
            vectors = self.embedder.embed_documents([child.text for child in batch])
            if len(vectors) != len(batch):
                raise MemoryBankError("EMBEDDING_FAILED", "Embedder returned the wrong vector count", status=500)
            for child, vector in zip(batch, vectors):
                normalized = _normalize_vector(vector)
                if expected_dim is None:
                    expected_dim = len(normalized)
                elif len(normalized) != expected_dim:
                    raise MemoryBankError("EMBEDDING_FAILED", "Inconsistent embedding dimensions", status=500)
                child.embedding = normalized
        return signature


    # -- ingest ------------------------------------------------------------

    def ingest_text(
        self,
        pack_id: str,
        text: str,
        *,
        source_name: str = "inline.txt",
        metadata: Optional[Mapping[str, Any]] = None,
        deduplicate: bool = True,
    ) -> Dict[str, Any]:
        if not text or not text.strip():
            raise MemoryBankError("EMPTY_TEXT", "Cannot ingest empty text")
        if not Path(source_name).suffix:
            source_name += ".txt"
        return self._ingest_bytes(
            pack_id, text.encode("utf-8"), source_name, "text/plain; charset=utf-8", metadata, deduplicate,
        )

    def ingest_file(
        self,
        pack_id: str,
        file: Union[str, Path, bytes, bytearray, BinaryIO],
        *,
        filename: Optional[str] = None,
        mime_type: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
        deduplicate: bool = True,
    ) -> Dict[str, Any]:
        if isinstance(file, (str, Path)):
            path = Path(file)
            filename = filename or path.name
            with path.open("rb") as handle:
                data = _read_limited(handle, self.config.max_file_bytes)
        elif isinstance(file, (bytes, bytearray)):
            data = bytes(file)
            if not filename:
                raise MemoryBankError("FILENAME_REQUIRED", "filename is required when uploading bytes")
        elif hasattr(file, "read"):
            data = _read_limited(file, self.config.max_file_bytes)  # type: ignore[arg-type]
            filename = filename or Path(getattr(file, "name", "upload.bin")).name
        else:
            raise TypeError("file must be a path, bytes, or a binary file-like object")
        if len(data) > self.config.max_file_bytes:
            raise MemoryBankError("FILE_TOO_LARGE", f"File exceeds {self.config.max_file_bytes} bytes", status=413)
        filename = filename or "upload.bin"
        mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return self._ingest_bytes(pack_id, data, filename, mime_type, metadata, deduplicate)

    async def aingest_file(self, pack_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await asyncio.to_thread(self.ingest_file, pack_id, *args, **kwargs)

    async def aingest_text(self, pack_id: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        return await asyncio.to_thread(self.ingest_text, pack_id, *args, **kwargs)

    def extract_text(
        self,
        data: bytes,
        filename: str,
        mime_type: str = "",
        *,
        max_chars: int = 24_000,
    ) -> Dict[str, Any]:
        """Parse a file into plain text without persisting it.

        Used by chat attachments (PDF / Word / Excel / PPT / CSV ...): the model
        receives the extracted text instead of binary bytes.
        """
        if not data:
            raise MemoryBankError("FILE_EMPTY", "Uploaded file is empty", status=422)
        if len(data) > self.config.max_file_bytes:
            raise MemoryBankError("FILE_TOO_LARGE", f"File exceeds {self.config.max_file_bytes} bytes", status=413)
        filename = filename or "upload.bin"
        mime_type = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        units = self._parse_units(filename, data, mime_type)
        text = "\n\n".join(unit.text for unit in units if unit.text.strip())
        truncated = len(text) > max_chars
        return {
            "filename": filename,
            "chars": len(text),
            "truncated": truncated,
            "text": text[:max_chars],
        }

    def _ingest_bytes(
        self,
        pack_id: str,
        data: bytes,
        filename: str,
        mime_type: str,
        metadata: Optional[Mapping[str, Any]],
        deduplicate: bool,
    ) -> Dict[str, Any]:
        self._load_manifest(pack_id)
        filename = Path(filename).name.strip()
        if not filename:
            raise MemoryBankError("INVALID_FILENAME", "Filename cannot be empty")
        suffix = _safe_suffix(filename)
        if suffix not in _SUPPORTED_EXTENSIONS:
            raise MemoryBankError("UNSUPPORTED_FORMAT", f"Unsupported file extension: {suffix}", status=415)
        if not data:
            raise MemoryBankError("EMPTY_FILE", "Cannot ingest an empty file")
        digest = hashlib.sha256(data).hexdigest()
        source_id = uuid4().hex
        now = _now()

        with self._lock(pack_id), self._db(pack_id) as connection:
            if deduplicate:
                duplicate = connection.execute(
                    """SELECT * FROM sources
                       WHERE filename = ? AND raw_sha256 = ? AND status = 'done'
                       ORDER BY version DESC LIMIT 1""",
                    (filename, digest),
                ).fetchone()
                if duplicate:
                    result = self._source_dict(duplicate)
                    result["deduplicated"] = True
                    return result
            previous = connection.execute(
                """SELECT source_id, version FROM sources
                   WHERE filename = ? AND status = 'done'
                   ORDER BY version DESC LIMIT 1""",
                (filename,),
            ).fetchone()
            version = int(previous["version"]) + 1 if previous else 1
            supersedes = previous["source_id"] if previous else None
            self._store_blob(pack_id, data, suffix, digest)
            connection.execute(
                """INSERT INTO sources(
                     source_id, filename, extension, mime_type, raw_sha256, size_bytes,
                     status, version, supersedes, is_latest, metadata_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?, 0, ?, ?, ?)""",
                (
                    source_id, filename, suffix, mime_type, digest, len(data), version,
                    supersedes, _json(dict(metadata or {})), now, now,
                ),
            )
            connection.commit()

            try:
                units = self._parse_units(filename, data, mime_type)
                if not units:
                    raise MemoryBankError("NO_CONTENT", "Parser found no searchable content", status=422)
                for unit in units:
                    if unit.raw_data:
                        extension = mimetypes.guess_extension(unit.raw_mime) or ".bin"
                        self._store_blob(pack_id, unit.raw_data, extension, unit.raw_sha256)
                parents = self._build_parents(source_id, units)
                # Long units may be split into multiple parent-local units; persist that list.
                stored_units = [unit for parent in parents for unit in parent.units]
                children = self._build_children(parents)
                if not children:
                    raise MemoryBankError("NO_CONTENT", "No chunks were produced", status=422)
                embedding_model = self._embed_children(pack_id, children)

                connection.execute("BEGIN IMMEDIATE")
                if previous:
                    connection.execute(
                        "UPDATE sources SET is_latest = 0, updated_at = ? WHERE filename = ?",
                        (_now(), filename),
                    )
                for parent in parents:
                    connection.execute(
                        """INSERT INTO parents(
                             parent_id, source_id, ordinal, kind, text, context_json, unit_ids_json
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            parent.parent_id, source_id, parent.ordinal, parent.kind, parent.text,
                            _json(parent.context), _json([unit.unit_id for unit in parent.units]),
                        ),
                    )
                for ordinal, unit in enumerate(stored_units):
                    connection.execute(
                        """INSERT INTO units(
                             unit_id, source_id, parent_id, ordinal, kind, text, group_key,
                             context_json, data_json, raw_sha256, raw_mime
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            unit.unit_id, source_id, unit.parent_id, ordinal, unit.kind, unit.text,
                            unit.group_key, _json(unit.context), _json(unit.data),
                            unit.raw_sha256, unit.raw_mime,
                        ),
                    )
                    if ordinal:
                        connection.execute(
                            """INSERT OR REPLACE INTO links(src_unit_id, dst_unit_id, relation, weight)
                               VALUES (?, ?, 'next', 1.0)""",
                            (stored_units[ordinal - 1].unit_id, unit.unit_id),
                        )
                for child in children:
                    connection.execute(
                        """INSERT INTO chunks(
                             chunk_id, parent_id, source_id, ordinal, kind, start_char, end_char,
                             text, context_json, unit_ids_json, embedding, embedding_dim, embedding_model
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            child.chunk_id, child.parent_id, source_id, child.ordinal, child.kind,
                            child.start_char, child.end_char, child.text, _json(child.context),
                            _json(child.unit_ids),
                            _vector_blob(child.embedding) if child.embedding is not None else None,
                            len(child.embedding) if child.embedding is not None else None,
                            embedding_model,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO chunks_fts(chunk_id, index_text) VALUES (?, ?)",
                        (child.chunk_id, _fts_text(child.text + " " + filename)),
                    )
                connection.execute(
                    """UPDATE sources SET status = 'done', is_latest = 1, unit_count = ?,
                       chunk_count = ?, updated_at = ?, error_message = '' WHERE source_id = ?""",
                    (len(stored_units), len(children), _now(), source_id),
                )
                connection.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")
                connection.commit()
                self._invalidate_vector_cache(pack_id)
                manifest = self._load_manifest(pack_id)
                if embedding_model and not manifest.get("embedding_model"):
                    manifest["embedding_model"] = embedding_model
                    manifest["retrieval_profile"]["mode"] = "hybrid"
                    self._save_manifest(manifest)
                self._refresh_manifest(pack_id, connection)
                result = self.get_source(pack_id, source_id)
                result.update({"pack_id": pack_id, "deduplicated": False})
                return result
            except Exception as exc:
                connection.rollback()
                message = exc.message if isinstance(exc, MemoryBankError) else f"{type(exc).__name__}: {exc}"
                connection.execute(
                    """UPDATE sources SET status = 'failed', is_latest = 0,
                       error_message = ?, updated_at = ? WHERE source_id = ?""",
                    (message, _now(), source_id),
                )
                connection.commit()
                self._refresh_manifest(pack_id, connection)
                if isinstance(exc, MemoryBankError):
                    raise
                raise MemoryBankError("INGEST_FAILED", f"Ingest failed: {message}", status=500) from exc

    # -- retrieval ---------------------------------------------------------

    def _filter_sql(
        self, filters: Optional[Mapping[str, Any]], *, alias: str = "c"
    ) -> Tuple[str, List[Any]]:
        filters = filters or {}
        clauses = ["s.status = 'done'"]
        params: List[Any] = []
        if not filters.get("include_history", False):
            clauses.append("s.is_latest = 1")
        source_ids = [str(value) for value in filters.get("source_ids", []) if str(value)]
        if source_ids:
            clauses.append(f"{alias}.source_id IN ({','.join('?' for _ in source_ids)})")
            params.extend(source_ids)
        kinds = [str(value) for value in filters.get("kinds", []) if str(value)]
        if kinds:
            clauses.append(f"{alias}.kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if filters.get("filename_contains"):
            clauses.append("s.filename LIKE ? ESCAPE '\\'")
            value = str(filters["filename_contains"]).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{value}%")
        if filters.get("created_after"):
            clauses.append("s.created_at >= ?")
            params.append(str(filters["created_after"]))
        if filters.get("created_before"):
            clauses.append("s.created_at <= ?")
            params.append(str(filters["created_before"]))
        return " AND ".join(clauses), params

    def _allowed_chunk_ids(self, connection: sqlite3.Connection, filters: Mapping[str, Any]) -> set[str]:
        where, params = self._filter_sql(filters)
        rows = connection.execute(
            f"SELECT c.chunk_id FROM chunks c JOIN sources s ON s.source_id = c.source_id WHERE {where}",
            params,
        ).fetchall()
        return {row["chunk_id"] for row in rows}

    def _bm25_rankings(
        self, connection: sqlite3.Connection, queries: Sequence[str], filters: Mapping[str, Any], limit: int
    ) -> List[List[str]]:
        where, params = self._filter_sql(filters)
        rankings: List[List[str]] = []
        for query in queries:
            match = _fts_query(query)
            if not match:
                rankings.append([])
                continue
            rows = connection.execute(
                f"""SELECT c.chunk_id, bm25(chunks_fts) AS rank
                    FROM chunks_fts
                    JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                    JOIN sources s ON s.source_id = c.source_id
                    WHERE chunks_fts MATCH ? AND {where}
                    ORDER BY rank ASC LIMIT ?""",
                [match, *params, limit],
            ).fetchall()
            rankings.append([row["chunk_id"] for row in rows])
        return rankings

    def _cached_vectors(self, pack_id: str) -> Optional[_VectorCacheEntry]:
        with self._db(pack_id) as connection:
            revision = int(connection.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()[0])
            with self._vector_cache_guard:
                cached = self._vector_cache.get(pack_id)
                if cached and cached.revision == revision:
                    return cached
            count = connection.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
            if count > self.config.vector_cache_max_chunks:
                return None
            rows = connection.execute(
                """SELECT c.chunk_id, c.embedding FROM chunks c
                   JOIN sources s ON s.source_id = c.source_id
                   WHERE c.embedding IS NOT NULL AND s.status = 'done' AND s.is_latest = 1
                   ORDER BY c.rowid"""
            ).fetchall()
        if not rows:
            return None
        try:
            import numpy as np
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install numpy for vector retrieval", status=500) from exc
        ids = [row["chunk_id"] for row in rows]
        dimension = len(rows[0]["embedding"]) // 4
        matrix = np.empty((len(rows), dimension), dtype=np.float32)
        for index, row in enumerate(rows):
            vector = np.frombuffer(row["embedding"], dtype="<f4")
            if len(vector) != dimension:
                raise MemoryBankError("VECTOR_CORRUPT", "Inconsistent stored vector dimensions", status=500)
            matrix[index] = vector
        entry = _VectorCacheEntry(revision, ids, matrix, time.monotonic())
        with self._vector_cache_guard:
            self._vector_cache[pack_id] = entry
        return entry

    def _dense_rankings(
        self, pack_id: str, queries: Sequence[str], filters: Mapping[str, Any], limit: int
    ) -> List[List[str]]:
        if not self.embedder:
            return [[] for _ in queries]
        try:
            import numpy as np
        except ImportError as exc:
            raise MemoryBankError("DEPENDENCY_MISSING", "Install numpy for vector retrieval", status=500) from exc
        use_cache = not filters.get("include_history", False)
        cache = self._cached_vectors(pack_id) if use_cache else None
        if cache is not None:
            with self._db(pack_id) as connection:
                allowed = self._allowed_chunk_ids(connection, filters)
            positions = [index for index, chunk_id in enumerate(cache.ids) if chunk_id in allowed]
            ids = [cache.ids[index] for index in positions]
            matrix = cache.matrix[positions] if positions else np.empty((0, cache.matrix.shape[1]), dtype=np.float32)
        else:
            with self._db(pack_id) as connection:
                where, params = self._filter_sql(filters)
                rows = connection.execute(
                    f"""SELECT c.chunk_id, c.embedding FROM chunks c
                        JOIN sources s ON s.source_id = c.source_id
                        WHERE c.embedding IS NOT NULL AND {where} ORDER BY c.rowid""",
                    params,
                ).fetchall()
            ids = [row["chunk_id"] for row in rows]
            if rows:
                dimension = len(rows[0]["embedding"]) // 4
                matrix = np.vstack([np.frombuffer(row["embedding"], dtype="<f4") for row in rows])
                if matrix.shape[1] != dimension:
                    raise MemoryBankError("VECTOR_CORRUPT", "Inconsistent vector dimensions", status=500)
            else:
                matrix = np.empty((0, 0), dtype=np.float32)
        rankings: List[List[str]] = []
        for query in queries:
            if not ids:
                rankings.append([])
                continue
            vector = np.asarray(_normalize_vector(self.embedder.embed_query(query)), dtype=np.float32)
            if vector.shape[0] != matrix.shape[1]:
                raise MemoryBankError(
                    "EMBEDDING_MODEL_MISMATCH", "Query and stored vector dimensions differ", status=409
                )
            scores = matrix @ vector
            count = min(limit, len(ids))
            if count == len(ids):
                indices = np.argsort(-scores)
            else:
                indices = np.argpartition(-scores, count - 1)[:count]
                indices = indices[np.argsort(-scores[indices])]
            rankings.append([ids[int(index)] for index in indices])
        return rankings

    def _rows_by_ids(self, connection: sqlite3.Connection, ids: Iterable[str]) -> Dict[str, sqlite3.Row]:
        unique = list(dict.fromkeys(ids))
        output: Dict[str, sqlite3.Row] = {}
        for start in range(0, len(unique), 800):
            batch = unique[start : start + 800]
            if not batch:
                continue
            rows = connection.execute(
                f"""SELECT c.*, p.text AS parent_text, p.context_json AS parent_context,
                           s.filename, s.raw_sha256, s.mime_type AS source_mime,
                           s.metadata_json AS source_metadata, s.version AS source_version
                    FROM chunks c
                    JOIN parents p ON p.parent_id = c.parent_id
                    JOIN sources s ON s.source_id = c.source_id
                    WHERE c.chunk_id IN ({','.join('?' for _ in batch)})""",
                batch,
            ).fetchall()
            output.update({row["chunk_id"]: row for row in rows})
        return output

    def retrieve(
        self,
        pack_id: str,
        query: str,
        *,
        top_k: int = 5,
        filters: Optional[Mapping[str, Any]] = None,
        query_variants: Optional[Sequence[str]] = None,
        use_reranker: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return parent contexts ranked by child evidence. Score is relative, not a probability."""
        self._load_manifest(pack_id)
        if not query or not query.strip():
            raise MemoryBankError("EMPTY_QUERY", "Query cannot be empty")
        if not 1 <= top_k <= 50:
            raise MemoryBankError("INVALID_TOP_K", "top_k must be between 1 and 50")
        variants = [query.strip()]
        for variant in query_variants or []:
            if variant and variant.strip() and variant.strip() not in variants:
                variants.append(variant.strip())
        variants = variants[:5]
        filters = dict(filters or {})
        candidate_limit = min(200, max(20, top_k * self.config.candidate_multiplier))

        with self._lock(pack_id), self._db(pack_id) as connection:
            lexical = self._bm25_rankings(connection, variants, filters, candidate_limit)
            dense = self._dense_rankings(pack_id, variants, filters, candidate_limit)
            candidate_ids = set().union(*(set(ranking) for ranking in [*lexical, *dense]))
            if not candidate_ids:
                return []
            rows = self._rows_by_ids(connection, candidate_ids)
            rrf: Dict[str, float] = {chunk_id: 0.0 for chunk_id in candidate_ids}
            channels: Dict[str, set[str]] = {chunk_id: set() for chunk_id in candidate_ids}
            variant_weight = 1.0 / len(variants)
            for ranking in lexical:
                for rank, chunk_id in enumerate(ranking, start=1):
                    rrf[chunk_id] += variant_weight * self.config.bm25_weight / (self.config.rrf_k + rank)
                    channels[chunk_id].add("bm25")
            for ranking in dense:
                for rank, chunk_id in enumerate(ranking, start=1):
                    rrf[chunk_id] += variant_weight * self.config.vector_weight / (self.config.rrf_k + rank)
                    channels[chunk_id].add("vector")
            max_rrf = max(rrf.values()) or 1.0
            candidates: List[Dict[str, Any]] = []
            identifiers = set(_IDENTIFIER_RE.findall(query))
            for chunk_id, raw_score in rrf.items():
                row = rows.get(chunk_id)
                if row is None:
                    continue
                score = raw_score / max_rrf
                matched_text = row["text"]
                if any(identifier.lower() in matched_text.lower() for identifier in identifiers):
                    score *= 1.10
                    channels[chunk_id].add("identifier")
                score *= _modality_boost(query, row["kind"])
                source_metadata = _load_json(row["source_metadata"], {})
                authority = source_metadata.get("authority", 1.0)
                if isinstance(authority, (int, float)) and math.isfinite(float(authority)):
                    score *= max(0.5, min(1.5, float(authority)))
                candidates.append({"chunk_id": chunk_id, "row": row, "score": score, "channels": channels[chunk_id]})
            candidates.sort(key=lambda item: item["score"], reverse=True)

            reranked = False
            if self.reranker and use_reranker and candidates:
                reranked = True
                candidates = candidates[: min(len(candidates), self.config.rerank_candidates)]
                scores = self.reranker.rerank(query, [item["row"]["text"] for item in candidates])
                if len(scores) != len(candidates) or not all(math.isfinite(float(score)) for score in scores):
                    raise MemoryBankError("RERANK_FAILED", "Reranker returned invalid scores", status=500)
                low, high = min(scores), max(scores)
                for item, rerank_score in zip(candidates, scores):
                    normalized = 0.5 if high == low else (rerank_score - low) / (high - low)
                    item["score"] = 0.2 * item["score"] + 0.8 * normalized
                    item["channels"].add("reranker")
                candidates.sort(key=lambda item: item["score"], reverse=True)

            selected: List[Dict[str, Any]] = []
            selected_tokens: List[set[str]] = []
            seen_parents: set[str] = set()
            for item in candidates:
                row = item["row"]
                if row["parent_id"] in seen_parents:
                    continue
                tokens = _lexical_tokens(row["text"])
                if any(_jaccard(tokens, previous) >= self.config.near_duplicate_threshold for previous in selected_tokens):
                    continue
                seen_parents.add(row["parent_id"])
                selected_tokens.append(tokens)
                selected.append(item)
                if len(selected) >= top_k:
                    break

            hits: List[Dict[str, Any]] = []
            for item in selected:
                row = item["row"]
                raw = connection.execute(
                    """SELECT raw_sha256, raw_mime FROM units
                       WHERE parent_id = ? AND raw_sha256 <> ''
                       ORDER BY CASE WHEN kind = 'image' THEN 0 ELSE 1 END, ordinal LIMIT 1""",
                    (row["parent_id"],),
                ).fetchone()
                context = _load_json(row["parent_context"], {})
                source_metadata = _load_json(row["source_metadata"], {})
                citation = f"{row['filename']} (v{row['source_version']})"
                if context.get("page"):
                    citation += f"，第 {context['page']} 页"
                if context.get("sheet"):
                    citation += f"，{context['sheet']}"
                hits.append({
                    "text": row["parent_text"],
                    "matched_text": row["text"],
                    "source": row["filename"],
                    "score": round(float(item["score"]), 6),
                    "chunk_id": row["chunk_id"],
                    "parent_id": row["parent_id"],
                    "source_id": row["source_id"],
                    "kind": row["kind"],
                    "citation": citation,
                    "provenance": {
                        "pack_id": pack_id,
                        "checksum": row["raw_sha256"],
                        "source_version": row["source_version"],
                        "context": context,
                        "parent_char_start": row["start_char"],
                        "parent_char_end": row["end_char"],
                        "unit_ids": _load_json(row["unit_ids_json"], []),
                    },
                    "metadata": source_metadata,
                    "raw_uri": f"memory://{pack_id}/{raw['raw_sha256'] if raw else row['raw_sha256']}",
                    "raw_mime": raw["raw_mime"] if raw else row["source_mime"],
                    "retrieval_channels": sorted(item["channels"]),
                })
            return hits

    def query(self, pack_id: str, query: str, *, top_k: int = 5, **kwargs: Any) -> Dict[str, Any]:
        started = time.perf_counter()
        hits = self.retrieve(pack_id, query, top_k=top_k, **kwargs)
        dense_used = bool(self.embedder) and any(
            "vector" in hit["retrieval_channels"] for hit in hits
        )
        return {
            "data": {"hits": hits},
            "meta": {
                "pack_id": pack_id,
                "query": query,
                "rag_backend": "hybrid" if dense_used else "keyword+structure",
                "reranked": any("reranker" in hit["retrieval_channels"] for hit in hits),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        }

    async def aretrieve(self, pack_id: str, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.retrieve, pack_id, *args, **kwargs)

    def evaluate_retrieval(
        self,
        pack_id: str,
        cases: Sequence[Mapping[str, Any]],
        *,
        top_k: int = 10,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Source-level diagnostic metrics; this does not pretend to be claim-level RAGChecker.

        Each case: {"query": str, "relevant_source_ids": [str, ...], "filters": optional}.
        Context precision is per returned parent; source recall is per gold source set.
        """
        if not cases:
            raise MemoryBankError("EMPTY_EVALUATION", "Evaluation cases cannot be empty")
        per_case: List[Dict[str, Any]] = []
        total_returned = total_relevant_returned = total_gold = total_relevant_sources = 0
        for case in cases:
            gold = {str(value) for value in case.get("relevant_source_ids", [])}
            if not gold:
                raise MemoryBankError("INVALID_EVALUATION_CASE", "Each case needs relevant_source_ids")
            case_filters = dict(filters or {})
            case_filters.update(case.get("filters", {}))
            hits = self.retrieve(
                pack_id, str(case["query"]), top_k=top_k, filters=case_filters,
                query_variants=case.get("query_variants"),
            )
            relevant_flags = [hit["source_id"] in gold for hit in hits]
            relevant_sources = {hit["source_id"] for hit in hits if hit["source_id"] in gold}
            returned_relevant = sum(relevant_flags)
            precision = returned_relevant / len(hits) if hits else 0.0
            recall = len(relevant_sources) / len(gold)
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            reciprocal_rank = next((1.0 / (index + 1) for index, flag in enumerate(relevant_flags) if flag), 0.0)
            dcg = sum((1.0 / math.log2(index + 2)) for index, flag in enumerate(relevant_flags) if flag)
            ideal_count = min(len(gold), top_k)
            idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
            per_case.append({
                "query": case["query"], "precision": precision, "recall": recall, "f1": f1,
                "reciprocal_rank": reciprocal_rank, "ndcg": dcg / idcg if idcg else 0.0,
                "hit": bool(relevant_sources), "returned": len(hits),
            })
            total_returned += len(hits)
            total_relevant_returned += returned_relevant
            total_relevant_sources += len(relevant_sources)
            total_gold += len(gold)
        macro = lambda key: sum(item[key] for item in per_case) / len(per_case)  # noqa: E731
        micro_precision = total_relevant_returned / total_returned if total_returned else 0.0
        # Aggregate source recall is computed from per-case intersections, not global source ids.
        micro_recall = total_relevant_sources / total_gold if total_gold else 0.0
        micro_f1 = (
            2 * micro_precision * micro_recall / (micro_precision + micro_recall)
            if micro_precision + micro_recall else 0.0
        )
        return {
            "cases": per_case,
            "macro": {
                "context_precision": macro("precision"), "source_recall": macro("recall"),
                "f1": macro("f1"), "recall_at_k": macro("hit"), "mrr": macro("reciprocal_rank"),
                "ndcg": macro("ndcg"),
            },
            "micro": {"context_precision": micro_precision, "source_recall": micro_recall, "f1": micro_f1},
            "top_k": top_k,
            "metric_scope": "retrieval/source-level; generation claim F1 requires a separate claim evaluator",
        }


# ---------------------------------------------------------------------------
# Optional FastAPI adapter (no global app, no router registered on import)
# ---------------------------------------------------------------------------


def create_memory_router(bank: MemoryBank, *, prefix: str = "/api/memory-bank") -> Any:
    """Create an isolated FastAPI router around an existing MemoryBank instance."""
    try:
        from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise MemoryBankError(
            "DEPENDENCY_MISSING", "Install fastapi to create the HTTP router", status=500
        ) from exc
    import functools

    router = APIRouter(prefix=prefix, tags=["memory-bank"])
    # BYOK Embedding 仅驻留进程内存；配置由 Node 随单次请求透传，不写数据库。
    request_banks: Dict[str, MemoryBank] = {}

    def bank_for(request: Request) -> MemoryBank:
        api_key = request.headers.get("x-embedding-key", "").strip()
        if not api_key:
            return bank
        base_url = request.headers.get("x-embedding-base-url", "").strip().rstrip("/")
        model = request.headers.get("x-embedding-model", "").strip()
        if not base_url.startswith(("https://", "http://")) or not model:
            raise MemoryBankError("INVALID_EMBEDDING_CONFIG", "Embedding Base URL 和模型不能为空", status=422)
        cache_key = f"{base_url}\n{model}\n{api_key}"
        if cache_key not in request_banks:
            request_banks[cache_key] = MemoryBank(
                root=bank.root,
                config=bank.config,
                embedder=OpenAICompatibleEmbedder(base_url, api_key, model, batch_size=10),
            )
        return request_banks[cache_key]

    class PackBody(BaseModel):
        name: str = Field(min_length=1, max_length=200)
        description: str = ""
        metadata: Dict[str, Any] = Field(default_factory=dict)

    class PatchPackBody(BaseModel):
        name: Optional[str] = Field(default=None, min_length=1, max_length=200)
        description: Optional[str] = Field(default=None, max_length=500)

    class TextBody(BaseModel):
        text: str = Field(min_length=1, max_length=8_000_000)
        source_name: str = "inline.txt"
        metadata: Dict[str, Any] = Field(default_factory=dict)
        deduplicate: bool = True

    class QueryBody(BaseModel):
        query: str = Field(min_length=1, max_length=8000)
        top_k: int = Field(default=5, ge=1, le=20)
        filters: Dict[str, Any] = Field(default_factory=dict)
        query_variants: List[str] = Field(default_factory=list, max_length=4)
        use_reranker: bool = True

    # NOTE: 本模块顶部有 `from __future__ import annotations`，路由处理器注解
    # 是字符串；FastAPI 解析时只查模块全局命名空间，而 PackBody 等是本工厂
    # 闭包内的局部类，解析失败会把 JSON body 退化成 query 参数（422）。
    # 发布到模块命名空间即可让字符串注解正确还原为请求模型。
    globals()["Request"] = Request
    globals()["PackBody"] = PackBody
    globals()["PatchPackBody"] = PatchPackBody
    globals()["TextBody"] = TextBody
    globals()["QueryBody"] = QueryBody

    def errors(handler: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(handler)
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(*args, **kwargs)
            except MemoryBankError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.to_dict()) from exc
        return wrapped

    def envelope(data: Any, **meta: Any) -> Dict[str, Any]:
        return {"data": data, "meta": {"trace_id": f"trace-{uuid4().hex[:12]}", **meta}}

    @router.post("/packs", status_code=201)
    @errors
    async def create_pack(body: PackBody) -> Dict[str, Any]:
        result = await asyncio.to_thread(bank.create_pack, body.name, body.description, metadata=body.metadata)
        return envelope(result)

    @router.get("/packs")
    @errors
    async def list_packs() -> Dict[str, Any]:
        packs = await asyncio.to_thread(bank.list_packs)
        return envelope({"packs": packs}, count=len(packs))

    @router.get("/packs/{pack_id}")
    @errors
    async def get_pack(pack_id: str) -> Dict[str, Any]:
        return envelope(await asyncio.to_thread(bank.get_pack, pack_id))

    @router.patch("/packs/{pack_id}")
    @errors
    async def patch_pack(pack_id: str, body: PatchPackBody) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            bank.update_pack, pack_id, name=body.name, description=body.description
        )
        return envelope(result)

    @router.delete("/packs/{pack_id}")
    @errors
    async def delete_pack(pack_id: str) -> Dict[str, Any]:
        await asyncio.to_thread(bank.delete_pack, pack_id)
        return envelope({"deleted": True, "pack_id": pack_id})

    @router.get("/packs/{pack_id}/sources")
    @errors
    async def list_sources(pack_id: str, include_history: bool = Query(False)) -> Dict[str, Any]:
        sources = await asyncio.to_thread(bank.list_sources, pack_id, include_history=include_history)
        return envelope({"sources": sources}, count=len(sources))

    @router.delete("/packs/{pack_id}/sources/{source_id}")
    @errors
    async def delete_source(pack_id: str, source_id: str) -> Dict[str, Any]:
        await asyncio.to_thread(bank.delete_source, pack_id, source_id)
        return envelope({"deleted": True, "source_id": source_id})

    @router.post("/packs/{pack_id}/text")
    @errors
    async def ingest_text(pack_id: str, body: TextBody) -> Dict[str, Any]:
        result = await bank.aingest_text(
            pack_id, body.text, source_name=body.source_name,
            metadata=body.metadata, deduplicate=body.deduplicate,
        )
        return envelope(result)

    @router.post("/packs/{pack_id}/files", status_code=201)
    @errors
    async def ingest_file(
        request: Request,
        pack_id: str,
        upload: Any = File(...),  # Any avoids a local ForwardRef under future annotations
        metadata: str = Form("{}"),
    ) -> Dict[str, Any]:
        try:
            parsed_metadata = json.loads(metadata)
            if not isinstance(parsed_metadata, dict):
                raise ValueError("metadata must be a JSON object")
        except ValueError as exc:
            raise MemoryBankError("INVALID_METADATA", f"Invalid metadata JSON: {exc}") from exc
        data = await upload.read(bank.config.max_file_bytes + 1)
        if len(data) > bank.config.max_file_bytes:
            raise MemoryBankError("FILE_TOO_LARGE", "Uploaded file exceeds the configured limit", status=413)
        result = await bank_for(request).aingest_file(
            pack_id, data, filename=upload.filename or "upload.bin",
            mime_type=upload.content_type or "", metadata=parsed_metadata,
        )
        return envelope(result)

    @router.post("/parse")
    @errors
    async def parse_file(request: Request, upload: Any = File(...)) -> Dict[str, Any]:
        """Extract plain text from an uploaded document without storing it."""
        data = await upload.read(bank.config.max_file_bytes + 1)
        if len(data) > bank.config.max_file_bytes:
            raise MemoryBankError("FILE_TOO_LARGE", "Uploaded file exceeds the configured limit", status=413)
        result = await asyncio.to_thread(
            bank_for(request).extract_text,
            data,
            upload.filename or "upload.bin",
            upload.content_type or "",
        )
        return envelope(result)

    @router.post("/packs/{pack_id}/query")
    @errors
    async def query_pack(request: Request, pack_id: str, body: QueryBody) -> Dict[str, Any]:
        result = await asyncio.to_thread(
            bank_for(request).query, pack_id, body.query, top_k=body.top_k,
            filters=body.filters, query_variants=body.query_variants,
            use_reranker=body.use_reranker,
        )
        result["meta"].update({"trace_id": f"trace-{uuid4().hex[:12]}"})
        return result

    @router.get("/packs/{pack_id}/blobs/{sha256}")
    @errors
    async def get_blob(pack_id: str, sha256: str) -> Any:
        data, mime_type = await asyncio.to_thread(bank.get_blob, pack_id, sha256)
        return Response(
            content=data, media_type=mime_type,
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    return router


__all__ = [
    "MemoryBank",
    "MemoryConfig",
    "MemoryBankError",
    "PackNotFound",
    "SourceNotFound",
    "Embedder",
    "VisionDescriber",
    "Reranker",
    "SentenceTransformerEmbedder",
    "OpenAICompatibleEmbedder",
    "OpenAIVisionDescriber",
    "TesseractVisionDescriber",
    "CrossEncoderReranker",
    "create_memory_router",
]
