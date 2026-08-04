"""Pick the right driver for a device descriptor."""

from __future__ import annotations

from typing import Any, Dict

from .base import TransportAdapter
from .govee_lan import GoveeLanAdapter
from .idotmatrix_ble import IDotMatrixBleAdapter

#: Govee SKUs that are 2D pixel panels rather than strips or bulbs, with their
#: grid size. These get the per-pixel driver instead of the zone-based one: the
#: standard LAN adapter can only address a handful of segments, which on a
#: 52x32 panel means a wash of colour where a picture belongs.
GOVEE_PIXEL_SKUS: Dict[str, str] = {
    "H6631": "52x32",   # Gaming Pixel Light, 52x32 (1,664 LEDs)
}


def is_govee_pixel_panel(device: Dict[str, Any]) -> bool:
    sku = str(device.get("model") or device.get("sku") or "").strip().upper()
    return sku in GOVEE_PIXEL_SKUS


def create_adapter(device: Dict[str, Any], server=None) -> TransportAdapter:
    """Build a :class:`TransportAdapter` for ``device``.

    Selection is by explicit ``transport``/``type`` hints on the descriptor,
    then by SKU for device families that need a specific driver, defaulting to
    the Govee LAN path (LumiSync's original behavior).
    """
    transport = str(device.get("transport", "")).lower()
    kind = str(device.get("type", "")).lower()

    if transport == "ble" or kind in ("idotmatrix", "idotmatrix_ble"):
        return IDotMatrixBleAdapter(device)
    if transport == "tuya" or kind in ("tuya", "lsc"):
        from .tuya_lan import TuyaLightAdapter

        return TuyaLightAdapter(device)
    if kind in ("govee_pixel", "pixel") or is_govee_pixel_panel(device):
        from .govee_pixel import GoveePixelAdapter

        sku = str(device.get("model") or device.get("sku") or "").strip().upper()
        descriptor = dict(device)
        descriptor.setdefault("matrix_size", GOVEE_PIXEL_SKUS.get(sku, "52x32"))
        return GoveePixelAdapter(descriptor, server)
    return GoveeLanAdapter(device, server)
