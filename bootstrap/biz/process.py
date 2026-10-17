import uvicorn

from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import build_app
from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import service_token


def run(config):
    storage = ServiceClient(
        config.services.storage_url, service_token(config), config.services.request_timeout
    )
    try:
        app = build_app(
            build_application(config, storage), build_application(config, storage, cli=True)
        )
        uvicorn.run(
            app, host=config.services.biz_host, port=config.services.biz_port, access_log=False
        )
    finally:
        storage.close()
