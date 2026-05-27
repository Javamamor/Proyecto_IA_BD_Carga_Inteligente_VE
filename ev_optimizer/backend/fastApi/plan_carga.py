"""
plan_carga.py
-------------
Router que genera el plan de carga óptimo para un VE.
Grupo 8IA · IES Abastos · 2025/26

Endpoint:
  POST /plan_carga  →  Construye una ventana de planificación combinando
                       precios reales de hoy (MongoDB) con predicciones
                       para mañana cuando no hay datos de REE disponibles.
                       Ejecuta el optimizador y devuelve el plan completo.

Flujo completo:
    Frontend
      └─► POST /plan_carga  (usuario_id, fecha, hora_inicio, hora_limite, config VE)
            ├─► Horas de hoy desde hora_inicio  →  precios reales de MongoDB
            ├─► Horas de mañana si se necesitan →  predicción con MEDIA_POR_HORA
            └─► generar_plan_carga() → devuelve plan estructurado

    Frontend muestra el plan y el usuario confirma
      └─► POST /planes  (guarda el plan en planes_de_carga)
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from database import get_db
from prediccion_carga import generar_plan_carga

router = APIRouter(prefix="/plan_carga", tags=["Plan de carga"])

# Mapeo clasificacion del modelo → tipo esperado por generar_plan_carga
_MAPA_TIPO = {"BAJO": "B", "MEDIO": "M", "ALTO": "A"}

# Factor de emisiones medio de la red española (gCO₂/kWh) — fuente: REE 2024
_EMISIONES_GCO2_KWH = 180.0

# Medias históricas por hora (€/kWh) para predicción sin datos reales
# Mismos valores que en main.py — fuente: PVPC 2020-2025
_MEDIA_POR_HORA: dict = {
    0: 0.0697,  1: 0.0691,  2: 0.0696,  3: 0.0697,
    4: 0.0702,  5: 0.0696,  6: 0.0700,  7: 0.1981,
    8: 0.1981,  9: 0.1981, 10: 0.1992, 11: 0.1993,
   12: 0.0998, 13: 0.1005, 14: 0.1005, 15: 0.1000,
   16: 0.1005, 17: 0.2501, 18: 0.2491, 19: 0.2508,
   20: 0.2489, 21: 0.2504, 22: 0.2508, 23: 0.0702,
}


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class SolicitudPlanCarga(BaseModel):
    """Parámetros necesarios para generar el plan de carga óptimo."""

    usuario_id:          str   = Field(..., description="ObjectId del usuario")
    fecha:               str   = Field(..., description="Día de inicio (YYYY-MM-DD) — normalmente hoy")
    hora_inicio:         int   = Field(
                                     default=None,
                                     ge=0, le=23,
                                     description="Hora a partir de la cual se puede cargar (0-23). "
                                                 "Si se omite, se usa la hora actual."
                                 )
    hora_limite:         Optional[str] = Field(
                                     default=None,
                                     description="Hora límite de carga en formato HH:MM (ej: '08:00'). "
                                                 "Si cae en el día siguiente, se usan predicciones."
                                 )
    # Configuración del vehículo y cargador
    capacidad_total_kwh: float = Field(..., gt=0,  description="Capacidad total de la batería (kWh)")
    soc_actual:          float = Field(..., ge=0, le=100, description="% de batería al enchufar")
    soc_objetivo:        float = Field(..., ge=0, le=100, description="% de batería deseado")
    potencia_kw:         float = Field(..., gt=0,  description="Potencia del cargador (kW)")
    clasificaciones_permitidas: Optional[list] = Field(
                                     default=["BAJO", "MEDIO", "ALTO"],
                                     description="Tipos de franja permitidos: BAJO, MEDIO, ALTO"
                                 )

    model_config = {
        "json_schema_extra": {
            "example": {
                "usuario_id":          "6650a1b2c3d4e5f6a7b8c9d0",
                "fecha":               "2026-05-27",
                "hora_inicio":         20,
                "hora_limite":         "08:00",
                "capacidad_total_kwh": 64.0,
                "soc_actual":          10.0,
                "soc_objetivo":        80.0,
                "potencia_kw":         7.4,
                "clasificaciones_permitidas": ["BAJO", "MEDIO"]
            }
        }
    }


# ══════════════════════════════════════════════════════════════════
# HELPERS INTERNOS
# ══════════════════════════════════════════════════════════════════

def _clasificar_precio_umbral(precio_kwh: float) -> str:
    """Clasificación simple por umbrales cuando el modelo no está disponible."""
    if precio_kwh < 0.083:
        return "BAJO"
    elif precio_kwh < 0.172:
        return "MEDIO"
    return "ALTO"


def _horas_reales_desde_mongo(db, fecha: str, hora_inicio: int) -> list:
    """
    Lee los precios clasificados de MongoDB para la fecha dada,
    filtrando solo las horas >= hora_inicio.
    Devuelve lista de dicts con hora, precio_kwh, precio_mwh,
    clasificacion y datetime. Lista vacía si no hay datos.
    """
    doc = db.precios_electricidad.find_one(
        {"fecha": fecha, "tipo": "actuales"},
        {"_id": 0, "horas": 1}
    )
    if not doc or not doc.get("horas"):
        return []

    horas = [
        h for h in doc["horas"]
        if h.get("hora", -1) >= hora_inicio and "clasificacion" in h
    ]
    return sorted(horas, key=lambda h: h["hora"])


def _horas_predichas(fecha: str, hora_desde: int, hora_hasta: int) -> list:
    """
    Genera horas con precios PREDICHOS usando las medias históricas.
    hora_desde y hora_hasta son ambos inclusivos (rango 0-23).
    Añade el campo 'es_prediccion': True para distinguirlas en el frontend.
    """
    try:
        mes = int(fecha.split("-")[1])
    except Exception:
        mes = datetime.now(timezone.utc).month

    resultado = []
    for h in range(hora_desde, hora_hasta + 1):
        precio_kwh = _MEDIA_POR_HORA.get(h, 0.15)
        clasificacion = _clasificar_precio_umbral(precio_kwh)
        resultado.append({
            "hora":          h,
            "precio_kwh":    precio_kwh,
            "precio_mwh":    round(precio_kwh * 1000, 4),
            "clasificacion": clasificacion,
            "datetime":      f"{fecha}T{str(h).zfill(2)}:00:00",
            "es_prediccion": True,
        })
    return resultado


def _construir_ventana_planificacion(
    db,
    fecha_hoy: str,
    hora_inicio: int,
    hora_limite_str: Optional[str],
) -> tuple:
    """
    Construye la ventana completa de horas disponibles para el plan,
    combinando precios reales de hoy con predicciones de mañana si es necesario.

    Devuelve:
        (ventana: list[dict], usa_prediccion: bool, fecha_manana: str)

    Ejemplo con hora_inicio=20, hora_limite='08:00':
        - Horas reales hoy:    20, 21, 22, 23  (MongoDB)
        - Horas predichas mañana: 00, 01, ..., 08  (MEDIA_POR_HORA)
    """
    fecha_dt = datetime.strptime(fecha_hoy, "%Y-%m-%d")
    fecha_manana = (fecha_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    # Parsear hora límite
    hora_limite_num = None
    if hora_limite_str:
        try:
            hora_limite_num = int(hora_limite_str.split(":")[0])
        except Exception:
            hora_limite_num = None

    # ── Horas de HOY ─────────────────────────────────────────────
    # Desde hora_inicio hasta el final del día (23h) o hasta hora_limite si es hoy
    hora_fin_hoy = 23
    if hora_limite_num is not None and hora_limite_num > hora_inicio:
        # La hora límite cae en el mismo día → no necesitamos mañana
        hora_fin_hoy = hora_limite_num - 1

    horas_hoy = _horas_reales_desde_mongo(db, fecha_hoy, hora_inicio)
    # Si no hay datos reales, usar predicción también para hoy
    if not horas_hoy:
        horas_hoy = _horas_predichas(fecha_hoy, hora_inicio, hora_fin_hoy)
    else:
        # Filtrar hasta hora_fin_hoy
        horas_hoy = [h for h in horas_hoy if h["hora"] <= hora_fin_hoy]

    usa_prediccion = any(h.get("es_prediccion") for h in horas_hoy)

    # ── Horas de MAÑANA (si la hora límite cae al día siguiente) ─
    horas_manana = []
    necesita_manana = (
        hora_limite_num is None  # sin límite → siempre puede necesitar mañana
        or hora_limite_num <= hora_inicio  # hora límite <= hora inicio → cruza medianoche
    )

    if necesita_manana:
        hora_fin_manana = hora_limite_num - 1 if hora_limite_num else 23

        # Intentar primero datos reales del día siguiente en MongoDB
        horas_manana_reales = _horas_reales_desde_mongo(db, fecha_manana, 0)
        if horas_manana_reales:
            horas_manana = [h for h in horas_manana_reales if h["hora"] <= hora_fin_manana]
        else:
            # No hay datos reales → usar predicción
            horas_manana = _horas_predichas(fecha_manana, 0, hora_fin_manana)
            usa_prediccion = True

    # Combinar: primero hoy, luego mañana
    # Añadir offset de 24 a las horas de mañana para que el optimizador
    # las ordene correctamente (hora 0 de mañana = hora 24 en la ventana)
    ventana = []
    for h in horas_hoy:
        ventana.append({**h, "hora_ventana": h["hora"]})
    for h in horas_manana:
        ventana.append({**h, "hora_ventana": h["hora"] + 24})

    return ventana, usa_prediccion, fecha_manana


def _calcular_horas_necesarias(soc_actual, soc_objetivo, capacidad_kwh, potencia_kw, eficiencia=0.9) -> float:
    """Calcula cuántas horas se necesitan para llegar al SOC objetivo."""
    energia_necesaria = ((soc_objetivo - soc_actual) / 100) * capacidad_kwh
    return energia_necesaria / (potencia_kw * eficiencia)


def _calcular_emisiones(franjas_activas: list, potencia_kw: float) -> float:
    horas_carga = len(franjas_activas)
    energia_kwh = potencia_kw * horas_carga
    return round(energia_kwh * _EMISIONES_GCO2_KWH, 2)


def _construir_franjas_para_frontend(
    plan_optimizador: dict,
    ventana: list,
    potencia_kw: float,
) -> list:
    """
    Construye las franjas de respuesta para el frontend.
    Cada franja tiene hora real (0-23 o 24-47 para mañana),
    on_off, precio, clasificacion y si es predicción.
    """
    info_ventana = {h["hora_ventana"]: h for h in ventana}

    horas_carga = set()
    for franja in plan_optimizador.get("plan_eco", []):
        horas_carga.add(franja["hora"])
    for franja in plan_optimizador.get("plan_emergencia", {}).get("horas_altas", []):
        horas_carga.add(franja["hora"])

    franjas = []
    for item in ventana:
        hv = item["hora_ventana"]
        on_off = hv in horas_carga
        precio_kwh = item.get("precio_kwh", 0.0)
        clasificacion = item.get("clasificacion", "MEDIO")

        franjas.append({
            "hora":          item["hora"],       # hora real del día (0-23)
            "hora_ventana":  hv,                  # posición en la ventana (puede ser 24-47)
            "fecha":         item.get("datetime", "")[:10],
            "on_off":        on_off,
            "potencia_kw":   potencia_kw if on_off else 0.0,
            "precio_kwh":    precio_kwh,
            "precio_mwh":    item.get("precio_mwh", round(precio_kwh * 1000, 4)),
            "clasificacion": clasificacion,
            "datetime":      item.get("datetime", ""),
            "coste_franja":  round(potencia_kw * precio_kwh, 4) if on_off else 0.0,
            "es_prediccion": item.get("es_prediccion", False),
        })

    return franjas


# ══════════════════════════════════════════════════════════════════
# ENDPOINT
# ══════════════════════════════════════════════════════════════════

@router.post(
    "",
    summary="Generar plan de carga óptimo con ventana multi-día",
    response_description="Plan de carga con franjas de hoy y mañana (predicción si no hay datos reales)",
)
def generar_plan(solicitud: SolicitudPlanCarga):
    """
    Genera el plan de carga óptimo combinando precios reales y predicciones.

    **Caso típico:** usuario enchufa el coche a las 20:00 y quiere tenerlo
    al 80% antes de las 08:00 del día siguiente.

    **Pasos internos:**
    1. Determina hora de inicio (la del request o la hora actual).
    2. Lee precios reales de HOY desde MongoDB (horas >= hora_inicio).
    3. Si la hora_limite cae en el día siguiente, añade horas predichas
       con medias históricas (MEDIA_POR_HORA) — o datos reales si ya
       están disponibles en MongoDB.
    4. Ejecuta el optimizador con la ventana combinada.
    5. Devuelve el plan marcando qué franjas son predicción.

    **Errores posibles:**
    - `422` — soc_actual >= soc_objetivo, o parámetros inválidos.
    - `404` — No hay precios ni reales ni estimables para la ventana.
    """
    if solicitud.soc_actual >= solicitud.soc_objetivo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="soc_actual debe ser menor que soc_objetivo.",
        )

    # Hora de inicio: la del request o la hora actual en Madrid
    hora_inicio = solicitud.hora_inicio
    if hora_inicio is None:
        hora_inicio = datetime.now(timezone.utc).hour  # simplificación; en prod usar pytz/ZoneInfo

    db = get_db()

    # ── 1. Construir ventana de planificación ─────────────────────
    ventana, usa_prediccion, fecha_manana = _construir_ventana_planificacion(
        db,
        solicitud.fecha,
        hora_inicio,
        solicitud.hora_limite,
    )

    if not ventana:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay horas disponibles en la ventana de planificación.",
        )

    # ── 2. Calcular horas necesarias (informativo) ────────────────
    horas_necesarias = _calcular_horas_necesarias(
        solicitud.soc_actual,
        solicitud.soc_objetivo,
        solicitud.capacidad_total_kwh,
        solicitud.potencia_kw,
    )

    # ── 3. Convertir ventana al formato del optimizador ───────────
    # Filtramos por clasificaciones permitidas
    permitidas = set(solicitud.clasificaciones_permitidas or ["BAJO", "MEDIO", "ALTO"])

    datos_red = [
        {
            "hora":   h["hora_ventana"],   # usamos hora_ventana para orden correcto
            "tipo":   _MAPA_TIPO.get(h.get("clasificacion", "MEDIO"), "M"),
            "precio": h.get("precio_kwh", 0.0),
        }
        for h in ventana
        if h.get("clasificacion", "MEDIO") in permitidas
    ]

    config_usuario = {
        "soc_actual":   solicitud.soc_actual,
        "soc_objetivo": solicitud.soc_objetivo,
        "potencia_kw":  solicitud.potencia_kw,
    }

    # ── 4. Ejecutar optimizador ───────────────────────────────────
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

    # ── 5. Construir franjas completas para el frontend ───────────
    franjas = _construir_franjas_para_frontend(
        resultado_optimizador,
        ventana,
        solicitud.potencia_kw,
    )

    franjas_activas   = [f for f in franjas if f["on_off"]]
    coste_estimado    = round(sum(f["coste_franja"] for f in franjas_activas), 4)
    emisiones_estimadas = _calcular_emisiones(franjas_activas, solicitud.potencia_kw)
    resumen_opt       = resultado_optimizador.get("resumen", {})

    n_horas_hoy    = sum(1 for f in franjas_activas if not f.get("es_prediccion"))
    n_horas_manana = sum(1 for f in franjas_activas if f.get("es_prediccion"))

    # ── 6. Respuesta final ────────────────────────────────────────
    return {
        "fecha":        solicitud.fecha,
        "fecha_manana": fecha_manana if usa_prediccion else None,
        "hora_inicio":  hora_inicio,
        "hora_limite":  solicitud.hora_limite,
        "usa_prediccion": usa_prediccion,

        "resumen": {
            "soc_actual":               solicitud.soc_actual,
            "soc_objetivo":             solicitud.soc_objetivo,
            "soc_final_estimado":       resumen_opt.get("soc_final_estimado", solicitud.soc_objetivo),
            "horas_necesarias_estimadas": round(horas_necesarias, 1),
            "horas_carga":              len(franjas_activas),
            "horas_carga_hoy":          n_horas_hoy,
            "horas_carga_manana":       n_horas_manana,
            "energia_cargada_kwh":      round(solicitud.potencia_kw * len(franjas_activas) * 0.9, 2),
            "coste_estimado_eur":       coste_estimado,
            "emisiones_estimadas_gco2": emisiones_estimadas,
            "viabilidad_economica":     resumen_opt.get("viabilidad_economica", True),
            "necesita_horas_altas":     resultado_optimizador.get(
                                            "plan_emergencia", {}
                                        ).get("requiere_autorizacion", False),
        },

        "franjas": franjas,

        "aviso_horas_altas": resultado_optimizador.get(
            "plan_emergencia", {}
        ).get("mensaje", ""),

        # Para guardar en POST /planes
        "para_guardar": {
            "usuario_id":               solicitud.usuario_id,
            "franjas": [
                {
                    "hora":          f["hora"],
                    "hora_ventana":  f["hora_ventana"],
                    "fecha":         f["fecha"],
                    "on_off":        f["on_off"],
                    "potencia_kw":   f["potencia_kw"],
                    "precio_kwh":    f["precio_kwh"],
                    "clasificacion": f["clasificacion"].lower(),
                    "es_prediccion": f["es_prediccion"],
                }
                for f in franjas
            ],
            "coste_estimado_eur":       coste_estimado,
            "emisiones_estimadas_gco2": emisiones_estimadas,
            "modelo_version":           "v1.0.0-mlflow",
        },
    }
