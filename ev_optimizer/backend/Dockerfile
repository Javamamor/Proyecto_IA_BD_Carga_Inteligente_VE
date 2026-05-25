FROM python:3.9-slim

WORKDIR /opt/ev_cargador

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt está en backend/ (raíz del context)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY fastApi/ ./fastApi/

# Copiar el modelo
COPY modelo/ ./modelo/

# Crear carpeta de datos
RUN mkdir -p data

WORKDIR /opt/ev_cargador/fastApi

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
