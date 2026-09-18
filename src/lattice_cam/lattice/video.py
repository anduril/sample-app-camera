"""VideoManager API: register and archive the camera's SRT ingress stream.

Ingress ids are **server-shaped**. Lattice does not simply echo the id the
caller supplies: observed live in this deployment, the client sent a UUID and
``CreateIngressStreamResponse.ingress_id`` came back as ``<client id>#<suffix>``
(``82a0497e-...#c8b80372-...``). Callers must therefore treat the *returned*
``video_id`` as the ingress identity: advertise it on the entity's ``Media``
and archive by it. Deleting by the id that was sent will not find the stream.
"""

from __future__ import annotations

from dataclasses import dataclass

from anduril.videomanager.v1.create_ingress_stream_request_pub_pb import (
    CreateIngressStreamRequest,
)
from anduril.videomanager.v1.delete_ingress_stream_request_pub_pb import (
    DeleteIngressStreamRequest,
)
from anduril.videomanager.v1.srt_settings_pub_pb import SrtSettings
from anduril.videomanager.v1.video_manager_api_pub_connect import (
    VideoManagerAPIClientSync,
)
from connectrpc.protocol import ProtocolType
from protobuf import Oneof

from .auth import AuthProvider


@dataclass
class SrtIngressInfo:
    """Result of registering an SRT ingress stream with Lattice VideoManager."""

    # The ingress id AS RETURNED by Lattice (``<client id>#<suffix>``), not
    # the one sent; advertise on the entity's Media and delete by this value.
    video_id: str
    push_url: str  # the SRT URL the producer (MediaMTX) should push to
    session_id: str  # the SRT streamid the producer must include


class VideoClient:
    def __init__(self, base_url: str, http_client, auth: AuthProvider) -> None:
        self._auth = auth
        self.stub = VideoManagerAPIClientSync(
            base_url, http_client=http_client, protocol=ProtocolType.GRPC
        )

    def create_srt_ingress(
        self,
        *,
        title: str,
        passphrase: str = "",
        ingress_id: str | None = None,
        timeout_ms: int | None = 30000,
    ) -> SrtIngressInfo:
        """Register an SRT ingress stream with Lattice VideoManager.

        Returns the video id (to advertise on the entity) and the SRT URL the
        producer should push to. The returned id is the server's, which is not
        the ``ingress_id`` passed in (see the module docstring). Raises on
        failure — the caller decides whether to proceed without video.
        """
        request = CreateIngressStreamRequest(
            title=title,
            ingress_id=ingress_id,
            ingress=Oneof("srt", SrtSettings(passphrase=passphrase or None)),
        )
        response = self.stub.create_ingress_stream(
            request, headers=self._auth.headers(), timeout_ms=timeout_ms
        )
        oneof = response.ingress  # Oneof(field, value)
        if oneof is None or oneof.field != "srt":
            raise RuntimeError(
                f"VideoManager returned unexpected ingress type: {getattr(oneof, 'field', None)}"
            )
        srt = oneof.value
        return SrtIngressInfo(
            video_id=response.ingress_id,
            push_url=srt.url,
            session_id=srt.session_id,
        )

    def delete_srt_ingress(self, ingress_id: str, *, timeout_ms: int | None = 30000) -> None:
        """Stop (archive) an ingress stream in Lattice VideoManager.

        ``ingress_id`` must be the id Lattice returned from
        :meth:`create_srt_ingress`, not the one the caller proposed. Lattice
        keeps the archived record retrievable under the same id, so a
        subsequent Start must register a new ingress under a new id.
        """
        self.stub.delete_ingress_stream(
            DeleteIngressStreamRequest(ingress_id=ingress_id),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )
