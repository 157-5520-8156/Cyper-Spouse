"""Exact typed-authority routing for Character Interior proposals.

The router is deliberately private to the deep Module.  It is not a generic
``dict`` sink: every proposal names one registered proposal type and the
corresponding handler validates and commits that domain authority.  The
deferred shell lets production build the actor Module before ledger-backed
acceptance authorities exist, then bind the complete registry exactly once.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping, Sequence
from types import MappingProxyType
from typing import Protocol

from .ports import _AuthorityRequest


class _InteriorTypedAuthorityHandler(Protocol):
    """One exact proposal family; semantic character choice is already over."""

    proposal_type: str

    def prepare(
        self,
        request: _AuthorityRequest,
        proposal: Mapping[str, object],
    ) -> object | Awaitable[object]: ...

    def submit(
        self,
        request: _AuthorityRequest,
        prepared: Sequence[object],
    ) -> Sequence[str] | Awaitable[Sequence[str]]: ...


class _InteriorAuthorityRouter:
    """Fail-closed dispatcher over a frozen exact handler registry."""

    def __init__(self, handlers: Sequence[_InteriorTypedAuthorityHandler]) -> None:
        by_type: dict[str, _InteriorTypedAuthorityHandler] = {}
        for handler in handlers:
            proposal_type = str(getattr(handler, "proposal_type", "")).strip()
            if not proposal_type:
                raise ValueError("interior authority handler needs a proposal type")
            if proposal_type in by_type:
                raise ValueError(
                    f"duplicate interior authority handler: {proposal_type}"
                )
            if not callable(getattr(handler, "prepare", None)) or not callable(
                getattr(handler, "submit", None)
            ):
                raise TypeError("interior authority handler must provide prepare and submit")
            by_type[proposal_type] = handler
        self._handlers = MappingProxyType(by_type)

    @property
    def proposal_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    async def submit(self, request: _AuthorityRequest) -> tuple[str, ...]:
        resolved: list[tuple[_InteriorTypedAuthorityHandler, Mapping[str, object]]] = []
        for proposal in request.proposals:
            proposal_type = proposal.get("proposal_type")
            if not isinstance(proposal_type, str) or not proposal_type:
                raise ValueError("interior proposal lacks an exact proposal_type")
            handler = self._handlers.get(proposal_type)
            if handler is None:
                raise ValueError(
                    f"unregistered interior proposal type: {proposal_type}"
                )
            resolved.append((handler, proposal))

        # Until the authority layer gains one shared atomic batch compiler,
        # fail closed on mixed domain families.  This is safer than committing
        # one authority before another family discovers a stale or malformed
        # proposal.
        handlers = {id(handler): handler for handler, _ in resolved}
        if len(handlers) != 1:
            raise ValueError(
                "one Interior authority request cannot mix proposal families"
            )

        # Every proposal receives a side-effect-free preparation pass before
        # the single handler crosses its CAS/acceptance boundary.  Therefore a
        # malformed second proposal cannot leave the first one committed.
        prepared: list[object] = []
        for handler, proposal in resolved:
            value = handler.prepare(request, proposal)
            prepared.append(await value if inspect.isawaitable(value) else value)
        handler = resolved[0][0]
        submitted = handler.submit(request, tuple(prepared))
        raw_refs = await submitted if inspect.isawaitable(submitted) else submitted
        if not isinstance(raw_refs, (tuple, list)):
            raise ValueError("interior authority handler returned invalid refs")
        refs = list(raw_refs)
        if any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError("interior authority handler returned an invalid ref")
        if len(refs) != len(prepared):
            raise ValueError("interior authority handler returned the wrong ref count")
        if len(refs) != len(set(refs)):
            raise ValueError("interior authority handlers returned duplicate refs")
        return tuple(refs)


class _DeferredInteriorAuthority:
    """One-shot late binding for the production typed-authority registry."""

    def __init__(self) -> None:
        self._delegate: _InteriorAuthorityRouter | None = None

    @property
    def is_bound(self) -> bool:
        return self._delegate is not None

    @property
    def proposal_types(self) -> tuple[str, ...]:
        return () if self._delegate is None else self._delegate.proposal_types

    def bind(self, handlers: Sequence[_InteriorTypedAuthorityHandler]) -> None:
        if self._delegate is not None:
            raise RuntimeError("CharacterInterior authority is already bound")
        self._delegate = _InteriorAuthorityRouter(handlers)

    async def submit(self, request: _AuthorityRequest) -> tuple[str, ...]:
        if self._delegate is None:
            raise RuntimeError("CharacterInterior production authority is not bound")
        return await self._delegate.submit(request)


__all__: list[str] = []
