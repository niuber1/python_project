import uvicorn

from crawler_tool.config import get_settings


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("crawler_tool.app:app", host=settings.bind_host, port=settings.bind_port, reload=False)
