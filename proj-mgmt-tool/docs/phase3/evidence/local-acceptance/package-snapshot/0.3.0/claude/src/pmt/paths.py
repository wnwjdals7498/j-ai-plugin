"""Resolve package-independent user data and configuration roots."""
import os
from pathlib import Path

APP_NAME = "pmt-v3"

def default_roots():
    if os.name == "nt":
        data_base = os.environ.get("LOCALAPPDATA")
        config_base = os.environ.get("APPDATA")
        if data_base is None:
            data_base = str(Path.home() / "AppData" / "Local")
        if config_base is None:
            config_base = str(Path.home() / "AppData" / "Roaming")
    else:
        data_base = os.environ.get("XDG_DATA_HOME")
        config_base = os.environ.get("XDG_CONFIG_HOME")
        if data_base is None:
            data_base = str(Path.home() / ".local" / "share")
        if config_base is None:
            config_base = str(Path.home() / ".config")
    return Path(data_base) / APP_NAME, Path(config_base) / APP_NAME

def resolve_roots(data_root=None, config_root=None):
    selected_data = data_root if data_root is not None else os.environ.get("PMT_DATA_ROOT")
    selected_config = config_root if config_root is not None else os.environ.get("PMT_CONFIG_ROOT")
    if selected_data is None:
        selected_data = default_roots()[0]
    if selected_config is None:
        selected_config = default_roots()[1]
    data = Path(selected_data).expanduser()
    config = Path(selected_config).expanduser()
    return data.resolve(), config.resolve()

def data_alias(path, data_root):
    """Return a diagnostic-safe label without exposing a user's absolute path."""
    try:
        return "<data>" + Path(path).resolve().relative_to(Path(data_root).resolve()).as_posix()
    except (ValueError, OSError):
        return "<external>"
