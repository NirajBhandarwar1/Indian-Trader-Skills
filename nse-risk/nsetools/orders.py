"""One order, identified the same way by every skill that has to recognise it.

/nse-risk records a verdict against an order's fingerprint, and /nse-broker shows an order as READY
only if that exact fingerprint passed today. If the two skills worked the fingerprint out
differently, a passed order would never be ready, or worse, a changed order would look passed. So
the function lives here, once.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json

# The fields a broker would act on, never the notes ("why", "plan"). Change the quantity, the
# price, the stop or the target and it is a different order.
BROKER_FIELDS = ("symbol", "exchange", "side", "quantity", "order_type", "price", "trigger_price",
                 "product", "validity", "order_class", "stop_loss_price", "take_profit_price")


def fingerprint(order: dict) -> str:
    """The exact order, as a short hash of its broker fields."""
    core = {k: order[k] for k in BROKER_FIELDS if order.get(k) is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
