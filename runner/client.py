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
from dataclasses import dataclass
from typing import Any, Protocol

from wire import (EVENT_OVERHEAD_BYTES, MAX_EVENT_BYTES, ChangeSource, OrderResult, OrdersResponse, RepoFacts,
                  RunRequest, RunTicket, SourceRequest, UploadRequest, UploadTargets, VerdictReport, event_cost,
                  from_json, to_json)


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
    """The service refused or could not answer, or a request could not be made at all. Where a
    service did answer, the message carries its status and body."""


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


REQUEST_BUDGET_BYTES = MAX_EVENT_BYTES - EVENT_OVERHEAD_BYTES
"""How much of one invocation this runner will spend on its body, whichever route it is calling."""

_SEPARATOR_COST = event_cost(", ")
"""What `json.dumps` spends between two entries of the `sources` list, escaped into the event."""


def orders_payload(facts: RepoFacts, sources: list[ChangeSource], limit: int) -> dict[str, Any]:
    """The body of one `/v1/orders` request.

    Spelled once: `plan_order_requests` sizes its requests by measuring this, and a plan measured
    against a shape other than the one sent bounds nothing.
    """
    return {"facts": to_json(facts), "sources": to_json(sources), "limit": limit}


@dataclass(frozen=True)
class OrderRequestPlan:
    """The selected changes divided into `/v1/orders` requests the platform will deliver."""

    batches: list[list[ChangeSource]]
    """The sources of each request, in selection order. Every change that can be sent is in exactly
    one of them."""
    oversized: dict[str, int]
    """What its source alone costs in bytes inside an invocation, for each change no request can
    carry — keyed by change id, which a selection holds only once.

    Named rather than sent: such a request cannot arrive, and its refusal would take every other
    change in it along."""


def plan_order_requests(facts: RepoFacts, sources: list[ChangeSource], limit: int,
                        budget: int = REQUEST_BUDGET_BYTES) -> OrderRequestPlan:
    """Pack the selected changes into requests that fit one invocation each.

    `/v1/orders` carries the source of every change it asks about, so one request for the whole
    selection lets the size of a repository's changes decide whether it is mined at all, and the
    platform refuses that request without naming a cause. Packing bounds each request by what it
    holds, and leaves a change too large to send failing on its own.

    Greedy in selection order, so a selection that fits in one request still makes exactly one.

    Args:
        facts: Phase-1 facts, on every request because the service re-derives each change's
            candidate from them.
        sources: The source for each selected change, in the order it was selected.
        limit: The candidate count, as sent.
        budget: What one request body may cost inside its invocation.

    Raises:
        ServiceError: If `facts` alone leaves no room for a single change, so no request can be
            built at all.
    """
    empty = event_cost(json.dumps(orders_payload(facts, [], limit)))
    room = budget - empty
    if room <= 0:
        raise ServiceError(
            f"this repository's own facts cost {empty:,} bytes of a request's {budget:,}, leaving no "
            f"room for any change's source; offer fewer changes with a smaller `history`")
    batches: list[list[ChangeSource]] = []
    oversized: dict[str, int] = {}
    batch: list[ChangeSource] = []
    spent = 0
    for source in sources:
        cost = event_cost(json.dumps(to_json(source)))
        if cost > room:
            oversized[source.change_id] = cost
            continue
        with_separator = cost + (_SEPARATOR_COST if batch else 0)
        if spent + with_separator > room:
            batches.append(batch)
            batch, spent = [source], cost
        else:
            batch.append(source)
            spent += with_separator
    if batch:
        batches.append(batch)
    return OrderRequestPlan(batches=batches, oversized=oversized)


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
        return _decoded(OrdersResponse, "/v1/orders",
                        self._call("/v1/orders", orders_payload(facts, sources, limit)))

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
        request_body = json.dumps(payload)
        # Bounded in this direction for the same reason `_MAX_RESPONSE_BYTES` bounds the other, and
        # at the same door, so it holds for every route rather than only the one that packs. The
        # platform's own refusal names no cause; this one does.
        cost = event_cost(request_body)
        if cost > REQUEST_BUDGET_BYTES:
            raise ServiceError(f"{path}: this request costs {cost:,} bytes of the {REQUEST_BUDGET_BYTES:,} "
                               f"one invocation carries, so it would be refused before the service saw it")
        request = urllib.request.Request(
            self.base_url + path,
            data=request_body.encode(),
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
        return _decoded(OrdersResponse, "/v1/orders",
                        self._call("/v1/orders", orders_payload(facts, sources, limit)))

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
