# Cuenta real de IBKR en solo lectura

Permite ver en la app el saldo, las posiciones y las cotizaciones de tu cuenta
real de Interactive Brokers, y que Kronos analice velas reales. **La app no puede
enviar ni cancelar órdenes con esta conexión**:

- La configuración rechaza arrancar con la cuenta real y el envío de órdenes activado,
  y lo desactiva aunque `TRADINGDESK_PAPER_ORDERS=1`.
- El conector se abre con `readonly=True` y nunca habilita el envío; además `submit`
  exige una cuenta Paper (`DU…`).
- `POST /v1/orders/paper` responde 409 con la cuenta real.
- La cabecera muestra «Cuenta: REAL · SOLO LECTURA» en rojo y la API marca los datos con `mode: "live"`.

## Activarla en el Mac

1. Abre IB Gateway e inicia sesión con tu usuario **real** («Negociación real» / Live Trading).
2. En IB Gateway, *Configure → Settings → API → Settings*:
   - «Enable ActiveX and Socket Clients» activado.
   - **«Read-Only API» activado** (segunda barrera: el propio Gateway rechaza órdenes).
   - Puerto `4001` (o `7496` si usas TWS) y «Allow connections from localhost only».
3. Elige la cuenta real para la app:

   ```sh
   echo real-readonly > ~/Library/Application\ Support/AITradingDesk/ib-account
   ```

   (o exporta `TRADINGDESK_IB_ACCOUNT=real-readonly` si arrancas el servicio a mano).
4. Cierra y vuelve a abrir la app.

Para volver a Paper: `echo paper > ~/Library/Application\ Support/AITradingDesk/ib-account`,
inicia sesión Paper en IB Gateway (puerto 4002) y reabre la app.

## Datos en tiempo real

Sin la suscripción de datos en tiempo real del mercado de EE. UU. en IBKR, las
cotizaciones llegan diferidas y las señales de Kronos quedan bloqueadas (IA-02).

## Órdenes reales (`real-trading`)

Autorizado por escrito por el titular el 16-10-2026: «Confirmo órdenes reales con
confirmación manual y un máximo de 1000€ por orden».

- Se activa con `real-trading` en el archivo `ib-account` (o `TRADINGDESK_IB_ACCOUNT`) y
  desmarcando «Read-Only API» en IB Gateway.
- Solo órdenes **limitadas**, válidas en la sesión del día, y solo tras confirmar en la app
  (hoja de la orden + diálogo «¿Enviar esta orden REAL?»). Nada se envía de forma automática:
  Kronos solo propone.
- Tope por orden en euros con el tipo de cambio de IBKR: `TRADINGDESK_MAX_REAL_ORDER_EUR`,
  1000 € por defecto y como máximo (subirlo exige cambiar el código).
- Sin ventas en corto, precio límite a menos de un 5 % de la referencia, «Detener operativa»
  bloquea nuevas órdenes, y el conector rechaza enviar si la sesión de IB Gateway es Paper.

Para volver a solo lectura: `echo real-readonly > ~/Library/Application\ Support/AITradingDesk/ib-account`
y reabrir la app.
