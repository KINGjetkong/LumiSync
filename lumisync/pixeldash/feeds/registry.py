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
    *,
    include_declarative: bool = True,
) -> Tuple[List[TradeFeed], List[FeedStatus]]:
    """Instantiate every configured feed — built-in brokers and declared APIs.

    Returns the feeds that came up plus a status for every feed the user
    touched. A feed with partial credentials yields an ``UNCONFIGURED``
    status — it is surfaced on the panel rather than skipped silently, because
    a half-configured feed is the most likely reason a number looks wrong.
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

    if include_declarative:
        declared_feeds, declared_statuses = build_declarative_feeds(config)
        feeds.extend(declared_feeds)
        statuses.extend(declared_statuses)

    if not feeds and not statuses:
        statuses.append(
            FeedStatus.unconfigured("broker", "no broker credentials in the environment")
        )
    return feeds, statuses


def build_declarative_feeds(
    config: PixelDashConfig,
) -> Tuple[List[TradeFeed], List[FeedStatus]]:
    """Load and instantiate every spec in the config's ``feeds.d`` directory.

    A spec that fails to parse becomes an error status naming the file, so a
    typo in a hand-edited spec is visible on the panel instead of just meaning
    "that feed didn't show up".
    """
    import os

    from .declarative import DeclarativeFeed
    from .specs import load_specs

    feeds: List[TradeFeed] = []
    statuses: List[FeedStatus] = []

    specs, errors = load_specs(config.feeds_dir)
    for path, message in errors:
        statuses.append(FeedStatus.error(os.path.basename(path), message))

    for spec in specs:
        try:
            feeds.append(DeclarativeFeed(spec))
        except FeedUnconfigured as exc:
            statuses.append(FeedStatus.unconfigured(spec.name, str(exc)))
        except FeedError as exc:
            statuses.append(FeedStatus.error(spec.name, str(exc)))

    return feeds, statuses
