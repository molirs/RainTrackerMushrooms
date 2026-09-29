from app.collectors.aemet import AemetCollector
from app.collectors.inforiego import InfoRiegoCollector
from app.collectors.larioja import LaRiojaCollector
from app.collectors.meteocat import MeteocatCollector
from app.collectors.meteonavarra import MeteoNavarraCollector
from app.collectors.open_meteo import OpenMeteoCollector
from app.collectors.siar import SiarCollector

COLLECTORS = {
    c.source: c
    for c in (AemetCollector, MeteocatCollector, LaRiojaCollector, InfoRiegoCollector,
              MeteoNavarraCollector, SiarCollector, OpenMeteoCollector)
}
