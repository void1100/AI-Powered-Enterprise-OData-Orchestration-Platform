import secrets
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    jwt_secret: str = ""
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    chroma_persist_dir: str = "./data/chroma_db"

    sqlite_db_path: str = "./data/app.db"

    llm_provider: str = "mock"
    openai_api_key: str = ""
    openai_api_keys: str = ""
    openai_base_url: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-r1"
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    llm_model: str = "gpt-4o-mini"
    embedding_model: str = "all-MiniLM-L6-v2"

    app_host: str = "0.0.0.0"
    app_port: int = 8000

    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    n8n_webhook_url: str = "http://n8n:5678/webhook/chat-share"
    n8n_email_webhook_url: str = "http://n8n:5678/webhook/email-share"
    n8n_whatsapp_webhook_url: str = "http://n8n:5678/webhook/whatsapp-share"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def jwt_secret_key(self) -> str:
        if self.jwt_secret:
            return self.jwt_secret
        return secrets.token_hex(32)

    @property
    def openai_api_keys_list(self) -> List[str]:
        """Return list of API keys: primary key + any additional keys."""
        keys = []
        if self.openai_api_key:
            keys.append(self.openai_api_key)
        if self.openai_api_keys:
            for k in self.openai_api_keys.split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys


settings = Settings()
