"""Production composition root for the first accepted Fact-v2 vertical.

The general WorldRuntime does not yet perform LLM deliberation or Fact proposal
normalization.  This narrow seam owns the pieces that *must* share process
identity once an audited Fact proposal reaches acceptance: proof reader,
sealed preparation registry, plan/manifest/atomic recorder, and the ledger's
opaque accepted-batch issuer.  It deliberately exposes no raw v3 write path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .accepted_ledger_batch import AcceptedLedgerBatchIssuer
from .fact_accepted_contracts import FactCommitIntentV2
from .fact_proof_backed_evidence import (
    ProofBackedFactEvidenceResolverV2,
    ResolvedFactCommitSourcesV2,
)
from .fact_proposal_audit_v2 import (
    FactCommitProposalAuthorityReaderV2,
    PinnedFactCommitProposalAuthorityHandleV2,
)
from .fact_v2_acceptance_envelope_authority import (
    FactV2AcceptanceEnvelopeAuthorityIssuer,
    FactV2AcceptanceEnvelopeRequestV2,
)
from .fact_v2_accepted_manifest_builder import FactV2AcceptedManifestBuilder
from .fact_v2_atomic_recorder import FactV2AtomicRecorder
from .fact_v2_production_plan import FactV2ProductionPlanIssuer
from .ledger import ObservationEventLocator
from .schemas import CommitResult, ProjectionCursor
from .sealed_fact_commit_adapter_v2 import FactCommitPolicyResolutionV2
from .sealed_production_fact_registry_v2 import (
    PreparedFactCommitMaterializationV2,
    SealedProductionFactPreparationRegistryV2,
)
from .sqlite_ledger import SQLiteProofBackedObservationReader, SQLiteWorldLedger


@dataclass(slots=True)
class FactV2AcceptanceRuntime:
    """One process-local Fact acceptance unit of work bound to one SQLite world.

    Opaque capabilities intentionally cannot cross a process restart.  A
    caller must pin, resolve, prepare, and accept against one current cursor;
    the final ledger CAS detects any intervening event before a new Fact is
    recorded.
    """

    ledger: SQLiteWorldLedger
    _proposal_reader: FactCommitProposalAuthorityReaderV2
    _history_reader: SQLiteProofBackedObservationReader
    _resolver: ProofBackedFactEvidenceResolverV2
    _registry: SealedProductionFactPreparationRegistryV2
    _envelope_issuer: FactV2AcceptanceEnvelopeAuthorityIssuer
    _plan_issuer: FactV2ProductionPlanIssuer
    _manifest_builder: FactV2AcceptedManifestBuilder
    _recorder: FactV2AtomicRecorder

    @classmethod
    def open(cls, *, path: Path, world_id: str) -> FactV2AcceptanceRuntime:
        """Create the only supported production wiring for this vertical."""

        batch_issuer = AcceptedLedgerBatchIssuer()
        ledger = SQLiteWorldLedger(
            path=path,
            world_id=world_id,
            accepted_batch_issuer=batch_issuer,
        )
        return cls.compose(ledger=ledger, batch_issuer=batch_issuer)

    @classmethod
    def compose(
        cls,
        *,
        ledger: SQLiteWorldLedger,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> FactV2AcceptanceRuntime:
        """Attach Fact acceptance to an already-owned SQLite ledger.

        The chat composition root owns one ledger and one accepted-batch
        issuer.  Opening a second Fact runtime against the same database
        would create a competing writer and would make its opaque batch
        capabilities unusable by the host ledger.  This constructor is the
        explicit, same-process alternative.
        """

        if type(ledger) is not SQLiteWorldLedger:
            raise TypeError("Fact v2 acceptance requires the exact SQLite ledger")
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("Fact v2 acceptance requires the exact batch issuer")
        proposal_reader = FactCommitProposalAuthorityReaderV2(ledger=ledger)
        history_reader = SQLiteProofBackedObservationReader(ledger=ledger)
        resolver = ProofBackedFactEvidenceResolverV2(reader=history_reader)
        registry = SealedProductionFactPreparationRegistryV2(resolver=resolver)
        envelope_issuer = FactV2AcceptanceEnvelopeAuthorityIssuer()
        plan_issuer = FactV2ProductionPlanIssuer(
            registry=registry,
            proposal_reader=proposal_reader,
            envelope_issuer=envelope_issuer,
        )
        manifest_builder = FactV2AcceptedManifestBuilder(plan_issuer=plan_issuer)
        return cls(
            ledger=ledger,
            _proposal_reader=proposal_reader,
            _history_reader=history_reader,
            _resolver=resolver,
            _registry=registry,
            _envelope_issuer=envelope_issuer,
            _plan_issuer=plan_issuer,
            _manifest_builder=manifest_builder,
            _recorder=FactV2AtomicRecorder(
                manifest_builder=manifest_builder,
                batch_issuer=batch_issuer,
            ),
        )

    def close(self) -> None:
        self.ledger.close()

    def pin_proposal(
        self, *, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedFactCommitProposalAuthorityHandleV2:
        return self._proposal_reader.pin(
            world_id=self.ledger.world_id,
            cursor=cursor,
            proposal_id=proposal_id,
        )

    def proposal_audit_event_ref(
        self, *, proposal_handle: PinnedFactCommitProposalAuthorityHandleV2
    ) -> str:
        """Return only the durable causation reference needed by a request."""

        return self._proposal_reader.audit(handle=proposal_handle).event_ref

    def resolve_sources(
        self,
        *,
        cursor: ProjectionCursor,
        intent: FactCommitIntentV2,
        locators: Sequence[ObservationEventLocator],
    ) -> ResolvedFactCommitSourcesV2:
        return self._resolver.resolve(
            handle=self._history_reader.pin(world_id=self.ledger.world_id, cursor=cursor),
            intent=intent,
            locators=locators,
        )

    def prepare(
        self,
        *,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        change_id: str,
        policy: FactCommitPolicyResolutionV2,
    ) -> PreparedFactCommitMaterializationV2:
        return self._registry.prepare_from_pinned_audit(
            proposal_reader=self._proposal_reader,
            proposal_handle=proposal_handle,
            change_id=change_id,
            policy=policy,
            world_id=self.ledger.world_id,
        )

    def accept(
        self,
        *,
        request: FactV2AcceptanceEnvelopeRequestV2,
        proposal_handle: PinnedFactCommitProposalAuthorityHandleV2,
        prepared: PreparedFactCommitMaterializationV2,
        sources: ResolvedFactCommitSourcesV2,
    ) -> CommitResult:
        """Run the only accepted Fact-v2 materialization and ledger CAS path."""

        envelope_handle = self._envelope_issuer.issue(
            proposal_reader=self._proposal_reader,
            proposal_handle=proposal_handle,
            request=request,
        )
        plan_handle = self._plan_issuer.issue(
            envelope_handle=envelope_handle,
            proposal_handle=proposal_handle,
            prepared=prepared,
            sources=sources,
        )
        bundle_handle = self._manifest_builder.build(plan_handle=plan_handle)
        batch = self._recorder.prepare_batch(bundle_handle=bundle_handle)
        return self.ledger.commit_accepted(batch, expected_cursor=request.cursor)


__all__ = ["FactV2AcceptanceRuntime"]
