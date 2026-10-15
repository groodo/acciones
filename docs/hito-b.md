# Hito B — cuenta Paper de IBKR

Objetivo: que la app muestre el saldo, las posiciones y las cotizaciones de la cuenta **Paper** de IBKR y permita
enviar órdenes limitadas a esa cuenta de práctica. No existe ninguna ruta hacia una cuenta real.

## Cómo conectarla

1. Instala **IB Gateway** (versión Stable para macOS) desde la web de Interactive Brokers.
2. Ábrelo, elige **IB API** y **Paper Trading**, e inicia sesión con tu usuario Paper. La contraseña y el 2FA
   se introducen solo en IB Gateway; ni la app ni el servicio los ven.
3. En *Configure → Settings → API → Settings*: marca «Enable ActiveX and Socket Clients» y «Allow connections from
   localhost only». Para enviar órdenes Paper, **desmarca «Read-Only API»**; si la dejas marcada, la app solo lee y
   IBKR rechaza las órdenes. El puerto Paper de IB Gateway es **4002**.
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

## Órdenes Paper

`POST /v1/orders/paper` (y la hoja «Nueva orden Paper» de la app) crea una orden **limitada** `DAY` en la cuenta
Paper. Antes de enviarla se comprueba, en este orden:

1. Que la operativa no está detenida (423) y que IB Gateway está conectado (503).
2. Que el usuario la ha confirmado (`confirmed=true`; la app lo envía solo tras el diálogo de confirmación).
3. Que el importe (`cantidad × límite`) no supera `TRADINGDESK_MAX_ORDER_VALUE` (1000 por defecto).
4. Que las ventas no superan las acciones que se tienen (sin cortos) y que la cantidad es entera.
5. Que el precio límite está a menos de un 5 % del precio de referencia de IBKR. Sin precio de referencia, se rechaza.

Si algo falla, no se crea ni se envía nada (422). Si pasa, la orden recorre `DRAFT → RISK_APPROVED → SUBMITTING`
y desde ahí la mueve IBKR: `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED` o `REJECTED` (con el motivo de
IBKR, por ejemplo «Read-Only API»). El `client_order_id` viaja como `orderRef`: repetir la petición devuelve la misma
orden sin reenviarla. Al reconectar, las órdenes que IBKR no reconoce pasan a `UNKNOWN` y nunca se reenvían solas.
`POST /v1/orders/{id}/cancel` pide a IBKR la cancelación de una orden aceptada; el estado cambia cuando IBKR la confirma.

Para volver a solo lectura: `TRADINGDESK_PAPER_ORDERS=0`.

## Garantías de seguridad

- Sin órdenes habilitadas, la conexión es `readonly=True`. `placeOrder` aparece una sola vez, dentro de `submit`, que
  vuelve a comprobar que la cuenta es Paper antes de cada envío (lo comprueban los tests).
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
- Token de la API en Keychain y WebSocket de eventos.
- Límites diarios (número de órdenes y pérdida máxima) y motor de riesgo completo (Hito D).
- Probar el envío con una orden real en la cuenta Paper (los tests usan un bróker falso).
