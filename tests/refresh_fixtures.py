"""Re-download the HTML fixtures used by the test suite.

Run sparingly and never in CI. The SEC portal is fronted by a bot defence that
starts serving a JavaScript interstitial when requests arrive too quickly, so
this script fetches sequentially with a delay and refuses to overwrite a fixture
with a challenge page.

    python tests/refresh_fixtures.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402
from app.http_client import detect_bot_challenge  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

TARGETS: list[tuple[str, str]] = [
    (
        "form59_gulf.html",
        f"{config.PATH_FORM59}?DateFrom=20220101&DateTo=20260804"
        "&DateType=1&uniqueIDReference=0000008616",
    ),
    ("profile_gulf.html", config.PATH_COMPANY_PROFILE.format(symbol="GULF")),
    ("profile_gjs.html", config.PATH_COMPANY_PROFILE.format(symbol="GJS")),
    ("listed_index_e.html", config.PATH_LISTED_INDEX.format(letter="E")),
]

DELAY_SECONDS = 3.0


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    failures = 0

    with httpx.Client(
        base_url=config.BASE_URL,
        headers=config.DEFAULT_HEADERS,
        timeout=config.HTTP_TIMEOUT,
        follow_redirects=True,
    ) as client:
        for index, (name, path) in enumerate(TARGETS):
            if index:
                time.sleep(DELAY_SECONDS)

            response = client.get(path)
            html = response.content.decode("utf-8", errors="replace")

            if response.status_code != 200:
                print(f"FAIL {name}: HTTP {response.status_code}")
                failures += 1
                continue

            if detect_bot_challenge(html):
                # Overwriting a good fixture with an interstitial would silently
                # gut the test suite, so bail out instead.
                print(f"FAIL {name}: bot challenge served - wait a few minutes and retry")
                failures += 1
                continue

            (FIXTURES / name).write_text(html, encoding="utf-8")
            print(f"OK   {name}: {len(html):,} bytes")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
