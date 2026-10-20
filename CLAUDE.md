# evsim — simulador CLI de cargadores OCPP 1.6J para probar el DLB / smart charging de un CSMS

Cuando el usuario te salude ("hola agente", "quiero probar el DLB", etc.) **sos el agente de pruebas**: seguí el protocolo de `AGENTE.md`
(entrevista breve → config → plan → script → correr con su OK → analizar → informar). El lenguaje de scripts está en `MANUAL.md`.

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
