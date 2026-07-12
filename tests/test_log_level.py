"""日志级别判定测试：按行文本启发式分类 info / warn / error。"""
from logs.operation_logger import classify_level


def test_错误关键词判error():
    assert classify_level("[12:00:00] 发送失败：串口未打开") == "error"
    assert classify_level("[12:00:00] [DRL错误] 模型加载异常") == "error"
    assert classify_level("[12:00:00] ONNX Error: bad model") == "error"


def test_警告关键词判warn():
    assert classify_level("[12:00:00] [警告] 当前未连接") == "warn"
    assert classify_level("[12:00:00] 紧急停止") == "warn"
    assert classify_level("[12:00:00] 母线告警") == "warn"


def test_普通操作判info():
    assert classify_level("[12:00:00] 启动仿真") == "info"
    assert classify_level("[12:00:00] 应用负载与机械  类型=恒转矩负载") == "info"


def test_错误优先于警告():
    # 同时含“警告”和“失败”时，以更严重的 error 为准
    assert classify_level("[12:00:00] 警告后仍失败") == "error"
