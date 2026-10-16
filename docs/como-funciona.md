# Cómo analiza y propone Kronos

1. **Qué acciones.**
   - **Mi lista**: los valores que eliges en Oportunidades (máximo 10; al principio AAPL, MSFT,
     NVDA, KO y SPY). Kronos da su opinión sobre cada uno cada 5 minutos.
   - **Recomendadas por el sistema**: cada 15 minutos el escáner de mercado de IBKR busca en el
     Nasdaq los valores con volumen inusual y los que más suben; los 15 primeros pasan por Kronos
     y se muestran los 10 mejores, primero los de «Comprar». Si el escáner no responde, se revisan
     por turnos 50 grandes valores del Nasdaq.
2. **Qué datos.** Velas de 5 minutos de la sesión regular de los últimos 5 días que da IB Gateway
   (apertura, máximo, mínimo, cierre y volumen). Hacen falta al menos 120 velas cerradas y recientes.
3. **Qué hace Kronos-small.** Con las últimas velas (hasta 500) genera 8 trayectorias posibles de
   las próximas 12 velas (60 minutos). De ellas se sacan, para 5, 15, 30 y 60 minutos, la mediana
   (p50) y el rango p10–p90 de la rentabilidad prevista. No son probabilidades calibradas.
4. **Cómo propone** (horizonte de 30 minutos):
   - Tamaño: lo que quepa en 250 € por posición.
   - Costes: 1 $ de comisión por lado y 0,10 % de spread y deslizamiento.
   - **Comprar** solo si el beneficio previsto neto de costes es ≥ 0,50 €, el escenario malo (p10) no
     pierde más de un 1 % y las muestras y la tendencia reciente no se contradicen.
   - **Vender posición** si ya la tienes y la mediana es negativa.
   - Si no, **Esperar**.
5. **Bloqueos.** Datos diferidos, velas viejas o incompletas, o fallo del modelo: la señal queda
   bloqueada o «No evaluable» y nunca se rellena con datos inventados.
