import os
from pathlib import Path

from dotenv import load_dotenv


def load_settings():
    load_dotenv(Path.cwd() / ".env")
    return {
        "model_backend": os.getenv("AGENT_MODEL_BACKEND", "codex"),
        "data_mode": os.getenv("AGENT_DATA_MODE", "replay"),
        "checkout_mode": os.getenv("AGENT_CHECKOUT_MODE", "disabled"),
        "db_path": os.getenv("DATABASE_URL") or os.getenv("AGENT_STATE_DB", "data/agent.sqlite"),
        "timezone": os.getenv("AGENT_DEFAULT_TIMEZONE", "Europe/Moscow"),
    }


def make_gateway(name):
    if name == "rules":
        from .interpretation import RuleBasedGateway
        return RuleBasedGateway()
    if name == "codex":
        from .adapters.model_codex import CodexModelGateway
        return CodexModelGateway()
    if name == "hf":
        from .adapters.model_hf import HFModelGateway
        return HFModelGateway(token=os.getenv("HF_TOKEN", ""), model_id=os.getenv("HF_MODEL_ID", ""), provider=os.getenv("HF_INFERENCE_PROVIDER", "auto"))
    raise ValueError("Неизвестный способ подключения модели")
