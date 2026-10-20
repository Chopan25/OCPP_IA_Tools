# Guía de uso de evsim (para el equipo web)

evsim simula **cargadores de autos eléctricos virtuales** y los conecta a nuestra plataforma, para comprobar que el **balanceo dinámico de carga (DLB)**
reparte bien la corriente. No hace falta saber de electricidad: esta guía explica lo necesario, y hay un agente de IA que te lleva de la mano.

> **Camino rápido:** cloná el repositorio, abrí Claude Code **dentro de esa carpeta** y escribí **"hola agente, quiero probar el DLB"** (o simplemente "quiero usar esto"). El agente prepara el
> entorno solo (Python y dependencias), te va a pedir que crees la estación
> en la plataforma y le pases los links de los cargadores; el resto lo hace él (configura, corre las pruebas, analiza y te cuenta cómo salió). Las secciones 3 a 6
> explican lo mismo por si querés hacerlo a mano.

---

## 1. ¿Qué es el DLB y qué vamos a probar?

Un lugar (un edificio, un estacionamiento) tiene una **cantidad máxima de corriente** que puede entregar en total, medida en **amperes (A)**. Si hay varios cargadores y todos
cargan a full, se pasan de ese máximo. El **DLB** reparte la corriente disponible entre los autos que están cargando y la reajusta cuando entra o sale un auto, o cuando un auto
empieza a tomar menos.

Ejemplo (límite del lugar: **40 A**, dos cargadores de **32 A**):

| Qué pasa | Qué debería hacer el DLB |
|---|---|
| Carga un solo auto (pide 32 A) | Le deja 32 A |
| Entra un segundo auto (pide 32 A) | Reparte: 20 A y 20 A (suman 40) |
| El primer auto empieza a tomar solo 10 A (su batería casi llena) | Lo deja en 10 A y le pasa el sobrante al otro: 30 A |
| El primer auto termina | El otro vuelve a 32 A |

evsim hace de cargadores y de autos, y **mide qué le ordena la plataforma a cada cargador y cuánto tarda**.

## 2. Qué necesitás

- Una PC con Windows y **Python 3.12** instalado.
- Este repositorio clonado en tu PC.
- Tu **cuenta de la plataforma de pruebas**, para crear una estación con cargadores.

### Instalación (una sola vez)
En una terminal, dentro de la carpeta del repositorio:
```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```
(Si la carpeta `.venv` ya existe, no hace falta.)

## 3. Crear la estación en la plataforma

En **tu cuenta**, creá:

1. **Una estación con 2 cargadores** (si después querés probar con más, creá 3 o 4). Cada uno con **1 conector**, **22 kW, AC trifásico (32 A)**.
   Ponéles nombres fáciles de reconocer, por ejemplo `EST_01` y `EST_02`.
2. **El DLB para esos cargadores, con un límite de 40 A en total.** Dos cargadores de 32 A podrían pedir 64 A juntos; con 40 A el DLB **tiene que repartir**, que es lo que queremos ver.

Después copiá el **link de conexión de cada cargador** (una URL que empieza con `wss://` y termina con el nombre/serial del cargador), por ejemplo:

```
wss://servidor:1403/ocpp/160/<tu-clave>/EST_01
```

> **Cuidado:** el tramo largo de ese link es **una clave de tu cuenta**. No lo pegues en chats públicos ni lo subas a ningún repositorio.

## 4. Crear la configuración con los links

Pasale los links a `evsim init` (uno por cargador, entre comillas):

```
.venv\Scripts\python.exe -m evsim init "wss://servidor:1403/ocpp/160/<tu-clave>/EST_01" "wss://servidor:1403/ocpp/160/<tu-clave>/EST_02" --limit 40
```

Esto crea el archivo **`sitio_real.json`** (solo en tu PC; contiene tu clave) y te muestra los cargadores con la clave oculta. En las pruebas, tus cargadores se llaman **A, B, C…** en el
orden en que pasaste los links.

Opciones: `--limit` límite del lugar en A (default 40) · `--max-a` corriente máxima de cada cargador (default 32) · `--phases` 1 o 3 (default 3) ·
`--id-tag` (default `plug_and_play`, el que funciona en el entorno de pruebas) · `--force` para reemplazar un `sitio_real.json` existente.

## 5. Primera prueba: ¿conectan y cargan?

```
.venv\Scripts\python.exe -m evsim run smoke_estados -c sitio_real.json
```

Dura ~1 minuto. **Mirá la plataforma mientras corre**: vas a ver los cargadores conectarse, el conector pasar a *Preparing* (auto enchufado) y *Charging* (cargando), y al final volver a
*Available*; se crean dos sesiones de carga de prueba (sin costo). Al terminar, mostrá el resumen:

```
.venv\Scripts\python.exe -m evsim summary
```

Si ves `PASSED`, la conexión anda. (Los cargadores se desconectan al terminar: si mirás la plataforma después, los vas a ver desconectados. Para tenerlos conectados ~3 minutos y mirar con calma:
`run mantener_conectado -c sitio_real.json`.)

## 6. Probar el DLB

**Si ya sabés cómo debería repartir** (ej. parejo, y el sobrante pasa al otro):
```
.venv\Scripts\python.exe -m evsim run dlb_parametrizado -c sitio_real.json
```
Verifica cada paso de la tabla de la sección 1 (edita las variables del inicio del archivo `scripts\dlb_parametrizado.evs` si tu límite no es 40 A).

**Si no sabés qué debería hacer** (o querés ver qué hace): usá el modo exploratorio, que solo observa y no da por mal nada:
```
.venv\Scripts\python.exe -m evsim run dlb_exploratorio -c sitio_real.json
.venv\Scripts\python.exe -m evsim summary
```
En el resumen, la sección **"Fotos del estado"** muestra, fase por fase, qué límite le dio la plataforma a cada cargador. Con eso decidís si es lo esperado.

Más pruebas (varios autos al azar, un cargador que pierde la conexión, medidores con ruido…): pedíselas al agente o mirá `scripts\` y `MANUAL.md`.

## 7. Entender los resultados

`evsim summary` muestra de la última corrida:

- **RESULTADO**: `PASSED` (todo bien), `FAILED` (alguna verificación falló), `ERROR` (la prueba no pudo correr: conexión, config…).
- **Verificaciones fallidas**: qué se esperaba y qué se obtuvo.
- **Invariantes**: la regla de oro, *"el lugar nunca se pasa del límite"*. Dice si se violó, cuándo y cuánto duró.
- **Convergencia**: segundos que tardó la plataforma en llegar al reparto correcto después de cada cambio.
- **Reacción por evento / Perfiles recibidos**: cada orden (`SetChargingProfile`) que mandó la plataforma: a qué cargador, cuántos amperes y si el cargador la aceptó.
- **Fotos del estado**: qué límite tenía cada cargador al final de cada fase (modo exploratorio).
- **Sitio**: cuánto se pasó del límite (si pasó) y cuánta energía se entregó contra la ideal.

Lo primero que hay que mirar:
1. Si en *Perfiles recibidos* dice **NINGUNO**: la plataforma **no mandó ningún límite**. El DLB no actuó (¿está activo para esos cargadores? ¿el grupo tiene límite?).
2. Si algún perfil figura **Rejected**: el cargador lo rechazó porque la orden era inválida.
3. Si el lugar se pasó del límite: ¿cuánto y por cuánto tiempo?

El reporte con gráficos está en `out\<fecha>_<script>\report.html` (abrilo con el navegador): corriente real por cargador (azul), límite que le dio la plataforma (naranja) y el límite del lugar (línea punteada).

## 8. Problemas frecuentes

| Qué pasa | Qué hacer |
|---|---|
| `no se acepto el BootNotification` / no conecta | Revisá que el link esté completo, que el cargador exista en la plataforma con ese nombre exacto y que tengas red (VPN/firewall) al puerto del link |
| `AuthorizeRejected:Invalid` | El idTag no es válido en tu entorno: regenerá la config con `init ... --id-tag <otro> --force` |
| El cargador aparece "sin estado" en la plataforma | Mirá **mientras** corre la prueba; al terminar se desconectan. Usá `mantener_conectado` |
| `cargador desconocido` | El script usa un nombre que no está en tu config (se llaman A, B, C… en el orden de los links) |
| `PASSED` pero no hay ningún límite del DLB | No hay nada para verificar: activá el DLB para esos cargadores y repetí |
| La prueba tarda mucho | Cada verificación fallida espera 10–30 s antes de darse por fallida: revisá lo esperado |
| Resultados raros | ¿Hay otra corrida de evsim abierta? Los cargadores comparten identidad: **una prueba a la vez** |

## 9. Referencia rápida

```
evsim init <links...> [--limit A] [-o archivo] [--force]   crea la config desde los links de los cargadores
evsim list    -c sitio_real.json                            scripts y cargadores
evsim check   <script> -c sitio_real.json                   valida un script sin conectar nada
evsim run     <script> -c sitio_real.json                   corre la prueba y genera el reporte
evsim summary [carpeta]                                     resumen de la última corrida
evsim mock-csms --limit 40                                  plataforma de juguete local, para ensayar sin tocar la real
```
(En todos los casos se escribe `.venv\Scripts\python.exe -m evsim ...`.)

Más documentación: `AGENTE.md` (cómo trabaja el agente de IA) · `MANUAL.md` (lenguaje de scripts, para escribir pruebas propias) · `PROJECT.md` (arquitectura, para quien quiera modificar evsim).

## 10. Glosario

- **Amperes (A):** cuánta corriente toma el auto. Más amperes = carga más rápida. El DLB reparte amperes.
- **Límite del lugar:** máximo de amperes que la instalación aguanta en total.
- **DLB:** reparto automático de la corriente disponible entre los autos que cargan.
- **Perfil de carga (`SetChargingProfile`):** la orden de la plataforma al cargador: "podés entregar hasta X amperes".
- **Sesión (transacción):** una carga de principio a fin.
- **Estados del conector:** *Available* libre · *Preparing* auto enchufado, esperando · *Charging* cargando · *SuspendedEV* el auto no quiere más (batería llena) · *SuspendedEVSE* el cargador frena (le ofrecieron menos de 6 A) · *Finishing* terminó, sigue enchufado · *Faulted* falla.
- **Mínimo de 6 A:** con menos de 6 A un auto no puede cargar.
