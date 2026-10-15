# Hito A — estructura funcional

Objetivo (PRD §15): monorepo, SwiftUI con datos mock explícitos, API local, SQLite, pruebas, logging,
documentación de ejecución y empaquetado macOS. **Aceptación:** UI usable sin IBKR y mocks claramente etiquetados.

## Qué incluye

- **Servicio Python** (`service/`): FastAPI + uvicorn, solo en `127.0.0.1`, token Bearer aleatorio (archivo 0600),
  rechazo de `Origin` y de `Host` ajenos. Toda respuesta lleva `source="MOCK"`, `is_mock=true` y un aviso.
- **Persistencia**: SQLite en modo WAL con migraciones versionadas (`PRAGMA user_version`), transacciones con
  rollback, importes como `TEXT` decimal. Tablas: `orders`, `order_events`, `account_snapshots`, `predictions`
  (vacía, para Hito C), `audit_log`, `settings`.
- **Máquina de estados de órdenes** (PRD §7): `DRAFT → RISK_APPROVED → SUBMITTING → ACKNOWLEDGED →
  PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/UNKNOWN`. `UNKNOWN` solo sale por reconciliación y nunca vuelve a
  `SUBMITTING` (sin reenvío automático). `client_order_id` idempotente. Cada transición queda en `order_events` y `audit_log`.
- **Kill switch** persistente: bloquea nuevas entradas (HTTP 423), lista órdenes y posiciones que siguen activas
  y no promete liquidarlas (SAFETY-01). Sobrevive a reinicios.
- **Bloqueo de modo real** (SAFETY-02): el servicio no arranca con `TRADINGDESK_MODE` distinto de `paper`,
  las órdenes `live` se rechazan en la capa de datos y no existe ninguna ruta de envío a bróker.
- **Logging** JSON por línea con rotación, redacción de tokens/contraseñas y enmascarado de IDs de cuenta IBKR.
- **Reloj de mercado** con zona `America/New_York` (horario de verano incluido).
- **App SwiftUI** (`app/`): barra lateral (Inicio, Oportunidades, Operaciones, Rendimiento, Configuración),
  cabecera persistente con estado y botón «Detener operativa», banda amarilla fija «DATOS SIMULADOS».
  Importes con signo, texto y color (nunca solo color). Sin servicio, no muestra saldos (INICIO-01).
  «Datos insuficientes» en lugar de una curva inventada. Sin botón de operación real.
- **Tests**: 66 de pytest (rutas peligrosas primero) y XCTest para decodificación, formato y cliente.
- **CI**: Python 3.11–3.13 en Linux; `swift build`, `swift test` y empaquetado `.app` en macOS.

## Decisiones técnicas

- **FastAPI** para la API local: validación con Pydantic y contratos tipados que reflejan el PRD §7.
- **HTTP + sondeo cada 15 s** en lugar de WebSocket: suficiente para datos simulados. El WebSocket de eventos llega con el streaming de IBKR (Hito B).
- **Token en archivo 0600** en `~/Library/Application Support/AITradingDesk/`. El PRD pide Keychain; se migrará en Hito B.
- **Swift Package** en lugar de proyecto Xcode: se compila y prueba desde la línea de comandos y en CI. Xcode lo abre directamente.
- **Decimal como texto** en JSON y SQLite para no perder precisión contable.

## Límites conocidos

- Todo son datos simulados y deterministas: no hay IBKR, Kronos, ledger, FX real ni motor de riesgo.
- El reloj de mercado no contempla festivos ni cierres anticipados.
- La app no arranca ni supervisa el servicio: hay que lanzarlo con `scripts/run_service.sh` (supervisión con launchd en hitos posteriores).
- El `.app` no está firmado ni notarizado (Hito F) e incluye el código del servicio solo como referencia.
- Las métricas de Rendimiento se muestran como «No concluyente».
- Pendiente de validar en un Mac real: capturas de pantalla y prueba de usabilidad de diez segundos (UX-01).
