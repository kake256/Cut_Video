"""Show which JS runtime and PO token provider yt-dlp will use for CUT's YouTube requests.

Nothing is sent to YouTube: this only builds yt-dlp with the options CUT passes
(moment_retrieval.youtube_block.client_options) and asks it what it found.

    venv\\Scripts\\python.exe scripts\\check_ytdlp_setup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    import yt_dlp
    from yt_dlp import YoutubeDL

    from moment_retrieval import youtube_block

    options = youtube_block.client_options("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    print(f"yt-dlp: {yt_dlp.version.__version__}")
    print(f"CUT options: {options}")
    ok = True
    with YoutubeDL({"quiet": True, "no_warnings": True, **options}) as ydl:
        try:
            runtimes = {name: runtime for name, runtime in ydl._js_runtimes.items()}
            usable = [f"{name}-{runtime.info.version}" for name, runtime in runtimes.items()
                      if runtime.info is not None and runtime.info.supported]
            print(f"JS runtime in use: {usable[0] if usable else 'none'}"
                  + (f" (also usable: {', '.join(usable[1:])})" if len(usable) > 1 else ""))
            ok = ok and bool(usable)
        except Exception as exc:  # internal API; report rather than fail
            print(f"JS runtime: could not be determined ({exc})")
        try:
            from yt_dlp.extractor.youtube.pot._director import initialize_pot_director

            director = initialize_pot_director(ydl.get_info_extractor("Youtube"))
            providers = list(director.providers.values())
            if not providers:
                print("PO token providers: none (pip install -r requirements.txt)")
            for provider in providers:
                if "HTTP" in provider.PROVIDER_KEY:  # checked only when a token is needed
                    state = "used only while a bgutil server runs (not checked here)"
                else:
                    state = "available" if provider.is_available() else "unavailable"
                print(f"PO token provider: {provider.PROVIDER_KEY} {provider.PROVIDER_VERSION} {state}")
            if youtube_block.pot_provider_installed() and not any(
                    provider.is_available() for provider in providers if "Script" in provider.PROVIDER_KEY):
                print("  -> run scripts\\setup_pot_provider.ps1 to build the token script")
                ok = False
        except Exception as exc:
            print(f"PO token providers: could not be determined ({exc})")
    print(f"YouTube player clients: {youtube_block.player_clients() or 'yt-dlp defaults'}")
    print(f"Wait between YouTube requests: {youtube_block.MIN_GAP_SEC:g}-{youtube_block.MAX_GAP_SEC:g} s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
