# Hito C — Kronos-small local

Con `TRADINGDESK_DATA_SOURCE=ibkr`, la pestaña Oportunidades deja de mostrar señales de ejemplo:
cada 5 minutos el servicio pide a IBKR las velas de 5 minutos de la lista de vigilancia, ejecuta
Kronos-small en el Mac y convierte la previsión en una propuesta.

## Instalar

```bash
scripts/run_service.sh      # una vez, para crear service/.venv (Ctrl+C después)
scripts/install_kronos.sh   # clona Kronos, instala PyTorch y descarga los pesos (~100 MB)
```

El script fija y anota el commit del repositorio y las revisiones de Hugging Face en
`~/Library/Application Support/AITradingDesk/kronos/kronos.lock.json`, con el SHA-256 de cada archivo.
El servicio comprueba esos hashes antes de cargar el modelo y termina con una inferencia de prueba que
mide la latencia. Para fijar versiones concretas: `KRONOS_COMMIT`, `KRONOS_MODEL_REVISION`,
`KRONOS_TOKENIZER_REVISION`.

Variables del servicio: `TRADINGDESK_WATCHLIST` (máx. 10 símbolos, por defecto `AAPL,MSFT,NVDA,KO,SPY`)
y `TRADINGDESK_SIGNAL_INTERVAL` (segundos, mínimo 60, por defecto 300).

## Cómo se decide

- Entrada: hasta 500 velas cerradas de 5 min de la sesión regular (últimos 5 días). Se descartan velas
  sin cerrar, datos de hace más de 15 minutos o precios inválidos: la señal queda en «No evaluable».
- Kronos genera 8 trayectorias independientes de 12 velas (60 min). Se calculan los percentiles 10, 50
  y 90 de la rentabilidad a 5, 15, 30 y 60 minutos. Son frecuencias de muestras, **no probabilidades
  calibradas**, y no se muestran como «% de confianza».
- Propuesta a 30 minutos con los valores del PRD §9: posición máxima 250 €, comisión 1 USD por lado y
  10 pb de spread y deslizamiento. **Comprar** solo si el neto estimado supera 0,50 €, el percentil 10
  no cae más de un 1 % y las muestras no se contradicen. **Vender posición** si la tienes y la mediana es
  negativa. En otro caso, **Esperar**.
- Con cotizaciones diferidas la señal queda «Bloqueada» (IA-02). Si Kronos falla, «No evaluable» y
  ninguna orden.
- Cada previsión se guarda en la tabla `predictions` con el corte de datos, la huella de las velas, la
  versión del modelo, las trayectorias, la latencia y el dispositivo (IA-01).

## Límites conocidos

- Con 250 € por posición, la comisión fija pesa mucho: un movimiento previsto del 1 % no la cubre y lo
  esperado es que la mayoría de señales sean «Esperar». Es el comportamiento que pide el PRD.
- `amount` se estima como volumen × precio medio porque IBKR no da el importe negociado por vela.
- Las horas futuras se generan sumando 5 minutos, sin saltar el cierre de la sesión.
- No hay todavía backtesting ni comparación con una estrategia base sin IA (Hito E).
- Las señales no envían órdenes por sí solas: la compra Paper sigue pidiendo confirmación manual.
