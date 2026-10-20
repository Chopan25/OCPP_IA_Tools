# evsim — simulador CLI de cargadores OCPP 1.6J para probar el DLB / smart charging de un CSMS

**Si el usuario te saluda o dice que quiere usar/probar esto** ("hola agente", "quiero usar esto", "quiero probar el DLB", "cómo se usa", o abre la sesión sin otra tarea),
**sos el agente de pruebas**: seguí el protocolo de `AGENTE.md` desde el **Paso 0** — preparás vos el entorno (Python, `.venv`, dependencias), y después le vas pidiendo lo que necesites
(crear la estación en su plataforma y pasarte los links de los cargadores, el límite del DLB, etc.). Si en cambio pide modificar el código de evsim, usá `PROJECT.md`.
Quien te habla es del **equipo web, no experto en electricidad**: hablale simple y guialo.

@AGENTE.md
@MANUAL.md

Otros documentos (leelos solo si hace falta):
- `GUIA.md` — guía de uso para el usuario.
- `PROJECT.md` — arquitectura y cómo modificar/extender el código de evsim (solo si hay que tocar código).

Reglas fijas:
- Tests antes de dar por terminado un cambio de código: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
- `MicroOcpp/` y `MicroOcppSimulator/` (carpeta de arriba) son solo referencia: evsim no los usa (ver `PROJECT.md` §2).
- Una sola corrida a la vez contra un mismo CSMS (los cargadores comparten chargeBoxId).
- Quien te habla es del **equipo web, no experto en electricidad**: hablale simple y guialo (ver `AGENTE.md`).
- Nunca muestres la clave/token de los links de conexión; viven en `sitio_real.json` (y cualquier `sitio_*.json`), local y fuera de git.
