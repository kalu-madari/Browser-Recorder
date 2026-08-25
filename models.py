from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List

@dataclass
class ResourceRecord:
    file_path: str
    size: int
    sha256: str
    mime_type: str = "application/octet-stream"
    extension: str = ".bin"

@dataclass
class TransactionState:
    id: int  # Internal unique ID, mapped to id(Playwright Request)
    sequence: int
    url: str
    method: str
    request_headers: Dict[str, str]
    request_time: str
    resource_type: str
    page_id: str
    frame_url: str
    
    post_data: Optional[str] = None
    post_data_file: Optional[str] = None
    request_cookies: List[Dict[str, Any]] = field(default_factory=list)
    context_cookies: List[Dict[str, Any]] = field(default_factory=list)
    initiator: Optional[Dict[str, Any]] = None
    
    # Response fields
    response_time: Optional[str] = None
    status: Optional[int] = None
    status_text: Optional[str] = None
    response_headers: Dict[str, str] = field(default_factory=dict)
    content_type: str = ""
    resource: Optional[ResourceRecord] = None
    
    error: Optional[str] = None
    completed: bool = False
    completion_time: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

@dataclass
class GUIEvent:
    type: str # "STARTED", "UPDATED", "COMPLETED", "FAILED"
    transaction: TransactionState
