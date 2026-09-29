"""HTTP-клиенты ParseDock."""

from parsedock.clients.addresses import address_allowed, ensure_target_allowed
from parsedock.clients.requests_client import FetchedPage, fetch_url

__all__ = ["FetchedPage", "address_allowed", "ensure_target_allowed", "fetch_url"]
