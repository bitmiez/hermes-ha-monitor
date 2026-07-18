class HermesLiveMonitorCard extends HTMLElement {
  setConfig(config) { this.config = config || {}; if (!this.shadowRoot) this.attachShadow({mode: 'open'}); }
  set hass(hass) { this._hass = hass; this.render(); }
  getCardSize() { return 8; }
  _s(id) { return this._hass?.states?.[id]; }
  _state(id, fb='—') { return this._s(id)?.state ?? fb; }
  _attr(id, key, fb=null) { const a=this._s(id)?.attributes || {}; return a[key] ?? fb; }
  _numState(id) { const n=parseFloat(this._state(id, '0')); return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0; }
  _quotaEntity(id) {
    const legacy=id.replace('sensor.hermes_live_','sensor.hermes_');
    const rest=this._s(legacy), live=this._s(id);
    // REST mirror accepts non-numeric unknown states; MQTT numeric entities can keep stale values.
    return rest || live;
  }
  _quotaAttr(id, key, fb=null) { const a=this._quotaEntity(id)?.attributes || {}; return a[key] ?? this._attr(id,key,fb); }
  _quotaState(id) { const raw=this._quotaEntity(id)?.state ?? 'unknown'; const n=parseFloat(raw); return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : null; }
  _snap(key, fallbackId) { const n=parseFloat(this._attr('sensor.hermes_live_host_metrics_snapshot', key, this._state(fallbackId, '0'))); return Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0; }
  _pct(v) { return v==null ? '—' : `${Math.round(v)}%`; }
  _hostColor(v) { return v >= 90 ? '#ff5f5f' : (v >= 75 ? '#f0b84a' : '#58d26b'); }
  _quotaColor(v) { return v >= 80 ? '#58d26b' : (v >= 35 ? '#f0b84a' : '#ff5f5f'); }
  _bar(label, id) {
    const v=this._quotaState(id), r=this._quotaAttr(id,'reset_display',''); const c=v==null ? '#777' : this._quotaColor(v); const w=v==null ? 0 : v;
    return `<div class="bar quota"><div class="barrow"><b>${label}</b><span>${this._pct(v)}</span><span>${r || ''}</span></div><div class="track"><div class="fill" style="width:${w}%;background:${c}"></div></div></div>`;
  }
  _hostbar(label, key, fallbackId) {
    const v=this._snap(key, fallbackId), c=this._hostColor(v);
    return `<div class="bar host"><div class="barrow hostrow"><b>${label}</b><span>${this._pct(v)}</span></div><div class="track hosttrack"><div class="fill" style="width:${v}%;background:${c}"></div></div></div>`;
  }
  _coreBars() {
    const raw=this._attr('sensor.hermes_live_host_metrics_snapshot','core_pcts', this._attr('sensor.hermes_live_host_cpu_usage','core_pcts', []));
    const vals=(Array.isArray(raw) && raw.length ? raw : ['sensor.hermes_live_host_cpu_core_0_usage','sensor.hermes_live_host_cpu_core_1_usage','sensor.hermes_live_host_cpu_core_2_usage'].map(id=>this._numState(id))).slice(0,3).map(x=>{ const n=parseFloat(x); return Number.isFinite(n)?Math.max(0,Math.min(100,n)):0; });
    return `<div class="cores">${vals.map((val,i)=>{ const v=Math.round(val), c=this._hostColor(v); return `<div class="core"><div class="coretop"><span>K${i+1}</span><b>${v}%</b></div><div class="coretrack"><div class="corefill" style="width:${v}%;background:${c}"></div></div></div>`; }).join('')}</div>`;
  }
  _authLine(idx, role) {
    const login=this._state(`sensor.hermes_live_openai_codex_auth${idx}_login`);
    const quota=this._state(`sensor.hermes_live_openai_codex_auth${idx}_quota_state`);
    const probe=this._state(`sensor.hermes_live_openai_codex_auth${idx}_quota_probe`);
    const state=this._state(`sensor.hermes_live_openai_codex_auth${idx}_state`);
    const active=state==='active';
    const quotaText=quota==='429' ? '429' : (probe==='ok' ? 'live ok' : (quota==='live_ok' ? 'live ok' : 'unbekannt'));
    const c=quota==='429' ? '#ff5f5f' : (probe==='ok' ? '#58d26b' : (active ? '#58d26b' : (login==='ok' ? '#aaa' : '#ff5f5f')));
    const prefix=active ? 'CLI aktiv · ' : '';
    return `<div class="authline"><b>Auth${idx}</b><span>${role}</span><span style="color:${c}">${prefix}Login ${login} · Quota ${quotaText}</span></div>`;
  }
  _accountQuota(idx, role) {
    const probe=this._state(`sensor.hermes_live_openai_codex_auth${idx}_quota_probe`);
    const label=probe==='ok' ? 'live' : (probe==='error' ? 'Fehler' : 'unbekannt');
    const color=probe==='ok' ? '#58d26b' : (probe==='error' ? '#ff5f5f' : '#f0b84a');
    return `<div class="accountquota"><div class="accounthead"><b>Auth${idx}</b><span>${role}</span><span style="color:${color}">${label}</span></div>${this._bar('5 Std.',`sensor.hermes_live_openai_codex_auth${idx}_5h_remaining`)}${this._bar('Wöchentlich',`sensor.hermes_live_openai_codex_auth${idx}_weekly_remaining`)}</div>`;
  }
  _section(title, icon, body) { return `<section><div class="secthead"><ha-icon icon="${icon}"></ha-icon><span>${title}</span></div>${body}</section>`; }
  render() {
    if (!this.shadowRoot || !this._hass) return;
    const quotaBody=`${this._accountQuota(1,'Primär')}${this._accountQuota(2,'Fallback')}`;
    const load=this._state('sensor.hermes_live_host_load_1m');
    const hostBody=`${this._hostbar('CPU','cpu_pct','sensor.hermes_live_host_cpu_usage')}${this._coreBars()}${this._hostbar('I/O wait','iowait_pct','sensor.hermes_live_host_iowait_usage')}${this._hostbar('RAM','ram_pct','sensor.hermes_live_host_ram_usage')}${this._hostbar('Disk /','disk_root_pct','sensor.hermes_live_host_disk_root_usage')}${this._hostbar('Disk /data','disk_data_pct','sensor.hermes_live_host_disk_data_usage')}<div class="detail"><span>Load 1m</span><b>${load}</b></div>`;
    const fallback=this._state('sensor.hermes_live_openai_codex_fallback_state');
    const fAttr=this._s('sensor.hermes_live_openai_codex_fallback_state')?.attributes || {}; const p429=fAttr.primary_429===true;
    const fallbackLabel=fallback==='active'?'ja':(fallback==='inactive'?'nein':'unbekannt');
    const fallbackColor=fallback==='active'?'#58d26b':(fallback==='inactive'?'#aaa':'#f0b84a');
    const lastErr=this._state('sensor.hermes_live_openai_codex_last_api_error'); const errAttr=this._s('sensor.hermes_live_openai_codex_last_api_error')?.attributes || {}; const errCat=errAttr.category || ''; const errText=lastErr==='none' ? 'none' : `${lastErr}${errCat?` · ${errCat}`:''}`; const errColor=(lastErr==='none'||lastErr==='unknown')?'#58d26b':(lastErr==='429'?'#ff5f5f':'#f0b84a');
    const rot=this._state('sensor.hermes_live_openai_codex_credential_rotation'); const rotAttr=this._s('sensor.hermes_live_openai_codex_credential_rotation')?.attributes || {}; const rotAge=rotAttr.age_seconds; const rotText=rot==='none'?'none':(rotAge!=null?`${Math.round(rotAge/60)} min her`:'rotation'); const rotColor=rot==='none'?'#58d26b':'#ffd166';
    const authBody=`<div class="detail plain"><span>Aktiver CLI-Login</span><span>${this._state('sensor.hermes_live_openai_codex_current_account')}</span></div>${this._authLine(1,'Primär')}${this._authLine(2,'Fallback')}<div class="detail plain"><span>Letzter API-Fehler</span><b style="color:${errColor}">${errText}</b></div><div class="detail plain"><span>Letzte Credential-Rotation</span><b style="color:${rotColor}">${rotText}</b></div><div class="detail"><span>Fallback-Konto aktiv</span><b style="color:${fallbackColor}">${fallbackLabel}</b></div><div class="detail plain small"><span>Auth1 429 erkannt</span><b style="color:${p429?'#ff5f5f':'#aaa'}">${p429?'ja':'nein'}</b></div>`;
    this.shadowRoot.innerHTML = `<style>
      :host{display:block;width:100%;max-width:100%;box-sizing:border-box}.card{box-sizing:border-box;width:100%;max-width:100%;min-width:100%;margin:0;padding:18px 18px 18px 8px;background:#171717;border:1px solid rgba(255,255,255,.08);border-radius:18px;color:#eee;font-family:var(--primary-font-family,Arial,sans-serif);text-align:left;overflow:visible}.title{position:relative;min-height:48px;display:flex;align-items:center;justify-content:center;text-align:center;width:100%;padding:0 34px;box-sizing:border-box}.title ha-icon{position:absolute;left:0;top:50%;transform:translateY(-50%);width:25px;height:25px;color:#d163ff}.title h2{font-size:23px;line-height:25px;font-weight:850;color:#fff;margin:0}.title div div{margin-top:4px;font-size:13px;line-height:16px;color:#aaa}section{border-top:1px solid rgba(255,255,255,.08);padding-top:15px;margin-top:16px}.secthead{display:grid;grid-template-columns:18px minmax(0,1fr);column-gap:9px;align-items:center;color:#f2f2f2;font-size:15px;font-weight:750;margin-bottom:12px}.secthead ha-icon{width:18px;height:18px;color:#ddd}.bar{margin-top:11px}.barrow{display:grid;grid-template-columns:minmax(0,1fr) auto auto;column-gap:12px;margin-bottom:7px;align-items:baseline;font-size:14px}.barrow b{color:#fff;font-size:15px}.hostrow{grid-template-columns:minmax(0,1fr) auto;margin-bottom:6px}.track{height:10px;background:#303030;border-radius:999px;overflow:hidden}.hosttrack{height:8px}.fill,.corefill{height:100%;border-radius:999px;transition:width .35s linear,background-color .35s linear}.cores{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:7px;margin-bottom:2px}.coretop{display:flex;justify-content:space-between;gap:4px;font-size:10px;line-height:12px;color:#aaa;margin-bottom:3px}.coretop b{color:#ddd}.coretrack{height:5px;background:#303030;border-radius:999px;overflow:hidden}.detail{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.06);font-size:13px;color:#aaa}.detail b{color:#fff}.detail .ok,.ok{color:#58d26b}.plain{border-top:none;padding-top:0;margin-top:9px}.small{margin-top:7px}.accountquota{margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08)}.accounthead{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:8px;align-items:center;font-size:12px;color:#aaa}.accounthead b{color:#fff}.accountquota .bar{margin-top:8px}.accountquota .barrow b{font-size:13px}.authline{display:grid;grid-template-columns:54px 70px minmax(0,1fr);gap:8px;align-items:center;margin-top:8px;font-size:14px;line-height:18px}.authline b{color:#fff}.authline span:nth-child(2){color:#aaa}@media (max-width:420px){.card{padding-right:14px}.accountquota{margin-top:12px;padding-top:10px;border-top:1px solid rgba(255,255,255,.08)}.accounthead{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:8px;align-items:center;font-size:12px;color:#aaa}.accounthead b{color:#fff}.accountquota .bar{margin-top:8px}.accountquota .barrow b{font-size:13px}.authline{grid-template-columns:48px 62px minmax(0,1fr);gap:6px;font-size:13px}.title h2{font-size:22px}}
      </style><div class="card"><div class="title"><ha-icon icon="mdi:robot-happy"></ha-icon><div><h2>Hermes Monitor</h2><div>Kontingent · Serverauslastung · Login/Fallback</div></div></div>${this._section('Kontingent je Account','mdi:speedometer',quotaBody)}${this._section('Server-Auslastung','mdi:server',hostBody)}${this._section('OpenAI-Codex Auth / Fallback','mdi:account-key',authBody)}</div>`;
  }
}
if (!customElements.get('hermes-live-monitor-card')) customElements.define('hermes-live-monitor-card', HermesLiveMonitorCard);
window.customCards = window.customCards || [];
window.customCards.push({type:'hermes-live-monitor-card',name:'Hermes Live Monitor Card',description:'Live Hermes status monitor'});
