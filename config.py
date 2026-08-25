import os
from dataclasses import dataclass

@dataclass
class AppConfig:
    output_dir: str = "captures"
    max_resource_size: int = 100 * 1024 * 1024  # 100 MB
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
    enable_dom_snapshots: bool = False
    enable_screenshots: bool = False

config = AppConfig()
