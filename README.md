# Radar de lluvias para setas

Mapa de lluvia diaria y acumulada (hasta 21 días) en Navarra, La Rioja, Soria, Zaragoza, Huesca,
Teruel, Lleida y Tarragona. Ver `CLAUDE.md` para los requisitos.

## Puesta en marcha (local)

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/Mac: .venv/bin/python
cp .env.example .env                                     # y rellena las claves que tengas

python -m scripts.probe_sources            # paso 1: prueba cada fuente (21 días) sin tocar la BD
python -m app.ingest                       # descarga 30 días de todas las fuentes a data/rain.sqlite
python -m uvicorn app.main:app --reload    # http://localhost:8000
python -m app.scheduler                    # ingesta programada (06:30 y 14:30 completa, cada 3 h rápida)
python -m pytest tests
```

En Windows basta con hacer doble clic en **`Radar de lluvias.bat`** (o en el acceso directo del escritorio):
la primera vez prepara Python solo, luego arranca la web + la actualización de datos y abre el navegador.

Con Docker: `docker compose up -d --build` (servicio `web` + `worker` con la BD en un volumen).
Para un solo proceso, `RUN_SCHEDULER=1` arranca el planificador dentro de la API.

## Fuentes

| Fuente | Estado | Clave | Cobertura (sep. 2026) |
|---|---|---|---|
| Meteocat (Dades Obertes) | ✅ funciona | no | 99 estaciones Lleida/Tarragona; diario consolidado con ~2 días de retraso + semihorario para los últimos días |
| SIAR La Rioja | ✅ funciona | no | 26 estaciones |
| InfoRiego (ITACyL) | ✅ funciona | no | 4 estaciones en Soria (CSV diario, sale ~13:30 del día siguiente) |
| Meteo Navarra | ✅ funciona (scraping) | no | ~140 estaciones automáticas + manuales; manuales miden 08–08 h |
| Open-Meteo | ✅ funciona | no | rejilla estimada de 0,15° (435 puntos) — capa aparte, no son medidas |
| AEMET OpenData | ✅ funciona | `AEMET_API_KEY` | 154 estaciones en las 8 provincias; diario oficial con 4 días de retraso + horario (últimas 24 h, se acumula en BD) |
| SiAR (MAPA) | ⏳ escrito, sin probar | `SIAR_TOKEN` o `SIAR_USER`+`SIAR_PASSWORD` | 78 estaciones activas (Navarra, Zaragoza, Huesca, Teruel, Soria); no tiene en La Rioja ni Cataluña |
| SAIH Ebro (CHE) | ✅ funciona | `SAIH_API_KEY` | ~330 pluviómetros de la cuenca (Pirineo, Moncayo, Ibérica); acumulado 00–24 h cada 15 min, datos provisionales |

**SAIH Ebro:** API Open Data (`/datos/apiopendata?tipo_senal=PACUM&inicio=AAAA-MM-DD`, 5 llamadas/min):
una llamada devuelve un día entero de todos los pluviómetros. Los días cerrados se guardan en `cache/saihebro/`,
así que tras la primera carga (~30 llamadas, unos 7 min) cada ingesta solo pide ayer y hoy. Coordenadas y
altitud de la capa pública `SAIH_Precipitacion` del geoportal SITEbro de la CHE. No se usan los endpoints
internos `/api/` de su web (su `robots.txt` los prohíbe).

## API

- `GET /api/stations?provinces=&sources=&alt_min=&alt_max=&estimated=`
- `GET /api/rain?from=&to=&days=&provinces=&sources=&alt_min=&alt_max=&min_mm=` — acumulado por estación
- `GET /api/rain/daily?days=21` — matriz estación × día (la usa el mapa para animar sin más peticiones)
- `GET /api/stations/{id}/series?days=21` — serie diaria + acumulado
- `GET /api/sources` — fuentes, titularidad y estado de la última actualización
- `GET /api/provinces` — GeoJSON de las 8 provincias

## Notas técnicas

- Todo se normaliza a `station_id, source, name, lat, lon, altitude, province, date, precip_mm`.
- Un valor parcial (día en curso) nunca pisa uno definitivo; los definitivos se re-descargan y corrigen.
- **Duplicados** (misma estación publicada por varias redes, `db.deduplicate`): se agrupan si comparten una
  clave declarada por el colector (`match_keys`: mismo código SiAR/InfoRiego, mismo nombre SiAR/Meteo Navarra
  "MAPA"/"INTIA") o si están a < 300 m con < 30 m de diferencia de altitud. Se muestra la de la fuente original
  (prioridades en `SOURCE_PRIORITY`); las demás quedan como `duplicate_of`.
- **Relleno de huecos** (vista `effective_observations`): si la estación principal no tiene dato un día, o solo
  parcial, se usa el de una duplicada. La API indica de qué red viene cada dato rellenado.
- **Día pluviométrico** (`day_window` por estación): AEMET 07–07 UTC (su diario oficial; los horarios se agregan
  en la misma ventana), Meteo Navarra manuales 08–08, el resto 00–24. Todas asignan la lluvia al día en que
  empieza la ventana; como mucho hay 7-8 h de desfase entre redes.
- `app/data/certs/` añade el intermedio de la FNMT, que los servidores de mapa.gob.es no envían.
