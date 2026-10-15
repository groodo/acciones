# Hito B (1.ª parte) — cuenta Paper de IBKR en solo lectura

Objetivo: que Inicio muestre el saldo, las posiciones y las cotizaciones reales de la cuenta **Paper** de IBKR.
Nada de este paso envía órdenes: las órdenes siguen siendo borradores simulados.

## Cómo conectarla

1. Instala **IB Gateway** (versión Stable para macOS) desde la web de Interactive Brokers.
2. Ábrelo, elige **IB API** y **Paper Trading**, e inicia sesión con tu usuario Paper. La contraseña y el 2FA
   se introducen solo en IB Gateway; ni la app ni el servicio los ven.
3. En *Configure → Settings → API → Settings*: marca «Enable ActiveX and Socket Clients», «Read-Only API» y
   «Allow connections from localhost only». El puerto Paper de IB Gateway es **4002**.
4. Abre `AI Trading Desk.app` (generada con `scripts/package_macos.sh`). La app arranca sola el servicio local con
   la fuente IBKR y lo detiene al salir. Si ya hay un servicio escuchando en el puerto, lo usa y no lo toca, así que
   también se puede seguir lanzando a mano:

   ```bash
   TRADINGDESK_DATA_SOURCE=ibkr scripts/run_service.sh
   ```

Variables opcionales: `TRADINGDESK_IB_PORT` (4002 por defecto; 7497 si usas TWS Paper),
`TRADINGDESK_IB_HOST` (solo localhost) y `TRADINGDESK_IB_CLIENT_ID` (17 por defecto).

## Qué cambia

- **Servicio**: nuevo `broker.py` con la TWS API oficial a través de `ib_async`. Corre en un hilo propio, se
  reconecta solo (2, 5, 10 y 30 s) y publica cada 5 s una instantánea de cuenta, posiciones y cotizaciones.
- `/v1/status`, `/v1/account` y `/v1/positions` salen de IBKR con `source="IBKR"` e `is_mock=false`. Señales, curva
  de patrimonio y órdenes siguen siendo `MOCK` y la banda amarilla lo dice.
- Sin IB Gateway, `/v1/account` y `/v1/positions` devuelven 503 con el motivo; la app muestra «Saldo no disponible»
  en lugar de cifras (INICIO-01) y el resto sigue funcionando.
- La variación diaria de una posición solo se muestra si IBKR envía el cierre anterior; si no, aparece «—».

- **Arranque automático**: el `.app` incluye un entorno Python (`Contents/Resources/service-runtime`) con el
  servicio instalado. La app lo lanza si `/health` no responde, lo relanza si cae (como mucho cada 20 s) y lo para al
  salir. Su salida va a `logs/service-stdout.log`. Con `swift run` usa `service/.venv` del repositorio.

## Garantías de seguridad

- Conexión con `readonly=True`. El módulo no llama a `placeOrder`, `cancelOrder` ni similares (lo comprueba un test).
- Si alguna cuenta de la sesión no empieza por `DU` (cuentas Paper), se desconecta y no reintenta.
- Los puertos de cuenta real (4001 de IB Gateway, 7496 de TWS) y los hosts no locales se rechazan al arrancar.
- Los identificadores de cuenta se enmascaran en la API y en los logs.

## Datos de mercado

Se piden con el tipo 3 (diferidos si no hay suscripción). Si la cuenta tiene suscripción en tiempo real, IBKR la
usa y la cabecera muestra «Tiempo real». Sin ella, las cotizaciones llegan con unos 15 minutos de retraso, la
cabecera dice «Diferidas» y cada respuesta lleva un aviso. Las suscripciones en tiempo real son de pago en IBKR.

## Pendiente

- Probar contra una sesión real de IB Gateway Paper (los tests usan un bróker falso).
- Guardar instantáneas de cuenta en `account_snapshots` para la curva de patrimonio real.
- Token de la API en Keychain, WebSocket de eventos y envío de órdenes Paper (siguientes partes del Hito B).
