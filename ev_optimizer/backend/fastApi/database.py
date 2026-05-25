"""
database.py
-----------
Conexión singleton a MongoDB Atlas.
Grupo 8IA · IES Abastos · 2025/26

Uso en cualquier router:
    from database import get_db
    db = get_db()
    db.usuarios.find_one(...)
"""

import os
from functools import lru_cache

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME   = os.getenv("DB_NAME", "ev_optimizer")


@lru_cache(maxsize=1)
def _get_client() -> MongoClient:
    """Crea (y cachea) el cliente MongoDB. Se reutiliza en toda la vida del proceso."""
    return MongoClient(MONGO_URI)


def get_db() -> Database:
    """Devuelve la instancia de la base de datos ev_optimizer."""
    return _get_client()[DB_NAME]
