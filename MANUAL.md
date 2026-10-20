# evsim — Manual para generar scripts de prueba (.evs)

> **Para quién es este documento:** una IA (o una persona) que tiene que **escribir scripts `.evs`** que prueben el
> balanceo dinámico de carga (DLB) / smart charging de un CSMS OCPP 1.6J. No hace falta leer el código.
> Si necesitás modificar el simulador en sí, leé `PROJECT.md`. Si sos el agente que guía una sesión de pruebas, el protocolo completo está en `AGENTE.md`.

---

## 0. Protocolo para la IA (leer primero)

1. **Pedí o deducí** estos datos antes de escribir el script:
   - ¿Cuántos cargadores y cuáles ids hay en `sitio.json`? (`python -m evsim list`). **Solo podés usar ids que existan.**
   - ¿Cuál es el límite del sitio en A y el tope físico de cada cargador (`max_a`)?
   - ¿Qué comportamiento esperado tiene el CSMS (reparto parejo, prioridades, mínimo de 6 A, etc.)?
2. **Escribí el script** siguiendo los patrones de la sección 6.
3. **Validalo siempre** antes de entregarlo: `python -m evsim check mi_script` (no conecta nada, devuelve errores con `archivo:línea`).
4. Si tenés acceso a un CSMS de prueba, corré `python -m evsim run mi_script`; si no, **no inventes resultados**: entregá el script y decí que no se ejecutó.
5. Guardá los scripts en `scripts/` con nombre `snake_case.evs` y un `SCENARIO` descriptivo en la primera línea útil.
6. Comentá el *por qué* de cada fase (`; comentario`), no el *qué*.

Reglas de oro (los errores más comunes de los scripts generados, ver §7):

- **Un `EXPECT_LIMIT` puede cumplirse con un valor viejo.** Si el valor esperado ya es el actual, el chequeo pasa al instante y no prueba nada. Elegí valores que *cambien* tras el evento, o usá `WAIT_UNTIL_STABLE` + `EXPECT_*`.
- **Los autos tardan en reaccionar** (rampa de 5 A/s por defecto): un `EXPECT_ACTUAL` justo después de un cambio necesita `within=` generoso o un `WAIT_UNTIL_STABLE` antes.
- **Con desviación (ruido) en el medidor, usá `tol=`.** Con `tol=0` un `==` casi nunca se cumple sobre corriente medida.
- **Un auto no carga por debajo de 6 A** (`vehicle.min_a`): si el CSMS ofrece 5 A, el conector pasa a `SuspendedEVSE` con 0 A.
- Todos los `EXPECT` esperan hasta `within=` (default 10 s) y **recién ahí fallan**; un script con muchos EXPECT fallidos tarda `n × within`.

---

## 1. Cómo se corre

```
python -m evsim list                          # scripts y cargadores disponibles
python -m evsim check scripts/dlb_basico.evs  # valida sintaxis y referencias (no conecta)
python -m evsim run   dlb_basico              # corre y genera el reporte en out/<fecha>_<script>/
python -m evsim run   dlb_basico --mode soft  # no corta en el primer fallo
python -m evsim run   dlb_basico --seed 5     # fuerza la semilla (pisa SEED del script)
python -m evsim summary                       # resumen de TEXTO de la ultima corrida (fallos, convergencia, cada SetChargingProfile del CSMS)
python -m evsim mock-csms --limit 40          # CSMS de juguete con DLB, para probar el simulador
```

`run` acepta una ruta o un nombre dentro de `scripts/` (con o sin `.evs`).
Códigos de salida: `0` todo pasó · `1` falló alguna verificación (o se cortó) · `2` error de config/script · `3` error de ejecución (ej. no conectó).

El reporte (`out/<fecha>_<script>/`): `report.html` (gráficos y tablas), `report.json`, `messages.log` (todo el OCPP crudo),
`samples.csv` (1 Hz por conector), `site.csv`.

---

## 2. Sintaxis

Una instrucción por línea: `VERBO arg arg clave=valor`. Reglas:

| Elemento | Regla |
|---|---|
| Comentarios | `;` hasta el fin de línea (salvo dentro de comillas) |
| Mayúsculas | Los **verbos** no distinguen mayúsculas. Los ids de cargador **sí** (`A` ≠ `a`). Las variables no distinguen. |
| Texto con espacios | entre comillas: `"Reparto básico"` o `'...'` |
| Parámetros con nombre | `clave=valor` sin espacios (`within=20s`, `tol=0.5`, `idtag=TAG1`) |
| Operadores de comparación | `==  !=  <=  >=  <  >` **siempre separados por espacios** |
| Duraciones | `500ms`, `20s`, `2m`, `1h`, o un número pelado = segundos |
| Números | `32`, `13.3`, o aritmética sin espacios: `40/3`, `max(6,40/4)`. Funciones: `min max abs round int float` |
| Variables | `SET n 10` o `SET n=10`; uso `$n` o `${n}` (en cualquier argumento) |
| Objetivos | `A` (conector 1), `A.2` (conector 2 del cargador A), `ALL` (todos) |
| Listas en parámetros | separadas por coma sin espacios: `chargers=A,B,C` · `of=A,B` |

El lenguaje **no tiene `IF`**. Para ramas condicionales, escribí scripts distintos.

---

## 3. Referencia de verbos

### 3.1 Declaraciones (conviene ponerlas arriba)

| Verbo | Qué hace |
|---|---|
| `SCENARIO "nombre"` | Nombre que aparece en el reporte. |
| `DESCRIPTION "texto"` | Descripción libre. |
| `SEED n` | Semilla del azar (ruido del medidor, `RANDOM_ARRIVALS`). Se aplica **antes** de crear los cargadores. `--seed` en CLI la pisa. Default 1. |
| `MODE strict\|soft` | `strict` (default): el **primer** EXPECT/invariante fallido corta el script. `soft`: sigue y lista todos los fallos. `--mode` en CLI pisa este valor. |
| `SET_SITE_LIMIT amps` | Límite del sitio que usa el oráculo (energía ideal, sobrecarga). Default: `site.limit_a` del config. **Es solo del simulador**: el límite real lo configurás en tu CSMS. |
| `TOLERANCE amps` | Tolerancia por defecto de los `EXPECT_*` (default **0.5 A**). |
| `DEFAULT_WITHIN 15s` | Plazo por defecto de los `EXPECT_*` (default **10 s**). |
| `SET nombre valor` / `SET nombre=valor` | Define una variable numérica o de texto. |
| `PRINT texto` | Imprime en consola y lo guarda en el registro de eventos. |
| `MARK "texto"` | Hito: dibuja una línea vertical rotulada en los gráficos. **Usalo en cada fase.** |
| `SNAPSHOT [nombre]` | Guarda el estado de todos los cargadores (límites, corrientes, perfiles instalados) en `report.json`. |

### 3.2 Ciclo de vida de los cargadores

| Verbo | Parámetros | Qué hace |
|---|---|---|
| `CONNECT A B ...` | `timeout=15s` | Abre el WebSocket y espera `BootNotification Accepted`. Error fatal si no se acepta a tiempo. Admite `ALL`. |
| `DISCONNECT A ...` | | Vacía la cola de mensajes pendientes (≤3 s) y cierra limpio. |
| `DROP_LINK A ...` | `for=10s` | Corta el WebSocket **sin cierre limpio** y no reconecta durante `for`. Después reconecta solo (auto_reconnect) y manda `BootNotification` de nuevo. Mientras tanto el cargador sigue con el último perfil. |

### 3.3 Vehículo y conector

| Verbo | Parámetros | Qué hace |
|---|---|---|
| `PLUG A [B ...]` | | Enchufa el auto → `StatusNotification Preparing`. Admite varios objetivos / `ALL`. |
| `UNPLUG A [B ...]` | | Desenchufa. Si hay transacción: `StopTransaction` con motivo `EVDisconnected`. |
| `START A` | `idtag=TAG` | Requiere `PLUG` previo. Envía `Authorize` (configurable) + `StartTransaction` y **espera la confirmación** (≤10 s). Si el idTag es rechazado lo registra como nota (no corta el script). |
| `STOP A [B ...]` | `reason=Local` | `StopTransaction`. Con `ALL` solo detiene los que están cargando. Con un id explícito sin transacción activa es error. |
| `DEMAND A amps` | `over=30s` | Corriente **máxima por fase que acepta el auto**. Sin `over` es instantáneo; con `over` cambia linealmente en ese tiempo (no bloquea el script). `DEMAND A 0` = auto lleno (→ `SuspendedEV`). |
| `FAULT A` | `code=OtherError` | Conector → `Faulted` (detiene la transacción con motivo `Other`). Códigos OCPP: `GroundFailure`, `OverCurrentFailure`, … |
| `CLEAR_FAULT A` | | Saca la falla. |
| `SEND A Action ['{json}']` | | Envía un CALL crudo desde el cargador (ej. `SEND A Heartbeat`, `SEND A DataTransfer '{"vendorId":"x"}'`) y muestra la respuesta. |

### 3.4 Tiempo

| Verbo | Parámetros | Qué hace |
|---|---|---|
| `WAIT 20s` | | Pausa. Las verificaciones de fondo (`ASSERT_ALWAYS`) siguen corriendo. |
| `WAIT_UNTIL_STABLE A B ...` | `for=5s timeout=60s` | Espera hasta que, para **todos** los objetivos, el límite efectivo y la corriente real **no cambien durante `for`**. Si vence `timeout` cuenta como verificación fallida. Es la herramienta principal para "esperar que el CSMS termine de reaccionar". |
| `WAIT_MSG A Action` | `timeout=10s count=1` | Espera a que el CSMS le mande a A un mensaje `Action` (ej. `SetChargingProfile`). Timeout = verificación fallida. |
| `WAIT_ARRIVALS` | `timeout=3600s` | Espera que terminen todas las llegadas de `RANDOM_ARRIVALS`. |

### 3.5 Verificaciones

Todas registran un resultado (✔/✘) con esperado/obtenido y, si pasan, **cuánto tardaron desde el último evento** (la
"convergencia" del reporte). Las que tienen `within=` esperan hasta ese plazo y fallan recién después.

| Verbo | Parámetros | Qué compara |
|---|---|---|
| `EXPECT_LIMIT A op valor` | `within` `tol` | **Límite efectivo** que el cargador aplica hoy, resuelto desde los `SetChargingProfile` recibidos (TxProfile > TxDefault, tope de ChargePointMax, A/W, períodos). Sin perfiles = tope físico del cargador. En amperes por fase. |
| `EXPECT_ACTUAL A op valor` | `within` `tol` | Corriente **real** del auto, **sin** desviación de medición. |
| `EXPECT_MEASURED A op valor` | `within` `tol` | Corriente **medida** (con la desviación configurada del cargador; promedio de las fases en uso). |
| `EXPECT_SUM_ACTUAL op valor` | `within` `tol` `of=A,B` | Suma de corriente real **de la fase más cargada** del sitio. `of=` restringe a esos cargadores. |
| `EXPECT_SUM_LIMIT op valor` | `within` `tol` `of=` | Suma de **límites asignados** a las transacciones activas (fase más cargada). Detecta sobre-asignación aunque los autos aún no consuman. |
| `EXPECT_SUM_LE valor` | `within` `tol` `of=` | Atajo de `EXPECT_SUM_ACTUAL <= valor`. |
| `EXPECT_STATE A Estado` | `within` | Estado del conector: `Available Preparing Charging SuspendedEV SuspendedEVSE Finishing Faulted Unavailable`. |
| `EXPECT_TX A active\|none` | `within` | Hay / no hay transacción activa. |
| `EXPECT_CONNECTED A` / `EXPECT_DISCONNECTED A` | `within` | Estado de la conexión (BootNotification aceptado). |
| `EXPECT_MSG A Action` | `within` `count=1` | Llegaron ≥`count` mensajes `Action` del CSMS **desde que empezó este paso**. |
| `EXPECT_NO_MSG A Action` | `for=10s` | **No** llegó ese mensaje durante `for`. Útil para detectar oscilación o "tormenta de perfiles". |
| `EXPECT_NO_PROFILE A` | `within` | El cargador no tiene perfiles instalados. |

**Tolerancia (`tol`)** es margen *a favor* de la verificación: `==` → `|got−want| ≤ tol`; `<=` → `got ≤ want+tol`; `>=` → `got ≥ want−tol`.
Default: `TOLERANCE` (0.5 A).

### 3.6 Invariantes (verificación continua)

```
ASSERT_ALWAYS METRICA op valor [tol=0] [grace=0s] [id=nombre]
ASSERT_STOP nombre
```

Se evalúa **en cada tick (0.25 s)** desde que se declara hasta `ASSERT_STOP` o el fin. Una violación sólo cuenta como
**fallo** si dura **más que `grace`** (cada tick violado suma 0.25 s). Las violaciones breves dentro de la gracia se
registran igual en el reporte. En `strict` el primer fallo corta el script.

Métricas:

| Métrica | Significado |
|---|---|
| `SUM_MEASURED` | Corriente **medida** (con desviación) del sitio, fase más cargada. Lo que "vería el interruptor". |
| `SUM_ACTUAL` | Igual pero sin desviación. |
| `SUM_LIMIT` | Suma de límites asignados a transacciones activas (fase más cargada). |
| `ACTUAL.A` `MEASURED.A` `LIMIT.A` (`A.2` para otro conector) | Valor de un conector. |

> Convención de nombres: **ACTUAL** = física real sin ruido · **MEASURED** = lo que reportaría el medidor (con ruido) · **LIMIT** = lo que el CSMS asignó.

### 3.7 Azar y control de flujo

| Verbo | Qué hace |
|---|---|
| `RANDOM_ARRIVALS n=5 over=60s chargers=A,B,C demand=10..32 duration=30s..120s seed=42 idtag=X` | Programa `n` llegadas en `[0, over]`. Cada una elige cargador y conector al azar, enchufa, pide una demanda al azar (en A, múltiplos de 0.5), arranca, **se queda `duration`**, detiene y desenchufa. **No bloquea**: seguí con otros pasos y cerrá con `WAIT_ARRIVALS`. Misma `seed` ⇒ mismo tráfico. Si el conector elegido está ocupado cuando le toca, esa llegada se omite (queda como nota). Todos los parámetros son opcionales (`n=5 over=60s demand=10..32 duration=30s..120s`). |
| `REPEAT n [var=i]` … `END` | Repite el bloque; con `var=i`, `$i` vale 1..n. |
| `FOR v IN a b c` … `END` | Itera sobre valores o un rango `1..5`. `FOR c IN A B C` / `FOR i IN 1..4`. |
| `INCLUDE otro.evs` | Inserta otro script (ruta relativa al actual; detecta ciclos). |

---

## 4. Lo que simula el cargador (para razonar los resultados)

- **Física del auto:** toma `min(límite ofrecido, demanda)`; si lo ofrecido < `min_a` (6 A) **no carga** (`SuspendedEVSE`);
  si la demanda es 0 → `SuspendedEV`. La corriente sigue al objetivo con rampa `ramp_a_per_s` (5 A/s por defecto): de 0 a 32 A tarda ~6 s.
- **Límite efectivo (smart charging):** el cargador resuelve los perfiles que le manda el CSMS como manda OCPP 1.6:
  mayor `stackLevel` activo dentro de cada propósito; `TxProfile` pisa a `TxDefaultProfile`; el resultado se topa con
  `ChargePointMaxProfile`; unidades A y W (W→A con voltaje × fases); Absolute / Recurring (Daily, Weekly) / Relative;
  `validFrom/validTo/duration`. Los `TxProfile` se borran al terminar la transacción. Rechaza (`Rejected`) perfiles inválidos
  (stack > `ChargeProfileMaxStackLevel`, TxProfile sin transacción, etc.).
- **Corriente por fase:** un cargador trifásico carga las 3 fases con la misma corriente; uno monofásico, sólo su fase (`phase`).
  `numberPhases` en el período del perfil puede forzar 1 fase en uno trifásico. El límite OCPP en A es **por fase**.
  Las métricas de sitio (`SUM_*`) toman la **fase más cargada**.
- **MeterValues** (`Energy.Active.Import.Register`, `Current.Import` por fase, `Current.Offered`, `Power.Active.Import`, `Voltage`;
  opcionales `Power.Offered` y `SoC`) con timestamps UTC con milisegundos. Intervalo = `MeterValueSampleInterval` (se puede
  cambiar desde el CSMS con `ChangeConfiguration`). La energía acumulada integra la **potencia medida**, así que la desviación también la afecta.
- **Desviación de corriente** (config por cargador): `none`, `gaussian` o `uniform`, con `abs_a` (A fijos), `pct` (% de la corriente) y `bias_a` (error sistemático). Es reproducible con `SEED`.
- **Mensajes del CSMS soportados:** `SetChargingProfile`, `ClearChargingProfile`, `GetCompositeSchedule`, `RemoteStartTransaction`,
  `RemoteStopTransaction`, `Reset`, `ChangeConfiguration`, `GetConfiguration`, `TriggerMessage`, `ChangeAvailability`,
  `UnlockConnector` (NotSupported), `ClearCache`, `DataTransfer` (UnknownVendorId), `GetLocalListVersion`, `SendLocalList` (NotSupported),
  `ReserveNow`/`CancelReservation` (Rejected). Cualquier otro → `CALLERROR NotImplemented`.
- **Estados:** `Available → Preparing (PLUG) → Charging/SuspendedEV/SuspendedEVSE (START) → Finishing (STOP, enchufado) → Available (UNPLUG)`; `Faulted`, `Unavailable`.

---

## 5. Archivo de config (resumen)

> **Lo normal es no escribirlo a mano:** `python -m evsim init "<link1>" "<link2>" --limit 40` lo genera desde los links de conexión de los cargadores (`sitio_real.json`, con credenciales; fuera de git). Lo que sigue es la referencia del formato.

```json
{
  "csms":  { "url": "ws://host:9000/ocpp", "subprotocol": "ocpp1.6", "tls_verify": true,
             "auth": { "type": "basic", "password_env": "CSMS_PASS" } },
  "site":  { "limit_a": 40, "voltage": 230, "phases": 3 },
  "defaults": { "max_a": 32, "phases": 3, "connectors": 1,
                "meter": { "sample_interval_s": 5, "start_wh": 0, "measurands": ["Energy.Active.Import.Register","Current.Import","Current.Offered","Power.Active.Import","Voltage"] },
                "deviation": { "type": "none", "abs_a": 0, "pct": 0, "bias_a": 0 },
                "vehicle": { "min_a": 6, "ramp_a_per_s": 5, "battery_kwh": null, "soc_percent": 20 } },
  "chargers": [ { "id": "A", "serial": "EVSE-0001" },
                { "id": "B", "serial": "EVSE-0002", "max_a": 16, "phases": 1, "phase": "L2",
                  "deviation": { "type": "gaussian", "abs_a": 0.4, "pct": 1 } } ]
}
```

- URL de cada cargador = `csms.url + "/" + serial` (convención OCPP-J; el serial es el chargeBoxId). Se puede pisar con `chargers[].url` (y `append_serial:false` para no agregarle el serial).
- `id` es el nombre que se usa en los scripts. `serial` es lo que ve el CSMS.
- Cualquier clave que empiece con `_` es un comentario. `defaults` aplica a todos; cada cargador lo pisa.
- Auth Basic: usuario = `serial` salvo que se defina `username`; contraseña por `password` o (mejor) `password_env`.
- Otras claves útiles por cargador: `id_tag` (default de `START`), `authorize_before_start`, `auto_reconnect`, `reconnect_s`, `vendor/model/firmware`, `smart.{max_stack_level,max_periods,max_profiles,allowed_units}`, `vehicle.battery_kwh` (activa SoC y auto "lleno" al 100 %).

---

## 6. Patrones y plantillas

### 6.1 Esqueleto

```
; mi_prueba.evs - una línea que diga qué se prueba y por qué importa
SCENARIO "Nombre corto"
DESCRIPTION "A -> A+B -> A baja"
MODE strict
TOLERANCE 1
DEFAULT_WITHIN 20s

ASSERT_ALWAYS SUM_MEASURED <= 40 id=sitio tol=1 grace=5s   ; el invariante central del DLB

CONNECT A B
MARK "Fase 1: A solo"
PLUG A
DEMAND A 32
START A
WAIT_UNTIL_STABLE A for=4s
EXPECT_LIMIT A == 32

; ... más fases con MARK ...

STOP ALL
UNPLUG ALL
DISCONNECT ALL
```

### 6.2 Reparto con llegada escalonada (el caso base)

**Punto de partida recomendado: `scripts/dlb_parametrizado.evs`** (todo en variables `SET limite/tope/baja/idtag` al inicio; los valores esperados se calculan solos con `min(...)`).

Límite 40 A, dos cargadores de 32 A: A solo → 32; entra B → 20/20; A baja a 10 → A 10, B 30; A termina → B 32.
Ver `scripts/dlb_basico.evs`.

### 6.3 N cargadores iguales entrando de a uno

```
FOR c IN A B C D
  PLUG $c
  DEMAND $c 32
  START $c
  WAIT_UNTIL_STABLE A B C D for=5s timeout=40s
END
; reparto parejo: 40/4 = 10 A cada uno
EXPECT_LIMIT A == 10 tol=1
EXPECT_SUM_LIMIT <= 40 tol=0.5
```
Cuidado: `WAIT_UNTIL_STABLE` con cargadores aún no conectados/enchufados es válido (están estables en su tope).

### 6.4 Auto que baja su consumo de forma gradual

```
DEMAND A 10 over=30s          ; rampa lineal de 30 s, no bloquea
WAIT_UNTIL_STABLE A B for=6s timeout=90s
EXPECT_LIMIT B == 30 tol=1.5
```

### 6.5 Mínimo de 6 A (suspensión)

```
; 5 autos en un sitio de 40 A: 8 A cada uno es válido, 7 cargadores darían <6 A
EXPECT_STATE E SuspendedEVSE within=30s   ; el CSMS debería poner en pausa a alguno, no repartir 5.7 A
```

### 6.6 Estabilidad / sin oscilación

```
WAIT_UNTIL_STABLE A B for=5s timeout=60s
EXPECT_NO_MSG A SetChargingProfile for=20s    ; en régimen no debe haber perfiles nuevos
```

### 6.7 Medidor con ruido
Definir `deviation` en el cargador (config) y usar `SUM_MEASURED` + `tol=` en los asserts, `EXPECT_MEASURED ... tol=` (≥ 3× la `abs_a`).

### 6.8 Pérdida de conexión
```
DROP_LINK B for=10s
EXPECT_DISCONNECTED B within=5s
EXPECT_TX B active              ; la transacción sigue localmente
WAIT 12s
EXPECT_CONNECTED B within=20s
WAIT_UNTIL_STABLE A B for=5s timeout=40s
EXPECT_SUM_LIMIT <= 40 tol=0.5
```

### 6.9 Tráfico aleatorio reproducible
```
SEED 42
MODE soft
ASSERT_ALWAYS SUM_MEASURED <= 40 id=sitio tol=1 grace=6s
CONNECT A B C
RANDOM_ARRIVALS n=8 over=60s chargers=A,B,C demand=16..32 duration=20s..40s
WAIT_ARRIVALS
EXPECT_SUM_ACTUAL == 0 tol=0.5 within=20s
```

### 6.10 Catálogo de escenarios que vale la pena cubrir
reparto parejo · reasignación del sobrante · llegada y salida escalonada · auto que se llena (`battery_kwh`, `DEMAND 0`) ·
mínimo de 6 A · monofásicos en fases distintas (balance de fases) · cargador con falla (`FAULT`) · pérdida de enlace ·
reinicio desde el CSMS (`Reset`) · ruido de medición · ráfaga de llegadas simultáneas · límite del sitio que cambia en el CSMS (cambialo allá y usá `WAIT_MSG`).

---

## 7. Errores frecuentes y cómo evitarlos

| Síntoma | Causa | Arreglo |
|---|---|---|
| `EXPECT_LIMIT` pasa al instante con "0.0 s tras el evento" y no prueba nada | El valor esperado ya era el actual | Esperá un valor distinto al previo, o chequeá `EXPECT_NO_MSG`/`EXPECT_MSG` además |
| `EXPECT_ACTUAL` falla justo después de `DEMAND` | Rampa de 5 A/s | `WAIT_UNTIL_STABLE` antes, o `within=` ≥ 10 s |
| `==` falla por centésimas | Redondeos del CSMS (13.3 vs 13.33) | `tol=0.5` o más |
| El script tarda minutos | Cada EXPECT fallido espera `within` | Bajá `DEFAULT_WITHIN` o corregí el esperado; usá `MODE strict` mientras desarrollás |
| `START` → "falta PLUG" | Orden incorrecto | `PLUG` → `DEMAND` → `START` |
| `START A.2` falla | El cargador tiene 1 conector | `connectors: 2` en el config |
| Falla de invariante por un pico de 1–2 s al cambiar | El auto tarda en bajar cuando entra otro | Agregá `grace=5s` (refleja la física: el CSMS no puede ser instantáneo) |
| Dos corridas simultáneas contra el mismo CSMS dan resultados raros | Comparten `chargeBoxId` y se pisan | Corré **una** a la vez |
| `variable no definida: $x` | Se usa antes de `SET` | Definila antes; los nombres no distinguen mayúsculas |

---

## 8. Cómo leer el reporte

- **Verificaciones:** cada fila muestra esperado/obtenido, cuánto se esperó y cuánto pasó desde el último evento (*convergencia*).
- **Invariantes:** cantidad de violaciones, mayor duración y peor valor. Violaciones dentro de la gracia aparecen pero no fallan.
- **Gráficos:** línea azul = corriente medida, naranja escalonada = límite asignado, línea punteada = límite del sitio. Las verticales son los `MARK`.
- **Reacción del CSMS a cada evento:** cuántos `SetChargingProfile` mandó tras cada evento y cuándo llegó el primero/último. Muchos perfiles por evento o un último muy tardío = oscilación o lentitud.
- **Energía entregada vs ideal:** el ideal asume reparto perfecto e instantáneo respetando el límite; la relación mide la eficiencia del DLB.
- `messages.log` tiene cada frame OCPP con timestamp (`->` cargador a CSMS, `<-` CSMS a cargador).
