"""Stable stdio entry point copied into the installation root by the installer."""
from pathlib import Path
import re
import sys


def main():
    launcher = Path(__file__).resolve()
    try:
        version = launcher.with_name("version.txt").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise ValueError
        scripts = launcher.parents[1] / "versions" / version / "scripts"
        if not (scripts / "bridge.py").is_file():
            raise ValueError
    except (OSError, ValueError):
        print("Screen Mentor MCP: run the Windows installer to restore the local entry point.", file=sys.stderr)
        return 1
    sys.path.insert(0, str(scripts))
    from bridge import main as run_bridge

    return run_bridge()


if __name__ == "__main__":
    sys.exit(main())
