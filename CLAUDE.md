# Radar de lluvias para setas

Webapp para seguir la lluvia diaria y acumulada (hasta 21 días atrás) en las estaciones meteorológicas de
Navarra, La Rioja, Soria, Zaragoza, Huesca, Teruel, Lleida, Tarragona y Castellón, con el objetivo de saber dónde
y cuándo ha llovido lo suficiente para que salgan setas.

## Funcionalidad

- Mapa interactivo (Leaflet + OpenStreetMap) con cada estación como un círculo coloreado según los mm.
- Escala de color de lluvia (0 mm gris → azules → morado para >100 mm), con leyenda.
- Filtros:
  - Rango de días: acumulado de los últimos N días (1–21) o un intervalo de fechas concreto.
  - "Cuándo llovió": ver un día concreto y animar día a día con un slider (play/pausa).
  - Provincia (multiselección) y red/fuente de datos.
  - Altitud mínima/máxima de la estación (las setas dependen mucho de la cota).
  - Umbral mínimo de mm para destacar zonas.
- Al hacer clic en una estación: nombre, fuente, altitud, gráfico de barras de lluvia diaria de los 21 días
  y línea de acumulado.
- Capa opcional de lluvia estimada en rejilla (Open-Meteo) para rellenar zonas de monte sin estaciones.
  Debe distinguirse visualmente de los datos medidos.

## Fuentes de datos (una por módulo `collectors/`)

| Fuente | Cobertura | Acceso |
|---|---|---|
| AEMET OpenData | Las 9 provincias | API REST, API key gratuita. Doble llamada (la 1ª devuelve una URL `datos`). Climatologías diarias con retraso de días; para los últimos días usar observación convencional (horaria, últimas 24 h) y acumular. |
| Meteocat (XEMA) | Lleida, Tarragona | Dades Obertes (Socrata, sin key) o API REST con key. Variable de precipitación diaria acumulada. |
| SiAR (MAPA) | Zonas de regadío de Aragón, Navarra, La Rioja, Cataluña | Web API con token (registro). Endpoint `Datos/Diarios/ESTACION`. |
| SIAR La Rioja / SOS Rioja | La Rioja | API del Gobierno de La Rioja y dataset abierto CC BY 4.0. |
| Meteo Navarra | Navarra (GN, INTIA, MAPA, AEMET, UPNA) | Web con sección "Descargar datos" y boletines diarios. Sin API formal: scraping respetuoso. |
| InfoRiego (ITACyL) | Soria (Castilla y León) | Datos abiertos JCyL y FTP con datos diarios. |
| SAIH Ebro (CHE) | Pluviómetros de toda la cuenca del Ebro | Web en tiempo real (cada 15 min) y "datos a la carta". Sin API pública documentada: investigar primero. |
| SAIH Júcar (CHJ) | Castellón y sur de Teruel | Endpoint público de su mapa: `saih.chj.es/lluviasIntervalo/desde/hasta` (sin key). |
| Open-Meteo | Rejilla, cualquier punto | Sin key para uso no comercial. `past_days` en la Forecast API. Solo como capa estimada. |

Reglas de ingesta:
- Normalizar todo a: `station_id`, `source`, `name`, `lat`, `lon`, `altitude`, `province`, `date`, `precip_mm`.
- Deduplicar estaciones que aparecen en varias redes (misma estación a <300 m) priorizando la fuente original.
- Guardar al menos 30 días de histórico; job diario (y otro cada pocas horas para el día en curso).
- Si una fuente falla, el resto sigue funcionando; registrar el error y la última actualización por fuente.
- Respetar límites de las APIs, cachear, y no martillear webs que se scrapean.
- Citar la titularidad de los datos en la web (p. ej. "© AEMET", Meteocat, CHE…).

## Stack

- Backend: Python + FastAPI, PostgreSQL (o SQLite para empezar), APScheduler o cron para la ingesta.
- Frontend: HTML + Leaflet + Chart.js (o HTMX), sin build complicado.
- Claves en `.env` (nunca en el repo): `AEMET_API_KEY`, `METEOCAT_API_KEY`, `SIAR_TOKEN`.
- Todo en Docker Compose para desplegar en un servidor.

## Forma de trabajar

1. Primero, un script de prueba por fuente que descargue 21 días y verifique qué estaciones de las 9 provincias devuelve.
2. Después el modelo de datos y la ingesta.
3. Después la API (`/stations`, `/rain?from=&to=&provinces=&sources=`).
4. Por último el mapa y los filtros.
