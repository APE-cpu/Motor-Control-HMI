"""RAG 检索引擎测试：分词、切块、BM25 检索相关性、索引失效检测。"""
import time

import pytest

from ai.rag import RAGIndex, _split_chunks, _tokenize, format_context


def test_分词_中英混合():
    tokens = _tokenize("QEP编码器 2500线 pole_pairs=4")
    assert "qep" in tokens
    assert "2500" in tokens
    assert "编码" in tokens and "码器" in tokens   # 字符二元组


def test_切块_按标题分节():
    text = "# 甲\n" + "转速环参数整定方法。" * 10 + "\n# 乙\n" + "电流环带宽设置。" * 10
    chunks = _split_chunks(text, "doc")
    labels = {label for label, _ in chunks}
    assert "doc§甲" in labels and "doc§乙" in labels


def test_过短碎片被丢弃():
    assert _split_chunks("# t\n太短", "doc") == []


@pytest.fixture
def kb(tmp_path):
    (tmp_path / "sensor.md").write_text(
        "# 霍尔传感器\n霍尔传感器分辨率为60度电角度，三路信号组成六个换相状态，"
        "适合低成本方波换相场景，不适合高精度位置控制。\n"
        "# 编码器\n增量式编码器QEP线数2500，四倍频后每转一万个计数，"
        "上电需要寻找Z脉冲校准零位，适合高性能伺服控制。",
        encoding="utf-8")
    (tmp_path / "mpc.md").write_text(
        "# FCS-MPC\n有限集模型预测控制遍历八个基本电压矢量，价值函数包含电流跟踪误差"
        "与开关次数惩罚，约束通过大罚值处理，预测时域通常取一到二拍。",
        encoding="utf-8")
    idx = RAGIndex(list(tmp_path.glob("*.md")))
    idx.build()
    return idx


def test_检索_命中相关块(kb):
    hits = kb.search("霍尔传感器的分辨率是多少")
    assert hits, "应检索到结果"
    assert "霍尔" in hits[0][2]

    hits = kb.search("MPC的价值函数怎么定义")
    assert "价值函数" in hits[0][2]


def test_检索_无关问题返回空(kb):
    assert kb.search("今天天气如何适合郊游吗", min_score=3.0) == []


def test_索引失效检测(kb, tmp_path):
    assert not kb.needs_rebuild()
    time.sleep(0.05)
    f = tmp_path / "sensor.md"
    f.write_text(f.read_text(encoding="utf-8") + "\n更新", encoding="utf-8")
    assert kb.needs_rebuild()


def test_格式化引用():
    ctx = format_context([(5.0, "doc§节", "内容片段")])
    assert "【资料1｜doc§节】" in ctx and "内容片段" in ctx


def test_pdf按页入索引_出处带页码(tmp_path, monkeypatch):
    import ai.rag as rag_mod
    fake = tmp_path / "教材.pdf"
    fake.write_bytes(b"%PDF-fake")
    monkeypatch.setattr(rag_mod, "_read_pdf_pages", lambda p: [
        (f"{p.stem}·P1", "同步电机的电磁转矩由磁场储能对转子角求偏导得到，"
                         "这是机电能量转换的基本原理。" * 3),
        (f"{p.stem}·P2", "感应电机的等效电路包含定子电阻漏抗与励磁支路，"
                         "转差率决定转子侧功率分配。" * 3),
    ])
    idx = rag_mod.RAGIndex([fake])
    assert idx.build() >= 2
    hits = idx.search("机电能量转换的基本原理是什么")
    assert hits[0][1] == "教材·P1"


def test_pdf损坏或无pypdf时静默跳过(tmp_path):
    bad = tmp_path / "坏文件.pdf"
    bad.write_bytes(b"not a pdf at all")
    from ai.rag import RAGIndex
    idx = RAGIndex([bad])
    assert idx.build() == 0   # 不抛异常
