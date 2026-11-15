from datetime import date as CalendarDate
from typing import Literal,Any
from zoneinfo import ZoneInfo
from pydantic import Field,StrictInt,AwareDatetime,model_validator
from ..materials_v3.contracts import Strict

class Common(Strict):
    schemaVersion: Literal['4.0']='4.0'
    requestId: str=Field(min_length=1)
    inputRevision: StrictInt=Field(ge=1)
    asOf: AwareDatetime
    timeZone: str='Asia/Shanghai'
    scope: dict
    @model_validator(mode='after')
    def zone(self):
        ZoneInfo(self.timeZone)
        if not self.scope.get('type'):raise ValueError('scope.type required')
        return self

class Consent(Strict):
    consentRevision: StrictInt=Field(ge=1)
    memoryRevision: StrictInt=Field(default=0,ge=0)
    allowedFields: list[str]=Field(default_factory=list)
    planningRead: bool=False
    memoryRead: bool=False
    memoryWrite: bool=False

class StyleSkill(Strict):
    kind: Literal['personality','expression','scene']
    origin: Literal['builtin','user']
    label: str=Field(min_length=1,max_length=32)
    text: str=Field(min_length=1,max_length=1600)

class Style(Strict):
    styleRevision: StrictInt=Field(default=1,ge=1)
    tone: Literal['gentle','direct','lively']='gentle'
    replyLength: Literal['short','normal','long']='normal'
    initiative: Literal['low','normal','high']='low'
    focusTopics: list[str]=Field(default_factory=list)
    cultivationExpression: Literal['none','light','rich']='light'
    description: str=''
    activeSkills: list[StyleSkill]=Field(default_factory=list,max_length=10)

class Message(Strict):
    clientMessageId: str=Field(min_length=1)
    userMessageId: str=Field(min_length=1)
    replyMessageId: str=Field(min_length=1)
    text: str=Field(min_length=1)

class History(Strict):
    messageId: str
    sequence: StrictInt=Field(ge=1)
    role: Literal['user','assistant']
    text: str
    sourceMemoryIds: list[str]=Field(default_factory=list)
    sanitizedAtMemoryRevision: StrictInt=Field(default=0,ge=0)

class Memory(Strict):
    memoryId: str
    revision: StrictInt=Field(ge=1)
    text: str
    sourceMessageId: str
    explicitlyConfirmed: bool

class Fact(Strict):
    evidenceRef: str
    kind: Literal['goal','stage','task','progress','risk','weekly','capacity']
    objectId: str
    title: str=Field(min_length=1)
    revision: StrictInt=Field(ge=1)
    data: dict=Field(default_factory=dict)
    @model_validator(mode='after')
    def parent_revision(self):
        value=self.data.get('goalRevision')
        if value is not None and (type(value)!=int or value<1):raise ValueError('goalRevision must be a positive integer')
        return self

class ConversationSummary(Strict):
    messageId: str=Field(min_length=1)
    sequence: StrictInt=Field(ge=1)
    text: str=Field(min_length=1,max_length=4000)

class PetInput(Common):
    mode: Literal['reply','preview']='reply'
    petId: str
    conversationId: str
    conversationRevision: StrictInt=Field(ge=1)
    message: Message
    styleSnapshot: Style=Field(default_factory=Style)
    consentSnapshot: Consent
    history: list[History]=Field(default_factory=list)
    conversationSummary: ConversationSummary | None=None
    compactThrough: StrictInt | None=Field(default=None,ge=0)
    memories: list[Memory]=Field(default_factory=list)
    facts: list[Fact]=Field(default_factory=list)
    pendingProposals: list[dict]=Field(default_factory=list)

class Trigger(Strict):
    triggerId: str
    logicalMessageId: str
    type: Literal['reminder','progress','chat']
    sourceId: str
    sourceRevision: StrictInt=Field(ge=1)
    currentSourceRevision: StrictInt=Field(ge=1)
    validUntil: AwareDatetime
    enabled: bool
    sourceActive: bool=True
    alreadyStarted: bool=False
    activeFocus: bool=False
    quietHours: bool=False
    remainingQuota: StrictInt=Field(ge=0)
    checkedAt: AwareDatetime
    deterministicTemplate: bool=False
    title: str

class ProactiveInput(PetInput):
    trigger: Trigger

class Claim(Strict):
    evidenceRef: str
    field: str
    value: str | StrictInt | float | bool | list[str] | None

class MemoryProposal(Strict):
    operation: Literal['remember','correct','forget']
    memoryId: str | None=None
    text: str
    sourceQuote: str=Field(min_length=1)

class Handoff(Strict):
    evidenceRef: str
    summary: str=Field(min_length=1)
    missingFields: list[str]=Field(default_factory=list)

class PetDraft(Strict):
    replyText: str=Field(min_length=1)
    responsePriority: Literal['ordinary','support','urgent']
    intents: list[Literal['chat','followup','emotion','correction','memory','planning']]
    evidenceRefs: list[str]=Field(default_factory=list)
    factualClaims: list[Claim]=Field(default_factory=list)
    clarifications: list[str]=Field(default_factory=list)
    memoryProposals: list[MemoryProposal]=Field(default_factory=list)
    editGoalRef: str | None=None
    editAction: Literal['editGoal','refinePlan'] | None=None
    skipReason: str | None=None

class PetReview(Strict):
    accepted: bool
    issues: list[str]
    evidenceRefs: list[str]

class RollingDigest(Strict):
    text: str=Field(min_length=1,max_length=1200)

class PetSummary(Strict):
    summary: str
    sourceMessageIds: list[str]
    latestCorrections: list[str]

class Invalidation(Strict):
    eventId: str
    petId: str
    scope: Literal['consent','memory','conversation','settings']
    ref: str
    revision: StrictInt=Field(ge=1)
    conversationId: str | None=None
    deletedMemoryIds: list[str]=Field(default_factory=list)
