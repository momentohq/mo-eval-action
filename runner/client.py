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


_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
"""How much a service response may carry. Task packages are large; a response is not unbounded."""

_MAX_DETAIL_BYTES = 64 * 1024
"""How much of an error body is read before it is truncated for the message."""

OIDC_AUDIENCE = "mo-eval-svc"


def github_id_token() -> str:
    """Ask the Actions runtime for an OIDC token naming this repository. Only a job that declared
    `permissions: id-token: write` has the request URL and bearer in its environment."""
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise ServiceError("no service token, and not in a GitHub Actions job with `id-token: write`")
    request = urllib.request.Request(f"{url}&audience={OIDC_AUDIENCE}", headers={"authorization": f"bearer {bearer}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(_MAX_DETAIL_BYTES))["value"]
    except (urllib.error.URLError, OSError, KeyError, TypeError, ValueError) as failure:
        # The credential is minted while the request that needs it is being built, which is outside
        # the handler around the call itself. Every failure here becomes the one type the runner's
        # callers catch, so a transient reach to the Actions token service is a message and an exit
        # code rather than a traceback out of the CLI.
        raise ServiceError(f"could not mint a GitHub Actions ID token: {type(failure).__name__}: {failure}") from failure


class ServiceError(RuntimeError):
    """The service refused or could not answer. The message carries its status and body."""


def _decoded(shape, route: str, body):
    """A response read into `shape`, or a `ServiceError` naming the route that sent it.

    `from_json` is strict — an unknown, missing or mistyped field raises — which is what makes a
    service answering something else a refusal rather than a silent misread. Reported as a service
    error because that is what a runner's commands handle: a rollout that puts a newer service in
    front of an older runner would otherwise end the published Action with a traceback.

    Raises:
        ServiceError: If the answer is not one this runner can read.
    """
    try:
        return from_json(shape, body)
    except (ValueError, TypeError, AttributeError) as unreadable:
        raise ServiceError(f"{route}: the service answered with something this runner cannot read: "
                           f"{unreadable}") from unreadable


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
        return _decoded(SourceRequest, "/v1/select",
                        self._call("/v1/select", {"facts": to_json(facts), "limit": limit}))

    def orders(self, facts: RepoFacts, sources: list[ChangeSource], limit: int) -> OrdersResponse:
        payload = {"facts": to_json(facts), "sources": to_json(sources), "limit": limit}
        return _decoded(OrdersResponse, "/v1/orders", self._call("/v1/orders", payload))

    def verdicts(self, results: list[OrderResult]) -> VerdictReport:
        return _decoded(VerdictReport, "/v1/verdicts", self._call("/v1/verdicts", {"results": to_json(results)}))

    def uploads(self, request: UploadRequest) -> UploadTargets:
        return _decoded(UploadTargets, "/v1/uploads", self._call("/v1/uploads", to_json(request)))

    def runs(self, request: RunRequest) -> RunTicket:
        return _decoded(RunTicket, "/v1/runs", self._call("/v1/runs", to_json(request)))


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
                # Bounded: the base URL is an argument, so the service on the other end is not
                # assumed to be well behaved about how much it sends back.
                body = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ServiceError(f"{path}: the service returned more than {_MAX_RESPONSE_BYTES} bytes")
            try:
                return json.loads(body)
            except json.JSONDecodeError as failure:
                # A 200 carrying something other than JSON. The bound above already says this
                # service is not assumed well behaved; what it sends back is the same.
                raise ServiceError(f"{path}: the service answered with something that is not JSON: {failure}") from failure
        except urllib.error.HTTPError as failure:
            # Bounded for the same reason the success path is: an error body is still the service
            # talking, and only the first few hundred characters are ever shown.
            detail = failure.read(_MAX_DETAIL_BYTES).decode(errors="replace")[:400]
            raise ServiceError(f"{path} -> {failure.code}: {detail}") from failure
        except (urllib.error.URLError, OSError) as failure:
            # `OSError` too: a timeout while the body is being read raises `TimeoutError`, which is
            # not a `URLError` — it would leave every caller with a traceback instead of the
            # message and exit code they handle.
            raise ServiceError(f"{path}: {getattr(failure, 'reason', failure)}") from failure

    def select(self, facts: RepoFacts, limit: int) -> SourceRequest:
        return _decoded(SourceRequest, "/v1/select", self._call("/v1/select", {"facts": to_json(facts), "limit": limit}))

    def orders(self, facts: RepoFacts, sources: list[ChangeSource], limit: int) -> OrdersResponse:
        payload = {"facts": to_json(facts), "sources": to_json(sources), "limit": limit}
        return _decoded(OrdersResponse, "/v1/orders", self._call("/v1/orders", payload))

    def verdicts(self, results: list[OrderResult]) -> VerdictReport:
        return _decoded(VerdictReport, "/v1/verdicts", self._call("/v1/verdicts", {"results": to_json(results)}))

    def uploads(self, request: UploadRequest) -> UploadTargets:
        return _decoded(UploadTargets, "/v1/uploads", self._call("/v1/uploads", to_json(request)))

    def runs(self, request: RunRequest) -> RunTicket:
        return _decoded(RunTicket, "/v1/runs", self._call("/v1/runs", to_json(request)))


def client_for(service: str, token: str | None) -> ServiceClient:
    """`local`, or a base URL with a bearer token."""
    if service == "local":
        return LocalService()
    if not token and not (os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        raise ServiceError("a hosted service needs a token (--token / MO_EVAL_TOKEN), or a GitHub Actions job with `id-token: write`")
    return HttpService(service, token)
