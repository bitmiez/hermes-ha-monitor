# Hermes Home Assistant Monitor

Dark live Home Assistant Lovelace card + Python collector for monitoring a Hermes/Codex host.

![Hermes Monitor dashboard screenshot](docs/assets/hermes-monitor-dashboard.png)

The screenshot shows the default dark Lovelace monitor with remaining Codex quota, server utilization, per-core CPU bars, and Auth1/Auth2 fallback status. Sensitive account data is redacted.

## Architecture

```text
Hermes/Codex host -> MQTT broker -> Home Assistant MQTT integration -> sensor.hermes_live_* -> Lovelace custom card
```

No Home Assistant long-lived token is required for the normal path. The collector publishes retained MQTT Discovery configs and one retained JSON state topic directly to the broker. Optional HA REST mirroring is still supported for local/private installs.

## What it shows

- OpenAI Codex remaining quota (5h + weekly windows)
- Codex CLI account/auth pool state and Auth1/Auth2 fallback status
- Latest Codex API error + credential-rotation signal from Hermes logs
- Host CPU/RAM/disk/load/uptime
- CPU pressure including iowait
- I/O wait as separate row
- Per-core CPU mini bars
- Atomic host metrics snapshot (`sensor.hermes_live_host_metrics_snapshot`) for smooth synchronized bars
- MQTT Discovery live sensors (`sensor.hermes_live_*`)

## Repository layout

```text
server/hermes_ha_monitor.py              # collector / MQTT publisher
systemd/hermes-ha-monitor.service        # oneshot service
systemd/hermes-ha-monitor.timer          # ~2s timer
homeassistant/hermes-live-monitor-card.js # live Lovelace custom card
homeassistant/hermes-monitor-card.yaml    # card stub for manual Lovelace card
homeassistant/hermes-monitor-card.json
tools/make-data-resource.py              # optional data: resource generator
docs/assets/hermes-monitor-dashboard.png
.env.example
```

## Requirements

- Linux host running Hermes/Codex CLI
- Home Assistant with MQTT integration connected to the same broker
- Python 3.11+
- MQTT broker credentials

No third-party Python package is required; the collector uses a tiny built-in MQTT 3.1.1 publisher over Python stdlib sockets.

The Lovelace UI uses a small vanilla web component, not `custom:button-card`. This avoids stale JS-template rendering while keeping the compact dark design.

## Install collector

```bash
sudo mkdir -p /opt/hermes-ha-monitor /var/lib/hermes-ha-monitor
sudo cp server/hermes_ha_monitor.py /opt/hermes-ha-monitor/hermes_ha_monitor.py
sudo chmod +x /opt/hermes-ha-monitor/hermes_ha_monitor.py
sudo cp .env.example /etc/hermes-ha-monitor.env
sudo chmod 600 /etc/hermes-ha-monitor.env
sudo editor /etc/hermes-ha-monitor.env
sudo cp systemd/hermes-ha-monitor.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-ha-monitor.timer
```

Minimal `/etc/hermes-ha-monitor.env`:

```env
MQTT_HOST=mqtt-broker.local
MQTT_PORT=1883
MQTT_USERNAME=hermes
MQTT_PASSWORD=CHANGEME
MQTT_TOPIC_PREFIX=hermes/monitor
```

Optional knobs:

```env
HERMES_AUTH_PATH=/root/.hermes/auth.json
CODEX_AUTH_PATH=/root/.codex/auth.json
CODEX_CONFIG_PATH=/root/.codex/config.toml
HERMES_HA_STATE_DIR=/var/lib/hermes-ha-monitor
HERMES_PRIMARY_AUTH_INDEX=1
HERMES_FALLBACK_AUTH_INDEX=2
HERMES_DEFAULT_ACTIVE_AUTH_INDEX=2
HERMES_TIMEZONE=Europe/Berlin
HERMES_LOG_PATHS=/root/.hermes/logs/errors.log:/root/.hermes/logs/agent.log
HERMES_AGENT_LOG_PATHS=/root/.hermes/logs/agent.log
```

## MQTT topics

- State topic: `hermes/monitor/state`
- Discovery topics: `homeassistant/sensor/hermes_live_*/config`

The state topic is one retained JSON object keyed by source entity id, e.g. `sensor.hermes_host_cpu_usage`. MQTT Discovery maps those into Home Assistant entities named `sensor.hermes_live_*`.

The collector also publishes `sensor.hermes_live_host_metrics_snapshot`. Its state changes every sample and its attributes contain `cpu_pct`, `core_pcts`, `ram_pct`, `disk_root_pct`, and `disk_data_pct`. The card reads host bars from this snapshot so CPU/RAM/disk update together instead of flickering independently.

## Home Assistant card

### Preferred: same-origin `/local` resource

Copy the web component to Home Assistant's `www` directory:

```bash
cp homeassistant/hermes-live-monitor-card.js /config/www/hermes-live-monitor-card.js
```

In Home Assistant, add a Lovelace resource:

```text
/local/hermes-live-monitor-card.js?v=1
Type: JavaScript module
```

Then add `homeassistant/hermes-monitor-card.yaml` as a manual Lovelace card:

```yaml
type: custom:hermes-live-monitor-card
```

### Alternative: self-contained data resource

If you cannot write to `/config/www`, generate a self-contained module URL:

```bash
python3 tools/make-data-resource.py
```

Add the printed `data:text/javascript;base64,...` value as a Lovelace JavaScript module resource, then use the same manual card YAML above. This mirrors the tested mobile-safe setup where external HTTP resources were blocked by Home Assistant/mobile clients.

## Refresh behavior

Default systemd timer:

```ini
OnUnitActiveSec=2s
AccuracySec=1s
```

The collector publishes roughly every 2 seconds. The custom card re-renders from Home Assistant's live `hass` state updates and preserves the original compact dark design.

## Security / privacy

Do not commit real MQTT passwords, Home Assistant tokens/URLs, account emails, auth JSON files, or state files. This repo includes only templates and code. The collector reads secrets from `/etc/hermes-ha-monitor.env` or environment variables.

Published sensors intentionally avoid raw tokens. Auth entries are matched using short token fingerprints and non-secret metadata only.

## Legacy Home Assistant REST mode

If `HOMEASSISTANT_URL` and `HOMEASSISTANT_TOKEN` are set, the collector can additionally post REST state updates to Home Assistant. Normal recommended mode is direct MQTT only.
