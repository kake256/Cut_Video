"""Load the WebUI's static CSS and JavaScript assets.

Keeping these resources outside :mod:`app` makes the Python composition layer
readable while retaining the historical module-level compatibility aliases.
"""

from pathlib import Path


_ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"


def _load_required_asset(filename: str) -> str:
    path = _ASSET_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required WebUI asset is missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"Required WebUI asset is not valid UTF-8: {path}") from exc


_APP_CSS = _load_required_asset("app.css")
_INTUITIVE_EDITOR_JS = _load_required_asset("intuitive_editor.js")
