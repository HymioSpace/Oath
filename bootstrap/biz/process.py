import uvicorn

from bootstrap.biz.assembly import build_application
from bootstrap.biz.server import build_app
from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import service_token


def run(config):
    storage = ServiceClient(
        config.services.storage_url, service_token(config), config.services.request_timeout
    )
    tools = ServiceClient(
        config.services.tools_url, service_token(config), config.services.request_timeout
    )
    try:
        app = build_app(
            build_application(config, storage),
            build_application(config, storage, cli=True),
            service_token(config),
        )
        from bootstrap.biz.tool_cli import routes

        app.router.routes.extend(routes(config, tools))
        from bootstrap.biz.prompt_preview import prompt_routes

        app.router.routes.extend(prompt_routes(config))
        uvicorn.run(
            app, host=config.services.biz_host, port=config.services.biz_port, access_log=False
        )
    finally:
        storage.close()
        tools.close()
