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

    # Dataverse Web API (direct KB writes). If empty, ESS Maker auto-resolves
    # from `pac auth list` (active profile's Environment Url).
    dataverse_org_url: str = ""
    # AAD tenant that owns the Dataverse environment (when it differs from the default az account)
    dataverse_tenant_id: str = ""

    # MCP servers
    mcs_mcp_url: str = ""
    pp_mcp_url: str = ""

    # Application Insights (Copilot Studio runtime telemetry)
    # appinsights_app_id = the GUID under App Insights -> API Access -> Application ID
    # appinsights_api_key = a key generated under App Insights -> API Access (read-only is fine)
    appinsights_app_id: str = ""
    appinsights_api_key: str = ""

    # ESS Doctor
    # Tenant IDs whose conversations may be inspected via Kusto (comma-separated). Required.
    doctor_allowed_tenants: str = ""
    # Copilot Studio agent that "Load from Copilot Studio" reads directly (new-UX agents live behind the PVA gateway).
    doctor_cs_tenant_id: str = ""
    doctor_cs_environment_id: str = ""
    doctor_cs_agent_id: str = ""
    # Copilot Studio gateway for the environment's region (internal endpoint; set in .env).
    doctor_cs_gateway: str = ""
    # Copilot Studio runtime telemetry in Kusto (internal; set in .env).
    doctor_kusto_db: str = ""
    doctor_kusto_region: str = "US"
    # Region → cluster URL, comma-separated: "US=https://…,EU-WEU=https://…"
    doctor_kusto_clusters: str = ""
    # AAD tenant used to get Kusto tokens.
    doctor_kusto_aad_tenant: str = ""
    doctor_schema_prefix: str = "msdyn_copilotforemployeeselfservice"
    # Folder of agent exports (*.yml) offered in the UI picker, and ESS baseline exports per persona.
    doctor_samples_dir: str = ""
    doctor_baseline_hr: str = ""
    doctor_baseline_it: str = ""
    doctor_baseline_hub: str = ""

    # Runtime
    ess_maker_workdir: Path = Field(default=Path("./.workdir"))
    log_level: str = "INFO"

    @property
    def workdir(self) -> Path:
        return self.ess_maker_workdir


settings = Settings()
settings.ess_maker_workdir.mkdir(parents=True, exist_ok=True)
