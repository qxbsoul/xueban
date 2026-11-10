# xueban/api/_limiter.py — shared limiter instance (Story x-5)
# Extracted from server.py so APIRouter endpoints can import without circular deps.
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
