from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed_priority: list[int] = Field(default_factory=lambda: [20002], max_length=20)
    purchase_budget: int = Field(default=100, ge=0, le=1_000_000)
    purchase_mode: Literal["funds", "budget", "inventory"] = "funds"
    fallback_radish: bool = True
    max_lands: int = Field(default=24, ge=1, le=24)
    fertilizer: Literal["off", "normal", "organic"] = "off"
    auto_claim: bool = False
    auto_sell: bool = False
    auto_care: bool = False
    reward_check_interval: int = Field(default=14400, ge=60, le=86400)
    random_action_delays: bool = True
    action_delay_min: float = Field(default=1, ge=1, le=10)
    action_delay_max: float = Field(default=3, ge=1, le=10)
    random_plant_order: bool = True
    maturity_grace_seconds: int = Field(default=60, ge=0, le=3600)
    friend_help: bool = False
    friend_steal: bool = False
    friend_insects: bool = False
    friend_weeds: bool = False
    help_priority: list[int] = Field(default_factory=list, max_length=200)
    steal_priority: list[int] = Field(default_factory=list, max_length=200)
    insects_priority: list[int] = Field(default_factory=list, max_length=200)
    weeds_priority: list[int] = Field(default_factory=list, max_length=200)
    friends_scope: Literal["selected", "all"] = "selected"
    friend_interval: int = Field(default=360, ge=60, le=3600)
    friends_per_cycle: int = Field(default=3, ge=1, le=10)
    friend_action_delay_min: float = Field(default=5, ge=1, le=30)
    friend_action_delay_max: float = Field(default=20, ge=1, le=30)

    @model_validator(mode="after")
    def valid_timing(self):
        if self.action_delay_min > self.action_delay_max:
            raise ValueError("操作间隔下限不能超过上限")
        if self.friend_action_delay_min > self.friend_action_delay_max:
            raise ValueError("好友操作等待下限不能超过上限")
        return self

    @field_validator(
        "seed_priority", "help_priority", "steal_priority", "insects_priority", "weeds_priority"
    )
    @classmethod
    def valid_ids(cls, values):
        if any(not 0 < value < 1 << 53 for value in values) or len(set(values)) != len(values):
            raise ValueError("编号重复或无效")
        return values
