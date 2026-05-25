"""
pvpc_ingesta.py
---------------
Pide los precios horarios del mercado eléctrico a la API pública y gratuita
de Red Eléctrica (apidatos.ree.es), los formatea, los guarda en MongoDB
y notifica al modelo IA que hay datos nuevos.

API usada (sin token, sin registro):
  GET https://apidatos.ree.es/es/datos/mercados/precios-mercados-tiempo-real
      ?start_date=YYYY-MM-DDTHH:MM
      &end_date=YYYY-MM-DDTHH:MM
      &time_trunc=hour
      &geo_trunc=electric_system
      &geo_limit=peninsular
      &geo_ids=8741

Colecciones MongoDB:
  · precios_actuales  → horas del día en curso (precio crudo €/MWh, sin clasificar)
  · precios_futuros   → horas del día siguiente (disponibles ~20:15 h, sin clasificar)
  · logs_sistema      → errores, warnings e info del proceso de ingesta

Nota: la clasificación de precios (bajo/medio/alto) es responsabilidad del
modelo de IA (Capa 2). Este script solo persiste los datos crudos de REE.

Control de errores implementado:
  · Reintentos automáticos con backoff exponencial (configurable)
  · Validación de datos recibidos de la API (horas, precios, completitud)
  · Fallback al día anterior si la API falla tras todos los reintentos
  · Registro de errores y alertas críticas en la colección logs_sistema

Uso:
  python pvpc_ingesta.py               # ingesta completa (hoy + mañana si procede)
  python pvpc_ingesta.py --solo-hoy    # solo actualiza precios actuales
  python pvpc_ingesta.py --solo-futuro # solo intenta traer el día siguiente
"""

import os
import json
import logging
import argparse
import time
from dotenv import load_dotenv
import requests
from datetime import datetime, timedelta, timezone
import zoneinfo

# Zona horaria oficial de España peninsular (gestiona automáticamente CET/CEST)
TZ_MADRID = zoneinfo.ZoneInfo("Europe/Madrid")


def ahora_madrid() -> datetime:
    """Devuelve el datetime actual en hora oficial de España (Europe/Madrid).
    Usa siempre esta función en lugar de datetime.now() para garantizar
    que la fecha guardada es la correcta independientemente de la TZ del servidor."""
    return datetime.now(tz=TZ_MADRID)
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN  (sobreescribible con variables de entorno)
# ─────────────────────────────────────────────────────────────
load_dotenv()
MONGO_URI    = os.getenv("MONGO_URI")
MONGO_DB     = os.getenv("MONGO_DB")
COL_ACTUALES = "precios_actuales"
COL_FUTUROS  = "precios_futuros"
COL_LOGS     = "logs_sistema"

# Hora a partir de la cual REE publica los precios del día siguiente
HORA_PUBLICACION_FUTUROS = 20   # 20:15 h → comprobamos >= 20 para simplificar

# Webhook del modelo IA (Flask / FastAPI local)
MODELO_WEBHOOK_URL = os.getenv("MODELO_WEBHOOK_URL")

# ── Configuración de reintentos ────────────────────────────────
# Se puede sobreescribir con variables de entorno
API_MAX_REINTENTOS   = int(os.getenv("API_MAX_REINTENTOS", 3))   # nº máximo de intentos
API_ESPERA_BASE_SEG  = int(os.getenv("API_ESPERA_BASE_SEG", 5))  # espera inicial en segundos
# La espera entre reintentos crece exponencialmente: 5s, 10s, 20s, ...

# ── Validación de datos ────────────────────────────────────────
HORAS_ESPERADAS      = 24   # una entrada por cada hora del día
PRECIO_MIN_VALIDO    = -10.0   # €/MWh — precios negativos son posibles pero raros
PRECIO_MAX_VALIDO    = 800.0   # €/MWh — techo razonable para detectar datos corruptos

# URL base y parámetros fijos de la API REE
API_BASE = "https://apidatos.ree.es"
API_PATH = "/es/datos/mercados/precios-mercados-tiempo-real"
GEO_PARAMS = {
    "geo_trunc":  "electric_system",
    "geo_limit":  "peninsular",
    "geo_ids":    "8741",
    "time_trunc": "hour",
}
CABECERAS = {
    "Accept":       "application/json",
    "Content-Type": "application/json",
    "Host":         "apidatos.ree.es",
}

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
# 0. LOGS EN MONGODB  (nueva sección)
# ═════════════════════════════════════════════════════════════

def _get_mongo_col(nombre_col: str):
    """Devuelve una colección de MongoDB. Lanza PyMongoError si no conecta."""
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    return client, client[MONGO_DB][nombre_col]


def registrar_log_mongo(
    nivel: str,
    componente: str,
    mensaje: str,
    detalle: str = "",
    resuelto: bool = False,
) -> None:
    """
    Inserta un documento en logs_sistema.

    Parámetros
    ----------
    nivel      : "info" | "warning" | "error" | "critical"
    componente : debe coincidir con el enum de la colección ("api_pvpc", "scheduler", ...)
    mensaje    : texto corto descriptivo
    detalle    : stack trace, respuesta HTTP, etc. (opcional)
    resuelto   : True si el proceso pudo continuar sin intervención manual
    """
    doc = {
        "timestamp":   ahora_madrid(),
        "nivel":      nivel,
        "componente": componente,
        "mensaje":    mensaje,
        "resuelto":   resuelto,
    }
    if detalle:
        doc["detalle"] = detalle[:2000]   # truncamos para no superar límites de BSON

    try:
        client, col = _get_mongo_col(COL_LOGS)
        col.insert_one(doc)
        client.close()
    except PyMongoError as exc:
        # Si no podemos escribir en Mongo, al menos lo dejamos en el log local
        log.error("No se pudo escribir en logs_sistema: %s", exc)


# ═════════════════════════════════════════════════════════════
# 1. VALIDACIÓN DE DATOS  (nueva sección)
# ═════════════════════════════════════════════════════════════

class ErrorValidacion(Exception):
    """Se lanza cuando los datos de la API no superan las comprobaciones."""


def validar_datos_ree(valores_raw: list[dict], fecha_str: str) -> None:
    """
    Valida la lista de valores devuelta por pedir_precios_ree().
    Lanza ErrorValidacion con un mensaje descriptivo si encuentra problemas.

    Comprobaciones realizadas:
      1. La lista no está vacía.
      2. Contiene exactamente 24 entradas (una por hora).
      3. No hay horas duplicadas.
      4. Todos los campos obligatorios están presentes ("hora", "precio_mwh", "datetime").
      5. El campo "hora" es un entero entre 0 y 23.
      6. El precio está dentro del rango válido [PRECIO_MIN_VALIDO, PRECIO_MAX_VALIDO].
      7. Las 24 horas del día están presentes (sin huecos).
    """
    if not valores_raw:
        raise ErrorValidacion(f"[{fecha_str}] La API devolvió una lista vacía.")

    if len(valores_raw) != HORAS_ESPERADAS:
        raise ErrorValidacion(
            f"[{fecha_str}] Se esperaban {HORAS_ESPERADAS} horas, "
            f"se recibieron {len(valores_raw)}."
        )

    horas_vistas = set()
    for i, v in enumerate(valores_raw):
        # Campos obligatorios
        for campo in ("hora", "precio_mwh", "datetime"):
            if campo not in v:
                raise ErrorValidacion(
                    f"[{fecha_str}] Registro #{i} sin campo obligatorio '{campo}': {v}"
                )

        hora = v["hora"]
        precio = v["precio_mwh"]

        # Tipo y rango de hora
        if not isinstance(hora, int) or not (0 <= hora <= 23):
            raise ErrorValidacion(
                f"[{fecha_str}] Hora inválida en registro #{i}: '{hora}'"
            )

        # Duplicados
        if hora in horas_vistas:
            raise ErrorValidacion(
                f"[{fecha_str}] Hora duplicada: {hora}"
            )
        horas_vistas.add(hora)

        # Rango de precio
        if not isinstance(precio, (int, float)):
            raise ErrorValidacion(
                f"[{fecha_str}] Precio no numérico en hora {hora}: '{precio}'"
            )
        if not (PRECIO_MIN_VALIDO <= precio <= PRECIO_MAX_VALIDO):
            raise ErrorValidacion(
                f"[{fecha_str}] Precio fuera de rango en hora {hora}: "
                f"{precio} €/MWh (rango válido: {PRECIO_MIN_VALIDO}–{PRECIO_MAX_VALIDO})"
            )

    # Todas las horas del día presentes
    horas_faltantes = set(range(24)) - horas_vistas
    if horas_faltantes:
        raise ErrorValidacion(
            f"[{fecha_str}] Horas faltantes en la respuesta: "
            f"{sorted(horas_faltantes)}"
        )

    log.info("[%s] Validación OK — 24 horas correctas, precios en rango.", fecha_str)


# ═════════════════════════════════════════════════════════════
# 2. OBTENCIÓN DE DATOS — API REE con REINTENTOS
# ═════════════════════════════════════════════════════════════

def _llamar_api_ree(fecha_str: str, params: dict) -> list[dict]:
    """
    Realiza UNA llamada a la API de REE y extrae los valores crudos.
    Lanza requests.RequestException si hay error de red/HTTP.
    Devuelve lista vacía si la respuesta no contiene datos utilizables.
    """
    url = f"{API_BASE}{API_PATH}"
    resp = requests.get(url, headers=CABECERAS, params=params, timeout=20)
    resp.raise_for_status()

    datos_json = resp.json()
    incluidos = datos_json.get("included", [])
    if not incluidos:
        log.warning("La API de REE no devolvió datos para %s.", fecha_str)
        return []

    indicador_precio = None
    for item in incluidos:
        item_id    = str(item.get("id", ""))
        item_title = item.get("attributes", {}).get("title", "")
        log.debug("Indicador disponible → id=%s  titulo=%s", item_id, item_title)
        if item_id == "600":
            indicador_precio = item
            break

    if indicador_precio is None:
        log.warning("No se encontró el indicador 600. Usando el primer indicador disponible.")
        indicador_precio = incluidos[0] if incluidos else None

    if indicador_precio is None:
        log.error("No hay indicadores en la respuesta de REE.")
        return []

    valores_raw = indicador_precio.get("attributes", {}).get("values", [])
    log.info(
        "Indicador '%s' → %d valores recibidos.",
        indicador_precio.get("attributes", {}).get("title", "desconocido"),
        len(valores_raw),
    )

    resultado = []
    for v in valores_raw:
        precio_mwh = float(v.get("value", 0))
        dt_str     = v.get("datetime", "")
        hora       = int(dt_str[11:13]) if len(dt_str) >= 13 else None
        resultado.append({
            "hora":       hora,
            "precio_mwh": precio_mwh,
            "datetime":   dt_str,
        })

    return resultado


def pedir_precios_ree(fecha: datetime) -> list[dict]:
    """
    Llama a la API pública de REE con reintentos y backoff exponencial.
    Si todos los intentos fallan, devuelve lista vacía.

    Flujo por intento:
      1. Llamada HTTP a la API.
      2. Validación de los datos recibidos.
      3. Si hay error → espera (base * 2^intento) segundos y reintenta.

    Tras agotar los reintentos registra el fallo en logs_sistema y devuelve [].
    """
    fecha_str = fecha.strftime("%Y-%m-%d")
    params = {
        **GEO_PARAMS,
        "start_date": f"{fecha_str}T00:00",
        "end_date":   f"{fecha_str}T23:59",
    }

    ultimo_error = ""

    for intento in range(1, API_MAX_REINTENTOS + 1):
        log.info(
            "Solicitando precios REE para %s (intento %d/%d) ...",
            fecha_str, intento, API_MAX_REINTENTOS,
        )
        try:
            raw = _llamar_api_ree(fecha_str, params)

            # ── Validación de los datos recibidos ──────────────────
            validar_datos_ree(raw, fecha_str)   # lanza ErrorValidacion si hay problema

            return raw   # ✅ éxito

        except requests.exceptions.Timeout as exc:
            ultimo_error = f"Timeout en intento {intento}: {exc}"
            log.warning(ultimo_error)

        except requests.exceptions.HTTPError as exc:
            ultimo_error = f"HTTP {exc.response.status_code} en intento {intento}: {exc}"
            log.warning(ultimo_error)
            # Errores 4xx (excepto 429) no mejorarán con reintentos
            if exc.response.status_code not in (429, 500, 502, 503, 504):
                log.error("Error HTTP no recuperable (%s). Abortando reintentos.", exc.response.status_code)
                break

        except requests.RequestException as exc:
            ultimo_error = f"Error de red en intento {intento}: {exc}"
            log.warning(ultimo_error)

        except ErrorValidacion as exc:
            ultimo_error = f"Validación fallida en intento {intento}: {exc}"
            log.warning(ultimo_error)
            # Datos malformados — reintentamos por si fue un problema transitorio

        # Backoff exponencial antes del siguiente intento
        if intento < API_MAX_REINTENTOS:
            espera = API_ESPERA_BASE_SEG * (2 ** (intento - 1))
            log.info("Esperando %ds antes del siguiente intento...", espera)
            time.sleep(espera)

    # ── Todos los reintentos agotados ──────────────────────────
    mensaje_critico = (
        f"API REE inaccesible tras {API_MAX_REINTENTOS} intentos para {fecha_str}."
    )
    log.error(mensaje_critico)
    registrar_log_mongo(
        nivel="error",
        componente="api_pvpc",
        mensaje=mensaje_critico,
        detalle=ultimo_error,
        resuelto=False,
    )
    return []


# ═════════════════════════════════════════════════════════════
# 3. FALLBACK AL DÍA ANTERIOR  (nueva sección)
# ═════════════════════════════════════════════════════════════

def obtener_fallback_dia_anterior(coleccion_nombre: str) -> dict | None:
    """
    Recupera de MongoDB el documento de precios del día anterior.
    Se usa cuando la API de REE falla completamente.

    Devuelve el documento si existe, None en caso contrario.
    El documento recuperado se marca con:
      · "es_fallback": True
      · "fecha_fallback_origen": fecha del documento original
    """
    ayer = ( ahora_madrid()  - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        client, col = _get_mongo_col(coleccion_nombre)
        doc = col.find_one({"fecha": ayer})
        client.close()

        if doc:
            log.warning(
                "⚠ Usando datos de FALLBACK del día anterior (%s) para la colección '%s'.",
                ayer, coleccion_nombre,
            )
            registrar_log_mongo(
                nivel="warning",
                componente="api_pvpc",
                mensaje=f"Fallback activado: usando precios del {ayer} en lugar de hoy.",
                detalle=f"Colección: {coleccion_nombre}",
                resuelto=True,   # el proceso continúa, aunque con datos aproximados
            )
            # Eliminamos _id para poder reinsertar el documento con nueva fecha
            doc.pop("_id", None)
            doc["es_fallback"]            = True
            doc["fecha_fallback_origen"]  = ayer
            return doc
        else:
            log.error(
                "No hay datos del día anterior (%s) disponibles en '%s'.",
                ayer, coleccion_nombre,
            )
            return None

    except PyMongoError as exc:
        log.error("Error al consultar fallback en MongoDB: %s", exc)
        return None


# ═════════════════════════════════════════════════════════════
# 4. FORMATEO  (sin clasificación — responsabilidad del modelo IA)
# ═════════════════════════════════════════════════════════════

def formatear_precios(valores_raw: list[dict], fecha: datetime) -> dict:
    """
    Construye el documento JSON crudo que se almacenará en MongoDB.
    NO incluye clasificación: eso lo hace el modelo de IA en Capa 2.

    Estructura del documento:
    {
      "fecha": "YYYY-MM-DD",
      "fecha_actualizacion": "<ISO UTC>",
      "fuente": "REE - apidatos.ree.es",
      "clasificado": false,          ← indica que aún no ha pasado por el modelo
      "horas": [
        {
          "hora": 0,
          "precio_mwh": 75.4,
          "precio_kwh": 0.0754,
          "datetime": "2026-04-29T00:00:00.000+02:00"
        },
        ...
      ],
      "resumen": {
        "precio_min_mwh": ...,
        "precio_max_mwh": ...,
        "precio_medio_mwh": ...
      }
    }
    """
    horas_formateadas = []
    for v in valores_raw:
        precio_mwh = v["precio_mwh"]
        horas_formateadas.append({
            "hora":       v["hora"],
            "precio_mwh": round(precio_mwh, 4),
            "precio_kwh": round(precio_mwh / 1000, 6),
            "datetime":   v["datetime"],
        })

    horas_formateadas.sort(key=lambda x: x["hora"] if x["hora"] is not None else 99)

    precios = [h["precio_mwh"] for h in horas_formateadas]
    resumen = {}
    if precios:
        resumen = {
            "precio_min_mwh":   round(min(precios), 4),
            "precio_max_mwh":   round(max(precios), 4),
            "precio_medio_mwh": round(sum(precios) / len(precios), 4),
        }

    return {
        "fecha":               fecha.strftime("%Y-%m-%d"),
        "fecha_actualizacion":  ahora_madrid(),
        "fuente":              "REE - apidatos.ree.es - mercados/componentes-precio-energia-cierre-desglose",
        "clasificado":         False,   # el modelo IA pondrá esto a True tras clasificar
        "horas":               horas_formateadas,
        "resumen":             resumen,
    }


# ═════════════════════════════════════════════════════════════
# 5. PERSISTENCIA EN MONGODB
# ═════════════════════════════════════════════════════════════

def guardar_en_mongo(documento: dict, coleccion_nombre: str) -> bool:
    """
    Inserta o actualiza (upsert) el documento en MongoDB.
    Clave de búsqueda: campo 'fecha'.
    Devuelve True si la operación fue exitosa.
    """
    try:
        client, col = _get_mongo_col(coleccion_nombre)
        operacion = UpdateOne(
            {"fecha": documento["fecha"]},
            {"$set": documento},
            upsert=True,
        )
        resultado = col.bulk_write([operacion])
        client.close()

        log.info(
            "MongoDB [%s] → upserted=%d  modified=%d  fecha=%s",
            coleccion_nombre,
            resultado.upserted_count,
            resultado.modified_count,
            documento["fecha"],
        )
        return True

    except PyMongoError as exc:
        msg = f"Error al guardar en MongoDB ({coleccion_nombre}): {exc}"
        log.error(msg)
        registrar_log_mongo(
            nivel="error",
            componente="api_pvpc",
            mensaje=f"Fallo al persistir datos en '{coleccion_nombre}'.",
            detalle=str(exc),
            resuelto=False,
        )
        return False


# ═════════════════════════════════════════════════════════════
# 6. NOTIFICACIÓN AL MODELO IA
# ═════════════════════════════════════════════════════════════

def notificar_modelo(tipo: str, fecha: str, documento: dict) -> None:
    """
    POST al webhook del modelo con el documento completo de precios.

    Payload enviado:
    {
      "evento":      "nuevos_precios",
      "tipo":        "actuales" | "futuros",
      "fecha":       "YYYY-MM-DD",
      "horas":       [{"hora": 0, "precio_mwh": ..., "precio_kwh": ..., "datetime": ...}, ...],
      "resumen":     { precio_min_mwh, precio_max_mwh, precio_medio_mwh },
      "es_fallback": bool   <- True si los datos vienen del dia anterior
    }
    """
    payload = {
        "evento":      "nuevos_precios",
        "tipo":        tipo,
        "fecha":       fecha,
        "horas":       documento.get("horas", []),
        "resumen":     documento.get("resumen", {}),
        "es_fallback": documento.get("es_fallback", False),
    }

    log.info("Notificando modelo IA → %s  [tipo=%s, fecha=%s]", MODELO_WEBHOOK_URL, tipo, fecha)
    try:
        resp = requests.post(MODELO_WEBHOOK_URL, json=payload, timeout=10)
        if resp.ok:
            log.info("Modelo notificado correctamente (HTTP %d).", resp.status_code)
            guardar_en_mongo(resp.json(), COL_ACTUALES)
        else:
            log.warning("El modelo respondió HTTP %d: %s", resp.status_code, resp.text[:200])
            registrar_log_mongo(
                nivel="warning",
                componente="modelo_ia",
                mensaje=f"Webhook del modelo devolvió HTTP {resp.status_code}.",
                detalle=resp.text[:500],
                resuelto=True,
            )
    except requests.RequestException as exc:
        # No es crítico: los datos ya están en Mongo
        log.warning("No se pudo notificar al modelo (¿está levantado?): %s", exc)
        registrar_log_mongo(
            nivel="warning",
            componente="modelo_ia",
            mensaje="No se pudo contactar con el webhook del modelo IA.",
            detalle=str(exc),
            resuelto=True,
        )


# ═════════════════════════════════════════════════════════════
# 7. COPIA LOCAL EN JSON (respaldo)
# ═════════════════════════════════════════════════════════════

def _json_serial(obj):
    """Serializer de respaldo para tipos no soportados por json (ej: datetime)."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Tipo no serializable: {type(obj)}")


def guardar_json_local(documento: dict, nombre_archivo: str) -> None:
    """Guarda una copia del documento en la carpeta 'data/' como respaldo."""
    carpeta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
    carpeta = os.path.normpath(carpeta)
    ruta = os.path.join(carpeta, nombre_archivo)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(documento, f, ensure_ascii=False, indent=2, default=_json_serial)
        log.info("Copia local guardada → %s", ruta)
    except OSError as exc:
        log.warning("No se pudo guardar copia local: %s", exc)


# ═════════════════════════════════════════════════════════════
# 8. FLUJOS DE INGESTA
# ═════════════════════════════════════════════════════════════

def ingestar_precios_actuales() -> bool:
    """
    Obtiene, valida, formatea y guarda los precios del día actual.
    Si la API falla tras todos los reintentos, activa el fallback
    con los datos del día anterior.
    """
    hoy = ahora_madrid()  # ← datetime, no string
    raw = pedir_precios_ree(hoy)

    if not raw:
        # ── Fallback al día anterior ───────────────────────────
        log.warning("Activando fallback: se usarán datos del día anterior.")
        doc_fallback = obtener_fallback_dia_anterior(COL_ACTUALES)

        if doc_fallback is None:
            msg = "API REE y fallback fallaron. No hay datos de precios disponibles."
            log.error(msg)
            registrar_log_mongo(
                nivel="critical",
                componente="api_pvpc",
                mensaje=msg,
                resuelto=False,
            )
            return False

        # Actualizamos la fecha al día de hoy para que el optimizador
        # no rechace el documento por fecha obsoleta
        doc_fallback["fecha"] = hoy.strftime("%Y-%m-%d")
        doc_fallback["fecha_actualizacion"] = ahora_madrid()
        doc = doc_fallback

    else:
        doc = formatear_precios(raw, hoy)
        registrar_log_mongo(
            nivel="info",
            componente="api_pvpc",
            mensaje=f"Precios actuales de REE ingestionados correctamente ({hoy.strftime('%Y-%m-%d')}).",
            resuelto=True,
        )

    guardar_json_local(doc, f"ree_{hoy.strftime('%Y-%m-%d')}_actuales.json")
    ok = guardar_en_mongo(doc, COL_ACTUALES)
    if ok:
        notificar_modelo("actuales", doc["fecha"], doc)
    return ok


def ingestar_precios_futuros() -> bool:
    """
    Intenta obtener los precios del día siguiente.
    REE los publica a las ~20:15 h; antes de esa hora termina sin error.
    Si la API falla, activa el fallback con los datos del día anterior.
    """
    ahora = ahora_madrid()  # ← igual aquí    
    if ahora.hour < HORA_PUBLICACION_FUTUROS:
        log.info(
            "Son las %02d:%02d h — los precios del día siguiente se publican a las %02d:15 h. "
            "Ingesta de futuros omitida.",
            ahora.hour, ahora.minute, HORA_PUBLICACION_FUTUROS,
        )
        return False

    manana = ahora + timedelta(days=1)
    raw = pedir_precios_ree(manana)

    if not raw:
        log.warning(
            "Los precios del día siguiente aún no están disponibles en REE. "
            "Activando fallback."
        )
        doc_fallback = obtener_fallback_dia_anterior(COL_FUTUROS)

        if doc_fallback is None:
            msg = "No se pudieron obtener precios futuros ni fallback del día anterior."
            log.error(msg)
            registrar_log_mongo(
                nivel="error",
                componente="api_pvpc",
                mensaje=msg,
                resuelto=False,
            )
            return False

        doc_fallback["fecha"] = manana.strftime("%Y-%m-%d")
        doc_fallback["fecha_actualizacion"] = ahora_madrid()
        doc = doc_fallback

    else:
        doc = formatear_precios(raw, manana)
        registrar_log_mongo(
            nivel="info",
            componente="api_pvpc",
            mensaje=f"Precios futuros de REE ingestionados correctamente ({manana.strftime('%Y-%m-%d')}).",
            resuelto=True,
        )

    guardar_json_local(doc, f"ree_{manana.strftime('%Y-%m-%d')}_futuros.json")
    ok = guardar_en_mongo(doc, COL_FUTUROS)
    if ok:
        notificar_modelo("futuros", doc["fecha"], doc)
    return ok


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingesta precios REE → MongoDB → notificación modelo IA"
    )
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--solo-hoy",    action="store_true", help="Solo ingesta precios actuales")
    grupo.add_argument("--solo-futuro", action="store_true", help="Solo ingesta precios día siguiente")
    args = parser.parse_args()

    log.info("======== Inicio ingesta REE ========")
    registrar_log_mongo(
        nivel="info",
        componente="scheduler",
        mensaje="Inicio del proceso de ingesta de precios REE.",
        resuelto=True,
    )

    if args.solo_hoy:
        ingestar_precios_actuales()
    elif args.solo_futuro:
        ingestar_precios_futuros()
    else:
        ingestar_precios_actuales()
        ingestar_precios_futuros()

    log.info("======== Fin ingesta REE ========")
    registrar_log_mongo(
        nivel="info",
        componente="scheduler",
        mensaje="Fin del proceso de ingesta de precios REE.",
        resuelto=True,
    )


if __name__ == "__main__":
    main()
