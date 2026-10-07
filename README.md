# Wildfire Downloader

A small desktop tool for downloading wildfire detections and weather data for any area you pick on a map.

## Why it exists

Fire detections from NASA FIRMS and weather from Open-Meteo come from different APIs with different formats and limits. This tool pulls both for the same bounding box in one go, so the data can be used together in GIS software, Unity, or anywhere else.

## What it does

- Downloads fire detections from NASA FIRMS (VIIRS, MODIS or Landsat) for a date range. FIRMS only allows 5 days per request, so longer ranges are split into windows and duplicate rows are removed.
- Optionally downloads daily weather from Open-Meteo for a grid of points inside the same area: temperature, wind speed and direction, and cloud cover.
- Saves everything as JSON, CSV or Shapefile (WGS84, with a `.prj` file).

## How it works

1. Pick the area. Choose a preset, search for a place by name, type a bounding box (west,south,east,north), or click "Draw box on map" and click two opposite corners.
2. Set the dates, your FIRMS API key, the data source and the output format.
3. Tick "Also download Open-Meteo weather" if you want weather. "Grid points per side" sets how many points are sampled (5 means a 5x5 grid).
4. Choose an output folder and click Download.

Files are written as `wildfire_<start>_<end>` and `wildfire_weather_<start>_<end>`.

The FIRMS API key is free (https://firms.modaps.eosdis.nasa.gov/) and is never saved to the config file. The Open-Meteo archive has no data for the last few days, so the weather end date is cut back automatically.

## Running it

Windows: download `WildfireDownloader.exe` from the Releases page and run it. Windows may warn about an unknown publisher since the file is unsigned.

From source (Python 3.10+):

```
pip install -r requirements.txt
python wildfire_downloader_gui.py
```

To build the exe yourself:

```
pyinstaller --onefile --windowed --collect-all tkintermapview --name WildfireDownloader wildfire_downloader_gui.py
```
