"""PackGraph — KnowledgePack 的确定性知识图层（Semantica 启发，2026-09-03 调研）。

设计原则（借鉴 Semantica 的「确定性基础设施」路线，与 LightRAG 的 LLM 黑盒
抽取互补）：

  - **构图不依赖 LLM**：实体来自 wiki-link（``[[...]]``，Obsidian 惯例，与
    legacy ``memory/layers.py`` 一致）、Markdown ATX 标题（h1-h3）、英文
    首字母大写多词短语；边来自标题包含（mentioned_in）、同 chunk 共现
    （co_occurs，带权重）、相邻 wiki-link 链（links_to）。全部确定性、可复现。
  - **溯源是结构的属性**：每个节点/边携带内容级血缘
    ``{source_id, chunk_index, checksum}``（chunk 切分与 ingest_pipeline 同一
    算法，checksum 是该 source 正文的 SHA-256），补上「知识内容级溯源」缺口。
  - **冲突标记而非静默覆盖**：同一 wiki-link 别名指向不同目标时记入
    ``conflicts[]``（SKOS 受控词表意义的 prefLabel 冲突），不覆盖、不吞掉。

存储：``.xueban/knowledge/{pack_id}/graph.json``，与 index.json /
hier_index.json 同目录，遵守同样的 per-pack lock + atomic_write_text 约定。
失败语义与 LightRAG hook 一致：构建失败只告警，关键词索引永远是权威 fallback。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from xueban.memory.ingest_pipeline import (
    _checksum,
    atomic_write_text,
    pack_lock,
    parse_source,
)
from xueban.memory.knowledge_pack import KnowledgePack, KnowledgeSource


logger = logging.getLogger(__name__)


_KNOWLEDGE_ROOT = Path(".xueban/knowledge")
_GRAPH_FILENAME = "graph.json"
_SCHEMA = "pack_graph/2"  # v2: 新增 part_of 标题层级边；is_graph_fresh 校验 schema

# 单个 chunk 内实体数超过该值时跳过共现边（避免 O(n²) 边爆炸）。
_MAX_COOCCUR_ENTITIES_PER_CHUNK = 30

_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]+?))?\]\]")
_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_EN_PHRASE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_-]*(?:[ \t]+[A-Z][A-Za-z0-9_-]*){1,3})\b")

# 句子开头高频词误报过滤（确定性 NER-lite 的代价，宁可漏不可噪）。
_PHRASE_STOPWORDS = {
    "The", "A", "An", "In", "On", "At", "For", "And", "But", "Or", "This",
    "That", "These", "Those", "It", "Its", "As", "If", "When", "While", "With",
}

# 关系类型（受控词表，写入端只允许这四种）。
REL_MENTIONED_IN = "mentioned_in"
REL_CO_OCCURS = "co_occurs"
REL_LINKS_TO = "links_to"
REL_PART_OF = "part_of"

# 关系族映射（移植 Shadow graphService.ts 的 relation family 设计：
# structural / sequential / semantic 六族分类中本图层覆盖的三族）。前端
# GraphLink.relationFamily 字段直接消费，用于着色/分组。
_RELATION_FAMILY = {
    REL_MENTIONED_IN: "structural",
    REL_PART_OF: "structural",
    REL_LINKS_TO: "sequential",
    REL_CO_OCCURS: "semantic",
}


# ---------------------------------------------------------------------------
# 文本切块（与 ingest_pipeline.chunk_text 同算法，但保留字符偏移，供标题包含
# 判断与 chunk 级溯源使用；复制自 runtime/hier_index._chunk_with_offsets 以
# 避免 memory → runtime 的层级反向依赖）
# ---------------------------------------------------------------------------


def _chunk_spans(text: str, chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not text:
        return []
    overlap = max(0, min(overlap, chunk_size - 1))
    step = chunk_size - overlap
    spans: List[Dict[str, Any]] = []
    i = 0
    n = len(text)
    while i < n:
        spans.append(
            {
                "index": len(spans),
                "start": i,
                "end": min(i + chunk_size, n),
                "text": text[i : i + chunk_size],
            }
        )
        i += step
    return spans


def _find_headings(text: str) -> List[Tuple[int, str, int]]:
    """[(level, title, char_offset)]，语义同 runtime/hier_index._find_headings。"""
    out: List[Tuple[int, str, int]] = []
    pos = 0
    for line in text.splitlines(keepends=True):
        m = _HEADING_RE.match(line.rstrip("\r\n"))
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), pos))
        pos += len(line)
    return out


def _slug(name: str) -> str:
    """实体名归一为节点 id：小写、非字母数字（含 CJK）折叠为 '-'。"""
    slug = re.sub(r"[^\w一-鿿]+", "-", name.strip().lower(), flags=re.UNICODE)
    return slug.strip("-")


def _extract_phrases(text: str) -> List[str]:
    """英文首字母大写多词短语（NER-lite）。CJK 不走这里——中文实体靠
    wiki-link / 标题表达，避免确定性规则在中文上产生大量噪声。"""
    out: List[str] = []
    for m in _EN_PHRASE_RE.finditer(text):
        phrase = m.group(1).strip()
        if phrase.split()[0] in _PHRASE_STOPWORDS:
            continue
        out.append(phrase)
    return out


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class PackGraphBuilder:
    """Accumulate sources → deterministic nodes/edges/conflicts → payload."""

    def __init__(self) -> None:
        # slug -> node record
        self._nodes: Dict[str, Dict[str, Any]] = {}
        # (source_slug, target_slug, relation) -> edge record
        self._edges: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        # alias.lower() -> set of target slugs（wiki-link 受控词表）
        self._alias_targets: Dict[str, Set[str]] = {}

    # -- nodes --------------------------------------------------------------

    def _touch_node(
        self,
        name: str,
        kind: str,
        *,
        source_id: str,
        chunk_index: Optional[int],
        checksum: str,
    ) -> Optional[str]:
        slug = _slug(name)
        if not slug:
            return None
        node = self._nodes.get(slug)
        if node is None:
            node = {
                "id": slug,
                "name": name.strip(),
                "kind": kind,
                "aliases": set(),
                "mentions": 0,
                "sources": {},
            }
            self._nodes[slug] = node
        node["mentions"] += 1
        if name.strip() != node["name"]:
            node["aliases"].add(name.strip())
        if source_id and chunk_index is not None:
            node["sources"][(source_id, chunk_index)] = {
                "source_id": source_id,
                "chunk_index": chunk_index,
                "checksum": checksum,
            }
        return slug

    def _touch_edge(
        self,
        source_slug: str,
        target_slug: str,
        relation: str,
        *,
        source_id: str,
        chunk_index: int,
    ) -> None:
        if not source_slug or not target_slug or source_slug == target_slug:
            return
        # co_occurs 语义上无向：固定端点序，避免 (a,b)/(b,a) 两条重复边。
        if relation == REL_CO_OCCURS and target_slug < source_slug:
            source_slug, target_slug = target_slug, source_slug
        key = (source_slug, target_slug, relation)
        edge = self._edges.get(key)
        if edge is None:
            edge = {
                "source": source_slug,
                "target": target_slug,
                "relation": relation,
                "weight": 0,
                "sources": {},
            }
            self._edges[key] = edge
        edge["weight"] += 1
        edge["sources"][(source_id, chunk_index)] = {
            "source_id": source_id,
            "chunk_index": chunk_index,
        }

    # -- source accumulation --------------------------------------------------

    def add_source(self, source: KnowledgeSource, text: str, *, chunk_size: int, overlap: int) -> None:
        """Extract entities/edges from one parsed source (deterministic)."""
        if not text.strip():
            return
        checksum = _checksum(text)
        spans = _chunk_spans(text, chunk_size, overlap)
        headings = _find_headings(text)

        # 标题节点（kind=heading）；chunk 归属 = 标题区间 [start, 下一个同级或
        # 更高级标题）包含 chunk.start。与 hier_index 的章节划分语义一致。
        # 标题节点本身的溯源 = 包含标题行的那个 chunk。
        def _chunk_index_at(offset: int) -> Optional[int]:
            for span in spans:
                if int(span["start"]) <= offset < int(span["end"]):
                    return int(span["index"])
            return None

        heading_slugs: List[Tuple[str, int, int]] = []  # (slug, start, end)
        heading_stack: List[Tuple[int, str, int]] = []  # (level, slug, chunk_index)
        for i, (level, title, start) in enumerate(headings):
            h_chunk = _chunk_index_at(start)
            slug = self._touch_node(
                title, "heading",
                source_id=source.source_id,
                chunk_index=h_chunk,
                checksum=checksum,
            )
            if not slug:
                continue
            end = len(text)
            for j in range(i + 1, len(headings)):
                if headings[j][0] <= level:
                    end = headings[j][2]
                    break
            heading_slugs.append((slug, start, end))
            # 标题层级边（Shadow 的 ParentOf/Contains 设计）：子标题 → 最近的
            # 上级标题。让 h2/h3 节点接入图而不是成为孤岛。
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            if heading_stack and h_chunk is not None:
                self._touch_edge(
                    slug, heading_stack[-1][1], REL_PART_OF,
                    source_id=source.source_id, chunk_index=h_chunk,
                )
            heading_stack.append((level, slug, h_chunk if h_chunk is not None else 0))

        def _heading_of(chunk_start: int) -> Optional[str]:
            best: Optional[Tuple[str, int]] = None  # (slug, start) 取最近标题
            for slug, start, end in heading_slugs:
                if start <= chunk_start < end:
                    if best is None or start > best[1]:
                        best = (slug, start)
            return best[0] if best else None

        for span in spans:
            chunk_text = str(span["text"])
            ci = int(span["index"])

            wikilinks = _WIKILINK_RE.findall(chunk_text)
            # wiki-link 文本先遮罩再做短语抽取，避免 [[Context Graph]] 被
            # 短语正则二次计数。
            phrases = _extract_phrases(_WIKILINK_RE.sub(" ", chunk_text))

            chunk_slugs: List[str] = []
            link_slugs: List[str] = []
            for target, alias in wikilinks:
                slug = self._touch_node(
                    target, "concept",
                    source_id=source.source_id, chunk_index=ci, checksum=checksum,
                )
                if not slug:
                    continue
                chunk_slugs.append(slug)
                link_slugs.append(slug)
                # 受控词表：alias（或 target 自身作为隐式显示名）→ target
                label = (alias or target).strip()
                if label:
                    self._alias_targets.setdefault(label.lower(), set()).add(slug)

            for phrase in phrases:
                slug = self._touch_node(
                    phrase, "concept",
                    source_id=source.source_id, chunk_index=ci, checksum=checksum,
                )
                if slug:
                    chunk_slugs.append(slug)

            # 边：标题包含
            heading_slug = _heading_of(int(span["start"]))
            if heading_slug:
                for slug in dict.fromkeys(chunk_slugs):
                    if slug != heading_slug:
                        self._touch_edge(
                            slug, heading_slug, REL_MENTIONED_IN,
                            source_id=source.source_id, chunk_index=ci,
                        )

            # 边：同 chunk 共现（无向、加权）
            unique = list(dict.fromkeys(chunk_slugs))
            if len(unique) <= _MAX_COOCCUR_ENTITIES_PER_CHUNK:
                for a_i in range(len(unique)):
                    for b_i in range(a_i + 1, len(unique)):
                        self._touch_edge(
                            unique[a_i], unique[b_i], REL_CO_OCCURS,
                            source_id=source.source_id, chunk_index=ci,
                        )

            # 边：相邻 wiki-link 叙事链（有向）
            for a, b in zip(link_slugs, link_slugs[1:]):
                self._touch_edge(
                    a, b, REL_LINKS_TO,
                    source_id=source.source_id, chunk_index=ci,
                )

    # -- output -----------------------------------------------------------------

    def _detect_conflicts(self) -> List[Dict[str, Any]]:
        """SKOS 意义的词表冲突：同一显示名（别名）指向多个不同目标。"""
        conflicts: List[Dict[str, Any]] = []
        for label, targets in sorted(self._alias_targets.items()):
            if len(targets) > 1:
                conflicts.append(
                    {
                        "entity": label,
                        "field": "wikilink_alias",
                        "values": sorted(targets),
                        "severity": "medium",
                    }
                )
        return conflicts

    def build(self, pack_id: str) -> Dict[str, Any]:
        nodes: List[Dict[str, Any]] = []
        for slug in sorted(self._nodes):
            node = self._nodes[slug]
            nodes.append(
                {
                    "id": node["id"],
                    "name": node["name"],
                    "kind": node["kind"],
                    "aliases": sorted(node["aliases"]),
                    "mentions": node["mentions"],
                    "sources": sorted(
                        node["sources"].values(),
                        key=lambda s: (s["source_id"], s["chunk_index"]),
                    ),
                }
            )
        edges: List[Dict[str, Any]] = []
        for key in sorted(self._edges):
            edge = self._edges[key]
            edges.append(
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "relation": edge["relation"],
                    "weight": edge["weight"],
                    "sources": sorted(
                        edge["sources"].values(),
                        key=lambda s: (s["source_id"], s["chunk_index"]),
                    ),
                }
            )
        conflicts = self._detect_conflicts()
        return {
            "pack_id": pack_id,
            "schema": _SCHEMA,
            "built_at": _now(),
            "nodes": nodes,
            "edges": edges,
            "conflicts": conflicts,
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "conflicts": len(conflicts),
            },
        }


def _now() -> str:
    """ISO 8601 UTC `Z` 后缀（与 runtime/hier_index 同一约定）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Persistence（与 index.json / hier_index.json 同一约定）
# ---------------------------------------------------------------------------


def graph_path(pack_id: str, *, root: Optional[Path] = None) -> Path:
    return (root or _KNOWLEDGE_ROOT) / pack_id / _GRAPH_FILENAME


def load_graph(pack_id: str, *, root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load the persisted graph; None when missing/corrupted/mismatched."""
    target = graph_path(pack_id, root=root)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("pack_graph: corrupted graph for pack %s: %s", pack_id, exc)
        return None
    if not isinstance(payload, dict) or payload.get("pack_id") != pack_id:
        return None
    return payload


def persist_graph(payload: Dict[str, Any], *, root: Optional[Path] = None) -> Path:
    pack_id = str(payload.get("pack_id") or "")
    with pack_lock(pack_id):
        atomic_write_text(
            graph_path(pack_id, root=root),
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        )
    return graph_path(pack_id, root=root)


def build_graph(pack: KnowledgePack, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Parse all sources, build the deterministic graph and persist it.

    与 hier_index.build_index 同一容错语义：解析失败的 source 跳过并告警，
    全部失败时产出空图而不是抛异常。
    """
    builder = PackGraphBuilder()
    for source in pack.sources:
        try:
            text = parse_source(source)
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the graph
            logger.warning(
                "pack_graph: skipping unparseable source %s of pack %s: %s",
                source.source_id,
                pack.pack_id,
                exc,
            )
            continue
        builder.add_source(
            source,
            text,
            chunk_size=pack.retrieval_profile.chunk_size,
            overlap=pack.retrieval_profile.overlap,
        )
    payload = builder.build(pack.pack_id)
    payload["fingerprint"] = graph_fingerprint(pack)
    persist_graph(payload, root=root)
    return payload


# ---------------------------------------------------------------------------
# 构建指纹（移植 Shadow graph_engine 的 graph_build_state 设计：内容指纹不变
# 就跳过重建，为将来的真增量构建打底）
# ---------------------------------------------------------------------------


def graph_fingerprint(pack: KnowledgePack) -> str:
    """排序后 ``source_id:checksum`` 列表 + 切块参数的 SHA-256。

    ingest 完成后的 pack 上 checksum 已填充；任何 source 内容变化都会改变
    指纹。chunk_size/overlap 也参与——chunk 级溯源（chunk_index）随切块参数
    变化，不改指纹会导致 PATCH 调参后的自动 reindex 跳过重建。注意：这是
    「是否需要重建」的判定，不是内容寻址存储。
    """
    parts = sorted(f"{s.source_id}:{s.checksum}" for s in pack.sources)
    parts.append(
        f"chunk:{pack.retrieval_profile.chunk_size}:{pack.retrieval_profile.overlap}"
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def is_graph_fresh(pack: KnowledgePack, *, root: Optional[Path] = None) -> bool:
    """已持久化的图与当前 pack 内容指纹一致且 schema 相同 → 无需重建。

    schema 校验即 Shadow graph_build_state 的 generation 语义：构图逻辑升级
    （schema bump）后旧图一律判过期，强制重建。
    """
    payload = load_graph(pack.pack_id, root=root)
    if payload is None or payload.get("schema") != _SCHEMA:
        return False
    return payload.get("fingerprint") == graph_fingerprint(pack)


# ---------------------------------------------------------------------------
# 图查询蓝图（移植 Shadow graph_engine GraphQuery trait 的核心方法：
# 健康度量 / N 跳邻域 / 最短路。全部纯函数、确定性、无 LLM）
# ---------------------------------------------------------------------------


def _adjacency(edges: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """无向邻接表（co_occurs 本无向；mentioned_in/links_to 遍历时也按无向走）。"""
    adj: Dict[str, Set[str]] = {}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if not s or not t or s == t:
            continue
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)
    return adj


def graph_health(nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]) -> Dict[str, Any]:
    """图健康指标：孤岛 / hub / 密度 / 连通分量（Shadow models.rs 健康度量移植）。

    接受任何 ``[{id}]`` / ``[{source, target}]`` 形状——PackGraph payload 与
    GraphML 导出的 ``{nodes, links}`` 均可直接传入。
    """
    ids = [str(n.get("id") or "") for n in nodes if n.get("id")]
    id_set = set(ids)
    adj_full = _adjacency(edges)
    adj = {i: adj_full.get(i, set()) & id_set for i in ids}

    n = len(ids)
    m = sum(len(v) for v in adj.values()) // 2
    density = round(2 * m / (n * (n - 1)), 4) if n > 1 else 0.0
    isolated = sorted(i for i in ids if not adj[i])

    seen: Set[str] = set()
    components = 0
    for i in ids:
        if i in seen:
            continue
        components += 1
        stack = [i]
        seen.add(i)
        while stack:
            for nb in adj[stack.pop()]:
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)

    name_by_id = {str(x.get("id")): str(x.get("name") or x.get("id")) for x in nodes}
    hubs = [
        {"id": i, "name": name_by_id.get(i, i), "degree": len(adj[i])}
        for i in sorted(ids, key=lambda x: len(adj[x]), reverse=True)[:5]
        if adj[i]
    ]
    return {
        "nodes": n,
        "edges": m,
        "density": density,
        "components": components,
        "isolated_count": len(isolated),
        "isolated": isolated[:20],
        "hubs": hubs,
    }


def subgraph(payload: Dict[str, Any], node_id: str, *, hops: int = 1) -> Dict[str, Any]:
    """N 跳邻域子图（GraphQuery.subgraph 蓝图）。返回 payload 同形状的子集。"""
    edges = payload.get("edges") or []
    adj = _adjacency(edges)
    keep = {node_id}
    frontier = {node_id}
    for _ in range(max(0, hops)):
        nxt: Set[str] = set()
        for cur in frontier:
            nxt |= adj.get(cur, set())
        nxt -= keep
        keep |= nxt
        frontier = nxt
        if not frontier:
            break
    return {
        "nodes": [n for n in payload.get("nodes") or [] if n.get("id") in keep],
        "edges": [
            e for e in edges if e.get("source") in keep and e.get("target") in keep
        ],
    }


def shortest_path(
    payload: Dict[str, Any], source_id: str, target_id: str
) -> Optional[List[str]]:
    """BFS 最短路（无向，GraphQuery.multi_hop 的基础件）。不可达返回 None。"""
    if source_id == target_id:
        return [source_id]
    adj = _adjacency(payload.get("edges") or [])
    if source_id not in adj:
        return None
    parent: Dict[str, Optional[str]] = {source_id: None}
    queue = [source_id]
    for cur in queue:
        for nb in adj.get(cur, ()):
            if nb in parent:
                continue
            parent[nb] = cur
            if nb == target_id:
                path = [target_id]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])  # type: ignore[arg-type]
                path.reverse()
                return path
            queue.append(nb)
    return None


# ---------------------------------------------------------------------------
# Export / query helpers
# ---------------------------------------------------------------------------


def to_graph_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a persisted payload to the frontend GraphData shape
    ({nodes, links}) — 与 api/knowledge._parse_graphml 的输出形状一致，
    节点额外携带 ``sources``（内容级血缘，供溯源 UI 使用）。``conflicts``
    挂到顶层，调用方决定透出方式。
    """
    raw_nodes = payload.get("nodes") or []
    raw_edges = payload.get("edges") or []

    degree: Dict[str, int] = {}
    links: List[Dict[str, Any]] = []
    for edge in raw_edges:
        src, tgt = str(edge.get("source") or ""), str(edge.get("target") or "")
        if not src or not tgt:
            continue
        links.append(
            {
                "source": src,
                "target": tgt,
                "relation": str(edge.get("relation") or "related"),
                "relationFamily": _RELATION_FAMILY.get(
                    str(edge.get("relation") or ""), "semantic"
                ),
                "weight": edge.get("weight", 1),
            }
        )
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    nodes: List[Dict[str, Any]] = []
    for node in raw_nodes:
        nid = str(node.get("id") or "")
        if not nid:
            continue
        aliases = node.get("aliases") or []
        nodes.append(
            {
                "id": nid,
                "name": str(node.get("name") or nid),
                # 与 GraphML 导出同一归一：type='tag'，原始 kind 放 raw_type。
                "type": "tag",
                "raw_type": str(node.get("kind") or ""),
                "text": ", ".join(str(a) for a in aliases),
                "val": min(degree.get(nid, 0) + 1, 10),
                "mentions": node.get("mentions", 0),
                "sources": node.get("sources") or [],
            }
        )
    return {
        "nodes": nodes,
        "links": links,
        "conflicts": payload.get("conflicts") or [],
    }


def query_context(
    pack_id: str,
    question: str,
    *,
    root: Optional[Path] = None,
    max_matched: int = 3,
    max_related: int = 5,
) -> Optional[Dict[str, Any]]:
    """图遍历扩展召回：query 中提及的实体 → 1 跳邻居（按边权排序）。

    返回 ``{"matched": [...], "related": [...]}``；图不存在 / 无命中时返回
    None（调用方据此省略 graph_context 字段）。确定性、无 LLM。
    """
    payload = load_graph(pack_id, root=root)
    if not payload or not question.strip():
        return None

    q = question.lower()
    matched: List[Dict[str, Any]] = []
    for node in payload.get("nodes") or []:
        name = str(node.get("name") or "")
        if len(name) < 2:
            continue
        surfaces = [name, *(str(a) for a in node.get("aliases") or [])]
        if any(len(s) >= 2 and s.lower() in q for s in surfaces):
            matched.append(
                {"id": node["id"], "name": name, "kind": node.get("kind", "")}
            )
    if not matched:
        return None
    matched = matched[:max_matched]
    matched_ids = {m["id"] for m in matched}

    # 1 跳邻居（所有关系类型；co_occurs 存的是排序端点，按无向处理）
    related: Dict[str, Dict[str, Any]] = {}
    for edge in payload.get("edges") or []:
        src, tgt = str(edge.get("source") or ""), str(edge.get("target") or "")
        via = ""
        other = ""
        if src in matched_ids and tgt not in matched_ids:
            via, other = src, tgt
        elif tgt in matched_ids and src not in matched_ids:
            via, other = tgt, src
        else:
            continue
        weight = int(edge.get("weight") or 1)
        entry = related.get(other)
        if entry is None or weight > entry["weight"]:
            related[other] = {
                "id": other,
                "relation": str(edge.get("relation") or "related"),
                "weight": weight,
                "via": via,
            }

    name_by_id = {str(n.get("id")): str(n.get("name") or "") for n in payload.get("nodes") or []}
    related_list = sorted(related.values(), key=lambda r: r["weight"], reverse=True)[
        :max_related
    ]
    for entry in related_list:
        entry["name"] = name_by_id.get(entry["id"], entry["id"])
        entry["via_name"] = name_by_id.get(entry["via"], entry["via"])

    return {"matched": matched, "related": related_list}


__all__ = [
    "PackGraphBuilder",
    "REL_MENTIONED_IN",
    "REL_CO_OCCURS",
    "REL_LINKS_TO",
    "REL_PART_OF",
    "graph_path",
    "load_graph",
    "persist_graph",
    "build_graph",
    "graph_fingerprint",
    "is_graph_fresh",
    "graph_health",
    "subgraph",
    "shortest_path",
    "to_graph_data",
    "query_context",
]
