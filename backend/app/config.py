import logging
import re
from typing import List
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Set up standard logging configuration for enterprise-grade audits
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.config")

# Local SQLite file used when DATABASE_URL is unset/empty or still holds an
# unfilled template placeholder (see Settings.async_database_url). Path is
# relative to the process CWD, which is backend/ when launched via `npm run dev`.
SQLITE_FALLBACK_URL = "sqlite+aiosqlite:///app.db"

# Detects unfilled .env.example template values: bracketed ones such as
# "[YOUR_PROJECT_REF]" / "[YOUR_PASSWORD]" and angle-bracket DSNs from the
# documented example ("postgresql://<user>:<password>@<host>:5432/<db>").
_PLACEHOLDER_RE = re.compile(
    r"\[your_[^\]]*\]|<(?:your|user|password|host|db|project)[^>]*>",
    re.IGNORECASE,
)


def contains_placeholder(url: str) -> bool:
    """Return True when *url* still contains an unfilled template placeholder.

    Such a value is a configuration mistake, never a usable DSN. Handing it to
    the driver produces the cryptic asyncpg error
    ``(ENOTFOUND) tenant/user postgres.[YOUR_PROJECT_REF] not found`` from the
    Supabase pooler, which hides the actual cause (an unedited .env).
    """
    return bool(_PLACEHOLDER_RE.search(url or ""))

class Settings(BaseSettings):
    """
    Enterprise Banking System Configuration.
    Loads variables dynamically from .env or ambient environment variables.
    """
    # Database Settings
    DATABASE_URL: str = "sqlite+aiosqlite:///app.db"
    # When DATABASE_URL is empty OR still holds an unfilled placeholder (e.g.
    # "[YOUR_PASSWORD]" copied from .env.example), degrade to the local SQLite
    # file with a loud warning instead of refusing to boot — this is the
    # behaviour the README documents ("works without a Supabase project via the
    # SQLite fallback"). Set false in production/deployments so a leftover
    # placeholder becomes a hard startup error rather than silently pointing the
    # app at an empty local database.
    ALLOW_SQLITE_FALLBACK: bool = True

    # JWT auth (loaded from backend/.env; consumed by app/auth.py). An empty
    # value — or an unfilled template placeholder such as
    # "[YOUR_SECRET_KEY_CHANGE_IN_PRODUCTION]" — falls back to the development
    # secret with a loud startup warning (see app/auth.py).
    JWT_SECRET_KEY: str = ""
    JWT_EXPIRATION_MINUTES: int = 720

    # Gate for ``POST /api/auth/register`` (documented in .env.example, and
    # previously dead config: the endpoint was callable no matter what this
    # said). Set false in shared/production deployments where accounts are
    # provisioned out-of-band, so the public surface cannot create users.
    SIGNUP_ENABLED: bool = True

    # --- Bootstrap system account -----------------------------------------
    # Consumed by ``app.migrations.seed_default_user``. The id must stay
    # ``00000000-...`` because ``ProjectRepository.DEFAULT_SYSTEM_USER_ID``
    # (and every project created without an authenticated owner) references it.
    SYSTEM_USER_ID: str = "00000000-0000-0000-0000-000000000000"
    SYSTEM_USER_EMAIL: str = "system@banking.com"
    SYSTEM_USER_NAME: str = "System User"
    SYSTEM_USER_ROLE: str = "Developer"
    # Plaintext bootstrap password, bcrypt-hashed on insert / back-filled onto
    # an existing row whose ``password_hash`` is still empty. Empty means "do
    # not touch the stored credential" (and the account stays un-loginable
    # until a password is provisioned by other means).
    SYSTEM_USER_PASSWORD: str = ""

    @property
    def async_database_url(self) -> str:
        url = (self.DATABASE_URL or "").strip()
        if not url:
            if not self.ALLOW_SQLITE_FALLBACK:
                raise RuntimeError(
                    "DATABASE_URL is empty and ALLOW_SQLITE_FALLBACK is false. "
                    "Configure the Supabase PostgreSQL connection string before startup."
                )
            logger.warning(
                "DATABASE_URL is empty. Falling back to the local SQLite database (%s); "
                "data written now will NOT reach Supabase.",
                SQLITE_FALLBACK_URL,
            )
            return SQLITE_FALLBACK_URL
        # Unfilled template placeholder (copied verbatim from .env.example).
        # Never hand this to the driver: asyncpg only reports it as the cryptic
        # "(ENOTFOUND) tenant/user postgres.[YOUR_PROJECT_REF] not found".
        if contains_placeholder(url):
            hint = (
                "DATABASE_URL still contains an unfilled placeholder (e.g. "
                "[YOUR_PROJECT_REF] / [YOUR_PASSWORD]). Fill in your real Supabase "
                "project reference and database password in backend/.env."
            )
            if not self.ALLOW_SQLITE_FALLBACK:
                raise RuntimeError(
                    f"{hint} ALLOW_SQLITE_FALLBACK is false, so startup is "
                    "aborted instead of falling back to local SQLite."
                )
            logger.warning(
                "%s Falling back to the local SQLite database (%s) so development "
                "can continue; data written now will NOT reach Supabase. Set a "
                "real DATABASE_URL and restart to switch.",
                hint,
                SQLITE_FALLBACK_URL,
            )
            return SQLITE_FALLBACK_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        # Add SSL mode for Supabase cloud connections
        if "supabase.com" in url and "ssl=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}ssl=require"
        # Supabase's transaction pooler (:6543) does not support prepared
        # statements; asyncpg uses them by default, so disable its cache.
        if (
            "pooler.supabase.com" in url
            and ":6543" in url
            and "prepared_statement_cache_size=" not in url
        ):
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}prepared_statement_cache_size=0"
        return url

    # Local LLM / LM Studio Orchestration Settings
    LM_STUDIO_URL: str = "http://localhost:1234/v1"
    LM_STUDIO_API_KEY: str = "lm-studio"
    # Default model id used for inference. Override via LM_STUDIO_MODEL_FALLBACK in
    # backend/.env to match whatever model is loaded in LM Studio (no longer a
    # hard-coded reference — see Finding #40). backend/.env.example ships qwen3.5.
    LM_STUDIO_MODEL_FALLBACK: str = "qwen3.5-9b-instruct"
    # Bound one provider call and the complete raw+fallback structured-output
    # sequence. Background jobs may outlive browser requests, but model work
    # must still terminate predictably.
    LLM_REQUEST_TIMEOUT_SECONDS: float = 600.0
    LLM_STRUCTURED_TOTAL_TIMEOUT_SECONDS: float = 630.0
    # LM Studio's OpenAI-compatible endpoint does not reliably implement
    # schema-constrained output for Qwen. Keep the optional provider fallback
    # available for other deployments, but do not repeat the complete prompt by
    # default after local JSON parsing fails.
    LLM_STRUCTURED_PROVIDER_FALLBACK: bool = False
    # Whole background-job deadline and process-wide heavy-inference limit.
    # One concurrent generation avoids swapping several 9B contexts on a 16GB Mac.
    LLM_BACKGROUND_JOB_TIMEOUT_SECONDS: float = 660.0
    LLM_MAX_CONCURRENT_HEAVY_JOBS: int = 1
    # Sequential, context-safe passes permitted for one audit. Eight allows a
    # normal project-wide + individual-policy evidence set on an 8k local model
    # while still bounding latency and inference work.
    AUDITOR_MAX_PASSES: int = 8
    # Keep this equal to (or below) the context length configured when the model
    # is loaded in LM Studio. MAX_CONTEXT_TOKENS may be raised for other flows,
    # but doing so must not make the Auditor assume the local model is larger.
    AUDITOR_CONTEXT_TOKENS: int = 8192
    # Completion tokens also occupy the model context window. The Auditor uses
    # this value both for the client and when calculating its input allowance.
    AUDITOR_OUTPUT_TOKENS: int = 3000
    # Qwen3.x models are REASONING models: they first emit a long `reasoning_content`
    # chain-of-thought and only then the real answer. With a bounded max_tokens the
    # model can exhaust the whole budget thinking and return an EMPTY `content`
    # (finish_reason="length"), so every structured-JSON parser fails and PRD /
    # audit generation surfaces as "Connection error" / "cannot generate PRD".
    # When enabled, every request sets chat_template_kwargs={"enable_thinking": false}
    # so the model writes its answer directly to `content` (fast + JSON-parseable).
    LM_STUDIO_DISABLE_THINKING: bool = True

    # Context & Memory Safety Constraints (optimised for MacBook Air M4 with 16GB RAM)
    MAX_CONTEXT_TOKENS: int = 8192
    TEMPERATURE: float = 0.0

    # Document upload & extraction (see app/routes/documents.py, app/document_processor.py)
    # MAX_UPLOAD_MB is the ONLY size gate applied at save time — it guards against
    # disk abuse. It is deliberately NOT a token check: the FULL converted markdown
    # is always persisted regardless of size. Token limits apply solely to the
    # LLM-feeding path (the explicit "process this document" action).
    MAX_UPLOAD_MB: int = 25
    # Fraction of MAX_CONTEXT_TOKENS reserved for a document chunk so the system
    # prompt + gatherer instructions + tool schema + output slack still fit when
    # the chunk is joined to the prompt (Option 2 math — never set the chunk near
    # the full context window).
    DOCUMENT_BUDGET_FRACTION: float = 0.6
    # Target token size of a single document chunk (overlap keeps headings and
    # context from being clipped at chunk boundaries).
    #
    # Sizing math (MAX_CONTEXT_TOKENS=8192, gatherer max_tokens=3000, ~700
    # prompt/system/format tokens): chunk 3500 + overlap 250 = 3750 input tokens
    # leaves 3750 tokens of headroom for the prompt and the extracted JSON, so a
    # single pass stays comfortably inside the window. Bigger chunks => fewer
    # gatherer passes => faster extraction for otherwise-equal document content.
    DOCUMENT_CHUNK_SIZE: int = 3500
    DOCUMENT_CHUNK_OVERLAP: int = 250
    # Bounded parallelism across chunked gatherer passes. 1 = fully sequential
    # (safe default for a single-slot local LM Studio model on a 16 GB laptop).
    # Raise to 2-4 when the inference gateway can serve concurrent requests
    # (LM Studio multi-slot, or an OpenAI-compatible cloud endpoint) to cut the
    # wall-clock time of many-chunk documents almost linearly. Results keep their
    # document order regardless of this value.
    DOCUMENT_EXTRACTION_CONCURRENCY: int = 1
    # Hard ceiling on how many chunked gatherer passes a single document
    # extraction may produce. Beyond this the extraction is refused (413) with a
    # clear message instead of silently degrading into a truncation.
    MAX_DOCUMENT_CHUNKS: int = 30

    # Rate limiting (per-client sliding window; see app/rate_limit.py).
    # Finding #39: LLM-facing endpoints accept arbitrary input, so inbound
    # requests are throttled per client IP per scope.
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_CHAT_LIMIT: int = 30
    RATE_LIMIT_CHAT_WINDOW: int = 60
    RATE_LIMIT_WORKFLOW_LIMIT: int = 60
    RATE_LIMIT_WORKFLOW_WINDOW: int = 60

    # Store backing the limiter. Only "in-process" is implemented today; the
    # startup guard (verify_single_worker_guarantee) then REQUIRES a single
    # uvicorn worker and refuses to boot otherwise, so 429 protection can never
    # silently disappear under a multi-process deployment. Set this to a
    # distributed store's name only once such a backend exists.
    RATE_LIMIT_STORE: str = "in-process"
    # Explicit opt-out: keep the in-process store across N workers (each worker
    # then gets its own independent budget). This is a *weaker* posture; the
    # app still logs a loud warning at startup when workers > 1.
    RATE_LIMIT_ALLOW_MULTI_PROCESS_IN_PROCESS: bool = False
    # Proxy-aware keying: when the immediate socket peer is one of
    # TRUSTED_PROXY_IPS, the real client IP is read from the rightmost
    # X-Forwarded-For / Forwarded entry. Headers are ignored unless the peer is
    # trusted, so a client can never widen its own bucket by spoofing them.
    TRUST_PROXY_HEADERS: bool = False
    TRUSTED_PROXY_IPS: str = ""

    # SSE pub/sub reliability (see app/event_manager.py).
    # The event bus is in-process (process-local subscriber queues + replay
    # buffer) and, like the rate limiter, is safe only for a single uvicorn
    # worker. The startup guard (verify_single_worker_guarantee) refuses to boot
    # when multiple workers are detected, so live updates can never silently
    # split across workers. A bounded per-project ring buffer
    # (SSE_HISTORY_BUFFER_SIZE) lets a reconnecting client replay events it
    # missed while disconnected (SSE Last-Event-ID); history is process-local so
    # a full restart loses it (forward progress resumes on the next publish).
    SSE_HISTORY_BUFFER_SIZE: int = 1000
    # Explicit opt-out: run N workers with the in-process SSE bus (broken event
    # delivery by construction). The app logs a loud warning at startup instead
    # of refusing to boot. Not recommended.
    SSE_ALLOW_MULTI_PROCESS_IN_PROCESS: bool = False
    # Semantic memory (see app/semantic_memory.py).
    # Distilled project facts + conversation embeddings retrieved at prompt
    # build time so agents "remember" prior decisions across sessions. The
    # whole pipeline is FAIL-OPEN: any LM Studio/DB failure degrades to the
    # pre-memory behaviour instead of breaking the chat turn.
    MEMORY_ENABLED: bool = True
    # Master switch for the LLM fact-extraction pass (recall stays on).
    MEMORY_FACT_EXTRACTION_ENABLED: bool = True
    # Embedding model served by LM Studio (nomic-embed-text = 768 dims, fast on M4).
    LM_STUDIO_EMBEDDING_MODEL: str = "text-embedding-nomic-embed-text-v1.5"
    # Embedding request timeout (embeddings must never block a chat turn long).
    MEMORY_EMBEDDING_TIMEOUT_SECONDS: float = 10.0
    # How many memories are injected into a prompt at most.
    MEMORY_TOP_K: int = 6
    # Hard token ceiling for the "Relevant project memory" prompt block,
    # mirroring the MAX_CONTEXT_TOKENS budget discipline (Finding #39).
    MEMORY_CONTEXT_MAX_TOKENS: int = 800
    # Candidate scan window: only the N most recent memories per project are
    # scored in-process (keeps the Python cosine pass O(400) worst case).
    MEMORY_CANDIDATE_LIMIT: int = 400
    # Relevance blend: score = MEMORY_RELEVANCE_WEIGHT * cosine
    #                 + (1 - MEMORY_RELEVANCE_WEIGHT) * recency_factor,
    # where recency_factor = 0.5 ** (age_days / MEMORY_RECENCY_HALF_LIFE_DAYS).
    MEMORY_RELEVANCE_WEIGHT: float = 0.7
    MEMORY_RECENCY_HALF_LIFE_DAYS: float = 30.0
    # New facts with cosine similarity >= this against an existing memory are
    # considered duplicates and skipped (keeps memory from bloating).
    MEMORY_DEDUPE_SIMILARITY: float = 0.92
    # Cap on facts extracted from a single chat turn (token-budget hygiene).
    MEMORY_MAX_FACTS_PER_TURN: int = 8

    # Reusable banking knowledge retrieval. Embeddings use the same local model
    # as semantic memory; lexical scoring remains available while it is offline.
    BANKING_RAG_CHUNK_TOKENS: int = 700
    BANKING_RAG_CHUNK_OVERLAP: int = 100
    BANKING_RAG_TOP_K: int = 6
    BANKING_RAG_MAX_CONTEXT_TOKENS: int = 3500

    # SSE pub/sub reliability (see app/event_manager.py).

    # Application details
    APP_NAME: str = "Enterprise Requirements Architecture Core"
    DEBUG: bool = False

    @field_validator("DEBUG", mode="before")
    @classmethod
    def normalize_debug_mode(cls, value):
        """Accept common runtime mode names accidentally exported as DEBUG.

        Some IDEs and process managers export ``DEBUG=release`` or
        ``DEBUG=development``.  Because pydantic-settings reads the ambient
        environment before uvicorn starts, those otherwise harmless values used
        to abort the entire backend and leave Vite reporting opaque proxy
        connection errors.  Preserve normal boolean parsing while mapping the
        conventional mode names to their intended boolean value.
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "production", "prod"}:
                return False
            if normalized in {"debug", "development", "dev"}:
                return True
        return value

    # CORS security: explicit allowed origins (comma-separated in .env).
    # Never use "*" with allow_credentials=True — browsers reject this combination.
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse the comma-separated CORS_ORIGINS string into a list."""
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def trusted_proxy_ip_list(self) -> List[str]:
        """Parse the comma-separated TRUSTED_PROXY_IPS string into a list."""
        return [ip.strip() for ip in self.TRUSTED_PROXY_IPS.split(",") if ip.strip()]

    # Force configurations from .env file if available
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

settings = Settings()
logger.info(f"Loaded config for application: '{settings.APP_NAME}' with Context Limit: {settings.MAX_CONTEXT_TOKENS}")
