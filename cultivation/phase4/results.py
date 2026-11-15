"""Versioned public result contracts, separate from model-only draft schemas."""
from typing import Literal
from pydantic import Field
from .contracts import Common,PetReview,Claim,ConversationSummary

class Result(Common):
    formalWrites: Literal[False]
    status: Literal['ok','needs_clarification','skipped','degraded']
    recoverySummary: dict | None=None
    sourceRelayCopies: list[dict]=Field(default_factory=list)

class PetResult(Result):
    conversationSummary: ConversationSummary | None=None
    canPublish: bool
    reply: dict | None=None
    petId: str | None=None
    conversationId: str | None=None
    conversationRevision: int | None=None
    consentRevision: int | None=None
    memoryRevision: int | None=None
    styleRevision: int | None=None
    responsePriority: Literal['ordinary','support','urgent'] | None=None
    intents: list[str]=Field(default_factory=list)
    evidenceRefs: list[str]=Field(default_factory=list)
    factualClaims: list[Claim]=Field(default_factory=list)
    clarifications: list[str]=Field(default_factory=list)
    memoryProposals: list[dict]=Field(default_factory=list)
    editGoalId: str | None=None
    editAction: Literal['editGoal','refinePlan'] | None=None
    handoffProposals: list[dict]=Field(default_factory=list)
    review: PetReview | None=None
    requiresBackendPublicationCheck: bool | None=None
    skipReason: str | None=None
    logicalMessageId: str | None=None
    sourceId: str | None=None
    sourceRevision: int | None=None
    source: str | None=None
    modelGenerationSucceeded: bool | None=None
    generationFailure: str | None=None
