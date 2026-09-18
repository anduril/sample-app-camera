"""EntityManager API: publish the camera entity and read it back."""

from __future__ import annotations

from anduril.entitymanager.v1.entity_manager_api_pub_connect import (
    EntityManagerAPIClientSync,
)
from anduril.entitymanager.v1.entity_manager_api_pub_pb import (
    GetEntityRequest,
    GetEntityResponse,
    PublishEntityRequest,
)
from connectrpc.protocol import ProtocolType

from .auth import AuthProvider


class EntityClient:
    def __init__(self, base_url: str, http_client, auth: AuthProvider) -> None:
        self._auth = auth
        self.stub = EntityManagerAPIClientSync(
            base_url, http_client=http_client, protocol=ProtocolType.GRPC
        )

    def publish_entity(
        self, request: PublishEntityRequest, *, timeout_ms: int | None = None
    ) -> None:
        """Publish (upsert) an entity, attaching fresh auth metadata."""
        self.stub.publish_entity(request, headers=self._auth.headers(), timeout_ms=timeout_ms)

    def get_entity(self, entity_id: str, *, timeout_ms: int | None = None) -> GetEntityResponse:
        """Read an entity back (used by the verification script)."""
        return self.stub.get_entity(
            GetEntityRequest(entity_id=entity_id),
            headers=self._auth.headers(),
            timeout_ms=timeout_ms,
        )
