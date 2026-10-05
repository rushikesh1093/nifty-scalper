from pydantic import BaseModel
from typing import Optional


class LoginRequest(BaseModel):
    api_key:     str
    client_id:   str
    password:    str
    totp_secret: str


class LoginResponse(BaseModel):
    status:        bool
    message:       str
    client_code:   Optional[str] = None
    feed_token:    Optional[str] = None
    logged_in_at:  Optional[str] = None


class SessionStatusResponse(BaseModel):
    is_logged_in:  bool
    client_code:   Optional[str] = None
    logged_in_at:  Optional[str] = None
    feed_connected: bool = False
    subscriptions:  int  = 0
