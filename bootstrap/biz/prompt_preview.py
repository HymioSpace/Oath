"""Authenticated Markdown preview using the worker's prompt builder."""

import hashlib
import hmac

from starlette.responses import JSONResponse
from starlette.routing import Route

from bootstrap.common.authentication import service_token
from bootstrap.common.extension_loader import load_extensions
from core.agent.prompt import system_prompt
from core.skill import SkillSystem


def prompt_routes(config):
    extensions = load_extensions(
        config.agent.extension_dirs, kinds=frozenset({"skills"}), skill_dirs=config.agent.skill_dirs
    )
    skills = SkillSystem(
        (config.agent.skill_dir, *extensions.skill_dirs), scan_dirs=config.agent.skill_scan_dirs
    )

    def endpoint(request):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + service_token(config)):
            return JSONResponse({"error": "unauthorized"}, 401)
        channel = request.query_params.get("channel", "web")
        if channel not in {"web", "ios", "desk", "chrome", "unknown"}:
            return JSONResponse({"error": "invalid_channel"}, 400)
        text = system_prompt(config.agent, skills, channel)
        return JSONResponse(
            {
                "markdown": text,
                "channel": channel,
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "source": "current_configuration",
                "characters": len(text),
                "includes_personal_context": False,
            }
        )

    return [Route("/admin/system-prompt", endpoint)]
