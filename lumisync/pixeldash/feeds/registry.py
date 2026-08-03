"""Turn a config into live feed objects, reporting what could not be built."""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from ..config import BrokerConfig, PixelDashConfig
from ..models import FeedStatus
from .base import FeedError, FeedUnconfigured, TradeFeed

FeedFactory = Callable[[BrokerConfig], TradeFeed]


def _tradier(config: BrokerConfig) -> TradeFeed:
    from .tradier import TradierFeed

    return TradierFeed(config)


def _alpaca(config: BrokerConfig) -> TradeFeed:
    from .alpaca import AlpacaFeed

    return AlpacaFeed(config)


#: Broker name -> constructor. Adding a broker is one entry plus a module.
FACTORIES: Dict[str, FeedFactory] = {
    "tradier": _tradier,
    "alpaca": _alpaca,
}


def build_feeds(
    config: PixelDashConfig,
    factories: Dict[str, FeedFactory] = None,
) -> Tuple[List[TradeFeed], List[FeedStatus]]:
    """Instantiate every configured broker.

    Returns the feeds that came up plus a status for every broker the user
    touched. A broker with partial credentials yields an ``UNCONFIGURED``
    status — it is surfaced on the panel rather than skipped silently, because
    a half-configured broker is the most likely reason a number looks wrong.
    """
    factories = factories if factories is not None else FACTORIES
    feeds: List[TradeFeed] = []
    statuses: List[FeedStatus] = []

    for name, broker in config.brokers.items():
        if not broker.enabled:
            continue
        factory = factories.get(name)
        if factory is None:
            statuses.append(FeedStatus.error(name, "no driver for this broker"))
            continue
        try:
            feeds.append(factory(broker))
        except FeedUnconfigured as exc:
            statuses.append(FeedStatus.unconfigured(name, str(exc)))
        except FeedError as exc:
            statuses.append(FeedStatus.error(name, str(exc)))

    if not feeds and not statuses:
        statuses.append(
            FeedStatus.unconfigured("broker", "no broker credentials in the environment")
        )
    return feeds, statuses
