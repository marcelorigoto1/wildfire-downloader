import csv
import io
import json
import threading
import tkinter as tk
import urllib.parse
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import shapefile
import tkintermapview

PRESETS = {
    "Paraguay": "-62.7,-27.6,-54.2,-19.0",
    "Portugal": "-9.6,36.8,-6.1,42.2",
    "Australia": "140.0,-39.0,154.0,-22.0",
    "Hawaii": "-160.3,18.7,-154.5,22.3",
    "Evros": "25.4,40.7,26.4,41.8",
    "California": "-124.5,32.4,-114.1,42.0",
    "Chile": "-75.8,-56.0,-66.4,-17.5",
    "Custom": "",
}
SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT", "LANDSAT_NRT"]
CONFIG_PATH = Path(__file__).with_name("wildfire_downloader_config.json")
MAX_DAYS = 5  # FIRMS area API window limit
FORMATS = ["json", "csv", "shapefile"]
ARCHIVE_LAG_DAYS = 5  # Open-Meteo archive has no data for the most recent days
WEATHER_BATCH = 10
# Open-Meteo daily variable -> output field (names match the Unity weather layers)
WEATHER_VARS = {
    "temperature_2m_mean": "temperature_2m_mean_c",
    "temperature_2m_max": "temperature_2m_max_c",
    "temperature_2m_min": "temperature_2m_min_c",
    "wind_speed_10m_mean": "wind_speed_10m_mean_ms",
    "wind_speed_10m_max": "wind_speed_10m_max_ms",
    "wind_direction_10m_dominant": "wind_direction_10m_dominant_deg",
    "cloud_cover_mean": "cloud_cover_mean_pct",
    "cloud_cover_max": "cloud_cover_max_pct",
}


def _to_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_to_records(header, rows):
    # A column becomes numeric only if every non-empty value parses as a number.
    numeric = {
        i for i in range(len(header))
        if all(r[i] == "" or _to_number(r[i]) is not None for r in rows)
    }
    return [
        {h: (_to_number(r[i]) if i in numeric and r[i] != "" else (r[i] or None)) for i, h in enumerate(header)}
        for r in rows
    ]


def write_dataset(base, records, fmt, metadata):
    """Write records as json/csv/shapefile; returns the main output path."""
    fields = list(records[0].keys()) if records else []
    if fmt == "json":
        path = base.with_suffix(".json")
        path.write_text(json.dumps({"metadata": metadata, "records": records}, indent=2), encoding="utf-8")
    elif fmt == "csv":
        path = base.with_suffix(".csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
    else:
        path = base.with_suffix(".shp")
        with shapefile.Writer(str(base), shapeType=shapefile.POINT) as shp:
            names = []
            for f in fields:
                short = f[:10]
                while short in names:  # DBF field names are limited to 10 chars
                    short = short[:9] + str(len(names) % 10)
                names.append(short)
                is_num = all(isinstance(r[f], (int, float)) or r[f] is None for r in records)
                shp.field(short, "F" if is_num else "C", 20 if is_num else 80, 6 if is_num else 0)
            for r in records:
                shp.point(r["longitude"], r["latitude"])
                shp.record(*[r[f] for f in fields])
        base.with_suffix(".prj").write_text(
            'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
            'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
    return path


def fetch_weather(bbox, start, end, grid, log, cancelled):
    w, s, e, n = (float(x) for x in bbox.split(","))
    end = min(end, date.today() - timedelta(days=ARCHIVE_LAG_DAYS))
    if end < start:
        log("Weather skipped: date range is too recent for the Open-Meteo archive")
        return []
    log(f"Weather range: {start} to {end}")
    step = lambda lo, hi, i: (lo + hi) / 2 if grid == 1 else lo + (hi - lo) * i / (grid - 1)
    points = [(round(step(s, n, i), 4), round(step(w, e, j), 4)) for i in range(grid) for j in range(grid)]
    records = []
    for b in range(0, len(points), WEATHER_BATCH):
        if cancelled():
            raise InterruptedError("Cancelled")
        batch = points[b:b + WEATHER_BATCH]
        query = urllib.parse.urlencode({
            "latitude": ",".join(str(p[0]) for p in batch),
            "longitude": ",".join(str(p[1]) for p in batch),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": ",".join(WEATHER_VARS),
            "wind_speed_unit": "ms",
            "timezone": "UTC",
        }, safe=",")
        log(f"Weather points {b + 1}-{b + len(batch)} of {len(points)}")
        try:
            with urllib.request.urlopen("https://archive-api.open-meteo.com/v1/archive?" + query, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            raise RuntimeError(f"Open-Meteo: {ex.read().decode('utf-8', 'replace')[:200]}") from ex
        for k, loc in enumerate(data if isinstance(data, list) else [data]):
            daily = loc["daily"]
            for d, day in enumerate(daily["time"]):
                rec = {"point_id": f"P{b + k:04d}", "latitude": loc["latitude"], "longitude": loc["longitude"], "date": day}
                for src, dst in WEATHER_VARS.items():
                    rec[dst] = daily[src][d]
                records.append(rec)
    return records


def fetch_rows(key, source, bbox, start, end, log, cancelled):
    rows, seen, header = [], set(), None
    cur = start
    while cur <= end:
        if cancelled():
            raise InterruptedError("Cancelled")
        days = min(MAX_DAYS, (end - cur).days + 1)
        # FIRMS expects raw commas in the area path; do not URL-encode.
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}/{cur.isoformat()}"
        log(f"Fetching {cur} (+{days}d)")
        with urllib.request.urlopen(url, timeout=60) as r:
            text = r.read().decode("utf-8", "replace")
        if not text.lstrip().lower().startswith("latitude"):
            raise RuntimeError(text.strip()[:200] or "Empty response")
        reader = csv.reader(io.StringIO(text))
        h = next(reader)
        header = header or h
        for row in reader:
            k = tuple(row)
            if k not in seen:
                seen.add(k)
                rows.append(row)
        cur += timedelta(days=days)
    return header, rows


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Wildfire Downloader")
        self.geometry("760x980")
        self.cancel_flag = False
        self.draw_mode = False
        self.first_corner = None
        self.box_polygon = None
        self.vars = {
            "preset": tk.StringVar(value="Paraguay"),
            "bbox": tk.StringVar(value=PRESETS["Paraguay"]),
            "start": tk.StringVar(value=(date.today() - timedelta(days=4)).isoformat()),
            "end": tk.StringVar(value=date.today().isoformat()),
            "key": tk.StringVar(),
            "source": tk.StringVar(value=SOURCES[0]),
            "format": tk.StringVar(value=FORMATS[0]),
            "weather": tk.BooleanVar(value=False),
            "grid": tk.IntVar(value=5),
            "out": tk.StringVar(value=str(Path(__file__).parent.parent / "pipeline_output")),
        }
        self._load_config()
        self._build()

    def _build(self):
        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        def row(i, label, widget):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=3)
            widget.grid(row=i, column=1, sticky="ew", pady=3)

        preset_row = ttk.Frame(f)
        preset_row.columnconfigure(2, weight=1)
        preset = ttk.Combobox(preset_row, textvariable=self.vars["preset"], values=list(PRESETS), state="readonly", width=14)
        preset.bind("<<ComboboxSelected>>", self._on_preset)
        preset.grid(row=0, column=0)
        ttk.Label(preset_row, text="Search online").grid(row=0, column=1, padx=(10, 4))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(preset_row, textvariable=self.search_var)
        entry.grid(row=0, column=2, sticky="ew")
        entry.bind("<Return>", lambda _: self._search_region())
        ttk.Button(preset_row, text="Search", command=self._search_region).grid(row=0, column=3, padx=(4, 0))
        row(0, "Region preset", preset_row)
        row(1, "Bounding box (W,S,E,N)", ttk.Entry(f, textvariable=self.vars["bbox"]))
        row(2, "Start date (YYYY-MM-DD)", ttk.Entry(f, textvariable=self.vars["start"]))
        row(3, "End date (YYYY-MM-DD)", ttk.Entry(f, textvariable=self.vars["end"]))
        row(4, "FIRMS API key", ttk.Entry(f, textvariable=self.vars["key"], show="*"))
        row(5, "Data source", ttk.Combobox(f, textvariable=self.vars["source"], values=SOURCES))

        out = ttk.Frame(f)
        out.columnconfigure(0, weight=1)
        ttk.Entry(out, textvariable=self.vars["out"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(out, text="Browse", command=self._browse).grid(row=0, column=1, padx=(4, 0))
        row(8, "Output folder", out)

        row(6, "Data format", ttk.Combobox(f, textvariable=self.vars["format"], values=FORMATS, state="readonly"))
        weather = ttk.Frame(f)
        ttk.Checkbutton(weather, text="Also download Open-Meteo weather", variable=self.vars["weather"]).pack(side="left")
        ttk.Label(weather, text="Grid points per side").pack(side="left", padx=(12, 4))
        ttk.Spinbox(weather, from_=1, to=10, width=4, textvariable=self.vars["grid"]).pack(side="left")
        row(7, "Weather", weather)

        btns = ttk.Frame(f)
        btns.grid(row=9, column=0, columnspan=2, pady=8)
        self.run_btn = ttk.Button(btns, text="Download", command=self._run)
        self.run_btn.pack(side="left", padx=4)
        self.cancel_btn = ttk.Button(btns, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Save config", command=self._save_config).pack(side="left", padx=4)

        map_bar = ttk.Frame(f)
        map_bar.grid(row=10, column=0, columnspan=2, sticky="ew")
        self.draw_btn = ttk.Button(map_bar, text="Draw box on map", command=self._toggle_draw)
        self.draw_btn.pack(side="left")
        ttk.Button(map_bar, text="Apply typed box", command=self._show_bbox).pack(side="left", padx=4)
        self.map_hint = ttk.Label(map_bar, text="Drag to pan, wheel to zoom")
        self.map_hint.pack(side="left", padx=8)

        self.map = tkintermapview.TkinterMapView(f, height=320, corner_radius=0)
        self.map.grid(row=11, column=0, columnspan=2, sticky="nsew", pady=4)
        self.map.add_left_click_map_command(self._map_click)
        f.rowconfigure(11, weight=3)
        self.after(200, self._show_bbox)

        log_frame = ttk.LabelFrame(f, text="Log")
        log_frame.grid(row=12, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_box = tk.Text(log_frame, height=8, state="disabled")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        f.rowconfigure(12, weight=1)

    def _on_preset(self, _=None):
        name = self.vars["preset"].get()
        if PRESETS[name]:
            self.vars["bbox"].set(PRESETS[name])
            self._show_bbox()

    def _search_region(self):
        query = self.search_var.get().strip()
        if not query:
            return
        self._log(f"Searching for '{query}'...")

        def work():
            try:
                url = "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" + urllib.parse.quote(query)
                req = urllib.request.Request(url, headers={"User-Agent": "WildfireDownloader/1.0"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    results = json.loads(r.read().decode("utf-8"))
                if not results:
                    self._log("No region found")
                    return
                s, n, w, e = (float(x) for x in results[0]["boundingbox"])  # Nominatim order: S,N,W,E
                bbox = f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}"
                self._log(f"Found: {results[0]['display_name']}")
                self.after(0, lambda: (self.vars["preset"].set("Custom"), self.vars["bbox"].set(bbox), self._show_bbox()))
            except Exception as ex:
                self._log(f"Search failed: {ex}")

        threading.Thread(target=work, daemon=True).start()

    def _toggle_draw(self):
        self.draw_mode = not self.draw_mode
        self.first_corner = None
        self.draw_btn.config(text="Cancel drawing" if self.draw_mode else "Draw box on map")
        self.map_hint.config(text="Click first corner" if self.draw_mode else "Drag to pan, wheel to zoom")

    def _map_click(self, coords):
        if not self.draw_mode:
            return
        if self.first_corner is None:
            self.first_corner = coords
            self.map_hint.config(text="Click opposite corner")
            return
        (lat1, lon1), (lat2, lon2) = self.first_corner, coords
        bbox = f"{min(lon1, lon2):.4f},{min(lat1, lat2):.4f},{max(lon1, lon2):.4f},{max(lat1, lat2):.4f}"
        self.vars["preset"].set("Custom")
        self.vars["bbox"].set(bbox)
        self._toggle_draw()
        self._show_bbox(fit=False)

    def _show_bbox(self, fit=True):
        try:
            w, s, e, n = (float(x) for x in self.vars["bbox"].get().split(","))
        except ValueError:
            return
        if self.box_polygon:
            self.box_polygon.delete()
        self.box_polygon = self.map.set_polygon(
            [(n, w), (n, e), (s, e), (s, w)], outline_color="red", fill_color=None, border_width=3)
        if fit:
            self.map.fit_bounding_box((n, w), (s, e))

    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.vars["out"].set(d)

    def _log(self, msg):
        def write():
            self.log_box.config(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, write)

    def _load_config(self):
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                for k, v in data.items():
                    if k in self.vars:
                        self.vars[k].set(v)
            except (OSError, ValueError):
                pass

    def _save_config(self):
        data = {k: v.get() for k, v in self.vars.items() if k != "key"}  # never persist the API key
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
        self._log(f"Config saved to {CONFIG_PATH}")

    def _cancel(self):
        self.cancel_flag = True

    def _run(self):
        try:
            start = datetime.strptime(self.vars["start"].get(), "%Y-%m-%d").date()
            end = datetime.strptime(self.vars["end"].get(), "%Y-%m-%d").date()
            w, s, e, n = (float(x) for x in self.vars["bbox"].get().split(","))
        except ValueError:
            messagebox.showerror("Invalid input", "Check dates (YYYY-MM-DD) and bbox (W,S,E,N).")
            return
        if end < start or not self.vars["key"].get().strip():
            messagebox.showerror("Invalid input", "End must be >= start and an API key is required.")
            return
        bbox = f"{min(w, e)},{min(s, n)},{max(w, e)},{max(s, n)}"
        self.cancel_flag = False
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        grid = max(1, min(10, self.vars["grid"].get()))
        args = (self.vars["key"].get().strip(), self.vars["source"].get(), bbox, start, end,
                Path(self.vars["out"].get()), self.vars["format"].get(), self.vars["weather"].get(), grid)
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, key, source, bbox, start, end, out_dir, fmt, with_weather, grid):
        try:
            cancelled = lambda: self.cancel_flag
            header, rows = fetch_rows(key, source, bbox, start, end, self._log, cancelled)
            out_dir.mkdir(parents=True, exist_ok=True)
            span = f"{start:%Y%m%d}_{end:%Y%m%d}"
            meta = {"bbox": bbox, "start_date": str(start), "end_date": str(end)}
            fires = rows_to_records(header, rows) if header else []
            if fires:
                path = write_dataset(out_dir / f"wildfire_{span}", fires, fmt, {**meta, "source": source})
                self._log(f"Done: {len(fires)} detections -> {path}")
            else:
                self._log("No fire detections found")
            if with_weather:
                weather = fetch_weather(bbox, start, end, grid, self._log, cancelled)
                if weather:
                    path = write_dataset(out_dir / f"wildfire_weather_{span}", weather, fmt,
                                         {**meta, "provider": "open-meteo", "grid_points": grid * grid})
                    self._log(f"Done: {len(weather)} weather records -> {path}")
        except Exception as ex:  # surfaced to the user log
            self._log(f"Failed: {ex}")
        finally:
            self.after(0, lambda: (self.run_btn.config(state="normal"), self.cancel_btn.config(state="disabled")))


if __name__ == "__main__":
    App().mainloop()
