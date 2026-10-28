"""Regional aerial imagery beyond Maryland (the user, 2026-10-15: "I think only maryland has the high res data. Try to
add the other data sources you found"): keyless state and national orthophoto services with an open or permissive
licence (docs/research/2026-10-10-us-state-imagery.md, docs/research/2026-10-10-imagery-sources-world.md), each
limited to its own bounding box. They come first in imagery.DEFAULT_CHAIN; where one has nothing (outside its state
inside the box: blank tiles) the next source fills in, down to NAIP, USGS and Sentinel-2.

Kinds: XYZ caches ({z}/{x}/{y}; {ty} is the TMS row 2^z - 1 - y), and WMS / ArcGIS export services (export=2: one
1024 px request per z15 parent, split into z17 tiles; lazy: fetched ahead of the player by warm(), as md6in).
"""

WMS = ("SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&STYLES=&CRS=EPSG:3857&BBOX={bbox}&WIDTH={size}&HEIGHT={size}"
       "&FORMAT=image/jpeg")
ARCGIS_EXPORT = "bbox={bbox}&bboxSR=3857&imageSR=3857&size={size},{size}&format=jpg&f=image"
# a WMS without Web Mercator: the tile's extent in latitude / longitude (imagery.lat_lon_bbox)
WMS_4326 = ("SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&STYLES=&CRS=EPSG:4326&BBOX={bbox4326}&WIDTH={size}&HEIGHT={size}"
            "&FORMAT=image/jpeg")

# The place each regional source covers, for the player (netfetch.role_of: what a source that does not answer affects)
PLACE = {
    "ct2023": "Connecticut", "ma2025": "Massachusetts", "dc2025": "Washington, DC", "indiana": "Indiana",
    "ohio": "Ohio", "ncarolina": "North Carolina", "penn": "Pennsylvania", "virginia": "Virginia",
    "newyork": "New York State", "newjersey": "New Jersey", "delaware": "Delaware", "vermont": "Vermont",
    "swiss": "Switzerland", "france": "France", "spain": "Spain", "netherlands": "the Netherlands",
    "luxembourg": "Luxembourg", "austria": "Austria", "it_bolzano": "South Tyrol", "it_pcn": "Italy",
    "poland": "Poland", "czechia": "Czechia", "estonia": "Estonia", "flanders": "Flanders (Belgium)",
    "de_bayern": "Bavaria", "de_nrw": "North Rhine-Westphalia", "de_bw": "Baden-Wurttemberg",
    "de_nds": "Lower Saxony", "de_berlin": "Berlin", "de_hessen": "Hesse", "japan": "Japan",
    "au_nsw": "New South Wales", "au_tas": "Tasmania", "ontario": "Ontario",
}

# name: (url, attribution, (south, west, north, east), kind, leaf_off)
#   kind "xyz": an XYZ / TMS cache; "export": a WMS / ArcGIS export (lazy)
REGIONAL = {
    # --- United States (state programmes; spring leaf-off flights) ---------------------------------------------
    "ct2023": ("https://cteco.uconn.edu/ctraster/rest/services/images/Ortho_2023_tiled/ImageServer/tile/{z}/{y}/{x}",
               "CT ECO 2023 orthoimagery (CC0)", (40.95, -73.73, 42.06, -71.78), "xyz", True),
    "ma2025": ("https://tiles.arcgis.com/tiles/hGdibHYSPO59RG1h/arcgis/rest/services/Massachusetts_Aerial_Imagery_2025/"
               "MapServer/tile/{z}/{y}/{x}", "MassGIS 2025 aerial imagery", (41.23, -73.51, 42.89, -69.92), "xyz", True),
    "dc2025": ("https://imagery.dcgis.dc.gov/dcgis/rest/services/Ortho/Ortho_2025/ImageServer/exportImage?bandIds=0,1,2&"
               + ARCGIS_EXPORT, "DC OCTO 2025 orthophoto (CC BY 4.0)", (38.79, -77.12, 39.0, -76.91), "export", True),
    "indiana": ("https://di-ingov.img.arcgis.com/arcgis/rest/services/CacheWebMercator/IndianaCurrentImageryCacheL19v4/"
                "MapServer/tile/{z}/{y}/{x}", "Indiana IGIO orthoimagery (CC0)", (37.77, -88.10, 41.77, -84.78), "xyz", True),
    "ohio": ("https://maps.ohio.gov/image/rest/services/osip_3_cache/MapServer/tile/{z}/{y}/{x}",
             "OGRIP OSIP orthoimagery (public domain)", (38.40, -84.82, 42.0, -80.52), "xyz", True),
    "ncarolina": ("https://services.nconemap.gov/secure/rest/services/Imagery/Orthoimagery_Latest_cached/ImageServer/"
                  "tile/{z}/{y}/{x}", "NC OneMap orthoimagery (NC CGIA, NC 911 Board)", (33.84, -84.33, 36.59, -75.45),
                  "xyz", True),
    "penn": ("https://imagery.pasda.psu.edu/arcgis/rest/services/PEMAImagery2021_2023cache/MapServer/tile/{z}/{y}/{x}",
             "PEMA orthoimagery via PASDA", (39.72, -80.52, 42.27, -74.69), "xyz", True),
    "virginia": ("https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VBMP_Imagery/MostRecentImagery_WGS/MapServer/"
                 "tile/{z}/{y}/{x}", "VGIN VBMP imagery", (36.54, -83.68, 39.47, -75.24), "xyz", True),
    "newyork": ("https://orthos.its.ny.gov/arcgis/rest/services/wms/Latest/MapServer/export?" + ARCGIS_EXPORT,
                "NYS ITS GIS Program Office orthoimagery", (40.49, -79.77, 45.02, -71.85), "export", True),
    "newjersey": ("https://maps.nj.gov/arcgis/rest/services/Basemap/Orthos_Natural_2020_NJ_WM/MapServer/tile/{z}/{y}/{x}",
                  "NJ OIT Office of GIS 2020 orthoimagery", (38.92, -75.57, 41.36, -73.89), "xyz", True),
    "delaware": ("https://imagery.firstmap.delaware.gov/imagery/rest/services/DE_Imagery/DE_Imagery_2024/ImageServer/"
                 "tile/{z}/{y}/{x}", "Delaware FirstMap 2024 imagery", (38.45, -75.79, 39.84, -75.04), "xyz", True),
    "vermont": ("https://maps.vcgi.vermont.gov/arcgis/rest/services/EGC_services/IMG_VCGI_CLR_WM_CACHE/ImageServer/"
                "tile/{z}/{y}/{x}", "VCGI orthoimagery", (42.72, -73.44, 45.02, -71.46), "xyz", True),
    # --- Europe -------------------------------------------------------------------------------------------------
    "swiss": ("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg",
              "(c) swisstopo SWISSIMAGE", (45.80, 5.95, 47.81, 10.50), "xyz", False),
    "france": ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=ORTHOIMAGERY.ORTHOPHOTOS"
               "&STYLE=normal&TILEMATRIXSET=PM&FORMAT=image/jpeg&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}",
               "IGN BD ORTHO (Licence Ouverte 2.0)", (41.30, -5.20, 51.10, 9.60), "xyz", False),
    "spain": ("https://tms-pnoa-ma.idee.es/1.0.0/pnoa-ma/{z}/{x}/{ty}.jpeg", "PNOA (c) IGN Espana (CC BY 4.0)",
              (27.60, -18.20, 43.80, 4.40), "xyz", False),
    "netherlands": ("https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg",
                    "Beeldmateriaal.nl / PDOK (CC BY 4.0)", (50.75, 3.35, 53.56, 7.23), "xyz", True),
    "luxembourg": ("https://wmts1.geoportail.lu/opendata/wmts/ortho_latest/GLOBAL_WEBMERCATOR_4_V3/{z}/{x}/{y}.jpeg",
                   "geoportail.lu orthophoto (CC0)", (49.44, 5.73, 50.19, 6.53), "xyz", False),
    "austria": ("https://maps.wien.gv.at/basemap/bmaporthofoto30cm/normal/google3857/{z}/{y}/{x}.jpeg",
                "Datenquelle: basemap.at (CC BY 4.0)", (46.37, 9.53, 49.02, 17.16), "xyz", False),
    # Italy (the user's reports #40 / #44 at the Stelvio Pass, 2026-10-17: "Ground to blurry here", the 10 m Sentinel
    # mosaic): South Tyrol's 2024 flight (open data, no access constraints) inside its province, then the national
    # 2012 orthophoto (Geoportale Nazionale, both UTM zones' layers; plain http: its https redirects there) for the
    # rest of the country
    "it_bolzano": ("https://geoservices.buergernetz.bz.it/mapproxy/wms?LAYERS=gvcc-Orthoimagery:Aerial-2024-RGB&" + WMS,
                   "Autonome Provinz Bozen - Suedtirol orthophoto 2024", (46.22, 10.38, 47.10, 12.48), "export", False),
    "it_pcn": ("http://wms.pcn.minambiente.it/ogc?map=/ms_ogc/WMS_v1.3/raster/ortofoto_colore_12.map&"
               "LAYERS=OI.ORTOIMMAGINI.2012.32,OI.ORTOIMMAGINI.2012.33&" + WMS,
               "Geoportale Nazionale (MASE) orthophoto 2012", (35.40, 6.60, 47.10, 18.60), "export", False),
    # Poland (a user's tip, 2026-10-22, from MapNG's developer): GUGiK's national orthophoto (Geoportal, ~25 cm; free
    # to download and use for any purpose, no access constraints in its INSPIRE record); its WMS has no Web Mercator
    "poland": ("https://mapy.geoportal.gov.pl/wss/service/PZGIK/ORTO/WMS/HighResolution?LAYERS=Raster&" + WMS_4326,
               "Ortofotomapa (c) GUGiK, geoportal.gov.pl", (49.00, 14.12, 54.84, 24.15), "export", False),
    "czechia": ("https://ags.cuzk.gov.cz/arcgis1/rest/services/ORTOFOTO_WM/MapServer/tile/{z}/{y}/{x}",
                "(c) CUZK ortofoto (CC BY 4.0)", (48.55, 12.09, 51.06, 18.86), "xyz", False),
    "estonia": ("https://tiles.maaamet.ee/tm/tms/1.0.0/foto@GMC/{z}/{x}/{ty}.jpg", "Ortofoto: Maa-amet",
                (57.50, 21.70, 59.70, 28.30), "xyz", False),
    "flanders": ("https://geo.api.vlaanderen.be/omw/wms?LAYERS=OMWRGB25VL&" + WMS, "Digitaal Vlaanderen orthophoto",
                 (50.68, 2.54, 51.51, 5.92), "export", True),
    "de_bayern": ("https://geoservices.bayern.de/od/wms/dop/v1/dop20?LAYERS=by_dop20c&" + WMS,
                  "Bayerische Vermessungsverwaltung DOP20 (CC BY 4.0)", (47.27, 8.97, 50.57, 13.84), "export", False),
    "de_nrw": ("https://www.wms.nrw.de/geobasis/wms_nw_dop?LAYERS=nw_dop_rgb&" + WMS, "Geobasis NRW DOP (dl-de/zero-2-0)",
               (50.32, 5.86, 52.53, 9.46), "export", False),
    "de_bw": ("https://owsproxy.lgl-bw.de/owsproxy/ows/WMS_LGL-BW_ATKIS_DOP_20_C?LAYERS=IMAGES_DOP_20_RGB&" + WMS,
              "LGL Baden-Wuerttemberg DOP20 (dl-de/by-2-0)", (47.53, 7.51, 49.79, 10.50), "export", False),
    "de_nds": ("https://opendata.lgln.niedersachsen.de/doorman/noauth/dop_wms?LAYERS=ni_dop20&" + WMS,
               "LGLN Niedersachsen DOP20 (dl-de/by-2-0)", (51.29, 6.65, 53.90, 11.60), "export", False),
    "de_berlin": ("https://gdi.berlin.de/services/wms/truedop_2024?LAYERS=truedop_2024&" + WMS,
                  "Geoportal Berlin TrueDOP 2024 (dl-de/zero-2-0)", (52.33, 13.08, 52.68, 13.77), "export", False),
    "de_hessen": ("https://www.gds-srv.hessen.de/cgi-bin/lika-services/ogc-free-images.ows?LAYERS=he_dop20_rgb&" + WMS,
                  "HVBG Hessen DOP20", (49.39, 7.77, 51.66, 10.24), "export", False),
    # --- Asia / Oceania / Americas -------------------------------------------------------------------------------
    "japan": ("https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg", "GSI Japan (Geospatial Information Authority of Japan) seamless photo",
              (24.0, 122.9, 45.6, 146.0), "xyz", False),
    "au_nsw": ("https://maps.six.nsw.gov.au/arcgis/rest/services/public/NSW_Imagery/MapServer/tile/{z}/{y}/{x}",
               "(c) Spatial Services NSW (CC BY 4.0)", (-37.6, 140.9, -28.1, 153.7), "xyz", False),
    "au_tas": ("https://services.thelist.tas.gov.au/arcgis/rest/services/Basemaps/Orthophoto/MapServer/tile/{z}/{y}/{x}",
               "(c) State of Tasmania theLIST", (-43.7, 143.8, -39.5, 148.5), "xyz", False),
    "ontario": ("https://ws.geoservices.lrc.gov.on.ca/arcgis5/rest/services/AerialImagery/"
                "GEO_Imagery_Data_Service_2018to2022/ImageServer/exportImage?" + ARCGIS_EXPORT,
                "Ontario GEO imagery (Open Government Licence - Ontario)", (41.7, -95.2, 56.9, -74.3), "export", False),
}
