# Xueban 多模态记忆库技术交接

## 1. 交付内容

核心交付文件：

```text
xueban/memory/memory_bank.py
```

这是一个单文件、嵌入式、可追溯的多模态 RAG 后端，负责：

- 纯文本及多格式文件上传；
- 原始文件、结构化知识、全文索引和向量索引存储；
- 父子分块与文本重叠分块；
- BM25、向量、RRF 融合和可选重排；
- 来源版本、去重、血缘和原始文件回溯；
- Python API 与可选 FastAPI HTTP API；
- 检索层 Recall、Precision、F1、MRR、nDCG 等评测。

本模块不修改前端，不替换现有 KnowledgePack，不覆盖原有 `pack.json`，不占用已有 `/api/knowledge` 或 `/api/memory` 路由。

当前模块负责“检索增强”中的数据解析、存储和检索，不直接调用 LLM 生成最终答案。上层应把检索结果中的 `hit["text"]` 作为上下文交给生成模型。

---

## 2. 先理解四类东西的关系

最容易混淆的是：`MemoryConfig`、函数参数、来源 `metadata`、模型适配器分别管什么。

| 类型 | 通俗理解 | 示例 |
|---|---|---|
| 函数 | 做什么动作 | 创建知识包、上传文件、执行查询 |
| 函数参数 | 这次动作针对谁、处理什么 | `pack_id`、文件、`query`、`top_k` |
| `MemoryConfig` | 整个记忆库按什么默认规则工作 | 块多大、候选多少、缓存多少 |
| 来源 `metadata` | 这一份数据本身的业务属性 | 部门、来源权威度、自定义标签 |
| 模型适配器 | 使用什么外部或本地能力 | Embedding、视觉理解、重排模型 |

例如：

```python
bank = MemoryBank(
    config=MemoryConfig(
        child_size=420,
        child_overlap=64,
        parent_size=1800,
    ),
    embedder=my_embedder,
)

bank.ingest_file(
    pack_id,
    "合同.pdf",
    metadata={"department": "legal"},
)
```

这行上传调用中：

- `ingest_file()`：动作是“上传并建立索引”；
- `pack_id`：决定上传到哪个知识包；
- `"合同.pdf"`：决定处理哪个文件；
- `metadata`：记录这份合同属于法务部门；
- `MemoryConfig`：决定文件限制、父子分块和向量批处理规则；
- `embedder`：决定用什么模型生成向量。

所以，**不需要在每次 `ingest_file()` 时重复传 `child_size`**。

### 2.1 配置的生效时机

这一点非常重要：

#### 只影响新上传数据的配置

以下参数主要在上传、解析和分块时使用：

- `child_size`；
- `child_overlap`；
- `parent_size`；
- `max_table_rows_per_parent`；
- 文件、页数、图片数、表格行数限制；
- `embedding_batch_size`。

文件一旦入库，其父子块和向量已经保存。之后修改 `child_size`，**不会自动重切旧文件**。

如果要改变旧数据的 chunking，应创建新知识包并重新上传或重建索引，不建议直接混用。

#### 修改后影响后续查询的配置

以下参数在查询时读取，重新创建 `MemoryBank` 实例并使用新配置后，会影响后续检索：

- `candidate_multiplier`；
- `rrf_k`；
- `bm25_weight`；
- `vector_weight`；
- `vector_cache_max_chunks`；
- `rerank_candidates`；
- `near_duplicate_threshold`。

它们不需要重新切分旧文档。

### 2.2 推荐部署原则

一个部署环境使用一套明确的 `MemoryConfig`，不要每个请求临时创建不同配置。

尤其不要频繁改变分块参数后继续往同一知识包上传，否则不同来源可能采用不同分块规则，增加排障和评测难度。

---

## 3. 安装与放置

Python 要求：3.10 及以上。

核心文本、CSV、JSON、SQLite 存储主要使用标准库；其他能力按需安装，不会因为某个解析器未安装而导致整个 Xueban 启动失败。

```bash
# 本地向量检索与重排
pip install numpy sentence-transformers

# Excel、Word、PDF、图片和 OCR
pip install openpyxl python-docx pymupdf pillow pytesseract

# FastAPI 路由及远程模型调用
pip install fastapi httpx
```

Tesseract OCR 除 Python 包外，还需要操作系统安装 Tesseract 程序和相应语言包。

建议将文件放到：

```text
xueban/memory/memory_bank.py
```

---

## 4. 最小使用方式

### 4.1 只用 Python API

```python
from xueban.memory.memory_bank import MemoryBank

bank = MemoryBank()

pack = bank.create_pack(
    name="销售知识库",
    description="销售合同、区域指标和续约材料",
)

pack_id = pack["pack_id"]

bank.ingest_text(
    pack_id,
    "2026 年 Q3 华东区域续费率为 91%。",
    source_name="续费结论",
)

bank.ingest_file(
    pack_id,
    "/data/销售明细.xlsx",
    metadata={"department": "sales"},
)

result = bank.query(pack_id, "华东区域续费率是多少？", top_k=5)

for hit in result["data"]["hits"]:
    print(hit["text"])
    print(hit["citation"])
    print(hit["score"])
```

没有注入 `Embedder` 时，系统明确退化为：

- FTS5/BM25 关键词检索；
- 结构和来源权重排序；
- 父子块返回；
- 过滤、去重和血缘。

此时没有向量语义召回，`meta.rag_backend` 会显示 `keyword+structure`。

### 4.2 挂载到 Xueban FastAPI 主服务

在 `xueban/server.py` 创建 `app` 之后添加：

```python
import os
from pathlib import Path

from xueban.memory.memory_bank import (
    MemoryBank,
    SentenceTransformerEmbedder,
    CrossEncoderReranker,
    create_memory_router,
)

memory_bank = MemoryBank(
    root=Path(
        os.getenv(
            "XUEBAN_KNOWLEDGE_DIR",
            ".xueban/knowledge",
        )
    ),
    embedder=SentenceTransformerEmbedder(
        model_name="BAAI/bge-m3",
        device=None,
    ),
    reranker=CrossEncoderReranker(
        model_name="BAAI/bge-reranker-v2-m3",
        device=None,
    ),
)

app.include_router(create_memory_router(memory_bank))
```

新增路由前缀：

```text
/api/memory-bank
```

Node 侧未命中的 `/api/*` 请求可沿用 Xueban 现有 `proxy-fallback` 转发到 Python FastAPI。

`MemoryBank` 应在主服务中创建一次并长期复用，不要每个请求重新创建。复用实例才能复用模型和向量缓存。

### 4.3 复用现有 KnowledgePack ID

```python
memory_bank.ensure_pack(
    pack_id=existing_pack_id,
    name="现有知识包的记忆库",
)
```

如果 sidecar 不存在就创建，存在则直接打开。

它只增加本模块自己的文件，不修改旧 KnowledgePack 数据。

---

## 5. MemoryConfig 完整说明

初始化方式：

```python
from xueban.memory.memory_bank import MemoryBank, MemoryConfig

config = MemoryConfig(
    child_size=420,
    child_overlap=64,
    parent_size=1800,
)

bank = MemoryBank(config=config)
```

### 5.1 分块参数

#### `child_size=420`

检索子块的字符预算。

子块负责“精确找到”。较小的块通常主题更集中，但太小会损失语义和上下文。

这里的 420 是字符预算，不是某个模型的 Token 数。结构边界优先，因此实际块长不保证恰好等于 420。

#### `child_overlap=64`

同一父块内，相邻文本子块保留的重叠字符预算。

作用是减少一句话或一个逻辑关系刚好被边界切断的问题。

它不会让表格行、图片或不同页面之间机械重叠。

#### `parent_size=1800`

父上下文的字符预算。

父块负责“把命中的内容讲完整”，返回给上层生成模型。

典型关系：

```text
Parent：一段较完整上下文，预算 1800 字符
  ├── Child：约 420 字符
  ├── Child：约 420 字符，与上一块有重叠
  └── Child：约 420 字符
```

如果检索命中 Child，最终 `hit["text"]` 返回 Parent，`hit["matched_text"]` 返回真正命中的 Child。

#### `max_table_rows_per_parent=24`

一个表格父上下文最多容纳多少行。

表格检索仍以单行或结构行单元为主，避免整张大表被压成一个主题混杂的向量。

### 5.2 上传与安全限制

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `max_file_bytes` | 64 MiB | 单文件最大字节数 |
| `max_pdf_pages` | 500 | 单 PDF 最大页数 |
| `max_images_per_document` | 48 | 单文档最多处理的图片数 |
| `max_table_rows` | 200000 | 表格最大行数限制 |
| `max_ooxml_uncompressed_bytes` | 256 MiB | DOCX/XLSX 解压后大小限制 |

DOCX、XLSX、XLSM 本质上是 ZIP/OOXML 容器，因此需要检查异常解压膨胀、成员数量和危险路径。

这些限制不能替代生产环境的病毒扫描、鉴权和配额系统。

### 5.3 向量生成参数

#### `embedding_batch_size=32`

每次提交给 Embedding 模型的文本数量。

较大批次可能提高吞吐，但增加内存和单次请求体积；较小时更稳，但调用次数更多。

它只影响处理过程，不改变向量模型和分块内容。

### 5.4 初排和融合参数

#### `candidate_multiplier=6`

初步候选数量相对最终 `top_k` 的倍数。

内部候选上限按以下规则计算：

```python
candidate_limit = min(200, max(20, top_k * candidate_multiplier))
```

例如：

```text
top_k = 5
candidate_multiplier = 6
最终保留 5 条，但初排先保留约 30 条候选
```

为什么要先取更多候选？因为后续还可能进行重排、近重复抑制和父块去重。如果一开始只取 5 条，去重后可能只剩 2 条。

#### `rrf_k=60`

RRF 融合中的平滑参数。

RRF 不直接相加 BM25 分数和向量相似度，而是使用各路排名进行融合。`rrf_k` 越大，头部名次之间的差距越平滑；越小，越强调每一路最靠前的结果。

通常不建议没有评测依据就调整。

#### `bm25_weight=1.0`

BM25 排名在 RRF 融合中的权重。

提高它，更偏向关键词、编号和精确词匹配。

#### `vector_weight=1.0`

向量排名在 RRF 融合中的权重。

提高它，更偏向语义召回。

建议两个权重非负且至少一个大于零。具体比例应通过业务评测集确定，而不是凭经验承诺某个固定比例最优。

### 5.5 重排参数

#### `rerank_candidates=40`

最多把多少条初排候选交给 CrossEncoder。

例如最终只需要 5 条结果，系统可以先对 40 条候选进行更精细的“问题＋正文”联合打分，再选前 5 条。

数值越大，可能覆盖更多候选，但延迟和模型成本也更高。

函数级参数 `use_reranker=False` 可以为某次查询临时关闭重排，不需要修改全局配置。

### 5.6 去重参数

#### `near_duplicate_threshold=0.92`

使用文本词项集合的 Jaccard 相似度做近重复抑制。

- 越接近 1：去重越宽松，只允许非常相似的结果被抑制；
- 越接近 0：去重越激进，可能误删相似但有价值的内容。

表格行可能共享表头，因此不要盲目把阈值调低。

### 5.7 缓存参数

#### `vector_cache_max_chunks=100000`

单个知识包最多缓存多少条 chunk 向量，组成 NumPy 矩阵用于精确相似度计算。

例如 10 万条、1024 维 float32 向量，仅矩阵约 400 MB，尚未计入其他对象开销。

超过此值不会导致向量功能消失，但会增加数据库读取和临时计算。

---

## 6. MemoryConfig 与公开函数的对应关系

| 函数 | 直接读取或间接受影响的配置 |
|---|---|
| `create_pack()` | 将分块参数和检索模式写入知识包 manifest |
| `ingest_text()` | 文件/文本处理规则、父子分块、向量批大小 |
| `ingest_file()` | 文件大小、格式限制、PDF/图片/表格限制、父子分块、向量批大小 |
| `retrieve()` | 候选倍数、RRF、两路权重、向量缓存、重排候选、近重复阈值 |
| `query()` | 与 `retrieve()` 相同，只额外封装返回值和统计耗时 |
| `evaluate_retrieval()` | 间接使用当前实例的全部检索配置 |
| `list_sources()` / `get_source()` | 基本不依赖检索配置 |
| `delete_source()` | 删除关联数据并使向量缓存失效 |
| `get_blob()` | 不依赖分块和检索配置 |

一句话记忆：

```text
上传函数主要使用“解析、限制、分块、向量批处理配置”；
查询函数主要使用“召回、融合、重排、去重、缓存配置”；
管理函数主要负责知识包和来源生命周期。
```

---

## 7. Python 公开 API

### 7.1 MemoryBank 初始化

```python
MemoryBank(
    root=".xueban/knowledge",
    config=None,
    embedder=None,
    vision=None,
    reranker=None,
)
```

| 参数 | 作用 |
|---|---|
| `root` | 知识包存储根目录 |
| `config` | `MemoryConfig` 实例 |
| `embedder` | 文本向量模型适配器 |
| `vision` | 图片描述或 OCR 适配器 |
| `reranker` | 二次精排模型适配器 |

### 7.2 知识包管理

| 方法 | 主要参数 | 功能 |
|---|---|---|
| `create_pack()` | `name`、`description`、可选 `pack_id`、`metadata` | 创建知识包 |
| `ensure_pack()` | `pack_id`、`name` | 打开或创建 sidecar |
| `list_packs()` | 无 | 知识包列表 |
| `get_pack()` | `pack_id` | 配置、模型签名和统计 |
| `stats()` | `pack_id` | 来源、单元、父块、子块、字节数 |
| `delete_pack()` | `pack_id` | 删除本模块 sidecar 数据 |

自定义 `pack_id` 必须是 32 位小写十六进制字符串。

### 7.3 上传纯文本

```python
bank.ingest_text(
    pack_id,
    text,
    source_name="inline.txt",
    metadata=None,
    deduplicate=True,
)
```

- `text`：非空文本；
- `source_name`：来源名称，无扩展名时自动补 `.txt`；
- `metadata`：可 JSON 序列化的来源元数据；
- `deduplicate`：是否避免同名同内容重复入库。

异步形式：

```python
await bank.aingest_text(...)
```

### 7.4 上传文件

```python
bank.ingest_file(
    pack_id,
    file,
    filename=None,
    mime_type="",
    metadata=None,
    deduplicate=True,
)
```

`file` 支持路径、`Path`、bytes 和二进制文件对象。

```python
# 文件路径
bank.ingest_file(pack_id, "/data/合同.pdf")

# bytes，必须同时提供 filename
bank.ingest_file(
    pack_id,
    uploaded_bytes,
    filename="合同.pdf",
    mime_type="application/pdf",
)

# 文件对象
with open("/data/指标.csv", "rb") as f:
    bank.ingest_file(pack_id, f)
```

异步形式：

```python
await bank.aingest_file(...)
```

支持格式：

```text
TXT、MD、MARKDOWN、LOG
CSV
JSON、JSONL
HTML、HTM
XLSX、XLSM
DOCX
PDF
PNG、JPG、JPEG、WEBP、BMP、TIF、TIFF
```

旧 `.doc`、`.xls` 需先转换；加密 PDF 需先合法解密。

上传结果包含 `source_id`、校验和、版本、状态、单元数、子块数、警告和是否去重等信息。

### 7.5 来源管理

| 方法 | 参数 | 功能 |
|---|---|---|
| `list_sources()` | `pack_id`、`include_history=False` | 列出来源 |
| `get_source()` | `pack_id`、`source_id` | 查询来源详情 |
| `delete_source()` | `pack_id`、`source_id` | 删除来源及其索引 |

删除最新版本后，如果还有同名旧完成版本，会恢复最近旧版本为默认检索版本。

### 7.6 检索

```python
hits = bank.retrieve(
    pack_id,
    query,
    top_k=5,
    filters=None,
    query_variants=None,
    use_reranker=True,
)
```

`retrieve()` 返回列表，适合上层 Agent 自由组合流程。

异步形式：

```python
hits = await bank.aretrieve(...)
```

`query()` 返回 Xueban 风格的 `{data, meta}`：

```python
result = bank.query(pack_id, "华东续费率", top_k=5)
```

两者底层检索逻辑相同。

### 7.7 过滤器

```python
filters = {
    "source_ids": ["source-id-1"],
    "kinds": ["table_row", "image"],
    "filename_contains": "销售",
    "created_after": "2026-01-01T00:00:00+00:00",
    "created_before": "2026-12-31T23:59:59+00:00",
    "include_history": False,
}
```

自定义 `metadata` 当前主要用于随结果返回和 `authority` 权重，不是通用 JSON 查询语言。

### 7.8 多查询变体

```python
bank.retrieve(
    pack_id,
    "比较华东和华南 Q3 续费率",
    query_variants=[
        "华东 Q3 续费率",
        "华南 Q3 续费率",
    ],
)
```

适合上层 Agent 做查询改写、子问题拆解或多跳检索。模块本身不绑定某个 LLM。

### 7.9 返回结果字段

- `text`：返回给 LLM 的父上下文；
- `matched_text`：实际命中的子块；
- `source`：来源文件名；
- `score`：当前候选集合中的相对排序分，不是正确率；
- `kind`：文本、表格行、图片等类型；
- `citation`：带版本、页码或 Sheet 的引用说明；
- `retrieval_channels`：BM25、向量、重排等命中渠道；
- `provenance`：校验和、版本、位置、字符偏移和单元 ID；
- `raw_uri`：原始文件或抽取图片地址；
- `metadata`：上传时保存的来源元数据。

读取原始 blob：

```python
sha256 = hit["raw_uri"].rsplit("/", 1)[-1]
content, mime_type = bank.get_blob(pack_id, sha256)
```

---

## 8. Embedding、视觉和重排模型

### 8.1 本地 Embedding

```python
from xueban.memory.memory_bank import (
    MemoryBank,
    SentenceTransformerEmbedder,
)

embedder = SentenceTransformerEmbedder(
    model_name="BAAI/bge-m3",
    device=None,
)

bank = MemoryBank(embedder=embedder)
```

适合中文和中英混合文档、私有数据和本地化部署。

`device` 可为 `cpu`、`cuda` 或 `None`。生产镜像建议预下载模型，避免首次请求时临时下载。

### 8.2 OpenAI 兼容 Embedding

```python
import os

from xueban.memory.memory_bank import OpenAICompatibleEmbedder

embedder = OpenAICompatibleEmbedder(
    base_url=os.environ["EMBEDDING_BASE_URL"],
    api_key=os.environ["EMBEDDING_API_KEY"],
    model=os.environ["EMBEDDING_MODEL"],
    timeout=60.0,
    batch_size=32,
)

bank = MemoryBank(embedder=embedder)
```

`base_url` 填到 `/v1` 层级，适配器请求其 `/embeddings` 路径。

API Key 不应硬编码或提交 Git。

### 8.3 自定义 Embedder

```python
class MyEmbedder:
    name = "my-company-embedding-v1"

    def embed_documents(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3]
```

要求：

- 文档与查询使用同一向量空间；
- 维度固定；
- 返回数量、顺序与输入一致；
- 不允许空向量、全零向量、NaN 或 Infinity；
- `name` 能区分模型版本。

模块会统一归一化向量。

### 8.4 不允许同一知识包混换模型

不同模型的向量不能直接比较。模型签名不一致时会报 `EMBEDDING_MODEL_MISMATCH`。

更换模型应创建新知识包并重新建立索引，评测通过后再切换。

### 8.5 CrossEncoder 重排

```python
from xueban.memory.memory_bank import CrossEncoderReranker

reranker = CrossEncoderReranker(
    model_name="BAAI/bge-reranker-v2-m3",
    device=None,
)
```

它将“问题＋候选正文”联合打分，只处理初排后的部分候选，以平衡精度和成本。

### 8.6 视觉模型与 OCR

```python
from xueban.memory.memory_bank import OpenAIVisionDescriber

vision = OpenAIVisionDescriber(
    base_url=os.environ["VISION_BASE_URL"],
    api_key=os.environ["VISION_API_KEY"],
    model=os.environ["VISION_MODEL"],
)
```

只使用本地 OCR：

```python
from xueban.memory.memory_bank import TesseractVisionDescriber

vision = TesseractVisionDescriber()
```

Tesseract 主要识别文字，不等同于复杂图表理解。

没有配置视觉能力时，图片上传会明确报 `VISION_REQUIRED`，不会拿文件名冒充图片内容。

---

## 9. 存储结构

每个知识包：

```text
.xueban/knowledge/{pack_id}/
├── memory_bank.json
├── memory_bank.sqlite
└── blobs/
    └── ab/
        └── abcdef...完整sha256.pdf
```

### 9.1 memory_bank.json

保存知识包级信息：名称、描述、schema 版本、状态、分块参数、Embedding 模型签名、统计和时间。

不保存全部正文，避免大型 JSON 全量读写。

### 9.2 memory_bank.sqlite

| 表 | 内容 | 作用 |
|---|---|---|
| `sources` | 文件、MIME、SHA-256、状态、版本、元数据 | 来源和版本管理 |
| `units` | 段落、表格行、图片、JSON 记录 | 保留原始结构 |
| `parents` | 完整上下文 | 给生成模型使用 |
| `chunks` | 小检索块、父块 ID、偏移、向量 | 精确召回 |
| `chunks_fts` | 全文索引 | BM25 检索 |
| `links` | 顺序、描述等确定性关系 | 结构导航 |
| `meta` | schema 和 revision 等 | 缓存一致性 |

逻辑关系：

```text
Source 原始来源
  ├── Unit：段落
  ├── Unit：表格行
  └── Unit：图片
         ↓
      Parent：完整上下文
         ↓
      Child：小检索块＋全文索引＋向量
```

### 9.3 blobs

保存原始上传文件、PDF 抽取图片和 Word 抽取图片。

使用 SHA-256 内容寻址，支持去重和完整性校验。SHA-256 不是加密。

### 9.4 表格存储

每行保留来源、Sheet、行号、表头和字段值，例如：

```text
来源：收入.xlsx｜工作表：收入明细｜行：2
产品线：A 产品线｜季度：Q2｜收入：735000｜合同编号：INV-2026-001
```

不会把整张大表简单压平成一个无结构文本块。

### 9.5 图片存储

图片单元保存：

- 原始图片 blob；
- 页码或邻近文档上下文；
- 视觉描述；
- OCR 文本；
- 标签；
- 与原始来源的关系。

### 9.6 版本与去重

- 同名同内容且 `deduplicate=True`：返回已有来源；
- 同名不同内容：生成新版本；
- 不同名同内容：可保留不同业务来源，底层 blob 仍复用；
- 新版本完整入库成功后，旧版本才退出默认检索；
- 新版本失败不会破坏旧版本；
- 默认只查询 `done` 且 `is_latest=1` 的来源；
- 审计时使用 `include_history=True`。

---

## 10. 检索链路

```text
结构优先分块
    ↓
BM25 关键词召回 ─────┐
                     ├─ RRF 融合
向量语义召回 ────────┘
    ↓
编号、模态和来源 authority 调整
    ↓
可选 CrossEncoder 精排
    ↓
近重复抑制与 parent_id 去重
    ↓
返回父上下文、命中子块和血缘
```

BM25 更适合编号、型号、金额、专有名词和精确字段；向量更适合同义表达和语义相关问题。

RRF 按排名融合，避免把不同量纲的原始分数直接相加。

---

## 11. HTTP API

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/api/memory-bank/packs` | 创建知识包 |
| GET | `/api/memory-bank/packs` | 知识包列表 |
| GET | `/api/memory-bank/packs/{pack_id}` | 知识包详情 |
| DELETE | `/api/memory-bank/packs/{pack_id}` | 删除 sidecar |
| POST | `/api/memory-bank/packs/{pack_id}/text` | 上传文本 |
| POST | `/api/memory-bank/packs/{pack_id}/files` | 上传文件 |
| GET | `/api/memory-bank/packs/{pack_id}/sources` | 来源列表 |
| DELETE | `/api/memory-bank/packs/{pack_id}/sources/{source_id}` | 删除来源 |
| POST | `/api/memory-bank/packs/{pack_id}/query` | 查询 |
| GET | `/api/memory-bank/packs/{pack_id}/blobs/{sha256}` | 下载 blob |

### 11.1 创建知识包

```bash
curl -X POST http://localhost:8000/api/memory-bank/packs \
  -H "Content-Type: application/json" \
  -d '{
    "name": "销售知识库",
    "description": "销售合同和指标",
    "metadata": {"department": "sales"}
  }'
```

### 11.2 上传文本

```bash
curl -X POST \
  "http://localhost:8000/api/memory-bank/packs/${PACK_ID}/text" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "华东 Q3 续费率为 91%。",
    "source_name": "续费结论",
    "metadata": {"department": "sales"},
    "deduplicate": true
  }'
```

### 11.3 上传文件

```bash
curl -X POST \
  "http://localhost:8000/api/memory-bank/packs/${PACK_ID}/files" \
  -F "upload=@/data/收入明细.xlsx" \
  -F 'metadata={"department":"finance","authority":1.1}'
```

### 11.4 查询

```bash
curl -X POST \
  "http://localhost:8000/api/memory-bank/packs/${PACK_ID}/query" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "华东 Q3 续费率是多少？",
    "top_k": 5,
    "filters": {
      "kinds": ["table_row", "image"],
      "include_history": false
    },
    "query_variants": ["华东区域第三季度续约率"],
    "use_reranker": true
  }'
```

HTTP 查询 `top_k` 为 1～20，额外查询变体最多 4 条。

---

## 12. 技术栈与选择原因

| 技术 | 通俗说明 | 选择原因 |
|---|---|---|
| SQLite | 一个文件中的关系数据库，无需独立数据库服务 | 部署简单，符合 Xueban 单实例和嵌入式形态 |
| 事务 | 一组操作全部成功或全部回滚 | 防止正文与索引只写入一半 |
| WAL | SQLite 读写日志模式 | 改善读写并发 |
| FTS5 | SQLite 全文索引引擎 | 无需独立 Elasticsearch 即可做 BM25 |
| BM25 | 关键词相关性排序算法 | 擅长编号、数字、型号和专有词 |
| 中文 bigram | 将中文转为相邻两字词 | 规避默认分词对连续中文的不足 |
| float32 BLOB | 二进制浮点向量 | 与 SQLite 事务和来源数据一起管理 |
| NumPy | 数值计算库 | 高效进行精确向量相似度计算 |
| RRF | 按检索排名融合 | 避免直接相加不可比较的原始分数 |
| sentence-transformers | 本地向量与重排模型工具库 | 可离线、模型选择多、适合私有数据 |
| OpenAI-compatible API | 常见模型 HTTP 接口格式 | 便于接主项目模型网关，降低供应商耦合 |
| httpx | HTTP 客户端 | 调用远程 Embedding 和视觉模型 |
| openpyxl | Excel 解析 | 保留 Sheet、表头、行号和值 |
| python-docx | Word 解析 | 提取标题、段落、表格和图片 |
| PyMuPDF | PDF 解析 | 提取页文本、页码和嵌入图片 |
| Pillow | 图片处理 | 图片读取和 OCR 前置 |
| pytesseract | Tesseract Python 接口 | 本地 OCR |
| FastAPI APIRouter | 可独立挂载的路由集合 | 主文件一行接入 |
| Pydantic | 请求模型校验 | 提前拦截非法参数 |
| asyncio.to_thread | 工作线程执行同步任务 | 避免解析和模型计算阻塞事件循环 |
| SHA-256 | 内容摘要 | 去重、校验和不可变寻址 |

---

## 13. 检索评测

```python
metrics = bank.evaluate_retrieval(
    pack_id,
    cases=[
        {
            "query": "合同 SF-2026-999 的金额",
            "relevant_source_ids": ["expected-source-id"],
        }
    ],
    top_k=10,
)
```

提供：

- context precision；
- source recall；
- F1；
- Recall@K；
- MRR；
- nDCG；
- macro 和 micro 汇总。

这些属于检索/来源层指标，不等于最终答案的 claim-level F1 或 faithfulness。

不应在没有业务评测集的情况下承诺固定的召回率、准确率或 F1 提升数字。

---

## 14. 运维边界

当前向量方案为 SQLite float32 BLOB＋NumPy 精确计算，不是独立向量数据库。

适合单实例、数万到十万级 chunk 的嵌入式场景，实际容量取决于维度、内存和延迟目标。

更大数据量可将内部向量检索替换为 Qdrant、Milvus 或 pgvector，同时尽量保持公共上传和检索 API 不变。

生产环境仍需主项目或网关补充：

- 鉴权与权限隔离；
- 上传配额；
- 病毒扫描；
- 审计；
- 磁盘或应用层加密；
- 多实例并发与分布式锁；
- 数据备份和恢复演练。

本模块不执行宏、公式、脚本或嵌入对象，但原始 blob 未做应用层加密，SQLite 也不是 SQLCipher。

---

## 15. 已完成的验证与交接注意事项

已验证：

- TXT、CSV、XLSX、DOCX、PDF、PNG 上传和检索流程；
- 中文与编号检索；
- 表格行上下文；
- 图片原始血缘；
- 同名文件版本切换；
- 默认最新版本与历史版本检索；
- 删除最新版本后的旧版本恢复；
- 重启后的持久化；
- FastAPI 路由创建和真实 HTTP 上传/查询；
- 长文本父子块和原文偏移。

流程测试中的 Embedding 和视觉能力使用可控适配器，不能据此声称真实模型的业务召回率或 F1。真实模型接入后，应使用 Xueban 自己的商业数据评测集重新验证。

推荐接手顺序：

1. 先不注入模型，验证文本和文件上传、关键词查询；
2. 注入 Embedding，验证向量召回和模型签名；
3. 注入视觉能力，验证图片与扫描 PDF；
4. 按延迟需要启用重排；
5. 建立固定评测集后再调整 MemoryConfig；
6. 最后再考虑向量数据库扩容或上层 Agentic RAG。

---

## 16. 引用文献与设计参考

以下资料用于指导本模块的数据结构、分块、混合检索、多模态处理、Agent 扩展边界和评测设计。这里表示“参考其方法或问题划分”，不表示完整复现论文系统，也不表示论文中的实验指标可以直接迁移到 Xueban。

### 16.1 正式论文

[1] Jeong, S., Baek, J., Cho, S., Hwang, S. J., & Park, J. C. **Adaptive-RAG: Learning to Adapt Retrieval-Augmented Large Language Models through Question Complexity**. *Proceedings of NAACL 2024, Volume 1: Long Papers*, pp. 7036–7050, 2024.  
原文：<https://aclanthology.org/2024.naacl-long.389/>  
本项目参考：简单查询不强制进入多轮检索；复杂问题可由上层 Agent 选择不同检索策略。当前模块提供单步混合检索和 `query_variants` 接口，不内置论文中的复杂度分类器。

[2] Wang, L., Chen, H., Yang, N., Huang, X., Dou, Z., & Wei, F. **Chain-of-Retrieval Augmented Generation**. *NeurIPS 2025*.  
用户提供文件：`NeurIPS-2025-chain-of-retrieval-augmented-generation-Paper-Conference.pdf`。  
本项目参考：为多跳问题保留查询改写、子查询和多路召回入口。`query_variants` 只是底层检索能力，不包含 CoRAG 的训练、拒绝采样、子答案生成或测试时解码策略。

[3] Ru, D., Qiu, L., Hu, X., et al. **RAGChecker: A Fine-grained Framework for Diagnosing Retrieval-Augmented Generation**. *NeurIPS 2024 Datasets and Benchmarks Track*.  
用户提供文件：`NeurIPS-2024-ragchecker-a-fine-grained-framework-for-diagnosing-retrieval-augmented-generation-Paper-Datasets_and_Benchmarks_Track.pdf`。  
本项目参考：区分检索模块与生成模块的评测责任。本文件实现的是来源/检索层的 `context_precision`、`source_recall`、F1、Recall@K、MRR 和 nDCG，不是完整 RAGChecker，也不冒充 claim-level 生成评测。

[4] Singh, A., Ehtesham, A., Kumar, S., Talaei Khoei, T., & Vasilakos, A. V. **Agentic Retrieval-Augmented Generation: A Survey on Agentic RAG**. *arXiv:2501.09136v4*, 2025.  
原文：<https://arxiv.org/abs/2501.09136>  
本项目参考：将规划、反思、工具调用和多 Agent 协作留在上层，不让底层存储模块绑定某个 Agent 框架或 LLM。

[5] Liang, J., Su, G., Lin, H., et al. **Reasoning RAG via System 1 or System 2: A Survey on Reasoning Agentic Retrieval-Augmented Generation for Industry Challenges**. *Findings of IJCNLP-AACL 2025*, pp. 1954–1966, 2025.  
原文：<https://aclanthology.org/2025.findings-ijcnlp.122/>  
本项目参考：区分固定检索流程和自主 Agent 推理流程。当前交付采用可解释的固定混合检索主链路，同时给后续推理式检索保留接口。

[6] Guo, Z., Xia, L., Yu, Y., Ao, T., & Huang, C. **LightRAG: Simple and Fast Retrieval-Augmented Generation**. *Findings of EMNLP 2025*, pp. 10746–10761, 2025.  
原文：<https://aclanthology.org/2025.findings-emnlp.568/>  
本项目参考：重视结构化关系、层次化检索和增量更新。当前 `links` 只写入顺序、描述等确定性结构边，没有完整复现 LightRAG 的实体关系抽取、双层图检索或图社区摘要。

[7] Guo, Z., Ren, X., Xu, L., Zhang, J., & Huang, C. **RAG-Anything: All-in-One RAG Framework**. *arXiv:2510.12323v1*, 2025.  
原文：<https://arxiv.org/abs/2510.12323>  
本项目参考：把段落、表格行和图片统一为可检索的原子知识单元，同时保留图片 OCR/视觉描述、页码、表格结构、邻近上下文和原始 blob 血缘。当前没有完整复现其跨模态知识图和全部模态解析能力。

[8] Xu, W., Liang, Z., Mei, K., Gao, H., Tan, J., & Zhang, Y. **A-Mem: Agentic Memory for LLM Agents**. *NeurIPS 2025*.  
用户提供文件：`NeurIPS-2025-a-mem-agentic-memory-for-llm-agents-Paper-Conference.pdf`。  
本项目参考：借鉴原子知识单元、上下文、标签、向量及单元关系的组织思路。当前版本不让 LLM 在入库时自行生成或演化“事实关系”，以避免不可审计的隐式改写；动态记忆演化属于后续扩展。

### 16.2 工程实践资料

[9] **《RAG 字节跳动实践手册》**。用户提供的工程实践资料。  
用户提供文件：`39E6C9ADF20836A45F7C765C31E_B2F36ACF_167F1B.pdf`。  
本项目参考：动态/结构化分块、多粒度表示、混合检索、重排和多模态处理等工程思路。该资料按实践材料使用，不将其表述为同行评审论文。

[10] **中文 RAG 实践笔记**。用户提供的 `。1.txt`。  
本项目参考：父子分块、BM25＋向量、RRF 和 BGE Reranker 的组合建议。资料中的效果百分比没有对应的 Xueban 实验数据，因此未写成本项目的性能承诺。

### 16.3 引用与实现边界

- 本模块采用“结构优先的父子分块＋文本子块重叠＋BM25/向量混合召回＋RRF＋可选重排”，不是对上述任一篇论文的完整复刻。
- 多模态部分当前覆盖文本、表格和图片的统一检索及血缘，不代表已经支持论文中所有数学公式、复杂图表推理或跨模态图算法。
- Agentic RAG、CoRAG 和动态记忆演化主要作为上层扩展方向；当前文件保持存储与检索层高内聚、低耦合。
- 论文或工程资料中的实验结果不构成 Xueban 的性能保证。真实 Recall、Precision、F1 和答案可靠性必须在固定业务数据集上单独评测。
