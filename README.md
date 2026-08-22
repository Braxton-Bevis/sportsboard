# Sportsboard

A spoiler-safe sports dashboard for an original Jetson Nano. It shows today's
fixtures for your teams, scores them on a delay so the board never runs ahead of
your TV, and scrolls the next week along the bottom.

Three files, no dependencies beyond Python 3 and a browser:

| File | What it does |
|---|---|
| `sportsboard.py` | Polls ESPN, holds the delay buffer, serves the page |
| `index.html` | The screen |
| `config.json` | Your teams, the delay, refresh rates |

## How the delay works

The server keeps every snapshot it fetches, tagged with the time it arrived, and
`/api/board` only ever hands out a snapshot that is at least `delay_seconds`
old. A newer score never reaches the browser, so nothing on the page can leak
ahead of your broadcast. The header shows the real measured offset (`−3:04`),
not a decorative label.

For the first three minutes after startup the buffer isn't deep enough to be
safe, so the server strips scores, clocks and final results out of the payload
entirely and the page shows a countdown until they unlock. Cold start can't
spoil you either.

## Run it

```bash
mkdir -p ~/sportsboard && cd ~/sportsboard
# copy sportsboard.py, index.html and config.json here
python3 sportsboard.py
```

Then open `http://localhost:8080`, or hit it from any other machine on your
network at `http://<nano-ip>:8080`.

Keyboard: `f` fullscreen, `r` force refresh, `c` toggle the mouse cursor,
`s` toggle auto-scroll.

## On a 20in 1080p / 900p panel

Tuned for it. Below 940px tall the header, cards and ticker tighten up so live,
upcoming and finished all sit on one screen instead of two, and the grid holds
four columns at 1600px wide.

A full NFL Sunday will still overflow, so the board creeps down at about 22px a
second, pauses seven seconds at the bottom, and comes back up. Scrolling by hand
pauses it for thirty seconds, `s` turns it off, and it never runs if the system
asks for reduced motion.

## Kiosk mode

Keep the screen awake and launch Chromium pointed at the board:

```bash
xset s off && xset -dpms && xset s noblank
chromium-browser --kiosk --incognito --noerrdialogs --disable-infobars \
  --disable-session-crashed-bubble --check-for-update-interval=31536000 \
  http://localhost:8080
```

Start the server on boot with systemd (replace `braxton` with your username):

```ini
# /etc/systemd/system/sportsboard.service
[Unit]
Description=Sportsboard
After=network-online.target

[Service]
User=braxton
WorkingDirectory=/home/braxton/sportsboard
ExecStart=/usr/bin/python3 /home/braxton/sportsboard/sportsboard.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now sportsboard
```

Start the browser on login by dropping a launcher in `~/.config/autostart/`:

```ini
# ~/.config/autostart/sportsboard.desktop
[Desktop Entry]
Type=Application
Name=Sportsboard
Exec=chromium-browser --kiosk --incognito http://localhost:8080
X-GNOME-Autostart-enabled=true
```

## Editing your teams

Every entry in `config.json` is one ESPN competition plus a list of team-name
fragments to keep. `"teams": "*"` keeps every game in that competition — that's
how SEC football and the NFL are set up.

```json
{ "key": "epl", "label": "PREMIER LEAGUE", "path": "soccer/eng.1", "teams": ["manchester city"] }
```

Matching is a case-insensitive substring test against both teams' names, so
`"nashville"` catches Nashville SC and `"real madrid"` won't catch Real Betis.
Add `"enabled": false` to park a competition without deleting it.

Other settings:

- `delay_seconds` — 180 by default. Raise it if your provider lags harder.
- `poll_seconds` — how often a competition with a game today is refreshed.
- `cold_poll_seconds` — how often everything else is refreshed. Only feeds with
  a game today or in progress poll fast, which keeps 32 competitions down to a
  trickle of requests in the off-season.
- `days_ahead` — how far the bottom ticker looks.
- `user_agent` — only touch this if a network blocks the default.

## What's already wired up

SEC football (all conference games plus SEC teams in bowls and the playoff),
the full NFL slate, Dodgers and Pirates, Nashville SC and Inter Miami (league,
Leagues Cup, Concacaf Champions Cup, U.S. Open Cup), Manchester City, Barcelona
and Real Madrid (league, Champions League and qualifying, Europa, domestic cups,
Super Cup, Club World Cup), Flamengo (Brasileirão, Libertadores, Sudamericana,
Copa do Brasil, Recopa), Middlesbrough (Championship, FA Cup, Carabao Cup), and
the USMNT across friendlies, World Cup qualifying, the World Cup, Gold Cup,
Nations League and the Olympics.

Youth: ESPN carries the U20 World Cup and the Concacaf U23 tournament, both
enabled. It does not publish a U19 US national team feed — U20 is the lowest
level with real coverage, so that's what's here. If a U19 feed appears later,
add it as one more line in `config.json`.

## Notes on the OG Nano

The animation is deliberately cheap: transforms and opacity only, no blur, no
`backdrop-filter`, no canvas. Cards are patched in place rather than re-rendered,
so the score flash and the tally pulse don't restart every 15 seconds. It should
sit comfortably at 1080p on Maxwell.

If the fonts don't load, the page falls back to condensed system faces and still
looks right. If the network drops, the last good board stays on screen and the
header switches to "No link to server" — it won't blank out on you.

## The data

Everything comes from ESPN's public `site.api.espn.com` scoreboard endpoints.
There's no API key and no signup, but it's also undocumented, so a competition
slug can change without warning. If one competition goes quiet, the header will
show "1 feed down" and the rest of the board keeps working.
