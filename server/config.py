"""Everything this service is told from outside, read once at startup.

Nothing in here is a secret, because this service holds no secrets. The only key
it knows is the **public** half of the release key, and its only job is to let
the server notice when the manifest it fetched is not the one the release
machine signed. If this file ever grows a private key, the design has gone
wrong.

Missing configuration stops the process rather than starting a site whose
Download button is broken. A deploy that fails loudly gets fixed in two minutes
by the person who just pressed Deploy; a deploy that half works gets discovered
by a stranger.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import signing


class ConfigurationError(RuntimeError):
    """Printed at startup and then the process exits. The message is the fix."""


@dataclass(frozen=True)
class Config:
    public_key: bytes          # Ed25519, 32 bytes: verifies the manifest
    key_id: str                # first 8 hex of its SHA-256, for the logs and /api/health
    repo: str                  # "DeroXP/Mistery" - the source and releases links
    manifest_url: str          # where the signed manifest is published
    manifest_json: str         # a signed manifest pasted in directly; wins over the URL
    manifest_ttl: int          # seconds before the server looks again
    fetch_timeout: float       # seconds: one deadline over the whole fetch, not per read
    rate_limit_requests: int   # per window, per IP, on /api/* and /update/manifest.json
    rate_limit_window: int     # seconds
    site_url: str              # absolute https URL of this site, for the canonical link

    @property
    def releases_url(self) -> str:
        return f"https://github.com/{self.repo}/releases"

    @property
    def source_url(self) -> str:
        return f"https://github.com/{self.repo}"


def _int(env: dict, name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a whole number, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}, got {value}")
    return value


def load(env: dict | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    raw_key = env.get("MISTERY_UPDATE_PUBLIC_KEY", "").strip()
    if not raw_key:
        raise ConfigurationError(
            "MISTERY_UPDATE_PUBLIC_KEY is not set.\n"
            "  It is the public key printed by `python packaging/make_key.py`, and\n"
            "  also the one line in packaging/public_key.txt. Without it this\n"
            "  service cannot tell a real manifest from a forged one, so it will\n"
            "  not start. Railway: the service, then Variables, then New Variable."
        )
    try:
        public_key = signing.parse_public_key(raw_key)
    except ValueError as exc:
        raise ConfigurationError(f"MISTERY_UPDATE_PUBLIC_KEY is unusable: {exc}") from None

    repo = env.get("MISTERY_REPO", "DeroXP/Mistery").strip().strip("/")
    if repo.count("/") != 1 or not all(part.strip() for part in repo.split("/")):
        raise ConfigurationError(f"MISTERY_REPO should look like owner/name, got {repo!r}")

    # The default points at GitHub's "latest release" alias, so cutting a release
    # publishes it: no variable to change here, no redeploy, nothing to forget.
    # `mistery-update.json` is the name .github/workflows/release.yml uploads;
    # that file name is the whole contract between the release and this service,
    # and MISTERY_MANIFEST_URL is how it gets fixed without a code change if it
    # ever moves.
    manifest_url = env.get(
        "MISTERY_MANIFEST_URL",
        f"https://github.com/{repo}/releases/latest/download/mistery-update.json",
    ).strip()
    if not manifest_url.startswith("https://"):
        raise ConfigurationError(
            f"MISTERY_MANIFEST_URL must be https, got {manifest_url!r}.\n"
            "  The signature would catch a swapped manifest on a plain http hop,\n"
            "  but there is no reason to let anyone try."
        )

    site_url = env.get("MISTERY_SITE_URL", "").strip().rstrip("/")
    if not site_url:
        # Railway sets this for every service it gives a domain to, so the
        # canonical link and the Open Graph tags are right with no variable set.
        domain = env.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        site_url = f"https://{domain}" if domain else ""
    if site_url and "://" not in site_url:
        # A bare domain is what Railway shows you on its own Networking page, so
        # it is what people paste. Reading it as https is not a guess: this
        # service is only ever reached over https, and the alternative was
        # refusing to start over a missing prefix, which is what happened.
        site_url = f"https://{site_url}"
    if site_url and not site_url.startswith("https://"):
        raise ConfigurationError(
            f"MISTERY_SITE_URL must be https, got {site_url!r}.\n"
            f"  Drop the http:// and let it be https://, or unset the variable:\n"
            f"  with no value it uses the domain Railway already tells the service about."
        )

    # Kept exactly as it was pasted, trailing newline and all. This service's
    # one promise about the manifest is that it repeats bytes rather than
    # reformatting them, and stripping whitespace here would quietly break that
    # for the one path where a human is involved.
    pasted = env.get("MISTERY_MANIFEST_JSON", "")
    if not pasted.strip():
        pasted = ""

    return Config(
        public_key=public_key,
        key_id=signing.key_id(public_key),
        repo=repo,
        manifest_url=manifest_url,
        # The escape hatch for the day GitHub is down, or a release has to be
        # pinned by hand: paste the whole signed manifest in as one variable. It
        # still has to carry a good signature - this is not a way round that.
        manifest_json=pasted,
        manifest_ttl=_int(env, "MISTERY_MANIFEST_TTL", 300, 15, 86400),
        fetch_timeout=float(_int(env, "MISTERY_FETCH_TIMEOUT", 10, 1, 60)),
        # An updater checks once an hour and almost always gets a 304. 60 an hour
        # per IP is far more than any real client needs and low enough that a
        # stuck retry loop stops costing us GitHub requests. A household behind
        # one address would have to run 60 copies of Mistery to notice.
        rate_limit_requests=_int(env, "MISTERY_RATE_LIMIT_REQUESTS", 60, 1, 100000),
        rate_limit_window=_int(env, "MISTERY_RATE_LIMIT_WINDOW", 3600, 1, 86400),
        site_url=site_url,
    )
