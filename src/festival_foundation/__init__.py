"""节日公共服务协作基础层的服务端基础包。"""

from .festival import FestivalService
from .service import DomainService

__all__ = ["DomainService", "FestivalService"]
