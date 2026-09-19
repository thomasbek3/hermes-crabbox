"""Validated public request shapes and execution lifecycle."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted', 'paused'})
LIVE = frozenset({'preparing', 'running', 'waiting_input', 'awaiting_approval', 'checkpointing', 'held', 'verifying'})
TRANSITIONS = {
    'queued': {'preparing', 'cancelled'},
    'preparing': {'running', 'failed', 'cancelled', 'interrupted'},
    'running': {'waiting_input', 'awaiting_approval', 'checkpointing', 'held', 'verifying', 'failed', 'cancelled', 'interrupted'},
    'waiting_input': {'running', 'checkpointing', 'held', 'cancelled', 'interrupted'},
    'awaiting_approval': {'running', 'checkpointing', 'held', 'cancelled', 'interrupted', 'failed'},
    'held': {'running', 'waiting_input', 'awaiting_approval', 'checkpointing', 'cancelled', 'interrupted'},
    'checkpointing': {'paused', 'running', 'failed', 'interrupted'},
    'verifying': {'completed', 'failed', 'cancelled', 'interrupted'},
}

class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class AcceptanceCriterion(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_.-]+$')
    description: str = Field(min_length=1, max_length=8192)
    mandatory: bool = True


class SessionRequest(StrictModel):
    project_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=131072)
    agent: str = Field(min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    model: str | None = Field(default=None, max_length=128)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list, max_length=100)
    input_ids: list[str] = Field(default_factory=list, max_length=100)
    environment_version: str | None = Field(default=None, max_length=128)

    @field_validator('acceptance', mode='before')
    @classmethod
    def normalize_acceptance(cls, value):
        if not isinstance(value, list):
            return value
        return [{'id': f'manual-{index+1}', 'description': item, 'mandatory': True} if isinstance(item, str) else item for index,item in enumerate(value)]

    @field_validator('acceptance')
    @classmethod
    def unique_acceptance_ids(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError('Acceptance criterion IDs must be unique')
        return value


class MessageRequest(StrictModel):
    message: str = Field(min_length=1, max_length=131072)

class InputRequest(StrictModel):
    name: str = Field(min_length=1, max_length=255)
    mime: str = Field(default='application/octet-stream', max_length=128)
