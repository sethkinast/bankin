# BanKin — Bank of Kinast

A simple, boring ledger so the kids can track money they get (birthdays
and the like): deposits, withdrawals, a balance, and one optional saving
goal per kid. It is a record of cash held for them, not a bank account.

## Important: LAN-only, no login

BanKin has **no PIN and no authentication**. Anyone on the home network
who can reach it can see and change every kid's ledger. That is
deliberate for v1 (the brief says no PIN), so:

- Run it LAN-only, behind the house Caddy with the internal-only rule
  (the house caddy snippet already returns 403 to non-private IPs).
- Do not expose it to the internet, do not forward a port to it, and
  do not put it on a network guests can reach.

## What v1 does

- One account per kid, no interest, no fees. Amounts are integer cents.
- Deposits and withdrawals with a source/giver and an optional note.
  A withdrawal larger than the balance is rejected; the balance never
  goes negative from a withdrawal or a goal purchase.
- A single optional saving goal per kid: name, picture, price. The kid
  page shows balance vs. price as a percentage. Completing a goal
  records a withdrawal for its price and archives the goal.
- Kids are added via the UI. There are no seeded kids.
- A 240×240 JPEG TV card per kid at `/kid/<id>/tv.jpg` (name, balance,
  goal name/progress if any).

## GeekMagic TV push (stock firmware first)

Each kid has an optional TV address (IP/hostname, e.g. `192.168.2.50`),
set on the add-kid form or on the kid page. On every ledger change, and
via the explicit "Push to TV" button, BanKin pushes the card using the
stock SmallTV-Ultra pattern, with no authentication:

1. `POST` multipart, field `file`, filename `bankin-<kid_id>.jpg`, to
   `http://<tv_ip>/doUpload?dir=/image/`
2. `GET http://<tv_ip>/set?img=/image/bankin-<kid_id>.jpg`
3. `GET http://<tv_ip>/set?theme=3`

A push failure is logged and shown as a warning on the kid page. It
**never** rolls back or corrupts the ledger — the ledger commits first
and is the source of truth.

### How to test the TV push

1. Find a TV's IP (shown on its screen after it connects, or in the
   router's client list).
2. Enter that IP on the kid's page and save it.
3. Click "Push to TV", or make a small deposit.
4. Check the TV shows the new card. If it shows an older image, open
   the TV's web UI once and pick the uploaded `bankin-<id>.jpg` as the
   active picture; some stock firmwares only auto-update a file after
   it has been picked once.
5. If the push fails, check first that the NAS/container can reach
   `http://<tv_ip>/v.json` — the TVs are on the IoT VLAN and the
   container must be able to initiate to that VLAN.

Custom firmware is a fallback for a later version and is not in v1.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
BANKIN_DATA_DIR=./data PORT=8080 .venv/bin/python app.py
```

Open http://127.0.0.1:8080/. The SQLite database and goal pictures
land in `./data/` (git-ignored). Tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Deploy on the Synology (paste step)

**Seth pastes the snippet into his live compose and deploys; the agent
does not touch the NAS, its SSH, or its Docker.**

1. Wait for the GitHub workflow (`.github/workflows/build.yml`) to
   finish building `ghcr.io/sethkinast/bankin:latest` for this push.
2. Paste the `bankin:` service from `compose-snippet.yml` into the
   live `docker-compose.yml`, under `services:`.
3. Add `"bankin.${DOMAIN}:host-gateway"` to x-base's `extra_hosts` in
   `xbase.yml` (house hairpin pattern for a new public service).
4. Deploy in Synology Container Manager. Data persists in
   `/volume1/docker/appdata/bankin` (mounted at `/data` in the
   container) — the database and goal pictures live there, never in
   the repo.
5. Open `https://bankin.<your domain>` on the LAN, add the kids, and
   set each kid's TV IP on their page.

The app listens on port 8080 inside the container (a high port, never
:80 as a non-root process), and Caddy proxies to it via the labels in
the snippet.

## What is NOT in v1

- No interest, no fees, no real money movement.
- No PIN or authentication of any kind (see the LAN-only warning).
- No Google Home / voice queries.
- No custom GeekMagic firmware.
- No seeded kids, no multi-goal buckets, no earmarked goal funds —
  a goal is a target against the whole balance.

## Project layout

- `app.py` — the whole app: Flask routes, SQLite ledger, TV card
  rendering (Pillow), and the stock-firmware TV push.
- `templates/` — server-rendered pages, no JS framework.
- `tests/test_bankin.py` — balance math, goal progress, TV card size,
  TV push sequence, and the pinned example cases.
- `Dockerfile`, `compose-snippet.yml`, `.github/workflows/build.yml`
