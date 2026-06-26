"""
Central configuration. Everything secret comes from environment variables.
Copy backend/.env.example to backend/.env and fill it in for local dev.
On Render, set these in the dashboard (or via render.yaml).
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App ---
    APP_NAME: str = "AdLens"
    ENV: str = "dev"                       # dev | prod
    SESSION_SECRET: str = "change-me-to-a-long-random-string"
    SESSION_MAX_AGE: int = 400 * 24 * 60 * 60   # ~400 days (browser cap); refreshed on each visit
    REMEMBER_MAX_AGE: int = 400 * 24 * 60 * 60  # restore session without re-clicking Facebook
    TOKEN_REFRESH_DAYS: int = 14                # refresh Meta token when this many days left
    FRONTEND_URL: str = "http://localhost:8000"   # where to send users after login
    BASE_URL: str = "http://localhost:8000"       # this backend's public URL

    # --- Database ---
    # Render Postgres provides DATABASE_URL automatically.
    DATABASE_URL: str = "sqlite:///./adlens.db"

    # --- Meta (Facebook) App ---
    # Create at developers.facebook.com -> My Apps -> Create App (type "Business").
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_API_VERSION: str = "v25.0"
    # Must EXACTLY match the redirect URI you whitelist in the Meta app's
    # Facebook Login settings, e.g. https://api.yourdomain.com/auth/facebook/callback
    META_REDIRECT_URI: str = "http://localhost:8000/auth/facebook/callback"
    META_SCOPES: str = "ads_read,public_profile,email"

    # --- Token encryption ---
    # Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    FERNET_KEY: str = ""

    # --- Billing (Stripe) ---
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PRICE_ID: str = ""              # the price for your paid plan
    STRIPE_WEBHOOK_SECRET: str = ""

    # --- Insights cache ---
    CACHE_TTL_FREE_MIN: int = 60           # free tier: refresh at most hourly
    CACHE_TTL_PRO: int = 15                # pro tier: refresh every 15 min

    # --- Admin bypass (comma-separated; gets Pro features, shows as Admin) ---
    ADMIN_EMAILS: str = "nomanpeera@gmail.com"
    ADMIN_FB_IDS: str = ""

    @property
    def meta_redirect_uri(self) -> str:
        """Use explicit META_REDIRECT_URI, or derive from BASE_URL in prod."""
        default = "http://localhost:8000/auth/facebook/callback"
        if self.META_REDIRECT_URI != default:
            return self.META_REDIRECT_URI
        if self.BASE_URL.rstrip("/") != "http://localhost:8000":
            return f"{self.BASE_URL.rstrip('/')}/auth/facebook/callback"
        return default

    def meta_configured(self) -> bool:
        return bool(self.META_APP_ID and self.META_APP_SECRET and self.FERNET_KEY)


settings = Settings()
