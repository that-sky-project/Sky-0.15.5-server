# route/__init__.py
# 这个文件让 route 成为一个 Python 包
from .account import account_bp
from .service import service_bp
from .chat import chat_bp
from .client import client_bp

__all__ = ['account_bp', 'service_bp', 'chat_bp', 'client_bp']
