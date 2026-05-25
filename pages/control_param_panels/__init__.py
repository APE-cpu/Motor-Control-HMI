from .panels import (
    AnglePositionPanel,
    CurrentChoppingPanel,
    EKFPanel,
    HFIPanel,
    HallPanel,
    MPCPanel,
    MRASPanel,
    OpenLoopPanel,
    PIPanel,
    QEPPanel,
    ResolverPanel,
    SMOPanel,
    SensorlessPanel,
    VoltageControlPanel,
)

__all__ = [
    "PIPanel", "OpenLoopPanel", "MPCPanel", "SensorlessPanel",
    "CurrentChoppingPanel", "AnglePositionPanel", "VoltageControlPanel",
    "HallPanel", "QEPPanel", "ResolverPanel", "SMOPanel",
    "EKFPanel", "MRASPanel", "HFIPanel",
]
