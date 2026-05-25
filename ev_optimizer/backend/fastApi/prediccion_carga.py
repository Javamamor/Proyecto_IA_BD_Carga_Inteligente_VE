import math

def generar_plan_carga(datos_red, config_usuario, capacidad_total_kwh):
    """
    Genera un plan de carga optimizado al momento de enchufar el vehículo.
    Calcula el ahorro, la viabilidad y requiere autorización para horas altas.
    """
    # 1. Parámetros iniciales
    soc_actual = config_usuario['soc_actual'] 
    soc_objetivo = config_usuario['soc_objetivo'] 
    potencia_kw = config_usuario['potencia_kw'] 
    # Eficiencia de carga (pérdidas por calor ~10%)
    EFICIENCIA = 0.9 
    
    energia_necesaria_kwh = ((soc_objetivo - soc_actual) / 100) * capacidad_total_kwh
    incremento_soc_hora = (potencia_kw * EFICIENCIA / capacidad_total_kwh) * 100
    
    # 2. Filtrar y clasificar horas disponibles por precio ascendente 
    # Separamos en pools para respetar la jerarquía de ahorro
    pool_bajas = sorted([h for h in datos_red if h['tipo'] == 'B'], key=lambda x: x['precio']) 
    pool_medias = sorted([h for h in datos_red if h['tipo'] == 'M'], key=lambda x: x['precio']) 
    pool_altas = sorted([h for h in datos_red if h['tipo'] == 'A'], key=lambda x: x['precio']) 

    plan_final = []
    soc_simulado = soc_actual
    coste_total = 0.0
    
    # 3. Función auxiliar para llenar el plan por categorías
    def asignar_horas(pool, estado_inicial_soc):
        nonlocal soc_simulado, coste_total
        horas_asignadas = []
        for hora in pool:
            if soc_simulado < soc_objetivo:
                soc_simulado = min(soc_simulado + incremento_soc_hora, soc_objetivo)
                coste_hora = potencia_kw * hora['precio']
                coste_total += coste_hora
                horas_asignadas.append({
                    "hora": hora['hora'],
                    "tipo": hora['tipo'],
                    "precio": hora['precio'],
                    "coste_tramo": round(coste_hora, 2),
                    "soc_alcanzado": round(soc_simulado, 2)
                })
        return horas_asignadas

    # Ejecución de la cascada de eficiencia
    plan_eco = asignar_horas(pool_bajas, soc_simulado)
    plan_eco += asignar_horas(pool_medias, soc_simulado)
    
    soc_alcanzado_eco = soc_simulado
    necesita_altas = soc_simulado < soc_objetivo
    
    # 4. Cálculo de emergencia (Horas Altas) 
    plan_emergencia = []
    if necesita_altas:
        plan_emergencia = asignar_horas(pool_altas, soc_simulado)

    # 5. Respuesta estructurada para el Front-end
    return {
        "status": "success",
        "resumen": {
            "soc_actual": soc_actual,
            "soc_objetivo": soc_objetivo,
            "soc_final_estimado": round(soc_simulado, 2),
            "viabilidad_economica": not necesita_altas,
            "coste_total_estimado": round(coste_total, 2),
            "horas_totales_carga": len(plan_eco) + len(plan_emergencia)
        },
        "plan_eco": plan_eco,
        "plan_emergencia": {
            "requiere_autorizacion": necesita_altas,
            "horas_altas": plan_emergencia,
            "mensaje": "Se necesitan horas altas para alcanzar el objetivo" if necesita_altas else "Objetivo cubierto con horas B/M"
        }
    }