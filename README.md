# Hermes Home Assistant Monitor

Dark Home Assistant Lovelace card + Python collector for monitoring a Hermes/Codex host.

## What it shows

- OpenAI Codex remaining quota (5h + weekly windows)
- Codex CLI account/auth pool state and fallback status
- Host CPU/RAM/disk/load/uptime
- CPU pressure including iowait
- I/O wait as separate row
- Per-core CPU mini bars
- MQTT Discovery live sensors (`sensor.hermes_live_*`) plus legacy REST-created sensors

## Repository layout

```text
server/hermes_ha_monitor.py          # collector / HA pusher
systemd/hermes-ha-monitor.service    # oneshot service
systemd/hermes-ha-monitor.timer      # ~2s timer
homeassistant/hermes-monitor-card.yaml
homeassistant/hermes-monitor-card.json
.env.example
```

## Requirements

- Linux host running Hermes/Codex CLI
- Home Assistant with:
  - MQTT integration enabled
  - `custom:button-card` installed
- Python 3.11+
- `websocket-client` Python package
- Home Assistant long-lived access token

## Install

```bash
sudo mkdir -p /opt/hermes-ha-monitor /var/lib/hermes-ha-monitor
sudo cp server/hermes_ha_monitor.py /opt/hermes-ha-monitor/hermes_ha_monitor.py
sudo chmod +x /opt/hermes-ha-monitor/hermes_ha_monitor.py
sudo cp .env.example /etc/hermes-ha-monitor.env
sudo editor /etc/hermes-ha-monitor.env
sudo cp systemd/hermes-ha-monitor.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-ha-monitor.timer
```

Install Python dependency if needed:

```bash
python3 -m pip install websocket-client
```

## Home Assistant card

Add `homeassistant/hermes-monitor-card.yaml` as a manual Lovelace card. The card expects the MQTT live entities created by the collector, e.g.:

- `sensor.hermes_live_openai_codex_5h_remaining`
- `sensor.hermes_live_host_cpu_usage`
- `sensor.hermes_live_host_cpu_core_0_usage`
- `sensor.hermes_live_host_iowait_usage`

First collector run publishes MQTT discovery. If counters are new, the first run seeds CPU deltas; the second run produces CPU/per-core values.

## Refresh rate

Default systemd timer:

```ini
OnUnitActiveSec=2s
AccuracySec=1s
```

So data push is roughly every 2 seconds. Lovelace re-render depends on HA/browser WebSocket updates.

## Security / privacy

Do not commit real tokens, Home Assistant URLs, account emails, or auth JSON files. This repo includes only templates and code. The collector reads secrets from `/etc/hermes-ha-monitor.env` or environment variables.

## Notes

The collector uses Home Assistant REST `/api/states` for compatibility and mirrors all Hermes sensors to MQTT Discovery through HA WebSocket `mqtt.publish`, because REST service calls can appear successful while MQTT discovery does not update reliably on some HA setups.
