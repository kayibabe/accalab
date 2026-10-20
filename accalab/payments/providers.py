"""PayChangu Standard Checkout and manually reviewed payment adapters."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx


class PaymentProviderError(RuntimeError):
    """A payment provider operation failed without exposing credentials."""


@dataclass(frozen=True)
class CheckoutSession:
    tx_ref: str
    checkout_url: str | None
    provider_reference: str | None = None


class PaymentProvider(Protocol):
    name: str

    def create_payment(
        self,
        *,
        amount_mwk: int,
        email: str,
        tx_ref: str,
        callback_url: str,
        return_url: str,
    ) -> CheckoutSession: ...

    def verify_payment(self, tx_ref: str) -> dict | None: ...

    def handle_webhook(self, raw_body: bytes, signature: str) -> dict: ...


class ManualPaymentProvider:
    name = "manual"

    def create_payment(
        self,
        *,
        amount_mwk: int,
        email: str,
        tx_ref: str,
        callback_url: str = "",
        return_url: str = "",
    ) -> CheckoutSession:
        del amount_mwk, email, callback_url, return_url
        return CheckoutSession(tx_ref=tx_ref, checkout_url=None)

    def verify_payment(self, tx_ref: str) -> dict | None:
        del tx_ref
        return None

    def handle_webhook(self, raw_body: bytes, signature: str) -> dict:
        del raw_body, signature
        raise PaymentProviderError("Manual payments do not accept provider webhooks.")


class PayChanguProvider:
    name = "paychangu"
    api_url = "https://api.paychangu.com"
    checkout_host = "checkout.paychangu.com"

    def __init__(
        self,
        *,
        secret_key: str,
        webhook_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not secret_key.strip() or not webhook_secret.strip():
            raise PaymentProviderError("PayChangu API and webhook credentials are required.")
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._client = client or httpx.Client(base_url=self.api_url, timeout=20)
        self._client.headers.update(
            {
                "Authorization": f"Bearer {secret_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def create_payment(
        self,
        *,
        amount_mwk: int,
        email: str,
        tx_ref: str,
        callback_url: str,
        return_url: str,
    ) -> CheckoutSession:
        if amount_mwk <= 0:
            raise PaymentProviderError("Payment amount must be positive.")
        try:
            response = self._client.post(
                "/payment",
                json={
                    "amount": amount_mwk,
                    "currency": "MWK",
                    "email": email,
                    "tx_ref": tx_ref,
                    "callback_url": callback_url,
                    "return_url": return_url,
                    "customization": {
                        "title": "AccaLab Pro plan",
                        "description": "Prepaid 30-day access",
                    },
                },
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentProviderError("PayChangu could not create a checkout session.") from exc
        if not isinstance(body, dict):
            raise PaymentProviderError("PayChangu returned an invalid checkout response.")
        data = body.get("data")
        checkout_url = data.get("checkout_url") if isinstance(data, dict) else None
        if body.get("status") != "success" or not isinstance(checkout_url, str):
            raise PaymentProviderError("PayChangu returned an invalid checkout response.")
        parsed = urlsplit(checkout_url)
        if parsed.scheme != "https" or parsed.hostname != self.checkout_host:
            raise PaymentProviderError("PayChangu returned an untrusted checkout address.")
        nested = data.get("data") if isinstance(data.get("data"), dict) else {}
        returned_ref = nested.get("tx_ref") or tx_ref
        if returned_ref != tx_ref:
            raise PaymentProviderError("PayChangu returned a different transaction reference.")
        return CheckoutSession(
            tx_ref=tx_ref,
            checkout_url=checkout_url,
            provider_reference=str(nested.get("reference")) if nested.get("reference") else None,
        )

    def verify_payment(self, tx_ref: str) -> dict:
        try:
            response = self._client.get(f"/verify-payment/{tx_ref}")
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PaymentProviderError("PayChangu could not verify this payment.") from exc
        if not isinstance(body, dict):
            raise PaymentProviderError("PayChangu returned an invalid verification response.")
        data = body.get("data")
        if body.get("status") != "success" or not isinstance(data, dict):
            raise PaymentProviderError("PayChangu returned an invalid verification response.")
        return data

    def handle_webhook(self, raw_body: bytes, signature: str) -> dict:
        expected = hmac.new(self._webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature.strip()):
            raise PaymentProviderError("Invalid PayChangu webhook signature.")
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PaymentProviderError("Invalid PayChangu webhook payload.") from exc
        if not isinstance(payload, dict):
            raise PaymentProviderError("Invalid PayChangu webhook payload.")
        tx_ref = payload.get("tx_ref")
        if not isinstance(tx_ref, str) or not tx_ref.strip():
            raise PaymentProviderError("PayChangu webhook has no transaction reference.")
        return payload

    def close(self) -> None:
        self._client.close()


def build_payment_provider(name: str | None = None) -> PaymentProvider:
    provider_name = (name or os.getenv("PAYMENT_PROVIDER", "manual")).strip().casefold()
    if provider_name == "manual":
        return ManualPaymentProvider()
    if provider_name == "paychangu":
        return PayChanguProvider(
            secret_key=os.getenv("PAYCHANGU_SECRET_KEY", ""),
            webhook_secret=os.getenv("PAYCHANGU_WEBHOOK_SECRET", ""),
        )
    raise PaymentProviderError("Unsupported payment provider configuration.")
