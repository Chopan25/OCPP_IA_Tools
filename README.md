# evsim

Banco de pruebas de **smart charging / balanceo dinámico de carga** para un CSMS OCPP 1.6J.
Levanta cargadores virtuales (con medidor, energía acumulada, desviación de corriente y auto simulado), les hace ejecutar un
script tipo gcode y genera un reporte con gráficos y verificaciones.

## Instalación (Windows, Python 3.12)

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy sitio.example.json sitio.json      # y editarlo: URL del CSMS, límite del sitio, cargadores
```

## Uso

```
.venv\Scripts\python.exe -m evsim list                  # scripts y cargadores
.venv\Scripts\python.exe -m evsim check dlb_basico      # valida el script sin conectar
.venv\Scripts\python.exe -m evsim run dlb_basico        # corre y genera out\<fecha>_dlb_basico\report.html
```

Para probar el simulador sin tu CSMS, en otra terminal: `.venv\Scripts\python.exe -m evsim mock-csms --limit 40`
(con `sitio.json`, que apunta a `ws://127.0.0.1:9000/ocpp`).

Para tu plataforma real: `.venv\Scripts\python.exe -m evsim init "<link cargador 1>" "<link cargador 2>"` crea `sitio_real.json`, y se corre con `-c sitio_real.json` (ver `GUIA.md`).

## Documentación

- **`GUIA.md`** — cómo usarlo, paso a paso, para el equipo web (empezá por acá).
- **`AGENTE.md`** — playbook para que una IA guíe las pruebas y analice los resultados ("hola agente, quiero probar el DLB").
- **`MANUAL.md`** — lenguaje de scripts, todos los verbos, patrones y errores frecuentes (pensado para que una IA genere scripts).
- **`PROJECT.md`** — arquitectura, decisiones, cómo extenderlo (pensado para que una IA siga desarrollándolo).

## Tests

```
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```
