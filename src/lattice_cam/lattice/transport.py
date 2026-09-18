"""HTTP/2 transport for the Connect (gRPC) clients, with TLS wired in.

TLS verification is always on. The one supported customisation is trusting
an Anduril-issued CA certificate as an additional root (``LATTICE_CA_CERT_PATH``)
for offline environments; the OS trust store stays included so public and
cloud endpoints keep working.
"""

from __future__ import annotations

from pyqwest import SyncHTTPTransport

from ..config import Config, ConfigError
from ..logging_setup import get_logger

logger = get_logger(__name__)


def base_url(config: Config) -> str:
    return f"https://{config.lattice_endpoint}"


def build_transport(config: Config) -> SyncHTTPTransport:
    """Build the shared pyqwest transport, trusting an Anduril-issued CA if configured.

    The transport (not the ``SyncClient`` wrapping it) is what owns the
    connection pool and exposes ``close()``, so ``LatticeClient`` keeps a
    reference to it for shutdown.
    """
    ca_cert: bytes | None = None
    if config.ca_cert_path:
        try:
            with open(config.ca_cert_path, "rb") as handle:
                ca_cert = handle.read()
        except OSError as exc:
            raise ConfigError(
                f"failed to read LATTICE_CA_CERT_PATH ({config.ca_cert_path}): {exc}"
            ) from exc
        logger.info("trusting Anduril-issued CA certificate", ca_cert_path=config.ca_cert_path)

    return SyncHTTPTransport(tls_ca_cert=ca_cert, tls_include_system_certs=True)
