"""Centralized settings loaded from environment / .env."""
from __future__ import annotations

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Azure OpenAI
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployment: str = "gpt-4o"
    azure_openai_judge_deployment: str = "gpt-4o"

    # DirectLine (target ESS agent)
    directline_secret: str = ""
    directline_endpoint: str = "https://directline.botframework.com/v3/directline"
    # Copilot Studio agents: paste the "Token endpoint" from
    # Settings -> Channels -> Custom website / Mobile app
    mcs_token_endpoint: str = ""
    # M365 / Teams-only agents (no DirectLine secret, no token endpoint).
    # Uses MSAL device-code if APP_CLIENT_ID + TENANT_ID set, else DefaultAzureCredential.
    mcs_env_id: str = ""
    mcs_bot_schema: str = ""
    app_client_id: str = ""
    tenant_id: str = ""

    # Power Platform
    pp_environment_id: str = ""
    pp_bot_id: str = ""
    pp_tenant_id: str = ""
    pp_client_id: str = ""
    pp_client_secret: str = ""

    # MCP servers
    mcs_mcp_url: str = ""
    pp_mcp_url: str = ""

    # Application Insights (Copilot Studio runtime telemetry)
    # appinsights_app_id = the GUID under App Insights -> API Access -> Application ID
    # appinsights_api_key = a key generated under App Insights -> API Access (read-only is fine)
    appinsights_app_id: str = ""
    appinsights_api_key: str = ""

    # Runtime
    ess_maker_workdir: Path = Field(default=Path("./.workdir"))
    log_level: str = "INFO"

    @property
    def workdir(self) -> Path:
        return self.ess_maker_workdir


settings = Settings()
settings.ess_maker_workdir.mkdir(parents=True, exist_ok=True)
