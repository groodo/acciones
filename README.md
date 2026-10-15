# AI Trading Desk (macOS)

Aplicación nativa de macOS para supervisar una cuenta **IBKR Paper** con señales de **Kronos-small** ejecutado en local.
El PRD (v1.0, 15/10/2026) no se versiona en este repositorio público; las notas de cada hito están en [docs/](docs/).

> **Estado: Hito A (estructura funcional).** Todo lo que muestra la aplicación son **DATOS SIMULADOS**.
> No hay conexión con Interactive Brokers ni modelo de IA, y no existe ninguna ruta de código capaz de enviar una orden a un bróker.

## Estructura

```text
app/        App SwiftUI (Swift Package): TradingDeskCore (modelos, cliente API, formato) + TradingDeskApp (UI)
service/    Servicio Python local: API HTTP en 127.0.0.1, SQLite WAL, logging JSON, máquina de estados de órdenes
scripts/    Arranque del servicio, tests y empaquetado .app
docs/       Notas del Hito A: decisiones técnicas y límites conocidos
```

## Requisitos

- macOS 14 o posterior en Apple Silicon, con Xcode 15+ (o las Command Line Tools con Swift 5.9+).
- Python 3.11 o posterior.

## Ejecutar

1. Arranca el servicio local (la primera vez crea `service/.venv` e instala dependencias):

   ```bash
   scripts/run_service.sh
   ```

   Escucha en `http://127.0.0.1:8765`. Al arrancar genera un token aleatorio en
   `~/Library/Application Support/AITradingDesk/api-token` (permisos 0600); la app lo lee de ahí.
   La base de datos y los logs (`logs/service.jsonl`) viven en la misma carpeta.

2. En otra terminal, lanza la app:

   ```bash
   cd app && swift run AITradingDesk
   ```

   O ábrela en Xcode con `open app/Package.swift` y ejecuta el esquema `AITradingDesk`.

3. Para generar `AI Trading Desk.app` (sin firmar): `scripts/package_macos.sh` (deja el resultado en `dist/`).

Variables opcionales del servicio: `TRADINGDESK_PORT`, `TRADINGDESK_DATA_DIR`, `TRADINGDESK_LOG_LEVEL`.
Si cambias el puerto o la carpeta de datos, exporta las mismas variables antes de lanzar la app.
`TRADINGDESK_MODE` solo acepta `paper`: cualquier otro valor detiene el arranque.

## Tests

```bash
scripts/test_all.sh
```

Ejecuta los tests del servicio (pytest) y, en macOS, `swift build` y `swift test` de la app.
La integración continua (`.github/workflows/ci.yml`) hace lo mismo: Python en Linux y la app en un runner macOS, donde además se empaqueta el `.app`.

## API local

Todas las rutas salvo `/health` exigen `Authorization: Bearer <token>`. Se rechazan peticiones con cabecera `Origin` (navegadores) o con un `Host` distinto de localhost.
Cada respuesta va envuelta en `{source, is_mock, mode, generated_at, notice, data}` para que la UI nunca confunda datos simulados con reales.

| Método | Ruta | Descripción |
| --- | --- | --- |
| GET | `/health` | Comprobación de vida (sin token) |
| GET | `/v1/status` | Cabecera: bróker, modo, mercado, cotizaciones, kill switch |
| GET | `/v1/account` | `AccountSnapshot` (PRD §7) |
| GET | `/v1/positions` | Posiciones abiertas |
| GET | `/v1/equity-curve?range=hoy\|7d\|30d\|todo` | Curva de patrimonio; `sufficient=false` si no hay histórico |
| GET | `/v1/signals` | Tabla de oportunidades |
| GET | `/v1/orders` | Órdenes (`?open_only=true` para abiertas) |
| GET | `/v1/orders/{id}/events` | Cronología de estados de una orden |
| POST | `/v1/orders/simulated` | Crea un borrador `DRAFT` simulado (idempotente por `client_order_id`) |
| POST | `/v1/orders/{id}/cancel` | Cancela un borrador |
| GET/POST | `/v1/kill-switch` | Consulta o activa «Detener operativa» |
