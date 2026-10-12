"""Claude SessionStart setup for the administration frontdoor; never calls Host."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shlex
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    message = None
    if sys.version_info < (3, 13):
        message = "PMT Server needs Python 3.13 or later."
    elif len(argv) != 2 or argv[0] != "--config-root":
        message = "Set the PMT Host configuration folder in plugin settings."
    else:
        python_path = Path(sys.executable)
        config_root = Path(argv[1])
        if not python_path.is_absolute() or not python_path.is_file():
            message = "PMT Server Python is unavailable; configure a valid Python 3.13+ interpreter."
        elif not config_root.is_absolute():
            message = "Set the absolute PMT Host configuration folder in plugin settings."
        else:
            config_file = config_root / "host-config.json"
            if config_root.exists() and not config_root.is_dir():
                message = "PMT Host configuration path is not a folder; check the plugin settings."
            elif not config_file.is_file():
                message = "PMT Host is not initialized; run pmt-server init with the configured folder."
    can_export = (
        sys.version_info >= (3, 13)
        and len(argv) == 2
        and argv[0] == "--config-root"
        and Path(argv[1]).is_absolute()
        and Path(sys.executable).is_absolute()
        and Path(sys.executable).is_file()
    )
    if can_export:
        if os.environ.get("CLAUDE_ENV_FILE"):
            path = Path(os.environ["CLAUDE_ENV_FILE"])
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    raise OSError("unsafe environment file")
                lines = "export PMT_SERVER_PYTHON=" + shlex.quote(sys.executable) + "\n"
                lines += "export PMT_HOST_CONFIG_ROOT=" + shlex.quote(str(Path(argv[1]))) + "\n"
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(lines)
            except OSError:
                message = "PMT Server command setup failed; check the session environment file."
    print(json.dumps({"systemMessage": message} if message else {}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
