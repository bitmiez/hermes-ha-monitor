#!/usr/bin/env python3
"""Publish Hermes host + OpenAI Codex status sensors via MQTT Discovery.

No secrets are printed. MQTT credentials are read from a root-only env file or environment variables.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from zoneinfo import ZoneInfo

ENV_PATH = pathlib.Path(os.environ.get('HERMES_HA_ENV_PATH', '/etc/hermes-ha-monitor.env'))
HERMES_AUTH = pathlib.Path(os.environ.get('HERMES_AUTH_PATH', '/root/.hermes/auth.json'))
CODEX_AUTH = pathlib.Path(os.environ.get('CODEX_AUTH_PATH', '/root/.codex/auth.json'))
CODEX_CONFIG = pathlib.Path(os.environ.get('CODEX_CONFIG_PATH', '/root/.codex/config.toml'))
CODEX_ACCOUNTS_ROOT = pathlib.Path(os.environ.get('HERMES_CODEX_ACCOUNTS_ROOT', '/opt/hermes-ha-monitor/codex-accounts'))
STATE_DIR = pathlib.Path(os.environ.get('HERMES_HA_STATE_DIR', '/var/lib/hermes-ha-monitor'))
STATE_FILE = STATE_DIR / 'state.json'
PRIMARY_AUTH_INDEX = int(os.environ.get('HERMES_PRIMARY_AUTH_INDEX', '1'))
FALLBACK_AUTH_INDEX = int(os.environ.get('HERMES_FALLBACK_AUTH_INDEX', '2'))
MQTT_OUTBOX: list[tuple[str, str, bool]] = []
MQTT_STATE: dict[str, dict[str, Any]] = {}
MQTT_DISCOVERY_DUE = False


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def iso_from_epoch(epoch: int | float | None) -> str | None:
    if not epoch:
        return None
    try:
        return dt.datetime.fromtimestamp(float(epoch), tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


def local_reset_display(epoch: int | float | None, mode: str) -> str | None:
    if not epoch:
        return None
    try:
        local = dt.datetime.fromtimestamp(float(epoch), tz=dt.timezone.utc).astimezone(ZoneInfo(os.environ.get('HERMES_TIMEZONE', 'Europe/Berlin')))
    except Exception:
        return None
    if mode == 'time':
        return local.strftime('%H:%M')
    months = ['Jan.', 'Feb.', 'März', 'Apr.', 'Mai', 'Juni', 'Juli', 'Aug.', 'Sept.', 'Okt.', 'Nov.', 'Dez.']
    return f'{local.day}. {months[local.month - 1]}'


def token_fp(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def _cpu_line_snapshot(line: str) -> dict[str, int]:
    parts = line.split()
    nums = [int(x) for x in parts[1:]]
    idle_only = nums[3]
    iowait = nums[4] if len(nums) > 4 else 0
    return {'total': sum(nums), 'idle_only': idle_only, 'iowait': iowait}


def read_cpu_snapshot() -> dict[str, Any]:
    lines = pathlib.Path('/proc/stat').read_text().splitlines()
    total = _cpu_line_snapshot(lines[0])
    cores: list[dict[str, int]] = []
    for line in lines[1:]:
        if not line.startswith('cpu'):
            break
        name = line.split()[0]
        if name[3:].isdigit():
            cores.append(_cpu_line_snapshot(line))
    total['cores'] = cores
    return total


def _cpu_pct(cur: dict[str, int], old: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    if 'total' not in old or 'idle_only' not in old:
        return None, None, None
    d_total = int(cur['total']) - int(old['total'])
    d_idle_only = int(cur['idle_only']) - int(old['idle_only'])
    d_iowait = int(cur.get('iowait', 0)) - int(old.get('iowait', 0))
    if d_total <= 0:
        return None, None, None
    pressure_pct = round((1 - d_idle_only / d_total) * 100, 1)
    work_pct = round((1 - (d_idle_only + d_iowait) / d_total) * 100, 1)
    iowait_pct = round((d_iowait / d_total) * 100, 1)
    return pressure_pct, work_pct, iowait_pct


def cpu_percent(prev: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    cur = read_cpu_snapshot()
    old = prev.get('cpu') or {}
    pct, work_pct, iowait_pct = _cpu_pct(cur, old)
    core_pcts: list[float | None] = []
    core_work_pcts: list[float | None] = []
    old_cores = old.get('cores') if isinstance(old.get('cores'), list) else []
    for idx, core in enumerate(cur.get('cores', [])):
        old_core = old_cores[idx] if idx < len(old_cores) and isinstance(old_cores[idx], dict) else {}
        cp, cw, _ci = _cpu_pct(core, old_core)
        core_pcts.append(cp)
        core_work_pcts.append(cw)
    cur['work_pct'] = work_pct
    cur['iowait_pct'] = iowait_pct
    cur['core_pcts'] = core_pcts
    cur['core_work_pcts'] = core_work_pcts
    cur['core_count'] = len(cur.get('cores', []))
    return pct, cur


def mem_percent() -> tuple[float | None, dict[str, int]]:
    vals: dict[str, int] = {}
    for line in pathlib.Path('/proc/meminfo').read_text().splitlines():
        key, rest = line.split(':', 1)
        vals[key] = int(rest.strip().split()[0]) * 1024
    total = vals.get('MemTotal')
    avail = vals.get('MemAvailable')
    if not total or avail is None:
        return None, vals
    return round((1 - avail / total) * 100, 1), vals


def disk_percent(path: str) -> tuple[float | None, dict[str, int]]:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        used = total - avail
        pct = round((used / total) * 100, 1) if total else None
        return pct, {'total': total, 'used': used, 'available': avail}
    except Exception:
        return None, {}


def loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except Exception:
        return None


def uptime_seconds() -> int | None:
    try:
        return int(float(pathlib.Path('/proc/uptime').read_text().split()[0]))
    except Exception:
        return None


def _atomic_private_json(path: pathlib.Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, separators=(',', ':')))
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def sync_codex_account_homes(indices: list[int]) -> dict[int, str]:
    """Sync Hermes OAuth pool entries to isolated Codex homes without touching ~/.codex."""
    pool = (((read_json(HERMES_AUTH, {}) or {}).get('credential_pool') or {}).get('openai-codex') or [])
    base_template = read_json(CODEX_AUTH, {}) or {}
    result: dict[int, str] = {}
    for idx in indices:
        if idx < 1 or idx > len(pool):
            result[idx] = 'pool_entry_missing'
            continue
        entry = pool[idx - 1]
        if not entry.get('access_token') or not entry.get('refresh_token'):
            result[idx] = 'pool_tokens_missing'
            continue
        auth_path = CODEX_ACCOUNTS_ROOT / f'auth{idx}' / 'auth.json'
        existing = read_json(auth_path, {}) or {}
        # Existing isolated home retains account_id/id_token if Codex has refreshed them.
        auth = dict(existing or base_template)
        tokens = dict((existing or base_template).get('tokens') or {})
        tokens['access_token'] = entry['access_token']
        tokens['refresh_token'] = entry['refresh_token']
        auth['tokens'] = tokens
        auth['auth_mode'] = 'chatgpt'
        auth['OPENAI_API_KEY'] = None
        auth['last_refresh'] = entry.get('last_refresh') or auth.get('last_refresh')
        _atomic_private_json(auth_path, auth)
        result[idx] = 'ok'
    return result


def codex_quota_windows(rate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(rate, dict):
        return None
    by_id = rate.get('rateLimitsByLimitId') or {}
    snap = by_id.get('codex') or rate.get('rateLimits')
    if not isinstance(snap, dict):
        return None
    primary = snap.get('primary') or {}
    secondary = snap.get('secondary') or {}
    windows = [w for w in (primary, secondary) if isinstance(w, dict) and w]
    five_hour = next((w for w in windows if w.get('windowDurationMins') in range(240, 361)), {})
    weekly = next((w for w in windows if w.get('windowDurationMins') in range(9000, 11000)), {})
    if not five_hour and primary.get('windowDurationMins') in (None, 0) and secondary:
        five_hour = primary
    if not weekly and secondary.get('windowDurationMins') in (None, 0):
        weekly = secondary
    return {'snap': snap, 'primary': primary, 'secondary': secondary, 'five_hour': five_hour, 'weekly': weekly}


def run_codex_app_server(codex_home: str | None = None, timeout: float = 15) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    if shutil.which('codex') is None:
        return None, None, 'codex command missing'
    env = os.environ.copy()
    if codex_home:
        env['CODEX_HOME'] = codex_home
    p = subprocess.Popen(
        ['codex', 'app-server', '--listen', 'stdio://'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=0,
        env=env,
    )
    assert p.stdin and p.stdout and p.stderr

    def send(obj: dict[str, Any]) -> None:
        p.stdin.write(json.dumps(obj, separators=(',', ':')) + '\n')
        p.stdin.flush()

    try:
        send({'id': 0, 'method': 'initialize', 'params': {'clientInfo': {'name': 'hermes-ha-monitor', 'version': '1.0'}, 'capabilities': {'experimentalApi': True}}})
        send({'method': 'initialized', 'params': {}})
        send({'id': 1, 'method': 'account/read', 'params': {}})
        send({'id': 2, 'method': 'account/rateLimits/read', 'params': {}})
        out: list[str] = []
        err: list[str] = []
        deadline = time.time() + timeout
        while time.time() < deadline and len([x for x in out if '"id":1' in x or '"id":2' in x]) < 2:
            ready, _, _ = select.select([p.stdout, p.stderr], [], [], 0.2)
            for fd in ready:
                line = fd.readline()
                if not line:
                    continue
                if fd is p.stdout:
                    out.append(line)
                else:
                    err.append(line)
        account = None
        rate = None
        errors: list[str] = []
        for line in out:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get('id') == 1:
                if 'result' in obj:
                    account = obj['result']
                elif 'error' in obj:
                    errors.append('account/read: ' + obj['error'].get('message', 'error'))
            elif obj.get('id') == 2:
                if 'result' in obj:
                    rate = obj['result']
                elif 'error' in obj:
                    errors.append('rateLimits/read: ' + obj['error'].get('message', 'error'))
        if err and (account is None or rate is None):
            # Keep only non-secret operational text; Codex may emit harmless sandbox warnings on stderr.
            errors.append('stderr: ' + ' '.join(x.strip() for x in err)[0:300])
        return account, rate, '; '.join(errors) if errors else None
    finally:
        try:
            p.terminate()
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


def recent_codex_api_error(max_age_seconds: int = 6 * 60 * 60) -> dict[str, Any] | None:
    """Return latest openai-codex API error from Hermes logs, without secrets."""
    paths = [pathlib.Path(p) for p in os.environ.get('HERMES_LOG_PATHS', '/root/.hermes/logs/errors.log:/root/.hermes/logs/agent.log').split(':') if p]
    candidates: list[dict[str, Any]] = []
    now = time.time()
    ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),')
    session_re = re.compile(r'\[([^\]]+)\]')
    code_re = re.compile(r'(?:HTTP\s+|Error code:\s+)(\d{3})')
    for path in paths:
        try:
            text = path.read_text(errors='ignore')[-200_000:]
        except Exception:
            continue
        for line in text.splitlines():
            if 'provider=openai-codex' not in line or 'API call failed' not in line:
                continue
            m_ts = ts_re.search(line)
            epoch = None
            if m_ts:
                try:
                    epoch = dt.datetime.strptime(m_ts.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
                except Exception:
                    epoch = None
            if epoch and now - epoch > max_age_seconds:
                continue
            code = None
            m_code = code_re.search(line)
            if m_code:
                try:
                    code = int(m_code.group(1))
                except Exception:
                    code = None
            summary = line.split('summary=', 1)[-1] if 'summary=' in line else line
            summary = summary[:240]
            hay = summary.lower()
            quota_429 = code == 429 or any(x in hay for x in ('usage_limit', 'usage limit', 'quota', 'rate limit', 'too many requests'))
            if 'unsupported content type' in hay:
                category = 'unsupported_content_type'
            elif quota_429:
                category = 'quota_or_rate_limit'
            elif code in {401, 403}:
                category = 'auth'
            elif code and 400 <= code < 500:
                category = 'client_error'
            elif code and code >= 500:
                category = 'server_error'
            else:
                category = 'unknown'
            candidates.append({
                'timestamp': dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat() if epoch else None,
                'age_seconds': int(now - epoch) if epoch else None,
                'http_code': code,
                'category': category,
                'quota_429': quota_429,
                'session': (session_re.search(line).group(1) if session_re.search(line) else None),
                'summary': summary,
                'source_log': str(path),
            })
    candidates.sort(key=lambda x: x.get('timestamp') or '', reverse=True)
    return candidates[0] if candidates else None



def recent_codex_credential_rotation(max_age_seconds: int = 6 * 60 * 60) -> dict[str, Any] | None:
    """Return latest openai-codex credential rotation/client swap from Hermes logs."""
    paths = [pathlib.Path(p) for p in os.environ.get('HERMES_AGENT_LOG_PATHS', '/root/.hermes/logs/agent.log').split(':') if p]
    now = time.time()
    ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),')
    session_re = re.compile(r'\[([^\]]+)\]')
    candidates: list[dict[str, Any]] = []
    for path in paths:
        try:
            text = path.read_text(errors='ignore')[-2_500_000:]
        except Exception:
            continue
        for line in text.splitlines():
            if 'provider=openai-codex' not in line or 'credential_rotation' not in line:
                continue
            m_ts = ts_re.search(line)
            epoch = None
            if m_ts:
                try:
                    epoch = dt.datetime.strptime(m_ts.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
                except Exception:
                    epoch = None
            if epoch and now - epoch > max_age_seconds:
                continue
            candidates.append({
                'timestamp': dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat() if epoch else None,
                'age_seconds': int(now - epoch) if epoch else None,
                'session': (session_re.search(line).group(1) if session_re.search(line) else None),
                'event': 'credential_rotation',
                'source_log': str(path),
                'line': line[:240],
                'meaning': 'Hermes swapped/recreated shared OpenAI-Codex client for a credential-pool context. This is not proof of HTTP 429 by itself.',
            })
    candidates.sort(key=lambda x: x.get('timestamp') or '', reverse=True)
    return candidates[0] if candidates else None

def auth_pool_status() -> list[dict[str, Any]]:
    data = read_json(HERMES_AUTH, {})
    entries = (((data or {}).get('credential_pool') or {}).get('openai-codex') or [])
    codex_auth = read_json(CODEX_AUTH, {})
    codex_tokens = (codex_auth or {}).get('tokens') or {}
    active_access_fp = token_fp(codex_tokens.get('access_token'))
    active_refresh_fp = token_fp(codex_tokens.get('refresh_token'))
    result: list[dict[str, Any]] = []
    for idx, e in enumerate(entries[:10], 1):
        has_tokens = bool(e.get('access_token') and e.get('refresh_token'))
        fp_active = bool(
            has_tokens
            and active_access_fp
            and active_refresh_fp
            and token_fp(e.get('access_token')) == active_access_fp
            and token_fp(e.get('refresh_token')) == active_refresh_fp
        )
        active = fp_active
        err_code = e.get('last_error_code')
        err_reason = e.get('last_error_reason')
        err_msg = e.get('last_error_message')
        err = err_code or err_reason or err_msg
        quota_429 = str(err_code) == '429' or str(err_reason).lower() in {'rate_limit', 'rate_limited', 'too_many_requests'} or '429' in str(err_msg or '')
        login_ok = bool(has_tokens and not (err and not quota_429))
        if not has_tokens:
            state = 'missing'
        elif not login_ok:
            state = 'login_not_ok'
        elif quota_429:
            state = 'rate_limited'
        elif active:
            state = 'active'
        else:
            state = 'ok'
        role = 'primary' if idx == PRIMARY_AUTH_INDEX else ('fallback' if idx == FALLBACK_AUTH_INDEX else 'extra')
        result.append({
            'index': idx,
            'label': e.get('label') or f'auth{idx}',
            'role': role,
            'priority': e.get('priority'),
            'state': state,
            'login_ok': login_ok,
            'quota_429': quota_429,
            # Do not claim per-auth quota health unless this pool entry is the exact
            # active Codex CLI login. Codex app-server exposes quota for ~/.codex/auth.json
            # only; non-matching pool entries are login metadata, not live quota sources.
            'quota_state': '429' if quota_429 else ('live_ok' if active else 'unknown'),
            'active_codex_cli_login': active,
            'active_match_method': 'token_fingerprint' if fp_active else None,
            'live_quota_available': active,
            'source': e.get('source'),
            'last_status': e.get('last_status'),
            'last_status_at': e.get('last_status_at'),
            'last_refresh': e.get('last_refresh'),
            'last_error_code': err_code,
            'last_error_reason': err_reason,
            'last_error_message': err_msg,
            'last_error_reset_at': e.get('last_error_reset_at'),
            'request_count': e.get('request_count'),
        })
    return result


def ha_service(base_url: str, token: str, domain: str, service: str, data_obj: dict[str, Any]) -> None:
    data = json.dumps(data_obj).encode()
    req = urllib.request.Request(
        base_url.rstrip('/') + f'/api/services/{domain}/{service}',
        data=data,
        method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def mqtt_object_id(entity_id: str) -> str | None:
    prefix = 'sensor.hermes_'
    if not entity_id.startswith(prefix):
        return None
    return 'hermes_live_' + entity_id[len(prefix):]


def mqtt_publish(base_url: str, token: str, topic: str, payload: Any, retain: bool = True) -> None:
    # Queue and flush over Home Assistant WebSocket API. REST service calls returned 200
    # but did not trigger MQTT discovery reliably on this HA instance.
    if not isinstance(payload, str):
        payload = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    MQTT_OUTBOX.append((topic, payload, retain))


def _mqtt_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n:
            b |= 128
        out.append(b)
        if not n:
            return bytes(out)


def _mqtt_str(value: str) -> bytes:
    raw = value.encode('utf-8')
    return struct.pack('!H', len(raw)) + raw


def _mqtt_packet(sock: socket.socket, packet_type_flags: int, payload: bytes) -> None:
    sock.sendall(bytes([packet_type_flags]) + _mqtt_varint(len(payload)) + payload)


def _mqtt_read_packet(sock: socket.socket) -> tuple[int, bytes]:
    first = sock.recv(1)
    if not first:
        raise RuntimeError('MQTT broker closed connection')
    multiplier = 1
    remaining = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise RuntimeError('MQTT broker closed connection while reading length')
        digit = b[0]
        remaining += (digit & 127) * multiplier
        if not (digit & 128):
            break
        multiplier *= 128
    data = bytearray()
    while len(data) < remaining:
        chunk = sock.recv(remaining - len(data))
        if not chunk:
            raise RuntimeError('MQTT broker closed connection while reading payload')
        data.extend(chunk)
    return first[0], bytes(data)


def mqtt_flush(base_url: str, token: str) -> None:
    if not MQTT_OUTBOX:
        return
    env = mqtt_env()
    host = env.get('MQTT_HOST')
    if not host:
        raise RuntimeError('missing MQTT_HOST')
    port = int(env.get('MQTT_PORT') or 1883)
    username = env.get('MQTT_USERNAME')
    password = env.get('MQTT_PASSWORD')
    client_id = env.get('MQTT_CLIENT_ID') or ('hermes-ha-monitor-' + str(os.getpid()))
    keepalive = int(env.get('MQTT_KEEPALIVE') or 60)

    flags = 0x02  # clean session
    payload = _mqtt_str(client_id)
    if username:
        flags |= 0x80
        payload += _mqtt_str(username)
    if password:
        flags |= 0x40
        payload += _mqtt_str(password)
    variable = _mqtt_str('MQTT') + bytes([4, flags]) + struct.pack('!H', keepalive)

    with socket.create_connection((host, port), timeout=10) as sock:
        _mqtt_packet(sock, 0x10, variable + payload)
        typ, data = _mqtt_read_packet(sock)
        if typ != 0x20 or len(data) < 2 or data[1] != 0:
            code = data[1] if len(data) >= 2 else 'missing'
            raise RuntimeError(f'MQTT connect failed: {code}')
        for topic, payload_text, retain in list(MQTT_OUTBOX):
            body = _mqtt_str(topic) + payload_text.encode('utf-8')
            _mqtt_packet(sock, 0x31 if retain else 0x30, body)
        _mqtt_packet(sock, 0xE0, b'')
    MQTT_OUTBOX.clear()


def mqtt_env() -> dict[str, str]:
    return {**load_env(ENV_PATH), **{k: v for k, v in os.environ.items() if k.startswith(('MQTT_', 'HERMES_', 'HOMEASSISTANT_', 'CODEX_'))}}


def mqtt_topic_prefix() -> str:
    return (mqtt_env().get('MQTT_TOPIC_PREFIX') or 'hermes/monitor').strip().strip('/') or 'hermes/monitor'


def mqtt_state_topic() -> str:
    return mqtt_topic_prefix() + '/state'


def mqtt_discovery_config(entity_id: str, state: Any, attrs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    object_id = mqtt_object_id(entity_id)
    if not object_id:
        return None
    raw_name = str((attrs or {}).get('friendly_name') or entity_id.replace('sensor.', '').replace('_', ' ').title())
    name = raw_name.removeprefix('Hermes ').removeprefix('OpenAI Codex ')
    state_topic = mqtt_state_topic()
    cfg: dict[str, Any] = {
        'name': name,
        'unique_id': object_id,
        'object_id': object_id,
        'default_entity_id': 'sensor.' + object_id,
        'state_topic': state_topic,
        'value_template': "{{ value_json['" + entity_id + "'].state }}",
        'json_attributes_topic': state_topic,
        'json_attributes_template': "{{ value_json['" + entity_id + "'].attributes | tojson }}",
        'icon': (attrs or {}).get('icon', 'mdi:monitor-dashboard'),
        'device': {
            'identifiers': ['hermes_monitor_live'],
            'name': 'Hermes Monitor',
            'manufacturer': 'Hermes Agent',
            'model': 'Host + Codex MQTT live monitor',
        },
    }
    for k in ('unit_of_measurement', 'device_class', 'state_class'):
        # HA rejects non-numeric MQTT states for measurement-class sensors.
        # Codex rate endpoints can legitimately be unknown; do not retain stale values.
        if k == 'state_class' and object_id.startswith('hermes_live_openai_codex_'):
            continue
        if (attrs or {}).get(k) is not None:
            cfg[k] = attrs[k]
    # CPU bars should move every collector tick, even when rounded state repeats.
    # This keeps Lovelace/custom:button-card redraws clean and in sync.
    if (
        object_id.startswith('hermes_live_host_cpu')
        or object_id.startswith('hermes_live_host_disk')
        or object_id in {
            'hermes_live_host_iowait_usage',
            'hermes_live_host_ram_usage',
            'hermes_live_host_metrics_snapshot',
        }
    ):
        cfg['force_update'] = True
    return f'homeassistant/sensor/{object_id}/config', cfg


def ha_put(base_url: str, token: str, entity_id: str, state: Any, attrs: dict[str, Any] | None = None) -> None:
    attrs = attrs or {}
    if base_url and token:
        data = json.dumps({'state': str(state), 'attributes': attrs}).encode()
        req = urllib.request.Request(
            base_url.rstrip('/') + '/api/states/' + entity_id,
            data=data,
            method='POST',
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()

    # MQTT Discovery + one retained aggregate JSON state topic.
    if mqtt_object_id(entity_id):
        MQTT_STATE[entity_id] = {'state': str(state), 'attributes': attrs}
        if MQTT_DISCOVERY_DUE:
            disc = mqtt_discovery_config(entity_id, state, attrs)
            if disc:
                cfg_topic, cfg_payload = disc
                mqtt_publish(base_url, token, cfg_topic, cfg_payload, retain=True)

def publish_account_quota(base_url: str, token: str, idx: int, account: dict[str, Any] | None, rate: dict[str, Any] | None, error: str | None) -> list[str]:
    """Publish isolated live quota for one Hermes pool account. No email/tokens in attrs."""
    prefix = f'sensor.hermes_openai_codex_auth{idx}'
    windows = codex_quota_windows(rate)
    acct = (account or {}).get('account') if isinstance(account, dict) else {}
    plan = acct.get('planType') if isinstance(acct, dict) else None
    pushed: list[str] = []
    if windows:
        snap = windows['snap']
        common = {
            'auth_index': idx, 'planType': plan or snap.get('planType'),
            'limitId': snap.get('limitId'), 'rateLimitReachedType': snap.get('rateLimitReachedType'),
            'last_update': dt.datetime.now(dt.timezone.utc).isoformat(), 'isolated_codex_home': True,
        }
        for name, window, icon, title, display_mode in (
            ('5h', windows['five_hour'], 'mdi:timer-sand', '5h', 'time'),
            ('weekly', windows['weekly'], 'mdi:calendar-week', 'Woche', 'date'),
        ):
            used = window.get('usedPercent')
            remaining = 100 - used if isinstance(used, (int, float)) else 'unknown'
            reset = window.get('resetsAt')
            attrs = {'friendly_name': f'OpenAI Codex Auth{idx} {title} verbleibend', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:speedometer', 'usedPercent': used, 'windowDurationMins': window.get('windowDurationMins'), 'resetsAt': iso_from_epoch(reset), 'reset_display': local_reset_display(reset, display_mode), **common}
            used_attrs = {**attrs, 'friendly_name': f'OpenAI Codex Auth{idx} {title} verbraucht', 'icon': icon}
            ha_put(base_url, token, f'{prefix}_{name}_remaining', remaining, attrs)
            ha_put(base_url, token, f'{prefix}_{name}_used', used if isinstance(used, (int, float)) else 'unknown', used_attrs)
            pushed.extend([f'{prefix}_{name}_remaining', f'{prefix}_{name}_used'])
        state = snap.get('rateLimitReachedType') or 'ok'
        ha_put(base_url, token, f'{prefix}_quota_probe', state, {'friendly_name': f'OpenAI Codex Auth{idx} Live Quota', 'icon': 'mdi:check-circle-outline', **common})
    else:
        common = {'auth_index': idx, 'last_update': dt.datetime.now(dt.timezone.utc).isoformat(), 'isolated_codex_home': True, 'error': error or 'no rate response'}
        for name, title in (('5h', '5h'), ('weekly', 'Woche')):
            ha_put(base_url, token, f'{prefix}_{name}_remaining', 'unknown', {'friendly_name': f'OpenAI Codex Auth{idx} {title} verbleibend', 'unit_of_measurement': '%', 'icon': 'mdi:speedometer', **common})
            ha_put(base_url, token, f'{prefix}_{name}_used', 'unknown', {'friendly_name': f'OpenAI Codex Auth{idx} {title} verbraucht', 'unit_of_measurement': '%', 'icon': 'mdi:timer-sand' if name == '5h' else 'mdi:calendar-week', **common})
            pushed.extend([f'{prefix}_{name}_remaining', f'{prefix}_{name}_used'])
        ha_put(base_url, token, f'{prefix}_quota_probe', 'error', {'friendly_name': f'OpenAI Codex Auth{idx} Live Quota', 'icon': 'mdi:alert-circle', **common})
    pushed.append(f'{prefix}_quota_probe')
    return pushed


def push_sensors(base_url: str, token: str) -> dict[str, Any]:
    global MQTT_DISCOVERY_DUE, MQTT_STATE
    prev = read_json(STATE_FILE, {})
    MQTT_STATE = {}
    now_ts = time.time()
    MQTT_DISCOVERY_DUE = now_ts - float(prev.get('mqtt_discovery_at') or 0) > 600
    new_state: dict[str, Any] = {}
    pushed: list[str] = []
    live_unknown_updates: list[tuple[str, dict[str, Any]]] = []
    sample_at = dt.datetime.now(dt.timezone.utc).isoformat()

    cpu, cpu_state = cpu_percent(prev)
    new_state['cpu'] = cpu_state
    if cpu is not None:
        cpu_update = sample_at
        cpu_attrs = {'friendly_name': 'Hermes Host CPU', 'unit_of_measurement': '%', 'device_class': 'power_factor', 'state_class': 'measurement', 'icon': 'mdi:cpu-64-bit', 'meaning': 'non-idle CPU time including iowait', 'cpu_work_pct_excluding_iowait': cpu_state.get('work_pct'), 'iowait_pct': cpu_state.get('iowait_pct'), 'core_count': cpu_state.get('core_count'), 'core_pcts': cpu_state.get('core_pcts'), 'last_sample_at': cpu_update}
        ha_put(base_url, token, 'sensor.hermes_host_cpu_usage', cpu, cpu_attrs)
        if cpu_state.get('iowait_pct') is not None:
            ha_put(base_url, token, 'sensor.hermes_host_iowait_usage', cpu_state.get('iowait_pct'), {'friendly_name': 'Hermes Host I/O Wait', 'unit_of_measurement': '%', 'device_class': 'power_factor', 'state_class': 'measurement', 'icon': 'mdi:harddisk-clock', 'last_sample_at': sample_at, 'cpu_work_pct_excluding_iowait': cpu_state.get('work_pct'), 'cpu_pressure_pct_including_iowait': cpu})
            pushed.append('sensor.hermes_host_iowait_usage')
        for idx, core_pct in enumerate(cpu_state.get('core_pcts') or []):
            if core_pct is None:
                continue
            ha_put(base_url, token, f'sensor.hermes_host_cpu_core_{idx}_usage', core_pct, {'friendly_name': f'Hermes Host CPU Core {idx + 1}', 'unit_of_measurement': '%', 'device_class': 'power_factor', 'state_class': 'measurement', 'icon': 'mdi:chip', 'core_index': idx, 'meaning': 'per-core non-idle CPU time including iowait', 'last_sample_at': cpu_update, 'cpu_work_pct_excluding_iowait': (cpu_state.get('core_work_pcts') or [None])[idx] if idx < len(cpu_state.get('core_work_pcts') or []) else None})
            pushed.append(f'sensor.hermes_host_cpu_core_{idx}_usage')
        pushed.append('sensor.hermes_host_cpu_usage')

    mem, mem_raw = mem_percent()
    if mem is not None:
        ha_put(base_url, token, 'sensor.hermes_host_ram_usage', mem, {'friendly_name': 'Hermes Host RAM', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:memory', 'last_sample_at': sample_at, 'total_bytes': mem_raw.get('MemTotal'), 'available_bytes': mem_raw.get('MemAvailable')})
        pushed.append('sensor.hermes_host_ram_usage')

    disk_pcts: dict[str, float | None] = {}
    disk_raws: dict[str, dict[str, int]] = {}
    for name, path in [('root', '/'), ('data', '/data')]:
        if name == 'data' and not pathlib.Path(path).exists():
            continue
        pct, raw = disk_percent(path)
        disk_pcts[name] = pct
        disk_raws[name] = raw
        if pct is not None:
            ha_put(base_url, token, f'sensor.hermes_host_disk_{name}_usage', pct, {'friendly_name': f'Hermes Host Disk {path}', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:harddisk', 'path': path, 'last_sample_at': sample_at, **raw})
            pushed.append(f'sensor.hermes_host_disk_{name}_usage')

    snapshot_attrs = {
        'friendly_name': 'Hermes Host Metrics Snapshot',
        'icon': 'mdi:server',
        'last_sample_at': sample_at,
        'cpu_pct': cpu,
        'cpu_work_pct_excluding_iowait': cpu_state.get('work_pct'),
        'iowait_pct': cpu_state.get('iowait_pct'),
        'core_pcts': cpu_state.get('core_pcts'),
        'ram_pct': mem,
        'disk_root_pct': disk_pcts.get('root'),
        'disk_data_pct': disk_pcts.get('data'),
        'disk_root': disk_raws.get('root'),
        'disk_data': disk_raws.get('data'),
    }
    # Use changing state, not constant 'ok': some Lovelace/custom cards only rerender reliably
    # when the watched entity state changes, not only attributes. show_state is disabled in UI.
    ha_put(base_url, token, 'sensor.hermes_host_metrics_snapshot', sample_at, snapshot_attrs)
    pushed.append('sensor.hermes_host_metrics_snapshot')

    la = loadavg()
    if la:
        ha_put(base_url, token, 'sensor.hermes_host_load_1m', round(la[0], 2), {'friendly_name': 'Hermes Host Load 1m', 'state_class': 'measurement', 'icon': 'mdi:gauge', 'load_5m': round(la[1], 2), 'load_15m': round(la[2], 2)})
        pushed.append('sensor.hermes_host_load_1m')

    up = uptime_seconds()
    if up is not None:
        ha_put(base_url, token, 'sensor.hermes_host_uptime_seconds', up, {'friendly_name': 'Hermes Host Uptime', 'unit_of_measurement': 's', 'device_class': 'duration', 'state_class': 'total_increasing', 'icon': 'mdi:timer-outline'})
        pushed.append('sensor.hermes_host_uptime_seconds')

    auths = auth_pool_status()
    recent_error = recent_codex_api_error()
    recent_rotation = recent_codex_credential_rotation()
    if recent_error:
        last_error_state = str(recent_error.get('http_code') or recent_error.get('category') or 'error')
        ha_put(base_url, token, 'sensor.hermes_openai_codex_last_api_error', last_error_state, {'friendly_name': 'OpenAI Codex letzter API-Fehler', 'icon': 'mdi:alert-box-outline', **recent_error})
    else:
        ha_put(base_url, token, 'sensor.hermes_openai_codex_last_api_error', 'none', {'friendly_name': 'OpenAI Codex letzter API-Fehler', 'icon': 'mdi:check-circle-outline'})
    pushed.append('sensor.hermes_openai_codex_last_api_error')
    if recent_rotation:
        rot_state = 'rotation'
        ha_put(base_url, token, 'sensor.hermes_openai_codex_credential_rotation', rot_state, {'friendly_name': 'OpenAI Codex letzte Credential-Rotation', 'icon': 'mdi:swap-horizontal-circle-outline', **recent_rotation})
    else:
        ha_put(base_url, token, 'sensor.hermes_openai_codex_credential_rotation', 'none', {'friendly_name': 'OpenAI Codex letzte Credential-Rotation', 'icon': 'mdi:swap-horizontal-circle-outline'})
    pushed.append('sensor.hermes_openai_codex_credential_rotation')
    primary_auth = next((a for a in auths if a.get('index') == PRIMARY_AUTH_INDEX), None)
    fallback_auth = next((a for a in auths if a.get('index') == FALLBACK_AUTH_INDEX), None)
    for entry in auths:
        idx = entry['index']
        ha_put(base_url, token, f'sensor.hermes_openai_codex_auth{idx}_state', entry['state'], {'friendly_name': f'OpenAI Codex Auth{idx}', 'icon': 'mdi:account-key', **entry})
        ha_put(base_url, token, f'sensor.hermes_openai_codex_auth{idx}_login', 'ok' if entry.get('login_ok') else 'not_ok', {'friendly_name': f'OpenAI Codex Auth{idx} Login', 'icon': 'mdi:login', **entry})
        ha_put(base_url, token, f'sensor.hermes_openai_codex_auth{idx}_quota_state', entry.get('quota_state', 'unknown'), {'friendly_name': f'OpenAI Codex Auth{idx} Quota', 'icon': 'mdi:alert-octagon', **entry})
        pushed.extend([f'sensor.hermes_openai_codex_auth{idx}_state', f'sensor.hermes_openai_codex_auth{idx}_login', f'sensor.hermes_openai_codex_auth{idx}_quota_state'])
    primary_429 = bool((primary_auth or {}).get('quota_429'))
    runtime_429 = bool((recent_error or {}).get('quota_429'))
    primary_active = bool((primary_auth or {}).get('active_codex_cli_login'))
    fallback_active = bool((fallback_auth or {}).get('active_codex_cli_login'))
    fallback_login_ok = bool((fallback_auth or {}).get('login_ok'))
    active_auth = fallback_auth if fallback_active else (primary_auth if primary_active else None)
    if fallback_active:
        fallback_state = 'active'
        fallback_reason = 'fallback_auth_is_active_codex_cli_login'
    elif primary_active:
        fallback_state = 'inactive'
        fallback_reason = 'primary_auth_is_active_codex_cli_login'
    else:
        fallback_state = 'unknown'
        fallback_reason = 'active_codex_cli_login_does_not_match_hermes_pool_tokens'
    ha_put(base_url, token, 'sensor.hermes_openai_codex_fallback_state', fallback_state, {'friendly_name': 'OpenAI Codex Fallback', 'icon': 'mdi:backup-restore', 'primary_auth': primary_auth, 'fallback_auth': fallback_auth, 'active_auth': active_auth, 'primary_429': primary_429, 'runtime_429': runtime_429, 'recent_api_error': recent_error, 'recent_credential_rotation': recent_rotation, 'fallback_login_ok': fallback_login_ok, 'primary_active': primary_active, 'fallback_active': fallback_active, 'reason': fallback_reason, 'meaning': 'active = Auth2/fallback is the exact active Codex CLI login; inactive = Auth1/primary is active; unknown = Codex CLI tokens do not match any Hermes pool entry, so fallback use cannot be proven.'})
    pushed.append('sensor.hermes_openai_codex_fallback_state')

    # Per-account quotas use isolated CODEX_HOME dirs, so both accounts are
    # measured without switching the global Codex CLI login used by Hermes.
    configured_indices = sorted({PRIMARY_AUTH_INDEX, FALLBACK_AUTH_INDEX})
    home_sync = sync_codex_account_homes(configured_indices)
    for idx in configured_indices:
        home = CODEX_ACCOUNTS_ROOT / f'auth{idx}'
        account_i, rate_i, err_i = (None, None, home_sync[idx]) if home_sync.get(idx) != 'ok' else run_codex_app_server(str(home))
        pushed.extend(publish_account_quota(base_url, token, idx, account_i, rate_i, err_i))

    account, rate, codex_error = run_codex_app_server()
    acct_obj = (account or {}).get('account') if isinstance(account, dict) else None
    if acct_obj:
        ha_put(base_url, token, 'sensor.hermes_openai_codex_current_account', acct_obj.get('email') or 'chatgpt', {'friendly_name': 'OpenAI Codex aktiver Account', 'icon': 'mdi:account-circle', 'type': acct_obj.get('type'), 'planType': acct_obj.get('planType'), 'requiresOpenaiAuth': (account or {}).get('requiresOpenaiAuth')})
        pushed.append('sensor.hermes_openai_codex_current_account')
    else:
        ha_put(base_url, token, 'sensor.hermes_openai_codex_current_account', 'unknown', {'friendly_name': 'OpenAI Codex aktiver Account', 'icon': 'mdi:account-circle', 'error': codex_error})
        pushed.append('sensor.hermes_openai_codex_current_account')

    snap = None
    if isinstance(rate, dict):
        by_id = rate.get('rateLimitsByLimitId') or {}
        snap = by_id.get('codex') or rate.get('rateLimits')
    if isinstance(snap, dict):
        primary = snap.get('primary') or {}
        secondary = snap.get('secondary') or {}
        credits = snap.get('credits') or {}
        common = {'limitId': snap.get('limitId'), 'limitName': snap.get('limitName'), 'planType': snap.get('planType'), 'rateLimitReachedType': snap.get('rateLimitReachedType'), 'credits': credits, 'last_update': dt.datetime.now(dt.timezone.utc).isoformat()}

        # ChatGPT/Codex backend may return one or two windows, and labels are
        # not stable: older responses used primary=5h and secondary=weekly;
        # newer Plus responses can return only primary with windowDurationMins=10080
        # (weekly). Map by duration, not by field position, to avoid showing
        # missing weekly data as a fake 0%.
        windows = [w for w in (primary, secondary) if isinstance(w, dict) and w]
        five_hour = next((w for w in windows if w.get('windowDurationMins') in range(240, 361)), {})
        weekly = next((w for w in windows if w.get('windowDurationMins') in range(9000, 11000)), {})
        if not five_hour and primary.get('windowDurationMins') in (None, 0) and secondary:
            five_hour = primary
        if not weekly and secondary.get('windowDurationMins') in (None, 0):
            weekly = secondary

        five_used = five_hour.get('usedPercent')
        weekly_used = weekly.get('usedPercent')
        five_remaining = 100 - five_used if isinstance(five_used, (int, float)) else 'unknown'
        weekly_remaining = 100 - weekly_used if isinstance(weekly_used, (int, float)) else 'unknown'
        five_reset_display = local_reset_display(five_hour.get('resetsAt'), 'time')
        weekly_reset_display = local_reset_display(weekly.get('resetsAt'), 'date')
        ha_put(base_url, token, 'sensor.hermes_openai_codex_5h_used', five_hour.get('usedPercent', 'unknown'), {'friendly_name': 'OpenAI Codex 5h verbraucht', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:timer-sand', 'windowDurationMins': five_hour.get('windowDurationMins'), 'resetsAt': iso_from_epoch(five_hour.get('resetsAt')), 'reset_display': five_reset_display, 'raw_primary_windowDurationMins': primary.get('windowDurationMins'), 'raw_secondary_windowDurationMins': secondary.get('windowDurationMins'), **common})
        ha_put(base_url, token, 'sensor.hermes_openai_codex_weekly_used', weekly.get('usedPercent', 'unknown'), {'friendly_name': 'OpenAI Codex Woche verbraucht', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:calendar-week', 'windowDurationMins': weekly.get('windowDurationMins'), 'resetsAt': iso_from_epoch(weekly.get('resetsAt')), 'reset_display': weekly_reset_display, 'raw_primary_windowDurationMins': primary.get('windowDurationMins'), 'raw_secondary_windowDurationMins': secondary.get('windowDurationMins'), **common})
        ha_put(base_url, token, 'sensor.hermes_openai_codex_5h_remaining', five_remaining, {'friendly_name': 'OpenAI Codex 5h verbleibend', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:speedometer', 'usedPercent': five_used, 'windowDurationMins': five_hour.get('windowDurationMins'), 'resetsAt': iso_from_epoch(five_hour.get('resetsAt')), 'reset_display': five_reset_display, 'raw_primary_windowDurationMins': primary.get('windowDurationMins'), 'raw_secondary_windowDurationMins': secondary.get('windowDurationMins'), **common})
        ha_put(base_url, token, 'sensor.hermes_openai_codex_weekly_remaining', weekly_remaining, {'friendly_name': 'OpenAI Codex Woche verbleibend', 'unit_of_measurement': '%', 'state_class': 'measurement', 'icon': 'mdi:speedometer', 'usedPercent': weekly_used, 'windowDurationMins': weekly.get('windowDurationMins'), 'resetsAt': iso_from_epoch(weekly.get('resetsAt')), 'reset_display': weekly_reset_display, 'raw_primary_windowDurationMins': primary.get('windowDurationMins'), 'raw_secondary_windowDurationMins': secondary.get('windowDurationMins'), **common})
        ha_put(base_url, token, 'sensor.hermes_openai_codex_5h_reset', iso_from_epoch(five_hour.get('resetsAt')) or 'unknown', {'friendly_name': 'OpenAI Codex 5h Reset', 'device_class': 'timestamp', 'icon': 'mdi:clock-end', 'reset_display': five_reset_display, **common})
        ha_put(base_url, token, 'sensor.hermes_openai_codex_weekly_reset', iso_from_epoch(weekly.get('resetsAt')) or 'unknown', {'friendly_name': 'OpenAI Codex Wochenreset', 'device_class': 'timestamp', 'icon': 'mdi:calendar-clock', 'reset_display': weekly_reset_display, **common})
        reached = snap.get('rateLimitReachedType') or 'ok'
        ha_put(base_url, token, 'sensor.hermes_openai_codex_limit_state', reached, {'friendly_name': 'OpenAI Codex Limitstatus', 'icon': 'mdi:alert-circle-check', **common})
        pushed += ['sensor.hermes_openai_codex_5h_used', 'sensor.hermes_openai_codex_weekly_used', 'sensor.hermes_openai_codex_5h_remaining', 'sensor.hermes_openai_codex_weekly_remaining', 'sensor.hermes_openai_codex_5h_reset', 'sensor.hermes_openai_codex_weekly_reset', 'sensor.hermes_openai_codex_limit_state']
        # Response can contain only one window; clear stale values for the absent one.
        for prefix, label, used, reset_icon in (
            ('5h', '5h', five_used, 'mdi:clock-end'),
            ('weekly', 'Woche', weekly_used, 'mdi:calendar-clock'),
        ):
            if isinstance(used, (int, float)):
                continue
            live_unknown_updates.extend([
                (f'sensor.hermes_live_openai_codex_{prefix}_used', {'friendly_name': f'Hermes Monitor OpenAI Codex {label} verbraucht', 'unit_of_measurement': '%', 'icon': 'mdi:timer-sand' if prefix == '5h' else 'mdi:calendar-week', 'last_update': common['last_update']}),
                (f'sensor.hermes_live_openai_codex_{prefix}_remaining', {'friendly_name': f'Hermes Monitor OpenAI Codex {label} verbleibend', 'unit_of_measurement': '%', 'icon': 'mdi:speedometer', 'last_update': common['last_update']}),
                (f'sensor.hermes_live_openai_codex_{prefix}_reset', {'friendly_name': f'Hermes Monitor OpenAI Codex {label} Reset', 'device_class': 'timestamp', 'icon': reset_icon, 'last_update': common['last_update']}),
            ])
    else:
        unavailable = {'error': codex_error, 'last_update': dt.datetime.now(dt.timezone.utc).isoformat()}
        for entity_id, name, icon, device_class in (
            ('sensor.hermes_openai_codex_5h_used', 'OpenAI Codex 5h verbraucht', 'mdi:timer-sand', None),
            ('sensor.hermes_openai_codex_weekly_used', 'OpenAI Codex Woche verbraucht', 'mdi:calendar-week', None),
            ('sensor.hermes_openai_codex_5h_remaining', 'OpenAI Codex 5h verbleibend', 'mdi:speedometer', None),
            ('sensor.hermes_openai_codex_weekly_remaining', 'OpenAI Codex Woche verbleibend', 'mdi:speedometer', None),
            ('sensor.hermes_openai_codex_5h_reset', 'OpenAI Codex 5h Reset', 'mdi:clock-end', 'timestamp'),
            ('sensor.hermes_openai_codex_weekly_reset', 'OpenAI Codex Wochenreset', 'mdi:calendar-clock', 'timestamp'),
        ):
            attrs = {'friendly_name': name, 'icon': icon, **unavailable}
            if device_class:
                attrs['device_class'] = device_class
            else:
                attrs['unit_of_measurement'] = '%'
                attrs['state_class'] = 'measurement'
            ha_put(base_url, token, entity_id, 'unknown', attrs)
            live_entity_id = 'sensor.hermes_live_' + entity_id.removeprefix('sensor.hermes_')
            live_unknown_updates.append((live_entity_id, attrs))
            pushed.append(entity_id)
        ha_put(base_url, token, 'sensor.hermes_openai_codex_limit_state', 'unknown', {'friendly_name': 'OpenAI Codex Limitstatus', 'icon': 'mdi:alert-circle', **unavailable})
        pushed.append('sensor.hermes_openai_codex_limit_state')

    if MQTT_STATE:
        mqtt_publish(base_url, token, mqtt_state_topic(), MQTT_STATE, retain=True)
    mqtt_flush(base_url, token)
    # Apply REST corrections after MQTT flush; delayed MQTT cannot reassert stale values.
    for live_entity_id, attrs in live_unknown_updates:
        ha_put(base_url, token, live_entity_id, 'unknown', attrs)
    if MQTT_DISCOVERY_DUE:
        new_state['mqtt_discovery_at'] = now_ts
    new_state['last_run'] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(STATE_FILE, {**prev, **new_state})
    return {'pushed': pushed, 'codex_error': codex_error, 'mqtt_mirror': True}


def main() -> int:
    env = mqtt_env()
    base_url = env.get('HOMEASSISTANT_URL')
    token = env.get('HOMEASSISTANT_TOKEN')
    if not env.get('MQTT_HOST') and not (base_url and token):
        print('missing MQTT_HOST (or legacy HOMEASSISTANT_URL + HOMEASSISTANT_TOKEN)', file=sys.stderr)
        return 2
    try:
        result = push_sensors(base_url or '', token or '')
    except urllib.error.HTTPError as e:
        print(f'home assistant HTTP error: {e.code}', file=sys.stderr)
        return 3
    except Exception as e:
        print(f'collector error: {type(e).__name__}: {e}', file=sys.stderr)
        return 1
    print(json.dumps({'ok': True, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
