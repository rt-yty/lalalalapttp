"""Deployment entry point: uvicorn travel_agent.asgi:app --host 0.0.0.0."""
from .config import load_settings
from .web import create_app

settings = load_settings()
app = create_app(db_path=settings["db_path"], model_backend=settings["model_backend"],
                 data_mode=settings["data_mode"], checkout_mode=settings["checkout_mode"])
