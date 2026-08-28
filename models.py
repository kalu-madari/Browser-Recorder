from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List

@dataclass
class WebSocketFrame:
    sequence: int
    websocket_id: str
    timestamp: str
    direction: str
    payload_type: str
    payload_size: int
    payload_file: Optional[str] = None
    payload: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class WebSocketState:
    id: str
    page_id: str
    url: str
    created_time: str
    handshake_request_headers: Dict[str, str] = field(default_factory=dict)
    handshake_response_headers: Dict[str, str] = field(default_factory=dict)
    handshake_status: Optional[int] = None
    handshake_status_text: Optional[str] = None
    frames: List[WebSocketFrame] = field(default_factory=list)
    closed_time: Optional[str] = None
    close_code: Optional[int] = None
    close_reason: Optional[str] = None
    status: str = "OPEN"

    def to_dict(self) -> dict:
        d = asdict(self)
        d['frames'] = [f.to_dict() for f in self.frames]
        return d

@dataclass
class ResourceRecord:
    file_path: str
    size: int
    sha256: str
    mime_type: str = "application/octet-stream"
    extension: str = ".bin"

@dataclass
class TransactionState:
    id: str  # Internal unique ID, mapped to id(Playwright Request) or CDP requestId
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

    # State machine fields
    finalized: bool = False          # Guards against duplicate manifest writes
    correlation_state: str = "UNKNOWN"  # PENDING_CDP | PENDING_PW | CDP_CORRELATED | PLAYWRIGHT_ONLY | FINALIZED

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

@dataclass
class GUIEvent:
    type: str # "STARTED", "UPDATED", "COMPLETED", "FAILED"
    transaction: TransactionState

@dataclass
class DOMSnapshotRecord:
    snapshot_id: str
    page_id: str
    frame_id: str
    url: str
    timestamp: str
    reason: str
    title: str
    html_path: str
    html_sha256: str
    html_size: int
    truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class NavigationRecord:
    navigation_id: str
    page_id: str
    frame_id: str
    timestamp: str
    from_url: Optional[str]
    to_url: str
    type: str
    reason: str
    status: Optional[int]
    success: bool
    document_request_sequence: Optional[int]
    dom_snapshot_id: Optional[str]
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class InteractionRecord:
    interaction_id: str
    page_id: str
    frame_id: str
    timestamp: str
    event_type: str
    target_tag: str
    target_selector: str
    target_text: Optional[str]
    target_value: Optional[str]
    value_recorded: bool
    coordinates: Optional[Dict[str, int]]
    key: Optional[str]
    dom_snapshot_id: Optional[str]
    navigation_id: Optional[str]
    is_trusted: bool

    def to_dict(self) -> dict:
        return asdict(self)
