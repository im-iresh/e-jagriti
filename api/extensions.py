"""
Flask extension singletons.

Defined here — not in app.py — so that any module importing them (routes, etc.)
always gets the same object regardless of whether app.py was loaded as '__main__'
or as 'app'. Importing from a dedicated module avoids the classic factory-pattern
trap where `python app.py` and `from app import cache` resolve to two different
module instances, causing a KeyError inside Flask-Caching.
"""

from flask_caching import Cache
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_smorest import Api

cache    = Cache()
cors     = CORS()
limiter  = Limiter(key_func=get_remote_address)
api_docs = Api()
