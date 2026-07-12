"""轻量 RAG 检索引擎：BM25 + 中文字符二元组分词（离线可用）。

知识来源：项目自带文档 + knowledge/ 目录下用户放置的 md/txt/pdf 文件。
按标题/段落切块，检索 top-k 片段注入提示词。
PDF 需要 pypdf（未安装时自动跳过）；提取结果按 mtime 缓存，出处带页码。
"""
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import List, Tuple

_CHUNK_CHARS = 500      # 目标块大小（字符）
_CHUNK_OVERLAP = 80     # 相邻块重叠
_K1, _B = 1.5, 0.75     # BM25 参数


def _tokenize(text: str) -> List[str]:
    """中文字符二元组 + 英文/数字整词。无需分词库，对术语召回足够。"""
    tokens: List[str] = []
    for m in re.finditer(r"[A-Za-z0-9_.\-]+|[一-鿿]+", text.lower()):
        seg = m.group()
        if seg[0].isascii():
            tokens.append(seg)
        else:
            if len(seg) == 1:
                tokens.append(seg)
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


def _split_chunks(text: str, source: str) -> List[Tuple[str, str]]:
    """先按 markdown 标题分节，节内按长度滑窗切块。返回 [(出处, 块文本)]。"""
    sections: List[Tuple[str, str]] = []
    cur_title, cur_lines = "", []
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s+", line):
            if cur_lines:
                sections.append((cur_title, "\n".join(cur_lines)))
            cur_title = line.lstrip("# ").strip()
            cur_lines = [line]
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append((cur_title, "\n".join(cur_lines)))

    chunks: List[Tuple[str, str]] = []
    for title, body in sections:
        body = body.strip()
        if not body:
            continue
        label = f"{source}§{title}" if title else source
        step = _CHUNK_CHARS - _CHUNK_OVERLAP
        for i in range(0, len(body), step):
            piece = body[i:i + _CHUNK_CHARS].strip()
            # 目录页的点线引导符（"标题.......页码"）占比高，无检索价值
            dots = piece.count(".") + piece.count("…") + piece.count("·")
            if len(piece) >= 30 and dots < len(piece) * 0.25:
                chunks.append((label, piece))
            if i + _CHUNK_CHARS >= len(body):
                break
    return chunks


_OCR_ENGINE = None      # RapidOCR 单例；False 表示不可用


def _get_ocr():
    """懒加载本地 OCR 引擎（rapidocr_onnxruntime）；未安装返回 None。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR_ENGINE = RapidOCR()
        except Exception:
            _OCR_ENGINE = False
    return _OCR_ENGINE or None


def _ocr_page(page) -> str:
    """对扫描页的内嵌图片做本地 OCR，返回识别文本（不可用时空串）。"""
    engine = _get_ocr()
    if engine is None:
        return ""
    texts = []
    for img in page.images:
        try:
            result, _ = engine(img.data)
        except Exception:
            continue
        if result:
            texts.extend(t for _box, t, _score in result)
    return "\n".join(texts)


def _read_pdf_pages(p: Path, progress=None) -> List[Tuple[str, str]]:
    """提取 PDF 逐页文本，返回 [(出处标签, 页文本)]。

    - 文本层缺失的页（扫描版）自动走本地 OCR（需 rapidocr_onnxruntime）
    - 未安装 pypdf 或提取失败返回 []（调用方静默跳过）
    - 结果缓存到同目录 .pdfcache/，按文件 mtime 失效——
      大部头教材只在首次或更新后慢一次（OCR 结果同样进缓存）
    - progress(msg)：逐页进度回调，供 UI 显示
    """
    cache = p.parent / ".pdfcache" / f"{p.stem}_{int(p.stat().st_mtime)}.json"
    if cache.exists():
        try:
            return [tuple(x) for x in json.loads(
                cache.read_text(encoding="utf-8"))]
        except (json.JSONDecodeError, OSError):
            pass
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(str(p))
        total = len(reader.pages)
        pages = []
        for i, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").strip()
            if not text:
                if progress:
                    progress(f"OCR {p.stem} 第 {i}/{total} 页…")
                text = _ocr_page(page).strip()
            elif progress and i % 20 == 0:
                progress(f"提取 {p.stem} 第 {i}/{total} 页…")
            # PDF 数学字体常产生不成对代理字符（U+D800-DFFF），无法编码须剔除
            text = re.sub(r"[\ud800-\udfff]", "", text)
            if text:
                pages.append((f"{p.stem}·P{i}", text))
    except Exception:
        return []
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(pages, ensure_ascii=False),
                         encoding="utf-8")
    except OSError:
        pass
    return pages


class RAGIndex:
    """BM25 倒排索引。build() 后用 search() 检索。"""

    def __init__(self, paths: List[Path]) -> None:
        self._paths = [Path(p) for p in paths]
        self._chunks: List[Tuple[str, str]] = []       # (出处, 文本)
        self._tf: List[Counter] = []
        self._df: Counter = Counter()
        self._doc_len: List[int] = []
        self._avg_len = 1.0
        self._mtimes: dict = {}

    # ─── 构建 ───────────────────────────────────────────────
    def needs_rebuild(self) -> bool:
        current = {str(p): p.stat().st_mtime for p in self._paths if p.exists()}
        return current != self._mtimes

    def build(self, progress=None) -> int:
        """重建索引，返回块数。progress(msg) 为可选进度回调。"""
        self._chunks, self._tf, self._doc_len = [], [], []
        self._df = Counter()
        self._mtimes = {}
        for p in self._paths:
            if not p.exists():
                continue
            self._mtimes[str(p)] = p.stat().st_mtime
            if p.suffix.lower() == ".pdf":
                for label, page_text in _read_pdf_pages(p, progress):
                    self._chunks.extend(_split_chunks(page_text, label))
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._chunks.extend(_split_chunks(text, p.stem))
        for _src, chunk in self._chunks:
            tf = Counter(_tokenize(chunk))
            self._tf.append(tf)
            self._doc_len.append(sum(tf.values()))
            for term in tf:
                self._df[term] += 1
        self._avg_len = (sum(self._doc_len) / len(self._doc_len)) if self._doc_len else 1.0
        return len(self._chunks)

    @property
    def num_chunks(self) -> int:
        return len(self._chunks)

    @property
    def sources(self) -> List[str]:
        return [str(p) for p in self._paths if p.exists()]

    # ─── 检索 ───────────────────────────────────────────────
    def search(self, query: str, top_k: int = 4,
               min_score: float = 1.0) -> List[Tuple[float, str, str]]:
        """返回 [(得分, 出处, 块文本)]，按得分降序。"""
        if not self._chunks:
            return []
        n = len(self._chunks)
        q_terms = set(_tokenize(query))
        scores = [0.0] * n
        for term in q_terms:
            df = self._df.get(term)
            if not df:
                continue
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for i, tf in enumerate(self._tf):
                f = tf.get(term)
                if not f:
                    continue
                denom = f + _K1 * (1 - _B + _B * self._doc_len[i] / self._avg_len)
                scores[i] += idf * f * (_K1 + 1) / denom
        ranked = sorted(range(n), key=scores.__getitem__, reverse=True)
        return [(scores[i], self._chunks[i][0], self._chunks[i][1])
                for i in ranked[:top_k] if scores[i] >= min_score]


def format_context(hits: List[Tuple[float, str, str]]) -> str:
    """把检索结果格式化为提示词中的参考资料块。"""
    parts = []
    for k, (_score, src, text) in enumerate(hits, 1):
        parts.append(f"【资料{k}｜{src}】\n{text}")
    return "\n\n".join(parts)
