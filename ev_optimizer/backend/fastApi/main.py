"""
main.py
-------
Punto de entrada unificado de la API REST del Optimizador de Carga para VE.
Grupo 8IA · IES Abastos · 2025/26

Incluye:
  · Clasificador ML  →  POST /nuevo_dato       /  GET /health
  · Auth             →  POST /auth/register    /  POST /auth/login
  · Plan de carga    →  POST /plan_carga
  · Planes CRUD      →  POST/GET/PUT/DELETE /planes

Arrancar:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import sys
import pickle
import warnings
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timezone
import os
from dotenv import load_dotenv
from database import get_db

from fastapi import FastAPI, HTTPException
from typing import Optional
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth
import planes
import plan_carga   # ← nuevo router

# ══════════════════════════════════════════════════════════════════
# CARGA DEL MODELO AL ARRANCAR
# ══════════════════════════════════════════════════════════════════
load_dotenv()
MODEL_PATH = Path(os.getenv("MODELO_PATH"))

if not MODEL_PATH.exists():
    raise FileNotFoundError(
        f"Modelo no encontrado en '{MODEL_PATH}'. "
        "Pon clasificador_precio.pkl dentro de la carpeta modelo/."
    )

with open(MODEL_PATH, "rb") as f:
    _artefacto = pickle.load(f)

MODELO        = _artefacto["modelo"]
FEATURES      = _artefacto["features"]
MODELO_NOMBRE = _artefacto["nombre"]
MODELO_FECHA  = _artefacto["fecha"]
MODELO_ACC    = _artefacto["accuracy"]

MEDIA_POR_HORA: dict[int, float] = {
    0: 0.0697,  1: 0.0691,  2: 0.0696,  3: 0.0697,
    4: 0.0702,  5: 0.0696,  6: 0.0700,  7: 0.1981,
    8: 0.1981,  9: 0.1981, 10: 0.1992, 11: 0.1993,
   12: 0.0998, 13: 0.1005, 14: 0.1005, 15: 0.1000,
   16: 0.1005, 17: 0.2501, 18: 0.2491, 19: 0.2508,
   20: 0.2489, 21: 0.2504, 22: 0.2508, 23: 0.0702,
}

print(f"Modelo cargado: {MODELO_NOMBRE} | Accuracy: {MODELO_ACC:.4f}")


# ══════════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════════

class HoraREE(BaseModel):
    hora:          int
    precio_mwh:    float
    precio_kwh:    float
    clasificacion: Optional[str] = None
    datetime:      str


class PayloadREE(BaseModel):
    evento:  str
    tipo:    str
    fecha:   str
    resumen: dict
    horas:   list[HoraREE]


# ══════════════════════════════════════════════════════════════════
# LÓGICA DE CLASIFICACIÓN
# ══════════════════════════════════════════════════════════════════

def _construir_X(hora: int, precio_kwh: float, precios_dia: list[float], mes: int) -> pd.DataFrame:
    lag_1h  = precios_dia[hora - 1] if hora > 0 else precio_kwh
    lag_24h = MEDIA_POR_HORA.get(hora, precio_kwh)
    ventana = precios_dia[:hora] if hora > 0 else [precio_kwh]
    rolling = float(np.mean(ventana))
    media_hora = MEDIA_POR_HORA.get(hora, 0.15)
    pvhm = precio_kwh / media_hora if media_hora > 0 else 1.0

    return pd.DataFrame([{
        "hour":                   hora,
        "day_of_week":            0,
        "month":                  mes,
        "is_weekend":             0,
        "price_lag_1h":           lag_1h,
        "price_lag_24h":          lag_24h,
        "price_rolling_mean_24h": rolling,
        "price_vs_hour_mean":     pvhm,
    }])[FEATURES]


def _clasificar_horas(horas_ree: list[HoraREE], fecha: str) -> list[dict]:
    try:
        mes = int(fecha.split("-")[1])
    except Exception:
        mes = datetime.now(timezone.utc).month

    horas_ordenadas = sorted(horas_ree, key=lambda h: h.hora)
    precios_dia     = [h.precio_kwh for h in horas_ordenadas]

    resultado = []
    for h in horas_ordenadas:
        X     = _construir_X(h.hora, h.precio_kwh, precios_dia, mes)
        clase = MODELO.predict(X)[0]

        resultado.append({
            "hora":          h.hora,
            "precio_kwh":    h.precio_kwh,
            "precio_mwh":    h.precio_mwh,
            "clasificacion": clase,
            "datetime":      h.datetime,
        })

    return resultado


def _construir_doc_mongo(fecha: str, tipo: str, horas: list[dict]) -> dict:
    precios     = [h["precio_mwh"] for h in horas]
    bajo_horas  = [h["hora"] for h in horas if h["clasificacion"] == "BAJO"]
    medio_horas = [h["hora"] for h in horas if h["clasificacion"] == "MEDIO"]
    alto_horas  = [h["hora"] for h in horas if h["clasificacion"] == "ALTO"]

    return {
        "fecha":               fecha,
        "tipo":                tipo,
        "fuente":              "REE - apidatos.ree.es",
        "clasificado":         True,
        "modelo_clasificador": MODELO_NOMBRE,
        "fecha_clasificacion": datetime.now(timezone.utc).isoformat(),
        "horas": [
            {
                "hora":          h["hora"],
                "precio_kwh":    h["precio_kwh"],
                "precio_mwh":    h["precio_mwh"],
                "clasificacion": h["clasificacion"],
                "datetime":      h["datetime"],
            }
            for h in horas
        ],
        "resumen": {
            "precio_min_mwh":   round(min(precios), 4),
            "precio_max_mwh":   round(max(precios), 4),
            "precio_medio_mwh": round(float(np.mean(precios)), 4),
            "horas_bajo":       bajo_horas,
            "horas_medio":      medio_horas,
            "horas_alto":       alto_horas,
            "n_horas_bajo":     len(bajo_horas),
            "n_horas_medio":    len(medio_horas),
            "n_horas_alto":     len(alto_horas),
        },
    }


# ══════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════

app = FastAPI(
    title="EV — Optimizador de Carga",
    description="Clasificador PVPC + Generador de planes de carga para VE.",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5500", "http://127.0.0.1:5500"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(planes.router)
app.include_router(plan_carga.router)   # ← nuevo


# ── Endpoints propios de main ──────────────────────────────────────

@app.get("/health", tags=["Sistema"])
def health():
    """Estado de la API y del modelo ML cargado."""
    return {
        "status":       "ok",
        "modelo":       MODELO_NOMBRE,
        "accuracy":     round(MODELO_ACC, 4),
        "entrenado_el": MODELO_FECHA,
    }


@app.post("/nuevo_dato", tags=["Clasificador ML"])
def nuevo_dato(payload: PayloadREE):
    """
    Recibe el JSON completo de REE (24 horas), clasifica con el modelo
    y devuelve el documento listo para upsert en MongoDB.
    Llamado por pvpc_ingesta.py vía notificar_modelo().
    """
    if len(payload.horas) != 24:
        raise HTTPException(
            status_code=422,
            detail=f"Se esperaban 24 horas, se recibieron {len(payload.horas)}.",
        )
    try:
        horas_clasificadas = _clasificar_horas(payload.horas, payload.fecha)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la clasificación: {e}")

    return _construir_doc_mongo(payload.fecha, payload.tipo, horas_clasificadas)


@app.get("/precios_electricidad", tags=["Precios"])
def get_precios(fecha: str, tipo: str = "actuales"):
    """
    Devuelve los precios clasificados de un día concreto.
    Busca en precios_actuales o precios_futuros según el tipo.

    Parámetros:
      - fecha: YYYY-MM-DD
      - tipo:  "actuales" | "futuros"
    """
    db = get_db()

    col_name = "precios_actuales" if tipo == "actuales" else "precios_futuros"

    # El campo fecha se guarda como string "YYYY-MM-DD"
    doc = db[col_name].find_one({"fecha": fecha}, {"_id": 0})

    if not doc:
        raise HTTPException(
            status_code=404,
            detail=f"No hay precios disponibles para {fecha} ({tipo}). "
                   "Ejecuta pvpc_ingesta.py para obtenerlos."
        )

    return doc