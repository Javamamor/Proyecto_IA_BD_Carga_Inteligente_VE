"""
routers/auth.py
---------------
Endpoints de autenticación del Optimizador de Carga para VE.
Grupo 8IA · IES Abastos · 2025/26

Endpoints:
  POST /auth/register  →  Crea un usuario nuevo en la colección 'usuarios'
  POST /auth/login     →  Verifica credenciales y devuelve datos del usuario
"""

import hashlib
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator

from database import get_db

router = APIRouter(prefix="/auth", tags=["Autenticación"])


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _hash_password(password: str) -> str:
    """Devuelve el SHA-256 hexadecimal del password."""
    return hashlib.sha256(password.encode()).hexdigest()


def _serialize_user(doc: dict) -> dict:
    """Convierte _id ObjectId a string y elimina el hash del password."""
    return {
        "id":     str(doc["_id"]),
        "nombre": doc["nombre"],
        "email":  doc["email"],
        "vehiculo":    doc.get("vehiculo", {}),
        "preferencias": doc.get("preferencias", {}),
        "creado_en": doc.get("creado_en", "").isoformat()
            if isinstance(doc.get("creado_en"), datetime) else doc.get("creado_en", ""),
    }


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class Vehiculo(BaseModel):
    modelo:                str
    capacidad_bateria_kwh: float
    potencia_max_carga_kw: float


class Preferencias(BaseModel):
    bateria_minima_pct:        int = 20
    bateria_objetivo_pct:      int = 90
    hora_limite_salida:        str = "08:00"
    clasificaciones_permitidas: list[str] = ["bajo", "medio"]
    modo_emergencia:            bool = False

    @field_validator("clasificaciones_permitidas")
    @classmethod
    def validar_clasificaciones(cls, v):
        validas = {"bajo", "medio", "alto"}
        for c in v:
            if c not in validas:
                raise ValueError(f"Clasificación '{c}' no válida. Usa: bajo, medio, alto.")
        return v


class RegisterRequest(BaseModel):
    nombre:      str
    email:       EmailStr
    password:    str
    vehiculo:    Vehiculo
    preferencias: Preferencias = Preferencias()


class LoginRequest(BaseModel):
    email:    EmailStr
    password: str


# ══════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Registrar nuevo usuario",
)
def register(body: RegisterRequest):
    """
    Crea un nuevo usuario en la colección `usuarios`.

    - El email debe ser único (índice único en MongoDB).
    - El password se almacena como SHA-256 (nunca en texto plano).
    - Se aplican los valores por defecto de `preferencias` si no se envían.
    """
    db = get_db()

    # Comprobar email duplicado antes de intentar insertar
    if db.usuarios.find_one({"email": body.email}):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un usuario con el email '{body.email}'.",
        )

    documento = {
        "nombre":    body.nombre,
        "email":     body.email,
        "password":  _hash_password(body.password),
        "vehiculo":  body.vehiculo.model_dump(),
        "preferencias": body.preferencias.model_dump(),
        "creado_en": datetime.now(timezone.utc),
    }

    resultado = db.usuarios.insert_one(documento)
    documento["_id"] = resultado.inserted_id

    return {
        "mensaje": "Usuario creado correctamente.",
        "usuario": _serialize_user(documento),
    }


@router.post(
    "/login",
    summary="Iniciar sesión",
)
def login(body: LoginRequest):
    """
    Verifica credenciales del usuario.

    - Compara el SHA-256 del password recibido con el almacenado.
    - Devuelve los datos del usuario (sin password) si son correctos.
    - Responde 401 si el email no existe o el password es incorrecto
      (mensaje genérico para no revelar cuál de los dos falló).
    """
    db = get_db()

    usuario = db.usuarios.find_one({"email": body.email})

    if not usuario or usuario.get("password") != _hash_password(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas.",
        )

    return {
        "mensaje": "Login correcto.",
        "usuario": _serialize_user(usuario),
    }
