import os
from dataclasses import dataclass, field

# Project root — the directory this file lives in
_HERE = os.path.dirname(os.path.abspath(__file__))

def _default_extension_path() -> str:
    env = os.environ.get("BROWSER_RECORDER_EXTENSION_PATH", "")
    if env:
        return env
    local = os.path.join(_HERE, "extensions", "cookie-editor")
    return local if os.path.isdir(local) else ""

def _default_user_data_dir() -> str:
    env = os.environ.get("BROWSER_RECORDER_USER_DATA_DIR", "")
    return env if env else os.path.join(_HERE, "user_data")

@dataclass
class AppConfig:
    output_dir: str = "captures"
    max_resource_size: int = 100 * 1024 * 1024  # 100 MB
    max_dom_snapshot_size: int = 50 * 1024 * 1024 # 50 MB
    save_response_bodies: bool = True
    save_request_bodies: bool = True
    save_cookies: bool = True
    save_headers: bool = True
    save_initiators: bool = True
    save_images: bool = True
    save_fonts: bool = True
    save_media: bool = False
    enable_cdp: bool = True
    enable_har: bool = True
    enable_dom_snapshots: bool = True
    enable_screenshots: bool = False
    enable_interactions: bool = True
    record_text_input_values: bool = False
    enable_stealth_mode: bool = True
    extension_path: str = field(default_factory=_default_extension_path)
    user_data_dir: str = field(default_factory=_default_user_data_dir)

config = AppConfig()
