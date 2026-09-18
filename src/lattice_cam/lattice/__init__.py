"""Lattice API plumbing: TLS transport, auth metadata, and one thin client per
Lattice API exposed as ``LatticeClient.entities`` / ``.video`` / ``.tasks``."""

from .client import LatticeClient
from .video import SrtIngressInfo

__all__ = ["LatticeClient", "SrtIngressInfo"]
