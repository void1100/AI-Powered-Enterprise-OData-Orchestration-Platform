"""Entry point: `python run.py`."""
import uvicorn
from app.config import settings
from app.main import app


if __name__ == "__main__":
    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port, reload=False)
