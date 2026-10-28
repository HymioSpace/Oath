"""One builder for the runtime prompt and its administrator preview."""

from core.agent.runtime_prompt import RUNTIME_PROMPT
from core.shell.bash_tool import workspace_note


def system_prompt(config, skills, channel="unknown"):
    parts = [config.system_prompt, RUNTIME_PROMPT]
    if config.builtin_bash:
        parts.append(workspace_note(config.workspace_root))
    if config.cli_public_url:
        parts.append(
            "## CLI environment\n"
            "oath-cli runs on the selected device and calls the cloud CLI server. "
            "The execution environment supplies credentials; do not print them. "
            "Use oath-cli help for the catalog and oath-cli help NAME for its schema.\n"
            "Client download: " + config.cli_public_url.rstrip("/") + "/cli/client\n"
            "Installer: " + config.cli_public_url.rstrip("/") + "/cli/install\n"
            "Installation verifies the OATH_CLI_SHA256 supplied by the runtime."
        )
    parts.append(skills.list(config.skill_index_tokens, channel=channel))
    return "\n\n".join(p for p in parts if p)
