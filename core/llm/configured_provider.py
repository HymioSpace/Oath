"""Resolve deployed model choices to their own endpoint and Vault identity."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from core.llm.openai_provider import OpenAICompatibleProvider
from core.protocol.datatype.config import AgentConfig
from core.protocol.datatype.llm import LlmRequest, LlmResponse
from core.protocol.error import LlmError
from core.protocol.interface.llm import DeltaSink, ModelProvider


class ConfiguredProvider(ModelProvider):
    def __init__(
        self,
        config: AgentConfig,
        secrets: Any,
        image_resolver: Callable[[str, list[str]], dict[str, str]] | None = None,
    ) -> None:
        self.config = config
        self.image_resolver = image_resolver
        self.providers = {
            name: OpenAICompatibleProvider(provider, secrets)
            for name, provider in config.model_providers.items()
        }
        self.default = OpenAICompatibleProvider(config.provider, secrets)

    def complete(self, request: LlmRequest, *, on_delta: DeltaSink | None = None) -> LlmResponse:
        name = request.model or self.config.provider.model
        selected = self.config.model_providers.get(name, self.config.provider)
        if name not in {
            *self.providers,
            self.config.provider.model,
            *self.config.selectable_models,
        }:
            raise LlmError("Requested model is not configured")
        refs = (
            list(
                dict.fromkeys(
                    ref
                    for message in request.messages
                    for ref in message.extra.get("image_refs", [])
                )
            )[-12:]
            if selected.supports_images
            else []
        )
        if refs:
            if not self.image_resolver or not request.user_id:
                raise LlmError("Native image resolution is not configured")
            images = self.image_resolver(request.user_id, refs)
        else:
            images = {}
        messages = []
        for message in request.messages:
            attached = message.extra.get("image_refs", [])
            if not attached:
                messages.append(message)
                continue
            missing = [ref for ref in attached if ref not in images]
            note = (
                (
                    "\n[Images not included in this model request: "
                    + ", ".join(missing)
                    + ". Do not assume their contents; use the image analysis tool if needed.]"
                )
                if missing
                else ""
            )
            messages.append(
                replace(
                    message,
                    content=message.content + note,
                    extra={
                        **message.extra,
                        "images": [images[ref] for ref in attached if ref in images],
                    },
                )
            )
        request = replace(request, messages=messages)
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
