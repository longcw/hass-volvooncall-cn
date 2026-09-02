const CARD_VERSION = "1.4.0";

// Remapped to hass-volvooncall-cn (longcw fork) entity IDs.
const ENTITY_DEFINITIONS = {
  battery: ["sensor", "battery_charge_level"],
  electric_range: ["sensor", "electric_range"],
  charging_status: ["sensor", "charging_status"],
  // longcw exposes the home-pile connector status (Chinese label) + pile
  // details as attributes here; used for the connection label.
  charger_connection: ["sensor", "home_charger_status"],
  charging_time: ["sensor", "estimated_charging_time"],
  charging_power: ["sensor", "charging_power"],
  charging_voltage: ["sensor", "charging_voltage"],
  charging_current: ["sensor", "charging_current"],
  session_energy: ["sensor", "charging_session_energy"],
  tm_energy: ["sensor", "energy_consumption"],
  home_charge: ["switch", "charging"],
  plug_and_charge: ["switch", "plug_and_charge"],
  charge_limit: ["number", "charge_limit"],
  charge_timer: ["switch", "charge_timer"],
  charge_timer_any_car: ["switch", "charge_timer_any_car"],
  charge_timer_start: ["time", "charge_timer_start"],
  // The timer checks both sides of the cable, so the card can too.
  home_pile_plugged: ["binary_sensor", "home_charger_plugged"],
  car_plugged: ["binary_sensor", "charger_connected"],
  distance_30d: ["sensor", "distance_last_30d"],
  energy_30d: ["sensor", "energy_last_30d"],
};

const CHARGING_STATES = new Set(["charging", "smart_charging", "starting"]);
const SCHEDULED_STATES = new Set(["scheduled", "hold"]);

const STATUS_LABELS = {
  charging: "充电中",
  smart_charging: "智能充电中",
  starting: "启动中",
  stopping: "停止中",
  scheduled: "已排程",
  hold: "已暂停",
  idle: "空闲",
  // longcw charging_status values:
  connected: "已插枪",
  disconnected: "未连接",
  done: "已完成",
  fault: "故障",
  error: "故障",
  discharging: "放电中",
  unknown: "未知",
};

const CONNECTION_LABELS = {
  connected_ac: "已连接 (AC)",
  connected_dc: "已连接 (DC)",
  connected: "已连接",
  plugged_in: "已插枪",
  disconnected: "未连接",
  fault: "连接故障",
  unknown: "未知",
};

// Per-toggle confirmation prompt (car/wallbox commands only) and feedback text.
const ACTION_FEEDBACK = {
  home_charge: {
    confirmOn: "确认开始家充？",
    confirmOff: "确认停止家充？",
    on: "家充启动指令已发送",
    off: "家充停止指令已发送",
  },
  plug_and_charge: {
    confirmOn: "确认开启即插即充？",
    confirmOff: "确认关闭即插即充？",
    on: "即插即充已开启",
    off: "即插即充已关闭",
  },
  charge_timer: { on: "定时充电已开启", off: "定时充电已关闭" },
  charge_timer_any_car: {
    on: "定时充电已改为不限车辆",
    off: "定时充电需车辆确认已插枪",
  },
};

// Battery has to be this far below the charge limit for the timer to start a
// charge; mirrors CHARGE_TIMER_DEADBAND in the integration.
const TIMER_DEADBAND = 5;

const LABELS = {
  vin: "车辆 VIN",
  name: "卡片标题",
  show_controls: "显示充电控制",
  show_statistics: "显示充电统计",
};

class VolvoChargingCard extends HTMLElement {
  constructor() {
    super();
    this._pendingActions = new Set();
    this._feedbackTimer = undefined;
    this._limitDragging = false;
    this._timerEditing = false;
    this._hasRendered = false;
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "vin", required: true, selector: { text: {} } },
        { name: "name", selector: { text: {} } },
        { name: "show_controls", selector: { boolean: {} } },
        { name: "show_statistics", selector: { boolean: {} } },
      ],
      computeLabel: (schema) => LABELS[schema.name] || schema.name,
    };
  }

  static getStubConfig() {
    return {
      vin: "",
      name: "充电中心",
      show_controls: true,
      show_statistics: true,
    };
  }

  setConfig(config) {
    this._config = {
      name: "充电中心",
      show_controls: true,
      show_statistics: true,
      entities: {},
      ...config,
    };
    this._lastStateSignature = undefined;
    this._hasRendered = false;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._limitDragging || this._timerEditing) return;
    if (this.shadowRoot?.querySelector(".confirm-dialog[open]")) return;
    const signature = this._stateSignature(hass);
    if (signature === this._lastStateSignature && this.shadowRoot) return;
    this._lastStateSignature = signature;
    this._render();
  }

  getCardSize() {
    return this._config?.show_statistics === false ? 6 : 8;
  }

  getGridOptions() {
    return {
      rows: this._config?.show_statistics === false ? 6 : 8,
      columns: 12,
      min_rows: 5,
      min_columns: 6,
    };
  }

  connectedCallback() {
    this._render();
  }

  _vin() {
    return String(this._config?.vin || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]/g, "");
  }

  _entityId(key) {
    const override = this._config?.entities?.[key];
    if (override) return override;
    const definition = ENTITY_DEFINITIONS[key];
    const vin = this._vin();
    return definition && vin
      ? `${definition[0]}.${vin}_${definition[1]}`
      : undefined;
  }

  _state(key) {
    const entityId = this._entityId(key);
    return entityId ? this._hass?.states?.[entityId] : undefined;
  }

  _stateSignature(hass) {
    if (!this._config) return "unconfigured";
    return Object.keys(ENTITY_DEFINITIONS)
      .map((key) => {
        const entityId = this._entityId(key);
        const state = entityId ? hass?.states?.[entityId] : undefined;
        const attrs = state?.attributes || {};
        return [
          entityId || "",
          state?.state || "missing",
          attrs.last_charge_order?.order_no || "",
          attrs.last_run || "",
        ].join(":");
      })
      .join("|");
  }

  _isAvailable(key) {
    const state = this._state(key)?.state;
    return state !== undefined && state !== "unavailable" && state !== "unknown";
  }

  _displayState(key, fallback = "—") {
    const stateObj = this._state(key);
    if (!stateObj || stateObj.state === "unknown" || stateObj.state === "unavailable") {
      return fallback;
    }
    if (this._hass?.formatEntityState) {
      return this._hass.formatEntityState(stateObj);
    }
    const unit = stateObj.attributes?.unit_of_measurement;
    return `${stateObj.state}${unit ? ` ${unit}` : ""}`;
  }

  _stateNumber(key) {
    const value = Number.parseFloat(this._state(key)?.state);
    return Number.isFinite(value) ? value : undefined;
  }

  _escape(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _chargingState() {
    return String(this._state("charging_status")?.state || "").toLowerCase();
  }

  _isCharging() {
    return CHARGING_STATES.has(this._chargingState());
  }

  _statusLabel() {
    const raw = this._chargingState();
    if (!raw || raw === "unavailable") return "数据同步中";
    return STATUS_LABELS[raw] || raw;
  }

  _connectionLabel() {
    const raw = String(this._state("charger_connection")?.state || "").toLowerCase();
    if (!raw || raw === "unavailable" || raw === "unknown") return "";
    return CONNECTION_LABELS[raw] || raw;
  }

  _chargeLimit() {
    const value = this._stateNumber("charge_limit");
    return Number.isFinite(value) ? Math.max(50, Math.min(100, value)) : 100;
  }

  _timerStart() {
    const raw = String(this._state("charge_timer_start")?.state || "");
    return /^\d{2}:\d{2}/.test(raw) ? raw.slice(0, 5) : "23:00";
  }

  _timerVerdict(limit, timerOn, anyCarOn, start) {
    if (!timerOn) {
      return {
        tone: "",
        icon: "mdi:information-outline",
        text: `开启后每日 ${start} 自动判断是否需要家充`,
      };
    }
    const threshold = Math.max(0, limit - TIMER_DEADBAND);
    const battery = this._stateNumber("battery");
    const pilePlugged = this._state("home_pile_plugged")?.state === "on";
    const carPlugged = this._state("car_plugged")?.state === "on";
    const charging = this._state("home_charge")?.state === "on";
    const lastRun = this._state("charge_timer")?.attributes?.last_run;
    const today = new Date().toLocaleDateString("sv");  // YYYY-MM-DD

    if (lastRun === today) {
      return { tone: "", icon: "mdi:check-decagram-outline", text: `今天已判断过，明天 ${start} 再检查` };
    }
    if (charging) {
      return { tone: "", icon: "mdi:flash", text: "正在充电，定时不会干预" };
    }
    if (!pilePlugged) {
      return { tone: "warn", icon: "mdi:power-plug-off", text: `${start} 不会充电：家充桩未插枪` };
    }
    if (!carPlugged && !anyCarOn) {
      return { tone: "warn", icon: "mdi:car-off", text: `${start} 不会充电：本车未确认插枪` };
    }
    if (!Number.isFinite(battery)) {
      return { tone: "", icon: "mdi:help-circle-outline", text: `电量未知，${start} 按当时电量判断` };
    }
    if (battery > threshold) {
      return {
        tone: "warn",
        icon: "mdi:battery-check",
        text: `${start} 不会充电：电量 ${Math.round(battery)}% 高于 ${threshold}%`,
      };
    }
    return {
      tone: "ok",
      icon: "mdi:play-circle-outline",
      text: `${start} 将开始家充：电量 ${Math.round(battery)}%，充到 ${limit >= 100 ? "满" : `${limit}%`}`,
    };
  }

  _formatMinutes(value) {
    const minutes = Number.parseFloat(value);
    if (!Number.isFinite(minutes) || minutes <= 0) return "—";
    if (minutes < 60) return `${Math.round(minutes)} 分钟`;
    const hours = Math.floor(minutes / 60);
    const rest = Math.round(minutes % 60);
    return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
  }

  _formatOrderTime(value) {
    if (!value) return "—";
    const date = new Date(String(value).replace(" ", "T"));
    if (Number.isNaN(date.getTime())) return String(value);
    const pad = (num) => String(num).padStart(2, "0");
    return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  _lastChargeOrder() {
    return (
      this._state("charging_status")?.attributes?.last_charge_order ||
      this._state("charger_connection")?.attributes?.last_charge_order ||
      undefined
    );
  }

  _pileInfo() {
    const attrs =
      this._state("charging_status")?.attributes ||
      this._state("charger_connection")?.attributes ||
      {};
    return {
      name: attrs.charge_pile_name,
      address: attrs.charge_pile_address,
    };
  }

  _render() {
    if (!this.isConnected || !this._config) return;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });

    const vin = this._vin();
    if (!vin) {
      this.shadowRoot.innerHTML = `${this._styles()}
        <ha-card class="setup-card">
          <ha-icon icon="mdi:ev-station"></ha-icon>
          <div><strong>配置 Volvo 充电卡片</strong><span>请填写 VIN。</span></div>
        </ha-card>`;
      return;
    }

    const charging = this._isCharging();
    const scheduled = SCHEDULED_STATES.has(this._chargingState());
    const battery = this._stateNumber("battery");
    const batteryPct = Number.isFinite(battery)
      ? Math.max(0, Math.min(100, battery))
      : 0;
    const limit = this._chargeLimit();
    const limitEnabled = limit < 100;
    const limitReached = limitEnabled && Number.isFinite(battery) && battery >= limit;
    const connectionLabel = this._connectionLabel();
    const power = this._stateNumber("charging_power");
    const title = this._config.name || "充电中心";
    const statusTone = charging ? "charging" : scheduled ? "scheduled" : "";
    const statusText = charging && Number.isFinite(power) && power > 0
      ? `${this._statusLabel()} · ${power.toFixed(1)} kW`
      : this._statusLabel();
    const animateIn = !this._hasRendered && Boolean(this._hass);

    this.shadowRoot.innerHTML = `${this._styles()}
      <ha-card class="${animateIn ? "animate-in" : ""}">
        <div class="hero">
          <div class="identity">
            <span class="eyebrow">VOLVO CHARGING</span>
            <h2>${this._escape(title)}</h2>
            <div class="hero-meta">
              <span class="status-pill ${statusTone}"><i></i>${this._escape(statusText)}</span>
              ${connectionLabel ? `<button class="link-value" data-more-info="${this._escape(this._entityId("charger_connection"))}"><ha-icon icon="mdi:power-plug-outline"></ha-icon>${this._escape(connectionLabel)}</button>` : ""}
            </div>
          </div>
          <button class="battery-ring ${charging ? "charging" : ""}" data-more-info="${this._escape(this._entityId("battery"))}"
                  aria-label="电量 ${this._escape(this._displayState("battery"))}">
            <svg viewBox="0 0 96 96" aria-hidden="true">
              <circle class="ring-track" cx="48" cy="48" r="41"></circle>
              <circle class="ring-value" cx="48" cy="48" r="41"
                      style="stroke-dasharray:${(batteryPct / 100) * 257.6} 257.6"></circle>
            </svg>
            <span class="ring-text"><strong>${Number.isFinite(battery) ? this._escape(String(Math.round(battery))) : "—"}</strong><small>%</small></span>
          </button>
        </div>

        <div class="battery-band">
          <div class="band-top">
            <span><ha-icon icon="${charging ? "mdi:battery-charging" : "mdi:battery"}"></ha-icon>电池电量</span>
            <small>${limitEnabled ? `上限 ${limit}%` : "未设上限"}</small>
          </div>
          <div class="level-track ${charging ? "charging" : ""} ${limitReached ? "reached" : ""}">
            <i style="width:${batteryPct}%"></i>
            ${limitEnabled ? `<b class="limit-mark" style="left:${limit}%" title="充电上限 ${limit}%"></b>` : ""}
          </div>
          <div class="band-bottom">
            <button data-more-info="${this._escape(this._entityId("electric_range"))}">
              <ha-icon icon="mdi:map-marker-distance"></ha-icon>纯电续航 <strong>${this._escape(this._displayState("electric_range"))}</strong>
            </button>
          </div>
        </div>

        <div class="telemetry ${charging ? "" : "idle"}">
          ${this._telemetryTile("charging_power", "充电功率", "mdi:lightning-bolt")}
          ${this._telemetryTile("charging_voltage", "电压", "mdi:sine-wave")}
          ${this._telemetryTile("charging_current", "电流", "mdi:current-ac")}
          ${this._telemetryTile("session_energy", "本次已充", "mdi:battery-charging-100")}
          ${this._timeTile()}
        </div>

        ${this._config.show_controls === false ? "" : this._controlsSection(limit, limitEnabled, limitReached)}
        ${this._config.show_statistics === false ? "" : this._statisticsSection()}
        ${this._config.show_statistics === false ? "" : this._monthlyStatsSection()}

        <dialog class="confirm-dialog">
          <div class="dialog-icon"><ha-icon icon="mdi:ev-station"></ha-icon></div>
          <h3>确认充电操作</h3>
          <p></p>
          <div class="dialog-actions">
            <button class="dialog-cancel">取消</button>
            <button class="dialog-confirm">确认</button>
          </div>
        </dialog>
        <div class="feedback" role="status" aria-live="polite" hidden>
          <ha-icon icon="mdi:check-circle"></ha-icon><span></span>
        </div>
      </ha-card>`;

    this._bindEvents();
    if (this._hass) this._hasRendered = true;
  }

  _telemetryTile(key, label, icon) {
    const available = this._isAvailable(key);
    return `
      <button class="tile ${available ? "" : "missing"}" data-more-info="${this._escape(this._entityId(key))}">
        <span><ha-icon icon="${icon}"></ha-icon>${label}</span>
        <strong>${this._escape(this._displayState(key))}</strong>
      </button>`;
  }

  _timeTile() {
    const available = this._isAvailable("charging_time");
    const value = available
      ? this._formatMinutes(this._state("charging_time")?.state)
      : "—";
    return `
      <button class="tile ${available ? "" : "missing"}" data-more-info="${this._escape(this._entityId("charging_time"))}">
        <span><ha-icon icon="mdi:timer-outline"></ha-icon>预计剩余</span>
        <strong>${this._escape(value)}</strong>
      </button>`;
  }

  _controlsSection(limit, limitEnabled, limitReached) {
    const homeCharge = this._state("home_charge");
    const homeAvailable = homeCharge && homeCharge.state !== "unavailable";
    const homeOn = homeCharge?.state === "on";
    const homePending = this._pendingActions.has("home_charge");
    const pncState = this._state("plug_and_charge");
    const pncAvailable = pncState && pncState.state !== "unavailable";
    const pncOn = pncState?.state === "on";
    const pncPending = this._pendingActions.has("plug_and_charge");
    const limitAvailable = this._isAvailable("charge_limit");

    return `
      <div class="controls-wrap">
        <div class="section-title">
          <div><ha-icon icon="mdi:ev-plug-type2"></ha-icon><span>充电控制</span></div>
          <small>${homeAvailable ? "已关联家充桩" : "未检测到家充桩"}</small>
        </div>
        <div class="limit-row ${limitAvailable ? "" : "missing"}">
          <div class="limit-head">
            <span><ha-icon icon="mdi:battery-charging-90"></ha-icon>充电上限</span>
            <strong class="${limitReached ? "reached" : ""}">${limitEnabled ? `${limit}%` : "不限制"}</strong>
          </div>
          <input class="limit-slider" type="range" min="50" max="100" step="5"
                 value="${limit}" style="--fill:${limit}" ${limitAvailable ? "" : "disabled"}
                 aria-label="充电上限 ${limit}%" />
          <small>${
            limitReached
              ? "已达上限，家充会自动停止"
              : "电量达到上限后自动停止家充 · 拉到 100% 关闭该功能"
          }</small>
        </div>
        ${this._timerRow(limit)}
        <div class="control-buttons">
          <button class="control home ${homeOn ? "active" : ""} ${homePending ? "pending" : ""}"
                  data-action="home_charge"
                  ${homeAvailable && !homePending ? "" : "disabled"}
                  aria-busy="${homePending}">
            <span class="control-icon"><ha-icon class="${homePending ? "pending-icon" : ""}" icon="${homePending ? "mdi:loading" : homeOn ? "mdi:stop-circle-outline" : "mdi:play-circle-outline"}"></ha-icon></span>
            <span>${homeOn ? "停止家充" : "开始家充"}</span>
          </button>
          <button class="control ${pncOn ? "active" : ""} ${pncPending ? "pending" : ""}"
                  data-action="plug_and_charge"
                  ${pncAvailable && !pncPending ? "" : "disabled"}
                  aria-busy="${pncPending}">
            <span class="control-icon"><ha-icon class="${pncPending ? "pending-icon" : ""}" icon="${pncPending ? "mdi:loading" : "mdi:power-plug-battery"}"></ha-icon></span>
            <span>${pncOn ? "即插即充 开" : "即插即充 关"}</span>
          </button>
        </div>
      </div>`;
  }

  _timerRow(limit) {
    const timer = this._state("charge_timer");
    const available = timer && timer.state !== "unavailable";
    const timerOn = timer?.state === "on";
    const timerPending = this._pendingActions.has("charge_timer");
    const anyCarOn = this._state("charge_timer_any_car")?.state === "on";
    const anyCarPending = this._pendingActions.has("charge_timer_any_car");
    const start = this._timerStart();
    const verdict = this._timerVerdict(limit, timerOn, anyCarOn, start);

    return `
      <div class="timer-row ${available ? "" : "missing"}">
        <div class="timer-head">
          <span><ha-icon icon="mdi:clock-time-eight-outline"></ha-icon>定时充电</span>
          <button class="timer-toggle ${timerOn ? "active" : ""}"
                  data-action="charge_timer"
                  ${available && !timerPending ? "" : "disabled"}
                  aria-busy="${timerPending}">${timerOn ? "已开启" : "已关闭"}</button>
        </div>
        <div class="timer-body">
          <label class="timer-time">每日
            <input type="time" step="60" value="${start}"
                   ${available ? "" : "disabled"} aria-label="定时充电开始时间" />
          </label>
          <button class="timer-any ${anyCarOn ? "active" : ""}"
                  data-action="charge_timer_any_car"
                  ${available && !anyCarPending ? "" : "disabled"}
                  aria-busy="${anyCarPending}">
            <ha-icon icon="mdi:car-multiple"></ha-icon>不限车辆
          </button>
        </div>
        <div class="timer-verdict ${verdict.tone}">
          <ha-icon icon="${verdict.icon}"></ha-icon><span>${verdict.text}</span>
        </div>
        <small>${
          timerOn
            ? `每日检查一次，电量需低于上限 ${TIMER_DEADBAND}% 才开始，不会打断手动充电`
            : `到点若已插枪且电量低于上限 ${TIMER_DEADBAND}% 则自动开始家充`
        }</small>
        <small>${
          anyCarOn
            ? "仅看家充桩是否已插枪，不校验车辆"
            : "需家充桩与本车都显示已插枪"
        }</small>
      </div>`;
  }

  _statisticsSection() {
    const order = this._lastChargeOrder();
    const rows = [];

    if (order) {
      const energy = Number.parseFloat(order.energy_kwh);
      rows.push(
        this._statRow(
          "mdi:history",
          "上次充电",
          `${Number.isFinite(energy) ? `${energy.toFixed(2)} kWh` : "—"} · ${this._formatMinutes(order.duration)}`,
          "charging_status",
        ),
      );
      rows.push(
        this._statRow(
          "mdi:clock-outline",
          "充电时间",
          `${this._formatOrderTime(order.start_time)} ~ ${this._formatOrderTime(order.end_time)}`,
          "charging_status",
        ),
      );
    }
    if (this._isAvailable("session_energy")) {
      rows.push(
        this._statRow(
          "mdi:battery-charging-100",
          "本次充电电量",
          this._displayState("session_energy"),
          "session_energy",
        ),
      );
    }
    if (this._isAvailable("tm_energy")) {
      rows.push(
        this._statRow(
          "mdi:chart-line",
          "行程电耗 (TM)",
          this._displayState("tm_energy"),
          "tm_energy",
        ),
      );
    }
    return `
      <div class="statistics">
        <div class="section-title">
          <div><ha-icon icon="mdi:chart-timeline-variant"></ha-icon><span>充电统计</span></div>
          <small>来自车辆与家充桩数据</small>
        </div>
        <div class="stat-list">
          ${rows.join("") || `<div class="stat-empty">暂无充电统计数据</div>`}
        </div>
      </div>`;
  }

  _monthlyStatsSection() {
    const distMonthly = this._isAvailable("distance_30d")
      ? this._state("distance_30d")?.attributes?.monthly || []
      : [];
    const energyMonthly = this._isAvailable("energy_30d")
      ? this._state("energy_30d")?.attributes?.monthly || []
      : [];
    if (!distMonthly.length && !energyMonthly.length) return "";
    return `
      <div class="monthly-stats">
        <div class="section-title">
          <div><ha-icon icon="mdi:chart-bar"></ha-icon><span>里程与充电统计</span></div>
          <small>近 12 个月</small>
        </div>
        ${this._histogram("每月里程", distMonthly, "km", "dist")}
        ${this._histogram("每月充电量", energyMonthly, "kWh", "energy")}
      </div>`;
  }

  _histogram(label, monthly, unit, cls) {
    if (!monthly.length) return "";
    const key = unit === "km" ? "km" : "kwh";
    const values = monthly.map((m) => Number(m[key]) || 0);
    const max = Math.max(...values, 1);
    const bars = monthly
      .map((m) => {
        const value = Number(m[key]) || 0;
        const height = value > 0 ? Math.max(4, Math.round((value / max) * 100)) : 0;
        const mm = String(m.month || "").slice(5).replace(/^0/, "");
        return `<div class="hist-bar" title="${this._escape(m.month)}: ${value} ${unit}">
          <span class="hist-value">${value ? value.toFixed(unit === "km" ? 0 : 1) : ""}</span>
          <span class="hist-fill ${cls}" style="height:${height}%"></span>
          <small>${mm}月</small>
        </div>`;
      })
      .join("");
    return `
      <div class="hist">
        <div class="hist-head"><span>${label}</span><small>${unit}</small></div>
        <div class="hist-bars">${bars}</div>
      </div>`;
  }

  _statRow(icon, label, value, moreInfoKey) {
    return `
      <button class="stat-row" data-more-info="${this._escape(this._entityId(moreInfoKey))}">
        <span class="row-label"><ha-icon icon="${icon}"></ha-icon><span>${label}</span></span>
        <strong>${this._escape(value)}</strong>
      </button>`;
  }

  _bindEvents() {
    this.shadowRoot.querySelectorAll("[data-more-info]").forEach((element) => {
      element.addEventListener("click", () => {
        const entityId = element.dataset.moreInfo;
        if (!entityId || !this._hass?.states?.[entityId]) return;
        this.dispatchEvent(
          new CustomEvent("hass-more-info", {
            detail: { entityId },
            bubbles: true,
            composed: true,
          }),
        );
      });
    });
    this.shadowRoot.querySelectorAll("[data-action]").forEach((element) => {
      element.addEventListener("click", () => this._runAction(element.dataset.action));
    });

    const timeInput = this.shadowRoot.querySelector(".timer-time input");
    if (timeInput) {
      timeInput.addEventListener("focus", () => {
        this._timerEditing = true;
      });
      timeInput.addEventListener("blur", () => {
        this._timerEditing = false;
      });
      timeInput.addEventListener("change", async () => {
        const value = timeInput.value;
        if (!/^\d{2}:\d{2}$/.test(value)) return;
        try {
          await this._hass.callService("time", "set_value", {
            entity_id: this._entityId("charge_timer_start"),
            time: `${value}:00`,
          });
          this._showFeedback(`定时充电已设为每日 ${value}`);
        } catch (error) {
          this._showFeedback(`设置失败：${error?.message || error}`, "error");
        }
        this._timerEditing = false;
        this._lastStateSignature = undefined;
        this._render();
      });
    }

    const slider = this.shadowRoot.querySelector(".limit-slider");
    if (slider) {
      const preview = () => {
        this._limitDragging = true;
        const value = Number.parseInt(slider.value, 10);
        slider.style.setProperty("--fill", String(value));
        const head = this.shadowRoot.querySelector(".limit-head strong");
        if (head) head.textContent = value >= 100 ? "不限制" : `${value}%`;
        const mark = this.shadowRoot.querySelector(".limit-mark");
        if (mark) mark.style.left = `${value}%`;
      };
      slider.addEventListener("input", preview);
      slider.addEventListener("change", async () => {
        const value = Number.parseInt(slider.value, 10);
        this._limitDragging = false;
        try {
          await this._hass.callService("number", "set_value", {
            entity_id: this._entityId("charge_limit"),
            value,
          });
          this._showFeedback(
            value >= 100 ? "已关闭充电上限" : `充电上限已设为 ${value}%`,
          );
        } catch (error) {
          this._showFeedback(`设置失败：${error?.message || error}`, "error");
        }
        this._lastStateSignature = undefined;
        this._render();
      });
    }
  }

  async _runAction(key) {
    if (this._pendingActions.has(key)) return;
    const entityId = this._entityId(key);
    const stateObj = entityId ? this._hass?.states?.[entityId] : undefined;
    if (!entityId || !stateObj) return;

    const turnOn = stateObj.state !== "on";
    const service = turnOn ? "turn_on" : "turn_off";
    const feedback = ACTION_FEEDBACK[key] || {};
    const message = turnOn ? feedback.confirmOn : feedback.confirmOff;

    if (message && !(await this._confirm(message))) return;
    this._pendingActions.add(key);
    this._render();
    let feedbackMessage = "指令已发送";
    let feedbackTone = "success";
    try {
      await this._hass.callService("switch", service, { entity_id: entityId });
      feedbackMessage = (turnOn ? feedback.on : feedback.off) || feedbackMessage;
    } catch (error) {
      feedbackMessage = `操作失败：${error?.message || error}`;
      feedbackTone = "error";
    } finally {
      this._pendingActions.delete(key);
      this._lastStateSignature = undefined;
      this._render();
      this._showFeedback(feedbackMessage, feedbackTone);
    }
  }

  _confirm(message) {
    const dialog = this.shadowRoot?.querySelector(".confirm-dialog");
    if (!dialog?.showModal) return Promise.resolve(window.confirm(message));

    dialog.querySelector("p").textContent = message;
    dialog.showModal();
    return new Promise((resolve) => {
      const cancel = dialog.querySelector(".dialog-cancel");
      const confirm = dialog.querySelector(".dialog-confirm");
      const finish = (result) => {
        dialog.close();
        cancel.removeEventListener("click", onCancel);
        confirm.removeEventListener("click", onConfirm);
        dialog.removeEventListener("cancel", onCancel);
        resolve(result);
      };
      const onCancel = (event) => {
        event?.preventDefault();
        finish(false);
      };
      const onConfirm = () => finish(true);
      cancel.addEventListener("click", onCancel);
      confirm.addEventListener("click", onConfirm);
      dialog.addEventListener("cancel", onCancel);
    });
  }

  _showFeedback(message, tone = "success") {
    const element = this.shadowRoot?.querySelector(".feedback");
    if (!element) return;
    if (this._feedbackTimer) clearTimeout(this._feedbackTimer);
    element.hidden = !message;
    element.classList.toggle("error", tone === "error");
    const icon = element.querySelector("ha-icon");
    if (icon) icon.setAttribute("icon", tone === "error" ? "mdi:alert-circle" : "mdi:check-circle");
    const text = element.querySelector("span");
    if (text) text.textContent = message;
    if (!message) return;
    this._feedbackTimer = setTimeout(() => {
      element.hidden = true;
      this._feedbackTimer = undefined;
    }, 3200);
    this._feedbackTimer?.unref?.();
  }

  _styles() {
    return `<style>
      :host {
        --voc-text: var(--primary-text-color, #141414);
        --voc-secondary: var(--secondary-text-color, #707070);
        --voc-bg: var(--ha-card-background, var(--card-background-color, #fff));
        --voc-panel: color-mix(in srgb, var(--voc-bg) 97%, var(--voc-text));
        --voc-surface: color-mix(in srgb, var(--voc-bg) 95%, var(--voc-text));
        --voc-line: color-mix(in srgb, var(--voc-text) 13%, transparent);
        --voc-line-soft: color-mix(in srgb, var(--voc-text) 8%, transparent);
        --voc-blue: #1c6eba;
        --voc-warning: #cd2314;
        --voc-orange: #eb7400;
        --voc-positive: #04721c;
        --voc-accent: color-mix(in srgb, var(--voc-blue) 74%, var(--voc-text));
        --voc-danger: color-mix(in srgb, var(--voc-warning) 72%, var(--voc-text));
        --voc-success: color-mix(in srgb, var(--voc-positive) 72%, var(--voc-text));
        display: block;
        container-type: inline-size;
        color: var(--voc-text);
        font-family: var(--ha-font-family-body, "Helvetica Neue", Arial, sans-serif);
      }
      * { box-sizing: border-box; }
      button { font: inherit; -webkit-tap-highlight-color: transparent; }
      ha-card {
        position: relative;
        display: block;
        overflow: hidden;
        container-type: inline-size;
        background: var(--voc-bg);
        border: 1px solid var(--voc-line-soft);
        border-radius: var(--ha-card-border-radius, 18px);
        box-shadow: 0 12px 32px rgba(0, 0, 0, .08);
      }
      .hero {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        padding: 22px 22px 14px;
      }
      .identity { min-width: 0; }
      .eyebrow {
        display: block;
        margin-bottom: 5px;
        color: var(--voc-secondary);
        font-size: 9px;
        font-weight: 600;
        letter-spacing: .18em;
        text-transform: uppercase;
      }
      h2 {
        margin: 0;
        overflow: hidden;
        font-size: 26px;
        font-weight: 450;
        letter-spacing: -.025em;
        line-height: 1.08;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .hero-meta {
        min-height: 20px;
        padding-top: 8px;
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        color: var(--voc-secondary);
        font-size: 10px;
      }
      .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        border: 1px solid var(--voc-line);
        border-radius: 12px;
        padding: 3px 9px;
        background: var(--voc-surface);
        white-space: nowrap;
      }
      .status-pill i { width: 6px; height: 6px; border-radius: 50%; background: var(--voc-secondary); }
      .status-pill.charging { border-color: color-mix(in srgb, var(--voc-positive) 40%, transparent); color: var(--voc-success); }
      .status-pill.charging i { background: var(--voc-positive); animation: voc-status-pulse 1.7s ease-in-out infinite; }
      .status-pill.scheduled { border-color: color-mix(in srgb, var(--voc-blue) 40%, transparent); color: var(--voc-accent); }
      .status-pill.scheduled i { background: var(--voc-blue); }
      .link-value {
        min-width: 0;
        border: 0;
        padding: 0;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        background: none;
        color: inherit;
        cursor: pointer;
        font-size: 10px;
        white-space: nowrap;
      }
      .link-value ha-icon { --mdc-icon-size: 13px; }
      .battery-ring {
        position: relative;
        width: 84px;
        height: 84px;
        border: 0;
        padding: 0;
        background: none;
        cursor: pointer;
        flex: 0 0 auto;
      }
      .battery-ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
      .ring-track { fill: none; stroke: color-mix(in srgb, var(--voc-text) 10%, transparent); stroke-width: 7; }
      .ring-value {
        fill: none;
        stroke: var(--voc-blue);
        stroke-width: 7;
        stroke-linecap: round;
        transition: stroke-dasharray .5s ease;
      }
      .battery-ring.charging .ring-value { stroke: var(--voc-positive); }
      .ring-text {
        position: absolute;
        inset: 0;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 1px;
      }
      .ring-text strong { font-size: 22px; font-weight: 500; letter-spacing: -.02em; }
      .ring-text small { color: var(--voc-secondary); font-size: 10px; padding-top: 6px; }
      .battery-band { margin: 0 22px; padding: 13px 15px 14px; border: 1px solid var(--voc-line-soft); border-radius: 14px; background: var(--voc-surface); }
      .band-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; color: var(--voc-secondary); font-size: 10px; }
      .band-top > span { display: inline-flex; align-items: center; gap: 5px; }
      .band-top ha-icon { --mdc-icon-size: 15px; color: var(--voc-blue); }
      .band-top small { white-space: nowrap; }
      .level-track {
        position: relative;
        height: 8px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--voc-text) 12%, transparent);
      }
      .level-track i {
        display: block;
        height: 100%;
        border-radius: inherit;
        background: var(--voc-blue);
        transform-origin: left center;
        transition: width .5s ease;
      }
      .level-track.charging i { background: var(--voc-positive); }
      .level-track.reached i { background: var(--voc-orange); }
      .limit-mark {
        position: absolute;
        top: -3px;
        bottom: -3px;
        width: 2px;
        margin-left: -1px;
        border-radius: 2px;
        background: var(--voc-orange);
        box-shadow: 0 0 0 1px color-mix(in srgb, var(--voc-bg) 70%, transparent);
      }
      .band-bottom { display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 10px; }
      .band-bottom button {
        min-width: 0;
        border: 0;
        padding: 0;
        display: inline-flex;
        align-items: center;
        gap: 5px;
        background: none;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
        white-space: nowrap;
      }
      .band-bottom ha-icon { --mdc-icon-size: 13px; }
      .band-bottom strong { color: var(--voc-text); font-weight: 600; }
      .telemetry {
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 7px;
        margin: 12px 22px 0;
      }
      .telemetry.idle .tile { opacity: .75; }
      .tile {
        min-width: 0;
        min-height: 58px;
        border: 1px solid var(--voc-line-soft);
        border-radius: 12px;
        padding: 8px 9px;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        gap: 4px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        text-align: left;
        cursor: pointer;
        font-size: 9px;
        transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
      }
      .tile > span { display: inline-flex; align-items: center; gap: 4px; overflow: hidden; white-space: nowrap; }
      .tile ha-icon { --mdc-icon-size: 13px; color: var(--voc-accent); flex: 0 0 auto; }
      .tile strong {
        overflow: hidden;
        color: var(--voc-text);
        font-size: 12px;
        font-weight: 600;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .tile.missing { opacity: .4; cursor: default; }
      .controls-wrap { margin-top: 15px; border-top: 1px solid var(--voc-line); padding: 14px 22px 17px; }
      .section-title {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 10px;
        margin-bottom: 11px;
      }
      .section-title > div { display: flex; align-items: center; gap: 7px; }
      .section-title > div ha-icon { --mdc-icon-size: 16px; color: var(--voc-secondary); }
      .section-title > div span { font-size: 12px; font-weight: 600; }
      .section-title small { color: var(--voc-secondary); font-size: 10px; }
      .limit-row {
        border: 1px solid var(--voc-line-soft);
        border-radius: 13px;
        padding: 12px 14px 11px;
        background: var(--voc-surface);
      }
      .limit-row.missing { opacity: .45; }
      .limit-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; font-size: 10px; color: var(--voc-secondary); }
      .limit-head > span { display: inline-flex; align-items: center; gap: 5px; }
      .limit-head ha-icon { --mdc-icon-size: 15px; color: var(--voc-orange); }
      .limit-head strong { color: var(--voc-text); font-size: 13px; font-weight: 600; }
      .limit-head strong.reached { color: var(--voc-orange); }
      .limit-slider {
        width: 100%;
        height: 22px;
        margin: 0;
        appearance: none;
        background: transparent;
        cursor: pointer;
      }
      .limit-slider:disabled { cursor: default; }
      .limit-slider::-webkit-slider-runnable-track {
        height: 6px;
        border-radius: 999px;
        background: linear-gradient(to right, var(--voc-orange) 0%, var(--voc-orange) calc((var(--fill, 100) - 50) / 50 * 100%), color-mix(in srgb, var(--voc-text) 12%, transparent) 0%);
      }
      .limit-slider::-webkit-slider-thumb {
        appearance: none;
        width: 18px;
        height: 18px;
        margin-top: -6px;
        border: 2px solid var(--voc-bg);
        border-radius: 50%;
        background: var(--voc-orange);
        box-shadow: 0 1px 4px rgba(0,0,0,.25);
      }
      .limit-slider::-moz-range-track {
        height: 6px;
        border-radius: 999px;
        background: color-mix(in srgb, var(--voc-text) 12%, transparent);
      }
      .limit-slider::-moz-range-progress { height: 6px; border-radius: 999px; background: var(--voc-orange); }
      .limit-slider::-moz-range-thumb {
        width: 14px;
        height: 14px;
        border: 2px solid var(--voc-bg);
        border-radius: 50%;
        background: var(--voc-orange);
        box-shadow: 0 1px 4px rgba(0,0,0,.25);
      }
      .limit-row small { display: block; margin-top: 5px; color: var(--voc-secondary); font-size: 9px; }
      .timer-row {
        margin-top: 9px;
        border: 1px solid var(--voc-line-soft);
        border-radius: 13px;
        padding: 12px 14px 11px;
        background: var(--voc-surface);
      }
      .timer-row.missing { opacity: .45; }
      .timer-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 10px; color: var(--voc-secondary); }
      .timer-head > span { display: inline-flex; align-items: center; gap: 5px; }
      .timer-head ha-icon { --mdc-icon-size: 15px; color: var(--voc-blue); }
      .timer-toggle, .timer-any {
        border: 1px solid var(--voc-line-soft);
        border-radius: 999px;
        padding: 4px 10px;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        background: var(--voc-bg);
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
        font-weight: 550;
        white-space: nowrap;
        transition: border-color .18s ease, background-color .18s ease, color .18s ease;
      }
      .timer-toggle.active, .timer-any.active {
        border-color: color-mix(in srgb, var(--voc-blue) 28%, transparent);
        background: color-mix(in srgb, var(--voc-blue) 8%, var(--voc-bg));
        color: var(--voc-accent);
      }
      .timer-any ha-icon { --mdc-icon-size: 13px; }
      .timer-toggle:disabled, .timer-any:disabled { opacity: .35; cursor: default; }
      .timer-body { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 9px; }
      .timer-time { display: inline-flex; align-items: center; gap: 6px; color: var(--voc-secondary); font-size: 10px; }
      .timer-time input {
        border: 1px solid var(--voc-line-soft);
        border-radius: 9px;
        padding: 5px 8px;
        background: var(--voc-bg);
        color: var(--voc-text);
        font: inherit;
        font-size: 13px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
      }
      .timer-time input:disabled { opacity: .5; }
      .timer-verdict {
        display: flex;
        align-items: center;
        gap: 5px;
        margin-top: 9px;
        color: var(--voc-secondary);
        font-size: 10px;
        font-weight: 550;
      }
      .timer-verdict ha-icon { --mdc-icon-size: 14px; flex: 0 0 auto; }
      .timer-verdict.ok { color: var(--voc-success); }
      .timer-verdict.ok ha-icon { color: var(--voc-positive); }
      .timer-verdict.warn { color: var(--voc-orange); }
      .timer-verdict.warn ha-icon { color: var(--voc-orange); }
      .timer-row small { display: block; margin-top: 5px; color: var(--voc-secondary); font-size: 9px; }
      .control-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 9px; }
      .control {
        min-width: 0;
        min-height: 52px;
        border: 1px solid transparent;
        border-radius: 12px;
        padding: 8px 10px;
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        background: var(--voc-surface);
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 11px;
        font-weight: 550;
        box-shadow: 0 6px 14px rgba(0, 0, 0, .025);
        transition: transform .18s ease, border-color .18s ease, background-color .18s ease, box-shadow .18s ease, color .18s ease;
      }
      .control-icon {
        width: 30px;
        height: 30px;
        display: grid;
        place-items: center;
        border-radius: 50%;
        background: var(--voc-bg);
        color: var(--voc-text);
        box-shadow: inset 0 0 0 1px var(--voc-line-soft);
        flex: 0 0 auto;
        transition: background-color .18s ease, color .18s ease;
      }
      .control-icon ha-icon { --mdc-icon-size: 17px; }
      .control.active { border-color: color-mix(in srgb, var(--voc-blue) 28%, transparent); background: color-mix(in srgb, var(--voc-blue) 8%, var(--voc-bg)); color: var(--voc-accent); }
      .control.active .control-icon { background: var(--voc-blue); color: #fff; }
      .control.home.active { border-color: color-mix(in srgb, var(--voc-positive) 32%, transparent); background: color-mix(in srgb, var(--voc-positive) 8%, var(--voc-bg)); color: var(--voc-success); }
      .control.home.active .control-icon { background: var(--voc-positive); }
      .control:disabled { opacity: .35; cursor: default; }
      .control.pending:disabled { opacity: .76; }
      .pending-icon { animation: voc-spin .8s linear infinite; }
      .statistics { border-top: 1px solid var(--voc-line); margin-top: 15px; padding: 14px 22px 18px; }
      .controls-wrap + .statistics { margin-top: 0; }
      .stat-list { overflow: hidden; border: 1px solid var(--voc-line); border-radius: 13px; background: var(--voc-surface); }
      .stat-row {
        width: 100%;
        min-height: 42px;
        border: 0;
        border-top: 1px solid var(--voc-line);
        padding: 8px 13px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 10px;
        background: transparent;
        color: var(--voc-secondary);
        cursor: pointer;
        font-size: 10px;
        text-align: left;
        transition: background-color .16s ease;
      }
      .stat-row:first-child { border-top: 0; }
      .row-label { min-width: 0; display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto; }
      .row-label ha-icon { --mdc-icon-size: 14px; color: var(--voc-secondary); }
      .stat-row strong {
        overflow: hidden;
        color: var(--voc-text);
        font-size: 10px;
        font-weight: 600;
        text-align: right;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .stat-empty { min-height: 52px; display: grid; place-items: center; color: var(--voc-secondary); font-size: 10px; }
      .monthly-stats { border-top: 1px solid var(--voc-line); padding: 14px 22px 18px; }
      .hist { margin-top: 10px; border: 1px solid var(--voc-line); border-radius: 13px; background: var(--voc-surface); padding: 12px 14px 10px; }
      .hist-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 9px; color: var(--voc-secondary); font-size: 10px; }
      .hist-head span { color: var(--voc-text); font-weight: 600; }
      .hist-bars { display: flex; align-items: flex-end; gap: 4px; height: 84px; }
      .hist-bar { flex: 1 1 0; min-width: 0; height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; gap: 3px; }
      .hist-value { font-size: 7px; color: var(--voc-secondary); line-height: 1; height: 8px; white-space: nowrap; }
      .hist-fill { width: 100%; max-width: 22px; min-height: 2px; border-radius: 5px 5px 2px 2px; animation: voc-bar-in .5s cubic-bezier(.2,.7,.2,1) both; transform-origin: bottom; }
      @keyframes voc-bar-in { from { transform: scaleY(0); } to { transform: scaleY(1); } }
      @media (prefers-reduced-motion: reduce) { .hist-fill { animation: none; } }
      .hist-fill.dist { background: var(--voc-blue); }
      .hist-fill.energy { background: var(--voc-success); }
      .hist-bar small { color: var(--voc-secondary); font-size: 8px; white-space: nowrap; }
      .confirm-dialog {
        width: min(88vw, 330px);
        border: 0;
        border-radius: 14px;
        padding: 24px;
        background: var(--voc-bg);
        box-shadow: 0 20px 60px rgba(0,0,0,.32);
        color: var(--voc-text);
        text-align: center;
      }
      .confirm-dialog[open] { animation: voc-dialog-in .2s cubic-bezier(.2, .8, .2, 1) both; }
      .confirm-dialog::backdrop { background: rgba(0,0,0,.48); backdrop-filter: blur(3px); }
      .dialog-icon { width: 46px; height: 46px; margin: 0 auto 13px; border-radius: 50%; display: grid; place-items: center; background: var(--voc-surface); color: var(--voc-blue); }
      .dialog-icon ha-icon { --mdc-icon-size: 23px; }
      .confirm-dialog h3 { margin: 0; font-size: 17px; font-weight: 500; }
      .confirm-dialog p { margin: 8px 0 20px; color: var(--voc-secondary); font-size: 13px; line-height: 1.5; }
      .dialog-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 9px; }
      .dialog-actions button { min-height: 42px; border: 0; border-radius: 10px; cursor: pointer; }
      .dialog-cancel { background: var(--voc-surface); color: var(--voc-text); }
      .dialog-confirm { background: var(--voc-blue); color: #fff; }
      .feedback {
        position: absolute;
        z-index: 8;
        left: 50%;
        bottom: 14px;
        min-height: 38px;
        max-width: calc(100% - 32px);
        border: 1px solid color-mix(in srgb, var(--voc-positive) 28%, transparent);
        border-radius: 19px;
        padding: 0 13px;
        display: flex;
        align-items: center;
        gap: 7px;
        transform: translateX(-50%);
        background: color-mix(in srgb, var(--voc-bg) 90%, var(--voc-positive));
        box-shadow: 0 10px 26px rgba(0, 0, 0, .18);
        color: var(--voc-success);
        font-size: 11px;
        white-space: nowrap;
        animation: voc-toast-in .24s cubic-bezier(.2, .8, .2, 1) both;
      }
      .feedback[hidden] { display: none; }
      .feedback ha-icon { --mdc-icon-size: 16px; flex: 0 0 auto; }
      .feedback span { overflow: hidden; text-overflow: ellipsis; }
      .feedback.error { border-color: color-mix(in srgb, var(--voc-warning) 30%, transparent); background: color-mix(in srgb, var(--voc-bg) 90%, var(--voc-warning)); color: var(--voc-danger); }
      .setup-card { min-height: 130px; padding: 24px; display: flex; align-items: center; gap: 14px; }
      .setup-card > ha-icon { --mdc-icon-size: 34px; color: var(--voc-blue); }
      .setup-card div { display: flex; flex-direction: column; gap: 4px; }
      .setup-card strong { font-weight: 500; }
      .setup-card span { color: var(--voc-secondary); font-size: 12px; }
      button:focus-visible, .limit-slider:focus-visible, .timer-time input:focus-visible { outline: 2px solid var(--voc-blue); outline-offset: 2px; }
      @media (hover: hover) {
        .tile:hover, .stat-row:hover { background: color-mix(in srgb, var(--voc-blue) 5%, var(--voc-surface)); }
        .control:not(:disabled):hover { transform: translateY(-2px); border-color: color-mix(in srgb, var(--voc-blue) 20%, var(--voc-line)); box-shadow: 0 10px 20px rgba(0, 0, 0, .08); }
      }
      .control:not(:disabled):active { transform: scale(.98); }
      .animate-in .level-track i { animation: voc-progress-in .55s cubic-bezier(.2, .7, .2, 1) both; }
      @keyframes voc-progress-in { from { transform: scaleX(0); } to { transform: scaleX(1); } }
      @keyframes voc-spin { to { transform: rotate(360deg); } }
      @keyframes voc-status-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
      @keyframes voc-dialog-in { from { opacity: 0; transform: translateY(8px) scale(.98); } to { opacity: 1; transform: translateY(0) scale(1); } }
      @keyframes voc-toast-in { from { opacity: 0; transform: translate(-50%, 8px) scale(.98); } to { opacity: 1; transform: translate(-50%, 0) scale(1); } }
      @container (max-width: 520px) {
        .hero { padding: 19px 16px 12px; }
        h2 { font-size: 23px; }
        .battery-ring { width: 74px; height: 74px; }
        .ring-text strong { font-size: 19px; }
        .battery-band { margin: 0 16px; }
        .telemetry { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-inline: 16px; }
        .controls-wrap, .statistics, .monthly-stats { padding-inline: 16px; }
      }
      @container (max-width: 340px) {
        .telemetry { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .control-buttons { grid-template-columns: 1fr; }
      }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
      }
    </style>`;
  }
}

if (!customElements.get("volvo-charging-card")) {
  customElements.define("volvo-charging-card", VolvoChargingCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "volvo-charging-card")) {
  window.customCards.push({
    type: "volvo-charging-card",
    name: "Volvo 充电中心卡",
    description: "充电状态、实时功率遥测、充电统计与家充控制，并支持充电上限自动停充。",
    preview: true,
    documentationURL: "https://github.com/Annincikee/hass-volvooncall-cn",
    getEntitySuggestion: (_hass, entityId) => {
      const match = entityId.match(
        /^(?:sensor|switch|number)\.([a-z0-9]+)_(?:battery_charge_level|charging_status|charging_power|charge_limit)$/,
      );
      if (!match) return null;
      return {
        config: {
          type: "custom:volvo-charging-card",
          vin: match[1].toUpperCase(),
          name: "充电中心",
          show_controls: true,
          show_statistics: true,
        },
      };
    },
  });
}

console.info(
  `%c VOLVO-CHARGING-CARD %c v${CARD_VERSION} `,
  "color:#fff;background:#04721c;padding:3px 5px",
  "color:#04721c;background:#e8f3ea;padding:3px 5px",
);
