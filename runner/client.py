"""How a runner reaches the service: in-process, or over HTTP. Same calls, same types.

`--service local` exists so the whole loop can be exercised on a laptop with nothing deployed, and
so that what is proven that way is what gets deployed: both clients hand the same JSON to the same
dispatcher. The HTTP client is standard-library `urllib`, because a thin runner with a dependency is
a runner that needs an install step before it can run one.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Protocol

from wire import (ChangeSource, OrderResult, OrdersResponse, RepoFacts, RunRequest, RunTicket, SourceRequest,
                  UploadRequest, UploadTargets, VerdictReport, from_json, to_json)


OIDC_AUDIENCE = "mo-eval-svc"


def github_id_token() -> str:
    """Ask the Actions runtime for an OIDC token naming this repository. Only a job that declared
    `permissions: id-token: write` has the request URL and bearer in its environment."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise ServiceError("no service token, and not in a GitHub Actions job with `id-token: write`")
    request = urllib.request.Request(f"{url}&audience={OIDC_AUDIENCE}", headers={"authorization": f"bearer {bearer}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["value"]


class ServiceError(RuntimeError):
    """The service refused or could not answer. The message carries its status and body."""


class ServiceClient(Protocol):
    def select(self, facts: RepoFacts, limit: int) -> SourceRequest: ...
    def orders(self, facts: RepoFacts, sources: list[ChangeSource], limit: int) -> OrdersResponse: ...
    def verdicts(self, results: list[OrderResult]) -> VerdictReport: ...
    def uploads(self, request: UploadRequest) -> UploadTargets: ...
    def runs(self, request: RunRequest) -> RunTicket: ...


class LocalService:
    """The service in this process, reached through its dispatcher — not by calling its functions
    directly, so the boundary is exercised even when nothing crosses a network."""

    def __init__(self) -> None:
        from service.app import MemoryStore, Service  # noqa: PLC0415 - only a local runner imports the service

        self._service = Service(MemoryStore())

    def _call(self, path: str, payload: dict[str, Any]) -> Any:
        from service.app import handle  # noqa: PLC0415

        status, body = handle(self._service, "POST", path, {}, json.dumps(payload).encode(), token=None)
        if status != 200:
            raise ServiceError(f"{path} -> {status}: {body.get('error', body)}")
        return body

    def select(self, facts: RepoFacts, limit: int) -> SourceRequest:
        return from_json(SourceRequest, self._call("/v1/select", {"facts": to_json(facts), "limit": limit}))

    def orders(self, facts: RepoFacts, sources: list[ChangeSource], limit: int) -> OrdersResponse:
        payload = {"facts": to_json(facts), "sources": to_json(sources), "limit": limit}
        return from_json(OrdersResponse, self._call("/v1/orders", payload))

    def verdicts(self, results: list[OrderResult]) -> VerdictReport:
        return from_json(VerdictReport, self._call("/v1/verdicts", {"results": to_json(results)}))

    def uploads(self, request: UploadRequest) -> UploadTargets:
        return from_json(UploadTargets, self._call("/v1/uploads", to_json(request)))

    def runs(self, request: RunRequest) -> RunTicket:
        return from_json(RunTicket, self._call("/v1/runs", to_json(request)))


class HttpService:
    """The hosted service."""

    def __init__(self, base_url: str, token: str | None, timeout_seconds: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._minted: str | None = None
        self._minted_at = 0.0
        self._timeout = timeout_seconds

    def _credential(self) -> str:
        """The static token when one was given; otherwise a GitHub Actions ID token, minted for the
        service's audience and renewed before it expires. The workflow needs `id-token: write`."""
        if self._token:
            return self._token
        if self._minted and time.time() < self._minted_at + 240:
            return self._minted
        self._minted, self._minted_at = github_id_token(), time.time()
        return self._minted

    def _call(self, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"content-type": "application/json", "authorization": f"Bearer {self._credential()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as failure:
            detail = failure.read().decode(errors="replace")[:400]
            raise ServiceError(f"{path} -> {failure.code}: {detail}") from failure
        except urllib.error.URLError as failure:
            raise ServiceError(f"{path}: {failure.reason}") from failure

    def select(self, facts: RepoFacts, limit: int) -> SourceRequest:
        return from_json(SourceRequest, self._call("/v1/select", {"facts": to_json(facts), "limit": limit}))

    def orders(self, facts: RepoFacts, sources: list[ChangeSource], limit: int) -> OrdersResponse:
        payload = {"facts": to_json(facts), "sources": to_json(sources), "limit": limit}
        return from_json(OrdersResponse, self._call("/v1/orders", payload))

    def verdicts(self, results: list[OrderResult]) -> VerdictReport:
        return from_json(VerdictReport, self._call("/v1/verdicts", {"results": to_json(results)}))

    def uploads(self, request: UploadRequest) -> UploadTargets:
        return from_json(UploadTargets, self._call("/v1/uploads", to_json(request)))

    def runs(self, request: RunRequest) -> RunTicket:
        return from_json(RunTicket, self._call("/v1/runs", to_json(request)))


def client_for(service: str, token: str | None) -> ServiceClient:
    """`local`, or a base URL with a bearer token."""
    if service == "local":
        return LocalService()
    if not token and not (os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        raise ServiceError("a hosted service needs a token (--token / MO_EVAL_TOKEN), or a GitHub Actions job with `id-token: write`")
    return HttpService(service, token)
