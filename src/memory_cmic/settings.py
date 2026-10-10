from __future__ import annotations

import os
from dataclasses import dataclass

from memory_cmic.auth import StaticCredentialStore
from memory_cmic.extraction_input import ExtractionBudget


@dataclass(frozen=True)
class Settings:
    database_url: str
    credentials: StaticCredentialStore
    dashscope_api_key: str | None
    dashscope_base_url: str
    dashscope_model: str
    siliconflow_api_key: str | None
    siliconflow_base_url: str
    siliconflow_embedding_model: str
    duplicate_candidate_threshold: float
    extraction_budget: ExtractionBudget = ExtractionBudget()
    profile_allow_inferred: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.environ.get("MEMORY_DATABASE_URL")
        credentials_json = os.environ.get("MEMORY_AUTH_CREDENTIALS_JSON")
        if not database_url:
            raise RuntimeError("MEMORY_DATABASE_URL must be set")
        if not credentials_json:
            raise RuntimeError("MEMORY_AUTH_CREDENTIALS_JSON must be set")
        threshold = float(os.environ.get("MEMORY_DUPLICATE_CANDIDATE_THRESHOLD", "0.85"))
        if not 0 <= threshold <= 1:
            raise RuntimeError("MEMORY_DUPLICATE_CANDIDATE_THRESHOLD must be between 0 and 1")
        return cls(
            database_url=database_url,
            credentials=StaticCredentialStore.from_json(credentials_json),
            dashscope_api_key=os.environ.get("DASHSCOPE_API_KEY"),
            dashscope_base_url=os.environ.get(
                "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ),
            dashscope_model=os.environ.get("DASHSCOPE_MODEL", "qwen3.8-max"),
            siliconflow_api_key=os.environ.get("SILICONFLOW_API_KEY"),
            siliconflow_base_url=os.environ.get(
                "SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"
            ),
            siliconflow_embedding_model=os.environ.get(
                "SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"
            ),
            duplicate_candidate_threshold=threshold,
            profile_allow_inferred=os.environ.get("MEMORY_PROFILE_ALLOW_INFERRED", "false").lower()
            == "true",
            extraction_budget=ExtractionBudget(
                input_tokens=int(os.environ.get("MEMORY_MODEL_INPUT_TOKENS", "12000")),
                output_tokens=int(os.environ.get("MEMORY_MODEL_OUTPUT_TOKENS", "2048")),
                max_model_calls=int(os.environ.get("MEMORY_TASK_MODEL_CALL_LIMIT", "20")),
            ),
        )
