"""Disposable real HTTP app for browser acceptance tests; never uses user data."""
from pathlib import Path
import tempfile

import uvicorn

from travel_agent.adapters.search_yandex import YandexSearchAdapter
from travel_agent.web import create_app


if __name__ == "__main__":
    project = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="odin-recommendations-ui-") as directory:
        app = create_app(
            Path(directory) / "browser.sqlite", model_backend="rules", data_mode="replay",
            checkout_mode="disabled", static_dir=project / "frontend/dist",
            web_search_adapter=YandexSearchAdapter(api_key="", folder_id=""),
        )
        uvicorn.run(app, host="127.0.0.1", port=8792, access_log=False)
