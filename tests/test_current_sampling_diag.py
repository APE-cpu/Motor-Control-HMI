import struct

from PySide6.QtWidgets import QApplication

from communications.comm_manager import CommManager
from communications.protocol_v2 import MessageType, V2Frame, encode_v2_frame
from pages.current_sampling_page import CurrentSamplingPage


def _app():
    return QApplication.instance() or QApplication([])


def test_f2_current_sampling_decoder_and_page():
    _app()
    comm = CommManager()
    page = CurrentSamplingPage(comm)
    received = []
    comm.currentSamplingDiagReceived.connect(received.append)
    payload = struct.pack("<IHHHHBHHHH", 1234, 2010, 2020, 4010, 4020,
                          3, 100, 200, 300, 350)
    raw = encode_v2_frame(V2Frame(MessageType.TELEMETRY, command=0xF2,
                                  payload=payload))
    comm._process_v2_responses([raw])
    assert received[0]["sector"] == 3
    assert received[0]["sample_point"] == 350
    assert page._labels["adc1_raw"].text() == "2010"
    page._flush()
    assert list(page.adc_curve._buffers["ADC1"]) == [2010.0]


def test_f2_extended_calibration_payload_is_decoded_without_stopping_rx():
    _app()
    comm = CommManager()
    received = []
    comm.currentSamplingDiagReceived.connect(received.append)
    base = struct.pack("<IHHHHBHHHH", 2000, 16400, 16390, 32800, 32780,
                       4, 2600, 2610, 2620, 5249)
    calibration = struct.pack("<HHHH", 16380, 16420, 16370, 16410)
    raw = encode_v2_frame(V2Frame(MessageType.TELEMETRY, command=0xF2,
                                  payload=base + calibration))
    comm._process_v2_responses([raw])
    assert received[0]["cal_adc1_pp"] == 40
    assert received[0]["cal_adc2_pp"] == 40
    assert received[0]["sample_point"] == 5249
    assert abs(received[0]["adc1_v"] - 1.6516) < 0.001
