"""Configuration. Everything comes from the environment, nothing is hardcoded.

Runs on SQLite with zero infrastructure for development, and on Postgres +
Redis in production by changing two environment variables. That is deliberate:
the code path is identical, so what you test locally is what ships.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---------------------------------------------------------
    app_name: str = "Trump Death Watcher"
    env: str = "dev"                       # dev | prod
    base_url: str = "http://127.0.0.1:8000"

    # SQLite for dev, e.g. postgresql+psycopg://user:pw@host/db for prod.
    database_url: str = "sqlite:///./var/backend.db"

    # Empty = in-process fanout (single replica). Set a redis:// URL to run
    # multiple app servers behind a load balancer; see app/fanout.py.
    redis_url: str = ""

    # --- auth ---------------------------------------------------------
    # MUST be overridden in production. Startup refuses to boot with the
    # default value when env=prod.
    jwt_secret: str = "dev-only-insecure-change-me"
    jwt_algorithm: str = "HS256"
    access_token_ttl_s: int = 60 * 60 * 24 * 30      # 30 days; mobile-friendly

    # --- billing ------------------------------------------------------
    price_cents: int = 4999                # $49.99 one-time
    currency: str = "usd"
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""              # optional; falls back to ad-hoc price

    # Stripe Managed Payments is on by default for new accounts, and it REQUIRES
    # a product tax code — without one, checkout.Session.create fails outright.
    #
    # txcd_10503004 = "Digital other news or documents — viewable only —
    # non subscription — with permanent rights", which is precisely this
    # product: one payment, lifetime access, viewed in-app rather than
    # downloaded. Reasonable alternatives if your accountant prefers:
    #   txcd_10701411  Electronically Delivered Information Services (personal)
    #   txcd_10000000  General — Electronically Supplied Services
    # The code affects how much sales tax/VAT your customers are charged, so
    # confirm it rather than inheriting this default forever.
    stripe_tax_code: str = "txcd_10503004"
    # Set false to opt out of Managed Payments per session and handle tax
    # yourself. Leaving it on means Stripe calculates and remits for you.
    stripe_managed_payments: bool = True
    # {CHECKOUT_SESSION_ID} is substituted by Stripe on redirect. It lets the
    # client ask us to verify the payment directly with Stripe, so a broken or
    # misconfigured webhook cannot leave a paying customer with no access.
    checkout_success_path: str = "/?paid=1&session_id={CHECKOUT_SESSION_ID}"
    checkout_cancel_path: str = "/?canceled=1"

    # Lets you use the product before Stripe exists. Refuses to apply when
    # env=prod, so it cannot become an accidental free-for-all in production.
    allow_dev_grant: bool = True

    # --- ingest from the detector -------------------------------------
    # Shared secret with the VPS. Must equal TICKER_WEBHOOK_SECRET there.
    ingest_secret: str = ""
    ingest_tolerance_s: int = 300          # replay window for signed requests

    # --- web push -----------------------------------------------------
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:ops@example.com"

    # --- content ------------------------------------------------------
    news_feed_urls: str = (
        "https://feeds.bbci.co.uk/news/world/rss.xml,"
        "https://www.theguardian.com/us-news/rss"
    )
    news_cache_ttl_s: int = 300
    market_cache_ttl_s: int = 60

    # --- our own coin -------------------------------------------------
    # A Solana token has two addresses people quote interchangeably, and they
    # are not the same thing:
    #   mint — the token itself. What wallets and pump.fun URLs use.
    #   pool — the liquidity pair it trades in. What Axiom links to, and the
    #          only one chart APIs accept, because price only exists in a pool.
    # Both below belong to the same token; keep them in step if you migrate
    # liquidity to a new pool, or the chart will quietly track the old one.
    memecoin_symbol: str = "TRUMPDEAD"
    memecoin_name: str = "Trump Dead Coin"
    memecoin_network: str = "solana"
    memecoin_mint: str = "GqzL6CPTPTXewuTAeUnuFVpVAjyyAR6hLwKRnFQ4pump"
    memecoin_pool: str = "FtKbVFZWka8qmJs2FQgYx5i2cmppBjvgxDhBWPL1T5Mt"
    memecoin_url: str = (
        "https://pump.fun/coin/GqzL6CPTPTXewuTAeUnuFVpVAjyyAR6hLwKRnFQ4pump"
    )

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in ("prod", "production")

    @property
    def sqlalchemy_url(self) -> str:
        """Normalise the URL Render (and Heroku) hand out.

        They inject `postgres://…`, which SQLAlchemy 2.0 refuses to parse, and
        even `postgresql://` picks the psycopg2 driver we do not install. Doing
        this here means the deploy config can paste the provider's string
        verbatim instead of everyone remembering to rewrite it.
        """
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def news_urls(self) -> list[str]:
        return [u.strip() for u in self.news_feed_urls.split(",") if u.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
