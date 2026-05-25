"""
plan_carga.py
-------------
Router que genera el plan de carga óptimo para un VE.
Grupo 8IA · IES Abastos · 2025/26

Endpoint:
  POST /plan_carga  →  Lee precios clasificados de MongoDB (precios_electricidad),
                       ejecuta el optimizador y devuelve el plan listo para
                       mostrarse en el frontend y guardarse vía POST /planes.

Flujo completo:
    Frontend
      └─► POST /plan_carga  (usuario_id, config VE)
            ├─► Calcula automáticamente qué días consultar (hoy y mañana)
            ├─► Lee precios clasificados de precios_electricidad
            └─► generar_plan_carga() → devuelve plan estructurado

    Frontend muestra el plan y el usuario confirma
      └─► POST /planes  (guarda el plan en planes_de_carga)

Lógica de ventana de precios:
    Se construye un pool de horas con los datos disponibles en MongoDB:
      · Siempre se incluyen las horas de HOY que aún no han pasado.
      · Si existen precios de MAÑANA (disponibles desde ~20:15 h), se añaden
        también, permitiendo al optimizador planificar en el tramo nocturno
        barato del día siguiente sin necesidad de lógica especial en el cliente.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from database import get_db
from prediccion_carga import generar_plan_carga

router = APIRouter(prefix="/plan_carga", tags=["Plan de carga"])

# Mapeo clasificacion del modelo → tipo esperado por generar_plan_carga
_MAPA_TIPO = {"BAJO": "B", "MEDIO": "M", "ALTO": "A"}

# Factor de emisiones medio de la red española (gCO₂/kWh) — fuente: REE 2024
_EMISIONES_GCO2_KWH = 180.0


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class SolicitudPlanCarga(BaseModel):
    """Parámetros necesarios para generar el plan de carga óptimo."""

    usuario_id:          str   = Field(..., description="ObjectId del usuario")
    # Configuración del vehículo y cargador
    capacidad_total_kwh: float = Field(..., gt=0,  description="Capacidad total de la batería (kWh)")
    soc_actual:          float = Field(..., ge=0, le=100, description="% de batería al enchufar")
    soc_objetivo:        float = Field(..., ge=0, le=100, description="% de batería deseado")
    potencia_kw:         float = Field(..., gt=0,  description="Potencia del cargador (kW)")
    clasificaciones_permitidas: list[Literal["BAJO", "MEDIO", "ALTO"]] = Field(default=["BAJO", "MEDIO"],description="Franjas de precio que el plan puede usar")
    model_config = {
        "json_schema_extra": {
            "example": {
                "usuario_id":          "6650a1b2c3d4e5f6a7b8c9d0",
                "capacidad_total_kwh": 64.0,
                "soc_actual":          20.0,
                "soc_objetivo":        80.0,
                "potencia_kw":         7.4,
                "clasificaciones_permitidas": ["BAJO", "MEDIO", "ALTO"]
            }
        }
    }


# ══════════════════════════════════════════════════════════════════
# HELPERS INTERNOS
# ══════════════════════════════════════════════════════════════════

def _obtener_horas_ventana(db) -> tuple[list[dict], str, Optional[str]]:
    """
    Construye el pool de horas disponibles y clasificadas para el optimizador.

    Busca en:
      · precios_actuales  → precios de hoy (pvpc_ingesta los guarda aquí)
      · precios_futuros   → precios de mañana (disponibles desde ~20:15 h)

    Estrategia:
      1. Lee el documento de HOY en precios_actuales.
      2. Filtra las horas que aún no han pasado (hora >= hora_actual).
      3. Si existen precios de MAÑANA en precios_futuros, los añade al pool.
      4. Si no quedan horas de hoy, usa solo las de mañana.
    """
    tz_madrid    = ZoneInfo("Europe/Madrid")
    ahora        = datetime.now(tz=tz_madrid)
    hora_actual  = ahora.hour
    fecha_hoy    = ahora.strftime("%Y-%m-%d")
    fecha_manana = (ahora + timedelta(days=1)).strftime("%Y-%m-%d")

    # ── Precios de hoy (precios_actuales) ─────────────────────────────
    doc_hoy = db["precios_actuales"].find_one(
        {"fecha": fecha_hoy},
        {"_id": 0, "horas": 1, "clasificado": 1}
    )

    if not doc_hoy:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No hay precios disponibles para hoy ({fecha_hoy}). "
                "Espera a que pvpc_ingesta.py descargue los datos de REE."
            ),
        )

    horas_hoy = doc_hoy.get("horas", [])
    _verificar_clasificacion(horas_hoy, fecha_hoy)

    # Solo las horas que aún no han pasado
    horas_pendientes_hoy = [h for h in horas_hoy if h["hora"] >= hora_actual]

    # ── Precios de mañana (precios_futuros, opcionales) ───────────────
    doc_manana = db["precios_futuros"].find_one(
        {"fecha": fecha_manana},
        {"_id": 0, "horas": 1, "clasificado": 1}
    )
    horas_manana: list[dict] = []

    if doc_manana:
        horas_m = doc_manana.get("horas", [])
        try:
            _verificar_clasificacion(horas_m, fecha_manana)
            horas_manana = horas_m
        except HTTPException:
            import logging
            logging.getLogger(__name__).info(
                f"Precios de {fecha_manana} aún no clasificados; se ignorarán."
            )

    # ── Pool final ────────────────────────────────────────────────────
    horas_pool = horas_pendientes_hoy + horas_manana

    if not horas_pool:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No quedan horas disponibles para hoy ({fecha_hoy}) "
                f"y no hay datos del día siguiente ({fecha_manana})."
            ),
        )

    return horas_pool, fecha_hoy, fecha_manana if horas_manana else None


def _verificar_clasificacion(horas: list[dict], fecha: str) -> None:
    """
    Lanza HTTPException 409 si alguna hora no tiene campo 'clasificacion'.
    Lanza HTTPException 422 si la lista de horas está vacía.
    """
    if not horas:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"El documento de precios de {fecha} existe pero no contiene horas.",
        )
    sin_clasificar = [h["hora"] for h in horas if "clasificacion" not in h]
    if sin_clasificar:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Los precios del {fecha} aún no han sido clasificados por el modelo. "
                f"Horas sin clasificar: {sin_clasificar}. "
                "Espera a que pvpc_ingesta.py notifique al modelo."
            ),
        )


def _calcular_emisiones(franjas_activas: list[dict], potencia_kw: float) -> float:
    """
    Estima las emisiones de CO₂ (en gramos) de la sesión de carga.
    Usa el factor medio de la red; en una versión avanzada se usaría
    el factor real hora a hora desde REE.
    """
    horas_carga = len(franjas_activas)
    energia_kwh = potencia_kw * horas_carga
    return round(energia_kwh * _EMISIONES_GCO2_KWH, 2)


def _construir_franjas_para_frontend(
    plan_optimizador: dict,
    horas_pool: list[dict],
    potencia_kw: float,
) -> list[dict]:
    """
    Combina el plan del optimizador con la información completa de cada hora.
    Devuelve la lista de horas del pool marcando con on_off=True las horas
    en las que se recomienda cargar.

    A diferencia de la versión anterior, no genera siempre 24 franjas fijas:
    devuelve solo las horas del pool (horas pendientes de hoy + mañana si existe),
    lo que permite al frontend mostrar la ventana real de planificación.
    """
    info_hora = {(h.get("fecha_dia", ""), h["hora"]): h for h in horas_pool}
    # Para compatibilidad, también indexamos solo por hora si no hay fecha_dia
    info_hora_simple = {h["hora"]: h for h in horas_pool}

    horas_carga: set[int] = set()
    for franja in plan_optimizador.get("plan_eco", []):
        horas_carga.add(franja["hora"])
    for franja in plan_optimizador.get("plan_emergencia", {}).get("horas_altas", []):
        horas_carga.add(franja["hora"])

    franjas = []
    for h in horas_pool:
        hora_num      = h["hora"]
        clasificacion = h.get("clasificacion", "MEDIO")
        precio_kwh    = h.get("precio_kwh", 0.0)
        on_off        = hora_num in horas_carga

        franjas.append({
            "hora":          hora_num,
            "on_off":        on_off,
            "potencia_kw":   potencia_kw if on_off else 0.0,
            "precio_kwh":    precio_kwh,
            "precio_mwh":    h.get("precio_mwh", round(precio_kwh * 1000, 4)),
            "clasificacion": clasificacion,
            "datetime":      h.get("datetime", ""),
            "coste_franja":  round(potencia_kw * precio_kwh, 4) if on_off else 0.0,
        })

    return franjas


# ══════════════════════════════════════════════════════════════════
# ENDPOINT
# ══════════════════════════════════════════════════════════════════

@router.post(
    "",
    summary="Generar plan de carga óptimo",
    response_description="Plan de carga listo para mostrar en el frontend y guardar en /planes",
)
def generar_plan(solicitud: SolicitudPlanCarga):
    """
    Genera el plan de carga óptimo para un usuario.

    **Pasos internos:**
    1. Calcula automáticamente la ventana de precios (horas restantes de hoy
       + horas de mañana si están disponibles en MongoDB).
    2. Convierte las horas al formato que espera `generar_plan_carga()`.
    3. Ejecuta el optimizador respetando la jerarquía BAJO → MEDIO → ALTO.
    4. Calcula coste estimado y emisiones de CO₂.
    5. Devuelve el plan completo con todas las franjas de la ventana.

    **Errores posibles:**
    - `404` — No hay precios para hoy en MongoDB.
    - `409` — Los precios existen pero el modelo aún no los ha clasificado.
    - `422` — Parámetros inválidos (soc_actual >= soc_objetivo, etc.).
    """
    if solicitud.soc_actual >= solicitud.soc_objetivo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="soc_actual debe ser menor que soc_objetivo.",
        )

    db = get_db()

    # ── 1. Leer ventana de precios clasificados ────────────────────────
    horas_pool, fecha_hoy, fecha_manana = _obtener_horas_ventana(db)

    # ── 2. Convertir al formato de generar_plan_carga ──────────────────
    _cls_permitidas = {c.upper() for c in solicitud.clasificaciones_permitidas}
    datos_red = [
        {
            "hora":   h["hora"],
            "tipo":   _MAPA_TIPO.get(h["clasificacion"], "M"),
            "precio": h["precio_kwh"],
        }
        for h in horas_pool
        if h.get("clasificacion", "MEDIO").upper() in _cls_permitidas
    ]

    config_usuario = {
        "soc_actual":   solicitud.soc_actual,
        "soc_objetivo": solicitud.soc_objetivo,
        "potencia_kw":  solicitud.potencia_kw,
    }

    # ── 3. Ejecutar optimizador ────────────────────────────────────────
    try:
        resultado_optimizador = generar_plan_carga(
            datos_red,
            config_usuario,
            solicitud.capacidad_total_kwh,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error en el optimizador de carga: {e}",
        )

    # ── 4. Construir franjas para el frontend ──────────────────────────
    franjas = _construir_franjas_para_frontend(
        resultado_optimizador,
        horas_pool,
        solicitud.potencia_kw,
    )

    franjas_activas     = [f for f in franjas if f["on_off"]]
    coste_estimado      = round(sum(f["coste_franja"] for f in franjas_activas), 4)
    emisiones_estimadas = _calcular_emisiones(franjas_activas, solicitud.potencia_kw)

    resumen_optimizador = resultado_optimizador.get("resumen", {})

    # ── 5. Respuesta final ─────────────────────────────────────────────
    return {
        "fecha_hoy":    fecha_hoy,
        "fecha_manana": fecha_manana,   # None si los precios de mañana aún no están disponibles

        "resumen": {
            "soc_actual":               solicitud.soc_actual,
            "soc_objetivo":             solicitud.soc_objetivo,
            "soc_final_estimado":       resumen_optimizador.get("soc_final_estimado", solicitud.soc_objetivo),
            "horas_carga":              len(franjas_activas),
            "energia_cargada_kwh":      round(solicitud.potencia_kw * len(franjas_activas) * 0.9, 2),
            "coste_estimado_eur":       coste_estimado,
            "emisiones_estimadas_gco2": emisiones_estimadas,
            "viabilidad_economica":     resumen_optimizador.get("viabilidad_economica", True),
            "necesita_horas_altas":     resultado_optimizador.get(
                                            "plan_emergencia", {}
                                        ).get("requiere_autorizacion", False),
        },

        # Franjas de la ventana de planificación (horas restantes de hoy + mañana si hay)
        "franjas": franjas,

        "aviso_horas_altas": resultado_optimizador.get(
            "plan_emergencia", {}
        ).get("mensaje", ""),

        # Para guardar directamente en POST /planes
        "para_guardar": {
            "usuario_id":               solicitud.usuario_id,
            "franjas":                  [
                {
                    "hora":          f["hora"],
                    "on_off":        f["on_off"],
                    "potencia_kw":   f["potencia_kw"],
                    "precio_kwh":    f["precio_kwh"],
                    "clasificacion": f["clasificacion"].lower(),
                }
                for f in franjas
            ],
            "coste_estimado_eur":       coste_estimado,
            "emisiones_estimadas_gco2": emisiones_estimadas,
            "modelo_version":           "v1.0.0-mlflow",
        },
    }
