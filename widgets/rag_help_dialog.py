"""RAG 工作原理说明对话框（AI 分析页帮助）。"""
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout,
)

_HTML = """
<h2>RAG（检索增强生成）工作原理</h2>
<p><b>关键认知：大模型自己不做检索。</b>检索由本机的 BM25 算法完成
（纯本地数学计算，毫秒级、零费用），大模型只负责“阅读”检索员递过去的片段。</p>

<h3>知识库来源（三处合并）</h3>
<ul>
<li><b>项目自带文档</b>：README、使用说明书、软件介绍（随软件走）</li>
<li><b>knowledge/ 目录</b>：你放入的教材、论文、手册（md / txt / pdf）。
文字版 PDF 直接提取文本；<b>扫描版 PDF 自动走本地 OCR</b>
（RapidOCR，离线识别），结果缓存在 knowledge/.pdfcache/，整本书只慢第一次</li>
<li><b>reports/ 目录</b>：AI 生成的历史实验报告自动入库——
模型因此“记得”之前的实验结果与异常</li>
</ul>

<h3>第 1 步：切块（建库时）</h3>
<p>全部文档切成约 500 字的小块（相邻块重叠 80 字，防止句子被拦腰截断），
每块记录出处，如「电机学第六版·P31」「README§参数辨识」。</p>

<h3>第 2 步：分词 + 倒排索引（建库时）</h3>
<p>中文按<b>字符二元组</b>拆分：“磁滞回线” → [磁滞][滞回][回线]
（英文/数字保留整词）。再建“哪个词出现在哪些块里”的倒排索引。</p>

<h3>第 3 步：BM25 打分（每次提问时，毫秒级）</h3>
<p>问题同样拆词，给所有块打分，直觉三条：</p>
<ul>
<li><b>词频 TF</b>：块内关键词出现越多越相关</li>
<li><b>逆文档频率 IDF</b>：全库罕见的词（如“磁滞”）权重大，
到处都有的词（如“电机”）权重小——不会被高频词带偏</li>
<li><b>长度归一化</b>：长块天然易命中，要打折</li>
</ul>
<p>排序取前 4 块。</p>

<h3>第 4 步：大模型阅读理解</h3>
<p>前 4 块原文（约 2000 字）随你的问题一起注入提示词，
并要求“优先依据资料回答、引用处标注来源”。模型从头到尾没有读过整本书——
它只是对眼前的原文做阅读理解，所以回答能标注“据资料1（某书·P某页）”。</p>

<h3>一句话总结</h3>
<p style="font-family: Consolas, monospace; background-color:#10131a; padding:8px;">
文档 →切块→ 检索块 →BM25(本地,毫秒)→ 最相关4块 →随提问发给大模型→ 带出处的回答</p>

<h3>成本与隐私</h3>
<p>书再厚，每次提问发给云端的只有检索出的约 2000 字，token 消耗恒定；
整本资料始终留在本机。</p>

<h3>已知局限</h3>
<p>BM25 是<b>字面匹配</b>：问“铁损”检索不到只写“磁滞损耗和涡流损耗”的段落
（同义词不匹配）。提问时尽量使用资料中的原始术语；
彻底解决需升级向量检索（embedding），属于后续可选方向。</p>
"""


class RAGHelpDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("RAG 知识库增强 — 工作原理")
        self.resize(680, 620)
        v = QVBoxLayout(self)
        browser = QTextBrowser()
        browser.setHtml(_HTML)
        v.addWidget(browser, 1)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.button(QDialogButtonBox.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        v.addWidget(btns)
