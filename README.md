# Orchard Water irrigation engine

Weekly irrigation advice for drip and flood fields, built on the FAO-56 water balance and free
weather and satellite data. It was written for citrus orchards in the Souss-Massa region of
Morocco and sends advice as a WhatsApp message.

The company behind it has stopped and nobody maintains it actively. The code is published so
others can use and check it. Treat it as research code.

## Status

| Part | Status |
|---|---|
| FAO-56 hourly water balance (dual crop coefficient, salinity leaching, drip and flood) | Works. Crop and soil constants come from FAO-56 tables and are marked `TODO(verify)`. They were never checked on real farms. |
| Weekly advice for one field (`python -m orchardwater.weekly`) | Works with live Open-Meteo weather. Advice quality on real farms is untested. |
| Weekly WhatsApp loop: one message per farm, reply buttons, saved replies | Works end to end with the "print" sender. Sending real messages needs your own Meta WhatsApp Cloud API account. |
| Per-farm monthly crop factor (Kc) from the irrigation meter (`kc.py`) | Built. About 6 % season error on one orchard (see below). |
| Sentinel-2 greenness (NDVI) and Landsat surface temperature reads | Work against the free public catalogues. They are not used in the advice yet. |
| Soil model: FNO/PINO neural operator (`experiments/soil_operator`) | Research only. It learns this project's own 1D Richards solver from synthetic data. It was never validated on a real farm. |
| Question chatbot | Not built. `chatbot.py` is an empty hook. |

### Result on a private dataset

Tested on three seasons of a private dataset from a citrus orchard in Souss-Massa:

- The textbook FAO-56 crop factor overshot the orchard's real water use by about a third (about 37 %).
- Calibrating the crop factor per farm from the irrigation meter (`kc.py`) cut that to about
  6 %, using only the meter and free public weather.
- Daily error was halved.

The dataset is private and is not included in this repo.

## Quick start

Needs Python 3.12 or newer.

```bash
git clone <this repo>
cd <this repo>
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Weekly advice for one field (fetches a weather forecast from Open-Meteo):

```bash
python -m orchardwater.weekly --lat 30.35 --lon -9.45 --ha 12 --pump-m3h 25 --efficiency 0.9 --ec-water 1.2
```

Weekly message for the made-up example farm (prints the message and sends nothing):

```bash
python -m orchardwater.loop send --farm farms/example.json
```

To send real WhatsApp messages, copy `.env.example` to `.env`, fill in your own Meta tokens and
read [messages/README.md](messages/README.md). Never commit `.env` or real farm files. The
`.gitignore` already blocks `farms/*` (except the example), `fields/*` and `data/`.

## Free data sources

| Source | Used for | Terms |
|---|---|---|
| FAO-56 (Allen et al. 1998) | Method and tables | Published method |
| [Open-Meteo](https://open-meteo.com) | Weather forecast and archive, FAO-56 ET0 | Data CC BY 4.0. The free API is for non-commercial use only. Commercial use needs a paid plan. |
| NASA/USGS Landsat 8-9 via Microsoft Planetary Computer | Surface temperature | Public domain |
| Copernicus Sentinel-2 via Element 84 Earth Search (AWS) | Greenness (NDVI), colour pictures | Free and open, with attribution |

The download scripts are in [scripts/](scripts). No data is bundled.

## Layout

```
src/orchardwater/   engine: fao56, water_balance, weekly, kc, soil_model, weather,
                    landsat, earth_search, ndvi, thermal, loop, whatsapp, webhook ...
messages/, cards/   message wording templates (English, French, Arabic)
farms/, fields/     made-up example farm and field only
experiments/        soil_operator (FNO/PINO neural operator, research only)
scripts/            free data download helpers
docs/               FAO-56, Landsat and Sentinel-2 notes, plus learning notes
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Data sources keep their own
licenses (table above).
