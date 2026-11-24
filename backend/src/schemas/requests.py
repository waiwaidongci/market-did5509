from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False

class ManifestRequest(Request):
    market: str = Field(min_length=1, max_length=300)
    date: str = Field(min_length=10, max_length=10)
    batch_no: int
    expected_shards: list[int] = Field(min_length=1, max_length=10_000)
    total_trucks: int

class ShardCount(Request):
    shard_no: int
    buckets: dict | list

class ShardsRequest(Request):
    market: str = Field(min_length=1, max_length=300)
    date: str = Field(min_length=10, max_length=10)
    batch_no: int
    shards: list[ShardCount] = Field(min_length=1, max_length=10_000)
