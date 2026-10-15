"""Payment provider boundary for hosted or direct Ugandan payment integrations."""
from typing import Protocol
from collections.abc import Mapping

import pesapal


class PaymentProvider(Protocol):
    name: str

    def configured(self) -> bool: ...

    def submit_order(self, order): ...

    def verify_transaction(self, tracking_id): ...

    def verify_webhook_signature(self, _raw_body: bytes, _headers: Mapping[str, str]) -> bool | None: ...

    def webhook_tracking_id(self, values, _headers: Mapping[str, str]) -> str: ...


class PesapalProvider:
    name = "pesapal"

    def configured(self):
        return pesapal.configured()

    def submit_order(self, order):
        return pesapal.submit_order(order)

    def verify_transaction(self, tracking_id):
        return pesapal.get_status(tracking_id)

    def verify_webhook_signature(self, _raw_body, _headers):
        return None

    def webhook_tracking_id(self, values, _headers):
        return values.get("OrderTrackingId", "")


_PROVIDERS = {"pesapal": PesapalProvider()}


def provider_for(name):
    provider = _PROVIDERS.get(name)
    if provider is None:
        raise ValueError(f"Payment provider {name!r} is not supported.")
    return provider


def configured_providers():
    return [provider for provider in _PROVIDERS.values() if provider.configured()]
