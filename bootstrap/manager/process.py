from bootstrap.clients.service_client import ServiceClient
from bootstrap.common.authentication import service_token
from bootstrap.common.rpc import serve_rpc
from bootstrap.manager.manager import Manager


def run(config):
    token = service_token(config)
    client = ServiceClient(config.services.storage_url, token, config.services.request_timeout)
    try:
        serve_rpc(
            Manager(client),
            token,
            {"wake", "brain.claim", "brain.finish", "lease.renew", "lease.release"},
            config.services.manager_url,
            bind_host=config.services.bind_host,
        )
    finally:
        client.close()
