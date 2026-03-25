from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    bittensor_network: str = "finney"
    cache_ttl_metagraph: int = 300
    cache_ttl_price: int = 30
    cache_ttl_dynamic_info: int = 120
    cache_ttl_balance: int = 60
    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
