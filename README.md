# KoulchiConnect Status

Uptime page for the KoulchiConnect app: https://mmgmolotov.github.io/koulchi-status/

A GitHub Actions job (`.github/workflows/check.yml`) runs `check.py` about
every 5 minutes. It checks the app server, login, database, realtime chat,
file storage and the push function, and stores only up/down and response
times on the `data` branch. While something is down an `outage` issue is
open (GitHub emails the owner); it closes when the service recovers.

Server address and test login are GitHub Actions secrets, never stored here.
