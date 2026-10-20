# evsim — Documento del proyecto (para quien siga desarrollando)

> Audiencia: una IA o un desarrollador que necesita **entender, modificar o extender** el simulador.
> Para *escribir scripts de prueba* usá `MANUAL.md`. Para el uso rápido, `README.md`.
> Idioma del código y docs: español (identificadores en inglés). Mantenelo.

## 1. Qué es y para qué sirve

`evsim` es un CLI en Python que levanta **N cargadores virtuales OCPP 1.6J** contra un CSMS real, les hace ejecutar
un **script** (`.evs`, estilo gcode) que enchufa autos, arranca/para transacciones y cambia su demanda de corriente, y
**verifica** que el balanceo dinámico de carga (DLB) / smart charging del CSMS calcule bien. Al final genera un reporte.

Caso de uso original (del usuario): límite fijo de sitio (ej. 40 A) → entra un EVSE de 32 A (carga 32) → entra otro
(20/20) → uno baja a 10 porque su auto consume menos (10/30) → etc. El CSMS manda los límites por perfiles de
smart charging (`SetChargingProfile`) y el simulador mide si el resultado es correcto y cuánto tarda.

**Qué NO es:** no es un cargador de producción ni un test de conformidad OCPP, ni prueba firmware. No simula OCPP 2.0.1.

## 2. Decisión de arquitectura (leer antes de "mejorarla")

El repo `MicroOcpp/` (firmware de cargadores, C++) y `MicroOcppSimulator/` (app C++ con GUI web) están clonados al lado
**como referencia**, pero **evsim NO los usa**. Por qué:

1. **MicroOcpp es de una sola instancia por proceso** (`Context *context` global en `src/MicroOcpp.cpp:48`, config y filesystem
   globales). N cargadores en un proceso es imposible; N procesos es pesado y complica el control.
2. El simulador oficial es una app de **un cargador** con GUI/API HTTP, y es **GPL-3.0** (copiar su código contagia la licencia).
3. No había toolchain C++ en la máquina de desarrollo (Windows sin cmake/compilador; WSL Ubuntu sin cmake).
4. Python 3.12 + `websockets` corre nativo en Windows, N cargadores = N corrutinas de `asyncio`, todo JSON.

**Costo asumido:** el resolvedor de perfiles de carga (`smartcharging.py`) es **propio**, no el de MicroOcpp. Está cubierto por
`tests/test_smartcharging.py`, pero si el CSMS y el simulador discrepan en un caso borde, **desconfiá también del simulador**.
La interfaz `ChargePoint` está aislada: se podría enchufar un backend "worker MicroOcpp" (un proceso C++ por cargador) para
comparar contra el resolvedor propio. Es la extensión natural si se quiere fidelidad de firmware (ver Roadmap).

## 3. Estructura

```
evsim_project/
├── README.md  GUIA.md  MANUAL.md  AGENTE.md  PROJECT.md  CLAUDE.md   # CLAUDE.md importa AGENTE.md y MANUAL.md
├── sitio_real.json / sitio_*.json   # configs locales generadas por `evsim init` (tienen la clave de la cuenta: fuera de git)
├── requirements.txt            # websockets>=14 (única dependencia)
├── sitio.example.json          # config de ejemplo; copiar a sitio.json (sitio.json es local del usuario)
├── scripts/*.evs               # escenarios de ejemplo (dlb_basico, dlb_ruido, dlb_llegadas_random, dlb_caida_enlace, demo_fallo)
├── evsim/
│   ├── __main__.py  cli.py     # entrada y subcomandos: init, run, check, list, summary, mock-csms
│   ├── config.py               # carga/validación del JSON → Config/ChargerCfg (dataclasses)
│   ├── script.py               # tokenizer + parser .evs → lista de Step; validación estática; INCLUDE; bloques
│   ├── engine.py               # intérprete: ejecuta Steps, ticker de física, métricas de sitio, EXPECT, ASSERT_ALWAYS
│   ├── charger.py              # ChargePoint: WebSocket OCPP-J, handlers CSMS→cargador, transacciones, MeterValues
│   ├── smartcharging.py        # ChargingProfile/ProfileStore: validación y resolución del límite efectivo
│   ├── vehicle.py              # modelo del auto: rampa, mínimo 6 A, SoC, desviación de medición
│   ├── recorder.py             # registro central: eventos, mensajes, muestras, expectativas
│   ├── report.py               # consola, report.json, CSVs, ventanas de evento
│   ├── report_html.py          # report.html autocontenido (SVG inline + JS), tema claro/oscuro
│   ├── initcfg.py              # `evsim init`: arma la config desde los links de conexion (parseo, enmascarado de la clave)
│   ├── summary.py              # `evsim summary`: resumen de texto de una corrida (lee report.json y messages.log)
│   ├── mockcsms.py             # CSMS de juguete con DLB (para probar evsim sin backend real)
│   └── util.py                 # duraciones, ISO-8601, safe_eval, colores ANSI
├── tests/                      # unittest (python -m unittest discover -s tests -t .)
└── out/                        # reportes generados (ignorar en git)
```

## 4. Flujo de una corrida

1. `cli.cmd_run` → `load_config` (JSON) y `parse_file` (script, validando contra los ids de la config) → `Engine`.
2. `Engine.run()`: aplica `SEED` (hoisted, antes de crear cargadores), crea un `ChargePoint` por cargador, lanza el **ticker**
   (`_tick_loop`, cada 0.25 s) y ejecuta los `Step` con `exec_steps` (verbos = métodos `v_<verbo>`).
3. **Ticker** (`TICK = 0.25`): `cp.tick(dt)` por cargador → resuelve límite efectivo, avanza `Vehicle`, calcula corriente por fase
   medida (con ruido), integra energía, refresca estado del conector (→ `StatusNotification`). Luego `_site_update`: suma por
   fase, energía ideal vs entregada, evalúa invariantes y guarda muestras a 1 Hz.
4. Los `EXPECT_*` hacen *polling* cada 0.1 s hasta `within`. El `Recorder` guarda todo.
5. Al terminar (o abortar) `_cleanup` cierra cargadores; `result()` arma el resumen; `report.write_reports` escribe
   `report.json`, `samples.csv`, `site.csv`, `report.html` (+ `messages.log` que se escribe en streaming).

Todo corre en **un solo hilo/event loop**. No hay locks. No agregues threads; usá corrutinas.

## 5. Conceptos clave y convenciones

- **Tiempo:** el reloj es real (`time.monotonic`), no hay aceleración: el CSMS vive en tiempo real. `Recorder.now()` = segundos desde el inicio.
- **ACTUAL / MEASURED / LIMIT** (nombres en verbos y métricas): *ACTUAL* = corriente física sin ruido (`vehicle.current_a`),
  *MEASURED* = con desviación (lo que va en MeterValues y ve el oráculo físico), *LIMIT* = límite efectivo asignado por perfiles.
- **Fase más cargada:** `SUM_*` = `max(L1, L2, L3)` de la carga del sitio. Un cargador trifásico suma su corriente a las 3 fases;
  uno monofásico a `cfg.phase`. El límite OCPP en A es **por fase**.
- **Estímulos:** los verbos que cambian el mundo (`PLUG`, `START`, `DEMAND`, …) llaman `Engine.stim()` → `Recorder.log(stimulus=True)`.
  El "tras el último evento" de cada EXPECT (convergencia) se mide contra el último estímulo.
- **Fallo:** `record_check` suma a las expectativas; en `strict` aborta con `AbortScript`. Los errores de script (`ScriptError`,
  `ScriptRuntimeError`) son **fatales** en ambos modos y dan status `error` (exit code 3 / 2 según origen).
- **Invariantes:** cada tick violado dura `TICK` (0.25 s); `duración = fin − inicio + TICK`; fallo si `> grace`.
- **Declaraciones hoisted:** sólo `SEED`. El resto (`MODE`, `TOLERANCE`, …) se ejecuta en orden. `--mode`/`--seed` del CLI pisan el script.
- **Config:** claves que empiezan con `_` son comentarios (se eliminan al cargar). Los errores de config llevan la ruta JSON (`chargers[1].max_a: ...`).
- **Mensajes:** `Recorder.msg()` lleva contadores por (cargador, acción, dirección) — de ahí salen `EXPECT_MSG`, `WAIT_MSG`, ventanas de evento.
- **Estilo de código:** comentarios y mensajes de usuario en español; docstrings que explican el *por qué*; sin dependencias nuevas sin motivo.

## 6. `charger.py` en detalle (la pieza más delicada)

- **Sesión:** `_run` mantiene la conexión con reconexión automática (`reconnect_s`); `_session` abre el WS (subprotocolo
  `ocpp1.6`, Basic auth si corresponde), lanza `reader`, hace `_boot` (BootNotification con reintento si `Pending/Rejected`),
  y luego `sender` + `heartbeat`. Todas las tareas hijas se cancelan y recogen en `finally` (evita "Task exception was never retrieved").
- **Cola de salida (`self.queue`):** `StatusNotification`, `StartTransaction`, `StopTransaction`, `MeterValues` van encolados y un
  único `_sender` los emite **de a uno, en orden, esperando la respuesta** (OCPP permite un CALL pendiente a la vez). Si se pierde la
  conexión los mensajes quedan en la cola y salen al reconectar. Los `build()` son lambdas para que el `transactionId` se complete
  recién cuando llega el `StartTransaction.conf`. `call()` directo se usa para `BootNotification`, `Heartbeat`, `Authorize`.
- **Handlers CSMS→cargador:** métodos `h_<Acción>(payload)` que devuelven el payload de respuesta, o `(payload, after)` si hay trabajo
  diferido (se ejecuta *después* de enviar la respuesta). Acción sin handler → `CALLERROR NotImplemented`.
- **Transacciones:** `Connector.tx` (objeto `Transaction`) existe desde `START` aunque el id llegue después. `_stop_tx` borra
  los `TxProfile`, encola `StopTransaction` y pone `Finishing` si sigue enchufado. `StopTransactionOnEVSideDisconnect` aplicado en `unplug`.
- **Estados:** `_compute_status` deriva el estado OCPP del conector (falla > inoperativo > transacción/vehículo > enchufado > disponible).
  `_refresh_status` emite `StatusNotification` sólo ante cambios.
- **MeterValues:** `meter_loop` (por cargador) dispara según `MeterValueSampleInterval` (clave OCPP editable por el CSMS).
  Medidas soportadas en `config.SUPPORTED_MEASURANDS`; ver `_sampled` para agregar una.
- **`DROP_LINK`:** `transport.abort()` del WS (sin handshake de cierre) + `_block_until` para no reconectar inmediatamente.
- **`drain()`** antes de cerrar: espera a que se envíe la cola (si no, un `STOP` seguido de `DISCONNECT` perdía el `StopTransaction`).

## 7. `smartcharging.py`

Implementa la parte de OCPP 1.6 §3.13 que importa al DLB. `ProfileStore.set()` valida y devuelve `Accepted|Rejected` (`last_reason`
guarda el motivo para el log). `effective()` resuelve así: mejor `TxProfile` activo (mayor stack) → si no hay, mejor `TxDefaultProfile`
(específico o del conector 0) → `min` con el mejor `ChargePointMaxProfile`. `composite()` arma `GetCompositeSchedule` evaluando en todos
los bordes de períodos/recurrencias/validez dentro de la ventana. **No implementado:** `minChargingRate`, persistencia de perfiles entre
corridas, reglas de `numberPhases` más allá de limitar fases en uso.

## 8. Cómo extender

**Agregar un verbo al script**
1. `script.py` → `VERBS["NUEVO"] = (min_pos, max_pos|None, {claves permitidas}, "descripción")`. Si su primer argumento es un
   cargador agregalo a `TARGET_FIRST` (o a `TARGET_ALL` si son varios); si compara con operador, a `OP_INDEX`.
2. `engine.py` → `async def v_nuevo(self, st, a, kw)`. Usá `self.num()`, `self.dur()`, `self.targets()/one()` para parsear. Si cambia el
   mundo llamá `self.stim(...)`. Si es una verificación, terminá en `self.record_check(...)` (o usá `check`/`check_eq`).
3. Documentalo en `MANUAL.md` (tabla del verbo + patrón si aplica) y agregá un test en `tests/`.

**Agregar un measurand de MeterValues:** `config.SUPPORTED_MEASURANDS` + rama en `ChargePoint._sampled`.
**Agregar un mensaje CSMS→cargador:** método `h_<Acción>` en `ChargePoint`.
**Agregar una métrica de invariante:** rama en `Engine.metric()` (+ documentarla en el MANUAL).
**Agregar una sección al reporte HTML:** `report_html.py` (placeholders `__X__` en `PAGE` + armado en `render_html`).
**Cambiar la política del mock DLB:** `MockCSMS._allocate/rebalance` (es un juguete; no lo uses como referencia de producción).

## 9. Mock CSMS (`mockcsms.py`)

Existe para poder probar el simulador sin un backend. `python -m evsim mock-csms --limit 40` escucha en `ws://127.0.0.1:9000/ocpp/<id>`.
Reparte el límite parejo con tope por cargador; si un cargador consume menos de lo ofrecido (y la medición es estable y posterior a
`settle_s`) lo considera "limitado por el auto", le asigna lo medido (mínimo `min_a` = 6 A) y reparte el sobrante. Se reinicia en cada
Start/StopTransaction. **Sirvió para descubrir clases de bug reales de DLB** (tomar la rampa de subida como demanda; asignar menos de
6 A y suspender al auto para siempre; leer una medición vieja tras subir el límite) — esos tres están en la historia de este archivo y
son buenos casos de prueba para el CSMS real.

## 10. Tests

```
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```
- `test_smartcharging.py`: resolvedor y validación de perfiles. `test_script.py`: parser, util, vehículo/desviación.
- `test_integration.py`: corre un escenario completo contra un `MockCSMS` en proceso (puerto efímero) y chequea el resultado.
- Los escenarios `scripts/*.evs` son *también* pruebas de humo contra el mock (`dlb_basico` debe pasar; `demo_fallo` debe fallar).

## 11. Gotchas / cosas que mordieron

- **Dos corridas simultáneas contra el mismo CSMS** comparten `chargeBoxId` y se pisan (resultados falsos). Una a la vez.
- **Windows:** `asyncio` usa Proactor (OK). `Ctrl+C` genera un reporte parcial con status `aborted`.
- **`websockets` ≥14** (API nueva `websockets.connect` con `additional_headers`; la conexión expone `.transport`). No usar la API legacy.
- El ticker usa `dt` real: bajo carga de CPU la física se vuelve más gruesa pero no se desincroniza.
- `EXPECT_*` con valor igual al actual pasan al instante (por diseño); es responsabilidad del script (ver MANUAL §7).
- `messages.log` y las muestras crecen con la duración; una corrida de horas con muchos conectores genera archivos grandes.
- Los heredocs de bash con comillas simples/`%` dentro de comandos de la herramienta fallaron al generar archivos; en este entorno
  se usó la herramienta de escritura de archivos.

## 11b. Verificado contra un CSMS real (WEVE; entorno de prueba, cuenta de un desarrollador)

Público objetivo: el **equipo web (no expertos en electricidad)**, cada uno con su cuenta y sus estaciones. Por eso `AGENTE.md` pide crear la estación y los links, y `GUIA.md` explica con glosario.

Corridas `smoke_estados` y `mantener_conectado` (cargadores `DLB_01`/`DLB_02`, idTag `plug_and_play`): conexión `wss://` con token en el path,
Boot/Status/Authorize/Start/MeterValues/Stop correctos, estados vistos en vivo en la plataforma. El CSMS hace `GetConfiguration` al conectar y
**no mandó `SetChargingProfile`** hasta ahora (falta confirmar si los cargadores están en un grupo DLB con límite). La plataforma calcula la
energía de la sesión con el último `MeterValues` periódico (no con `meterStop`). **No probado todavía contra el CSMS real:** Basic auth, wss con
certificado propio, y ningún escenario con DLB activo. La guía para el agente que conduce las sesiones es `AGENTE.md` (mantenela al día si cambia el entorno).

## 12. Roadmap sugerido

1. `live`: consola interactiva que ejecuta verbos tipeados (el parser ya soporta una línea suelta).
2. Backend MicroOcpp (proceso C++ por cargador, control por stdin/socket) para comparar el resolvedor propio con el real.
3. Comparar corridas (`evsim diff out/a out/b`) y modo `--repeat N` con estadísticas de convergencia.
4. Soporte `minChargingRate`, perfiles persistentes, `ReserveNow`, OCPP 2.0.1 (`SetChargingProfile` de 2.0.1 / `NotifyEVChargingNeeds`).
5. Pipeline CI (GitHub Actions/Azure DevOps): levantar el CSMS de prueba, `evsim run` de cada script, publicar `out/` como artefacto, fallar por exit code.
6. Generación asistida de scripts: un `evsim new` que parta de una descripción y use `MANUAL.md` como contexto.
