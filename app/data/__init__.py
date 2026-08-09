"""Market data collection and normalization layer.

Provides adapters for the Polymarket Gamma API, CLOB API, and WebSocket
stream, along with a normalizer to convert raw responses into the internal
storage schema and validators to assess data quality.
"""

from app.data.clob import ClobAdapter
from app.data.gamma import GammaAdapter
from app.data.normalizer import DataNormalizer
from app.data.validators import DataQuality, DataValidator, QualityReport
from app.data.websocket import ConnectionState, MarketEvent, WebSocketManager

__all__ = [
    "GammaAdapter",
    "ClobAdapter",
    "WebSocketManager",
    "DataNormalizer",
    "DataValidator",
    "DataQuality",
    "QualityReport",
    "ConnectionState",
    "MarketEvent",
]
