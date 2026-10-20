"""Resolve deployed model choices to their own endpoint and Vault identity."""

from dataclasses import replace

from core.llm.openai_provider import OpenAICompatibleProvider
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.error import LlmError
from core.protocol.interface.llm import DeltaSink, ModelProvider


class ConfiguredProvider(ModelProvider):
    def __init__(self, config: AgentConfig, secrets) -> None:
        self.config = config
        self.providers = {
            name: OpenAICompatibleProvider(provider, secrets)
            for name, provider in config.model_providers.items()
        }
        self.default = OpenAICompatibleProvider(config.provider, secrets)

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        name = request.model or self.config.provider.model
        if name in self.providers:
            config = self.config.model_providers[name]
            request = replace(
                request,
                model=config.model,
                extra_params={**config.extra_params, **request.extra_params},
            )
            return self.providers[name].complete(request, on_delta=on_delta)
        if name not in {self.config.provider.model, *self.config.selectable_models}:
            raise LlmError("Requested model is not configured")
        return self.default.complete(request, on_delta=on_delta)
