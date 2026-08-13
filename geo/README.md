# Map data

Pre-clipped geographic data for the end-of-contact station map
(`lynx_map.py`). Everything here is trimmed to a 1200 km radius around
the UK and stripped of unused attribute columns, which takes the full
Natural Earth download from 109 MB to under 6 MB.

That radius covers northern Italy (~865 km), northern Spain (~925 km),
Berlin, Copenhagen and Vienna — comfortably beyond anything the
repeater is likely to hear, while staying small enough to sit in the
repository without bloating it.

## Sources and licences

| Data | Source | Licence |
|---|---|---|
| Land, coastline, lakes, rivers, urban areas, borders, countries, populated places | [Natural Earth](https://www.naturalearthdata.com/) 10m | Public domain |
| `towns.csv` — towns above 15,000 population | [GeoNames](https://www.geonames.org/) | CC BY 4.0 |

The attribution line drawn on the card credits both. Natural Earth
requires none, but GeoNames does.

## Why both town sources

Natural Earth's populated-places layer carries only 13 entries across
the whole of southern England, which left early versions of the card
looking bare. GeoNames adds roughly 360 in the same window. The card
draws Natural Earth's places as ranked "majors" and the GeoNames set as
"minors", both spatially thinned so labels never collide.

## Regenerating

Only needed if the radius changes or Natural Earth publishes an update.
See `tools/build_geo.py`.
