"""Weekly check of the newest Japanese catalog published by the pinned BCSFE."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Scripts run with scripts/ as sys.path[0]; add the repository root so the
# application modules can be imported reliably in GitHub Actions.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Reuse the live app's fresh-save catalog builder. No account or game-server
# operations are performed by this check.
os.environ.setdefault("APP_PASSWORD", "catalog-check-only")
import app
import engine


def main() -> int:
    cc, gv = app._get_bcsfe()
    response = engine.core.RequestHandler(engine.core.GameDataGetter.repo_url()).get()
    if response is None or response.status_code != 200:
        raise RuntimeError("BCSFE game-data metadata is unavailable")
    metadata = response.json()
    versions = engine.core.GameDataGetter.get_versions(metadata).get(cc.get_code(), {})
    candidates = [
        version for version in versions
        if isinstance(version, str) and engine.version_to_string(version)
    ]
    if not candidates:
        raise RuntimeError("No Japanese game-data versions were published")
    latest = max(candidates, key=engine.version_key)

    # Same fresh target-version SaveFile path used by the live app.
    names, method = app._build_names(cc, gv, latest)
    if not names:
        raise RuntimeError(f"Catalog build returned no characters for JP {latest}")
    catalog_ids = set(names)
    stale_exclusions = sorted(engine.ERROR_CAT_IDS - catalog_ids)

    lines = [
        "# Weekly Japanese game-data check",
        f"- Pinned application catalog: **{engine.GAME_DATA_VERSION}**",
        f"- Latest BCSFE-published JP data: **{latest}**",
        f"- Fresh catalog entries: **{len(names)}**",
        f"- Catalog extraction method: `{method}`",
        f"- Error-character exclusion IDs absent from this catalog: `{stale_exclusions or 'none'}`",
    ]
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    print(" | ".join(line.replace("**", "") for line in lines[1:]))

    if engine.version_key(latest) > engine.version_key(engine.GAME_DATA_VERSION):
        print(
            f"::warning::Newer JP game data {latest} is available; "
            "review the full catalog diff before changing the pinned version."
        )
    if stale_exclusions:
        print(
            "::warning::Some legacy error-character exclusion IDs are absent "
            "from the latest catalog; review them before changing the list."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
