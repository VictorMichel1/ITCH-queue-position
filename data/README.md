# Data

The data files are too large for GitHub, so `.gitignore` keeps them out and this
note is the only file here in the repo. Two ways to fill the folder.

## Real data, NASDAQ TotalView-ITCH 5.0

NASDAQ publishes fifteen full trading days of raw ITCH at
`https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/`, free and without registration.
A full day is about 5.6 GB compressed. The study only needs the morning, so it
reads the first 2.7 GB (2,684,354,560 bytes) of 30 January 2020, which covers 04:00 to
12:28:

```bash
curl -r 0-2684354559 -o data/itch_20200130_prefix.gz \
  "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz"
```

The prefix is a truncated gzip stream with no end-of-stream marker. The reader
in `src/itch.py` handles that and decodes every complete message before the cut.

LOBSTER's free sample files, which many tutorials link to, are no longer served
from `data.lobsterdata.com`.

## Simulated data

```bash
python src/synth.py
```

writes `synthetic_itch.gz`, a 90 minute simulated session in genuine ITCH 5.0
binary for two made-up symbols. `python src/run_study.py --synthetic` writes it
afresh on every run.
