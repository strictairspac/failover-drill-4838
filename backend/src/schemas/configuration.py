from pydantic import BaseModel, Field, field_validator

class ConfigurationInput(BaseModel):
    member_ids: list[int] = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=2, max_length=200)
    request_id: str | None = Field(default=None, min_length=4, max_length=80)

    @field_validator('member_ids')
    @classmethod
    def unique_members(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('成员节点不能重复')
        return value
