"""Process entry point."""

import uvicorn

from fraud_engine.api.app import create_app
from fraud_engine.config import get_settings

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "fraud_engine.api.server:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
