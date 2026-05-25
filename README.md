# EV Optimizer — Carga Inteligente para Vehículos Eléctricos mediante IA

Sistema completo de optimización de carga para vehículos eléctricos que clasifica las franjas horarias del mercado eléctrico PVPC con un modelo Gradient Boosting (accuracy **99,67 %**) y genera planes de carga personalizados que minimizan coste económico y emisiones de CO₂.

> **Proyecto de fin de curso · Curso Especialización FP en Inteligencia Artificial y Big Data**  
> IES Abastos · Grupo 8IA · 2025/26  
> Maria Solaz Chávez · Javier Amaya Moreno  
> Tutora: Chelo Richart

---

## Índice

- [¿Qué hace este sistema?](#qué-hace-este-sistema)
- [Arquitectura](#arquitectura)
- [Stack tecnológico](#stack-tecnológico)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Requisitos previos](#requisitos-previos)
- [Instalación y puesta en marcha](#instalación-y-puesta-en-marcha)
- [Variables de entorno](#variables-de-entorno)
- [Referencia de la API](#referencia-de-la-api)
- [El modelo de IA](#el-modelo-de-ia)
- [Base de datos](#base-de-datos)
- [Ingesta automática de precios](#ingesta-automática-de-precios)
- [Frontend](#frontend)
- [Resultados](#resultados)
- [Trabajo futuro](#trabajo-futuro)

---

## ¿Qué hace este sistema?

El precio de la electricidad en tarifa PVPC varía hora a hora. Recargar un vehículo eléctrico a las 3 h puede costar menos de la mitad que hacerlo a las 19 h. Sin embargo, la mayoría de usuarios no sabe cuándo conviene cargar ni cuánto ahorra eligiendo bien.

Este sistema resuelve eso en cuatro pasos automáticos:

1. **Obtiene** los precios horarios diarios de la [API pública de Red Eléctrica](https://apidatos.ree.es) cada madrugada y, si están disponibles, también los del día siguiente a partir de las 20:15 h.
2. **Clasifica** cada franja horaria como `BAJO`, `MEDIO` o `ALTO` con un modelo Gradient Boosting entrenado sobre 52.608 registros históricos (2020–2025).
3. **Genera** un plan de carga óptimo adaptado al perfil del usuario: SOC actual, SOC objetivo, potencia del cargador y franjas que acepta. La estrategia es siempre `BAJO → MEDIO → ALTO` (cascada de eficiencia).
4. **Calcula** el ahorro económico frente a una carga no optimizada y las emisiones de CO₂ evitadas (factor 180 gCO₂/kWh de la red española).

---

## Arquitectura

```
┌─────────────────────────────────────────────────────────────────┐
│  Capa 1 — Ingesta  (contenedor ev_ingesta)                      │
│                                                                 │
│  API REE ──► pvpc_ingesta.py ──► MongoDB (precios crudos)       │
│              cron · reintentos · fallback · logs                │
└──────────────────────────────┬──────────────────────────────────┘
                               │ POST /nuevo_dato
┌──────────────────────────────▼──────────────────────────────────┐
│  Capa 2 — Procesamiento IA  (contenedor ev_backend)             │
│                                                                 │
│  Feature engineering ──► Gradient Boosting ──► Optimizador      │
│  (8 variables · Pandas)     (acc. 99,67 %)    (cascada B→M→A)  │
└──────────────────────────────┬──────────────────────────────────┘
                               │ Plan clasificado
┌──────────────────────────────▼──────────────────────────────────┐
│  Capa 3 — Backend API REST  (contenedor ev_backend · FastAPI)   │
│                                                                 │
│  /auth  ·  /plan_carga  ·  /planes  ·  /nuevo_dato  ·  /health │
└──────────────────────────────┬──────────────────────────────────┘
                               │ /api/* → proxy Nginx
┌──────────────────────────────▼──────────────────────────────────┐
│  Frontend SPA  (contenedor ev_frontend · Nginx)                 │
│  Dashboard · Nuevo plan · Historial                             │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│  Datos — MongoDB Atlas (ev_optimizer)                           │
│  precios_electricidad · usuarios · planes_de_carga              │
│  registros_carga · logs_sistema · logs_reentrenamiento          │
└─────────────────────────────────────────────────────────────────┘
```

Desplegado en **AWS EC2 t2.micro · Ubuntu 22.04 LTS · Docker Compose**.

---

## Stack tecnológico

| Área | Tecnología |
|------|-----------|
| Lenguaje backend | Python 3.9 |
| API REST | FastAPI 0.136 + Uvicorn + Pydantic |
| Modelo IA | Scikit-learn — `GradientBoostingClassifier` |
| Data science | Pandas 2.2 · NumPy 2.0 |
| Tracking ML | MLflow · DVC |
| Entrenamiento | Google Colab (`ev_clasificador.ipynb`) |
| Base de datos | MongoDB Atlas (PyMongo 4.17) |
| Ingesta | Requests + cron (contenedor dedicado) |
| Frontend | HTML + CSS + JavaScript puro (sin frameworks) |
| Servidor web | Nginx alpine (proxy inverso + SPA) |
| Contenerización | Docker + Docker Compose 3.9 |
| Cloud | AWS EC2 t2.micro (Free Tier) |
| Autenticación | SHA-256 (`hashlib`) |

---

## Estructura del repositorio

```
proyecto/
├── docker-compose.yml              # Orquesta los 3 servicios: frontend, backend, ingesta
│
├── frontend/
│   ├── Dockerfile                  # nginx:alpine
│   ├── nginx.conf                  # SPA + proxy /api/ → backend:8000
│   └── index.html                  # SPA completa (HTML/CSS/JS)
│
└── backend/
    ├── Dockerfile                  # python:3.9-slim · WORKDIR /opt/ev_cargador
    ├── requirements.txt            # fastapi, uvicorn, pymongo, scikit-learn, pandas...
    ├── .env.example                # Plantilla de variables de entorno
    ├── mongo_init_8IA.py           # Migración inicial: colecciones + validadores + índices
    │
    ├── modelo/
    │   └── clasificador_precio.pkl # GradientBoosting serializado
    │                               # Contiene: modelo, features, umbrales, accuracy, fecha
    │
    ├── ingesta/
    │   ├── Dockerfile              # Contenedor dedicado para el cron de ingesta
    │   └── pvpc_ingesta.py         # Obtiene precios REE · reintentos · fallback · logs
    │
    └── fastApi/
        ├── main.py                 # App FastAPI + clasificador + /health + /nuevo_dato
        ├── auth.py                 # Router /auth/register · /auth/login
        ├── plan_carga.py           # Router /plan_carga (ventana hoy+mañana + optimizador)
        ├── planes.py               # Router /planes — POST, GET, PUT, DELETE
        ├── database.py             # Singleton MongoDB con @lru_cache
        └── prediccion_carga.py     # Algoritmo cascada B→M→A · cálculo SOC · emisiones
```

> El notebook de entrenamiento está en `ev_clasificador.ipynb` (ejecutar en Google Colab).

---

## Requisitos previos

- [Docker](https://docs.docker.com/get-docker/) y [Docker Compose](https://docs.docker.com/compose/install/) v3.9+
- Cuenta en [MongoDB Atlas](https://www.mongodb.com/atlas) con un cluster activo y la IP del servidor en la lista de acceso
- Puerto **80** libre en el servidor (frontend) y **8000** accesible internamente (backend)

Para desarrollo local sin Docker: Python 3.9+ y las dependencias de `requirements.txt`.

---

## Instalación y puesta en marcha

### Con Docker Compose — producción (recomendado)

```bash
# 1. Clonar el repositorio
git clone https://github.com/<usuario>/<repo>.git
cd proyecto

# 2. Crear el fichero de variables de entorno
cp backend/.env.example backend/.env
# Editar backend/.env con las credenciales de MongoDB Atlas

# 3. Inicializar la base de datos (solo la primera vez)
pip install pymongo python-dotenv
python backend/mongo_init_8IA.py

# 4. Construir y levantar los tres contenedores
docker compose up --build -d

# 5. Verificar el estado
curl http://localhost/api/health
```

El frontend queda disponible en `http://localhost` y la API en `http://localhost/api`.

### Desarrollo local sin Docker

```bash
cd backend

# Instalar dependencias
pip install -r requirements.txt

# Arrancar el backend en modo recarga automática
uvicorn fastApi.main:app --reload --port 8000
```

En este modo el frontend detecta automáticamente el entorno y apunta a `http://localhost:8000`. Abrir `frontend/index.html` con Live Server (puerto 5500) o similar.

La documentación interactiva Swagger UI está en `http://localhost:8000/docs`.

### Comandos Docker útiles

```bash
# Ver logs en tiempo real de todos los servicios
docker compose logs -f

# Logs de un servicio concreto
docker compose logs -f backend

# Reiniciar un servicio sin reconstruir imagen
docker compose restart backend

# Detener y eliminar contenedores
docker compose down

# Reconstruir solo el backend tras cambios de código
docker compose up --build -d backend
```

---

## Variables de entorno

Crear `backend/.env` a partir de `backend/.env.example`:

```env
# Conexión MongoDB Atlas
MONGO_URI=mongodb+srv://<usuario>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_DB=ev_optimizer

# Ruta al modelo dentro del contenedor backend
MODELO_PATH=modelo/clasificador_precio.pkl

# URL interna del backend para el webhook de ingesta (nombre del servicio Docker)
MODELO_WEBHOOK_URL=http://ev_backend:8000/nuevo_dato

# Configuración de reintentos en pvpc_ingesta.py (valores por defecto si se omiten)
API_MAX_REINTENTOS=3
API_ESPERA_BASE_SEG=5
``` 
> El `docker-compose.yml` carga este fichero mediante `env_file: ./backend/.env`.

---

## Referencia de la API

En producción con Docker la base del path es `/api/` (proxy Nginx). En desarrollo local es `http://localhost:8000/`.

### Sistema

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `GET` | `/health` | Estado de la API, nombre del modelo, accuracy y fecha de entrenamiento |
| `POST` | `/nuevo_dato` | Recibe 24 horas de precios de REE, clasifica con el modelo GB y devuelve el documento enriquecido listo para MongoDB. Llamado internamente por `pvpc_ingesta.py` |

### Autenticación — prefijo `/auth`

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/auth/register` | Registro. Recibe `nombre`, `email`, `password` y perfil del vehículo (`modelo`, `capacidad_bateria_kwh`, `potencia_max_carga_kw`). Contraseña almacenada como SHA-256. Devuelve `409` si el email ya existe |
| `POST` | `/auth/login` | Login. Devuelve id, nombre, email, vehículo y preferencias. Responde `401` con mensaje genérico para no revelar si falla el email o la contraseña |

### Plan de carga — prefijo `/plan_carga`

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/plan_carga` | Genera el plan óptimo. Construye la ventana de planificación con las horas pendientes de hoy y, si están disponibles en MongoDB (~20:15 h), las de mañana. Devuelve `franjas`, `resumen` económico y `para_guardar` listo para `POST /planes` |

**Body de ejemplo:**
```json
{
  "usuario_id": "6650a1b2c3d4e5f6a7b8c9d0",
  "capacidad_total_kwh": 64.0,
  "soc_actual": 20.0,
  "soc_objetivo": 80.0,
  "potencia_kw": 7.4,
  "clasificaciones_permitidas": ["BAJO", "MEDIO", "ALTO"]
}
```

Errores: `404` sin precios · `409` precios no clasificados · `422` `soc_actual >= soc_objetivo`

### Historial de planes — prefijo `/planes`

| Método | Endpoint | Descripción |
|--------|----------|-------------|
| `POST` | `/planes` | Guarda un plan. Requiere `usuario_id`, `nombre_plan`, `franjas`, `coste_estimado_eur`, `emisiones_estimadas_gco2`, `modelo_version` |
| `GET` | `/planes/{user_id}` | Lista todos los planes del usuario, orden descendente por fecha |
| `PUT` | `/planes/{plan_id}/estado` | Activa `{"activo": true}` o desactiva `{"activo": false}` un plan |
| `DELETE` | `/planes/{plan_id}` | Elimina definitivamente un plan del historial |

---

## El modelo de IA

Entrenado en `ev_clasificador.ipynb` con datos PVPC 2020–2025 descargados de la API de REE.

### Features de entrada (8 variables)

| Variable | Descripción |
|----------|-------------|
| `hora` | Hora del día (0–23) |
| `dia_semana` | Día de la semana (0 = lunes) |
| `mes` | Mes (1–12) |
| `es_fin_semana` | Booleano |
| `price_lag_1h` | Precio de la hora anterior |
| `price_lag_24h` | Precio de la misma hora del día anterior |
| `price_rolling_mean_24h` | Media móvil de las últimas 24 horas |
| `price_vs_hour_mean` | Ratio precio actual / media histórica de esa hora |

### Etiquetado (variable objetivo)

| Etiqueta | Criterio | Umbral |
|----------|----------|--------|
| `BAJO` | Precio ≤ percentil 33 | ≤ 0,0830 €/kWh |
| `MEDIO` | Entre percentiles 33 y 67 | — |
| `ALTO` | Precio ≥ percentil 67 | ≥ 0,1723 €/kWh |

División: 80 % entrenamiento / 20 % test con estratificación por clase.

### Comparativa de modelos

| Modelo | Accuracy (test) | CV Mean (5-fold) | CV Std |
|--------|----------------|-----------------|--------|
| Logistic Regression | 0,9325 | 0,9328 | ±0,0017 |
| Random Forest | 0,9776 | 0,9767 | ±0,0011 |
| **Gradient Boosting** ✓ | **0,9967** | **0,9961** | **±0,0009** |

El modelo se serializa con `pickle` junto a sus metadatos y se monta en el contenedor backend como volumen de solo lectura: `./backend/modelo:/opt/ev_cargador/modelo:ro`.

---

## Base de datos

Base de datos `ev_optimizer` en MongoDB Atlas. Inicializar una sola vez:

```bash
python backend/mongo_init_8IA.py
```

| Colección | Descripción | Índice clave |
|-----------|-------------|--------------|
| `precios_electricidad` | Precios PVPC clasificados | Único: `fecha+hora` |
| `precios_actuales` | Precios del día en curso (sin clasificar) | Simple: `fecha` |
| `precios_futuros` | Precios del día siguiente (~20:15 h) | Simple: `fecha` |
| `usuarios` | Perfiles y configuración de vehículo | Único: `email` |
| `planes_de_carga` | Historial de planes generados | Compuesto: `usuario_id+fecha` |
| `registros_carga` | Sesiones completadas | Compuesto: `usuario_id+fecha_inicio` |
| `logs_sistema` | Errores e incidencias de todos los componentes | Compuesto: `timestamp+nivel` |
| `logs_reentrenamiento` | Métricas de cada versión del modelo | Simple: `fecha` |

La conexión se gestiona como singleton con `@lru_cache` en `database.py`, reutilizando el cliente durante toda la vida del proceso.

---

## Ingesta automática de precios

El servicio `ingesta` del `docker-compose.yml` contiene el cron. También se puede lanzar directamente:

```bash
# Ingesta completa (hoy + mañana si son ≥ 20:15 h)
python backend/ingesta/pvpc_ingesta.py

# Solo precios de hoy
python backend/ingesta/pvpc_ingesta.py --solo-hoy

# Solo precios de mañana
python backend/ingesta/pvpc_ingesta.py --solo-futuro
```

Con cron del SO en lugar del contenedor dedicado:

```bash
crontab -e

# Precios del día a las 00:05 h
5  0  * * *  /usr/bin/python3 /ruta/proyecto/backend/ingesta/pvpc_ingesta.py --solo-hoy

# Precios del día siguiente a las 20:20 h
20 20 * * *  /usr/bin/python3 /ruta/proyecto/backend/ingesta/pvpc_ingesta.py --solo-futuro
```

**Resiliencia implementada:**
- Hasta 3 reintentos con backoff exponencial (configurable en `.env`)
- Abort inmediato en errores 4xx no recuperables (excepto 429)
- Fallback al día anterior si todos los reintentos fallan (campo `es_fallback: true` en el documento)
- Validación: 24 horas exactas · sin duplicados · precios en rango [-10, 800] €/MWh
- Todos los eventos (info, warning, error, critical) se registran en `logs_sistema`
- Tras ingesta exitosa, llama automáticamente a `POST /nuevo_dato` para disparar la clasificación

---

## Frontend

SPA en HTML + CSS + JS puro, sin dependencias externas. Nginx sirve los estáticos y hace proxy de `/api/*` al backend, eliminando problemas de CORS en producción.

**Detección automática de entorno:**
```javascript
const API = window.location.hostname === 'localhost' && window.location.port === '5500'
  ? 'http://localhost:8000'   // desarrollo con Live Server
  : '/api';                   // producción Docker
```

**Tres secciones:**
- **Dashboard** — cuadrícula de 24 celdas codificadas por color (🟢 BAJO · 🟡 MEDIO · 🔴 ALTO) y 4 tarjetas resumen
- **Nuevo plan** — formulario de generación con selector de franjas permitidas, gráfico de barras horarias y resumen económico (coste, ahorro, CO₂ evitado)
- **Mis planes** — historial recuperado de `GET /planes/{usuario_id}`

Errores HTTP gestionados por `safeJson()`, notificaciones con toast en esquina inferior.

---

## Resultados

| Métrica | Valor |
|---------|-------|
| Accuracy clasificador (test, 10.522 registros) | **99,67 %** |
| Validación cruzada 5-fold | **99,61 % ± 0,09 %** |
| Datos de entrenamiento | 52.608 registros PVPC 2020–2025 |
| Factor emisiones CO₂ aplicado | 180 gCO₂/kWh (REE 2024) |
| Eficiencia de carga modelada | 90 % (pérdidas térmicas ~10 %) |

---

## Trabajo futuro

- Integración con cargadores físicos mediante protocolo OCPP 1.6/2.0
- Despliegue del stack ELK para dashboards de monitorización en tiempo real
- Incorporación de datos meteorológicos como variable predictora adicional
- Pipeline de reentrenamiento automático periódico con nuevos datos PVPC
- Mejora del frontend con React/Vue y notificaciones push
- Extensión del optimizador para tarifas con más de 3 tramos y V2G (vehicle-to-grid)
- Migración a instancia EC2 de mayor capacidad con balanceador de carga

---

## Autores

| Nombre | Rol |
|--------|-----|
| **Maria Solaz Chávez** | Backend  |
| **Maria Solaz Chávez** | Frontend  |
| **Maria Solaz Chávez** | Infraestructura  |
| **Javier Amaya Moreno** | Datos e IA  |


IES Abastos · Curso Especialización FP en Inteligencia Artificial y Big Data · Curso 2025/26
