"""
routers/planes.py
-----------------
Endpoints CRUD sobre la colección `planes_de_carga`.
Grupo 8IA · IES Abastos · 2025/26

Endpoints:
  POST   /planes                    →  Guarda un plan nuevo
  GET    /planes/{user_id}          →  Lista todos los planes del usuario (por fecha desc)
  PUT    /planes/{plan_id}/estado   →  Activa o desactiva el plan (body: {"activo": bool})
  DELETE /planes/{plan_id}          →  Elimina el plan
"""

from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from database import get_db

router = APIRouter(prefix="/planes", tags=["Planes de carga"])


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _parse_object_id(id_str: str, campo: str = "id") -> ObjectId:
    """Convierte string a ObjectId o lanza 400 si no es válido."""
    try:
        return ObjectId(id_str)
    except (InvalidId, Exception):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El {campo} '{id_str}' no es un ObjectId válido.",
        )


def _serialize_plan(doc: dict) -> dict:
    """Convierte ObjectIds a string y fechas a ISO para la respuesta JSON."""
    result = {k: v for k, v in doc.items()}
    result["id"]         = str(doc["_id"])
    result["usuario_id"] = str(doc["usuario_id"])
    del result["_id"]

    if isinstance(result.get("fecha_generacion"), datetime):
        result["fecha_generacion"] = result["fecha_generacion"].isoformat()
    if isinstance(result.get("activado_en"), datetime):
        result["activado_en"] = result["activado_en"].isoformat()

    return result


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class Franja(BaseModel):
    hora:          int           # 0-23
    on_off:        bool
    potencia_kw:   float
    precio_kwh:    float
    clasificacion: str           # "bajo" | "medio" | "alto"


class PlanRequest(BaseModel):
    usuario_id:              str
    nombre_plan:             str 
    franjas:                 list[Franja]
    coste_estimado_eur:      float
    emisiones_estimadas_gco2: float
    modelo_version:          str = "v1.0.0-mlflow"


# ══════════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════════

@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Guardar plan de carga",
)
def crear_plan(body: PlanRequest):
    """
    Guarda un plan de carga nuevo en la colección `planes_de_carga`.

    El plan se marca como **inactivo** al crearse; se activa mediante
    `PUT /planes/{plan_id}/activar` una vez confirmados los precios reales.
    """
    db         = get_db()
    usuario_id = _parse_object_id(body.usuario_id, "usuario_id")

    # Verificar que el usuario existe
    if not db.usuarios.find_one({"_id": usuario_id}):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Usuario '{body.usuario_id}' no encontrado.",
        )

    documento = {
        "usuario_id":              usuario_id,
        "fecha_generacion":        datetime.now(timezone.utc),
        "franjas":                 [f.model_dump() for f in body.franjas],
        "coste_estimado_eur":      body.coste_estimado_eur,
        "emisiones_estimadas_gco2": body.emisiones_estimadas_gco2,
        "modelo_version":          body.modelo_version,
        "activo":                  True,
        "nombre_plan":             body.nombre_plan,
    }

    resultado = db.planes_de_carga.insert_one(documento)
    documento["_id"] = resultado.inserted_id

    return {
        "mensaje": "Plan guardado correctamente.",
        "plan":    _serialize_plan(documento),
    }


@router.get(
    "/{user_id}",
    summary="Listar planes de un usuario",
)
def listar_planes(user_id: str):
    """
    Devuelve todos los planes del usuario ordenados por `fecha_generacion`
    descendente (el más reciente primero).
    """
    db         = get_db()
    usuario_id = _parse_object_id(user_id, "user_id")

    cursor = db.planes_de_carga.find(
        {"usuario_id": usuario_id}
    ).sort("fecha_generacion", -1)

    planes = [_serialize_plan(doc) for doc in cursor]

    return {
        "usuario_id": user_id,
        "total":      len(planes),
        "planes":     planes,
    }


class EstadoPlanRequest(BaseModel):
    activo: bool


@router.put(
    "/{plan_id}/estado",
    summary="Activar o desactivar un plan de carga",
)
def cambiar_estado_plan(plan_id: str, body: EstadoPlanRequest):
    """
    Cambia el estado activo/inactivo de un plan.

    - `{"activo": true}` → recalcula el coste con precios reales y activa el plan.
    - `{"activo": false}` → desactiva el plan sin modificar sus datos.
    """
    db  = get_db()
    oid = _parse_object_id(plan_id, "plan_id")
    plan = db.planes_de_carga.find_one({"_id": oid})

    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan '{plan_id}' no encontrado.",
        )

    # ── DESACTIVAR ─────────────────────────────────────────────
    if not body.activo:
        if not plan.get("activo"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El plan ya está inactivo.",
            )
        db.planes_de_carga.update_one(
            {"_id": oid},
            {"$set": {"activo": False}},
        )
        plan_actualizado = db.planes_de_carga.find_one({"_id": oid})
        return {
            "mensaje": "Plan desactivado correctamente.",
            "plan":    _serialize_plan(plan_actualizado),
        }

    # ── ACTIVAR con recálculo de precios reales ────────────────
    if plan.get("activo"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El plan ya está activo.",
        )

    # Desactivar cualquier otro plan activo del mismo usuario (solo puede haber uno)
    db.planes_de_carga.update_many(
        {"usuario_id": plan["usuario_id"], "activo": True, "_id": {"$ne": oid}},
        {"$set": {"activo": False}},
    )

    fecha_plan = plan["fecha_generacion"].date() if isinstance(
        plan["fecha_generacion"], datetime
    ) else datetime.fromisoformat(str(plan["fecha_generacion"])).date()

    fecha_inicio_dia = datetime(
        fecha_plan.year, fecha_plan.month, fecha_plan.day, tzinfo=timezone.utc
    )

    coste_real = 0.0
    franjas_actualizadas = []

    for franja in plan["franjas"]:
        if franja["on_off"]:
            precio_doc = db.precios_electricidad.find_one({
                "fecha": fecha_inicio_dia,
                "hora":  franja["hora"],
            })
            precio_real = precio_doc["precio_kwh"] if precio_doc else franja["precio_kwh"]
        else:
            precio_real = franja["precio_kwh"]

        coste_real += franja["potencia_kw"] * precio_real if franja["on_off"] else 0.0
        franjas_actualizadas.append({**franja, "precio_kwh": precio_real})

    db.planes_de_carga.update_one(
        {"_id": oid},
        {"$set": {
            "activo":         True,
            "activado_en":    datetime.now(timezone.utc),
            "coste_real_eur": round(coste_real, 4),
            "franjas":        franjas_actualizadas,
        }},
    )

    plan_actualizado = db.planes_de_carga.find_one({"_id": oid})

    return {
        "mensaje":        "Plan activado con precios reales.",
        "coste_estimado": plan["coste_estimado_eur"],
        "coste_real":     round(coste_real, 4),
        "plan":           _serialize_plan(plan_actualizado),
    }


@router.delete(
    "/{plan_id}",
    status_code=status.HTTP_200_OK,
    summary="Eliminar plan de carga",
)
def eliminar_plan(plan_id: str):
    """
    Elimina un plan de carga por su ID.

    No se permite eliminar planes activos para preservar el historial.
    """
    db  = get_db()
    oid = _parse_object_id(plan_id, "plan_id")

    plan = db.planes_de_carga.find_one({"_id": oid})

    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Plan '{plan_id}' no encontrado.",
        )

    if plan.get("activo"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede eliminar un plan activo. Desactívalo primero.",
        )

    db.planes_de_carga.delete_one({"_id": oid})

    return {"mensaje": f"Plan '{plan_id}' eliminado correctamente."}
