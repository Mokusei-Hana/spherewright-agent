const { createApp, nextTick } = Vue;

const app = createApp({
  data() {
    return {
      version: 'v3.0.0',
      theme: document.documentElement.dataset.theme,
      activeView: 'overview',
      views: [{id: 'overview', name: '运行总览', icon: 'overview'}, {id: 'tasks', name: '任务调度', icon: 'tasks'}, {id: 'ledger', name: '安全账本', icon: 'ledger'}, {id: 'logs', name: '实时日志', icon: 'logs'}],
      statusError: false,
      lastSync: null,
      busy: false,
      status: null,
      tasks: [],
      ledger: { events: [], total: 0, page: 1, pageSize: 25 },
      ledgerFilter: '',
      logs: [],
      logFilter: '',
      wsConnected: false,
      writeEnabled: false,
      emergencyStopped: false,
      taskGoal: '',
      selectedEvent: null,
      isRefreshing: false,
      toasts: [],
      ws: null,
      statusTimer: null,
      ledgerTimer: null,
      toastId: 0,
      quickGoalPresets: [
        '观察当前配方科技树并汇报',
        '采集铜矿10个并返回',
        '巡检电力网与机甲能量',
        '检查仓储与分拣器状态'
      ],
    };
  },

  computed: {
    energyRatio() {
      const value = this.status?.icarus?.coreEnergyRatio;
      return typeof value === 'number' && Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : null;
    },
    energyLabel() {
      if (this.energyRatio !== null) return (this.energyRatio * 100).toFixed(1) + '%';
      const value = this.status?.icarus?.coreEnergy;
      return value != null ? value + ' J' : '—';
    },
    positionLabel() {
      const p = this.status?.icarus?.position;
      return p ? ['x', 'y', 'z'].map(k => k.toUpperCase() + ' ' + (typeof p[k] === 'number' ? Math.round(p[k]) : '—')).join(' · ') : '—';
    },
    filteredLogs() {
      if (!this.logFilter.trim()) return this.logs;
      const q = this.logFilter.toLowerCase();
      return this.logs.filter(l => 
        String(l.message).toLowerCase().includes(q) || 
        l.type.toLowerCase().includes(q)
      );
    }
  },

  methods: {
    openRelease() {
      this.$refs.release.open = true;
      this.$refs.release.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'center' });
    },
    closeWriteBackdrop(event) {
      if (event.target !== this.$refs.writeDialog) return;
      const box = event.target.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) event.target.close();
    },
    // ---------- API Fetchers ----------
    async apiFetch(url, opts = {}) {
      try {
        const res = await fetch(url, {
          headers: { 'Content-Type': 'application/json' },
          ...opts,
        });
        const data = await res.json();
        if (!res.ok) {
          this.showToast(data.error || `HTTP ${res.status}`, 'error');
          return null;
        }
        return data;
      } catch (e) {
        this.showToast(`Network error: ${e.message}`, 'error');
        return null;
      }
    },

    async loadStatus() {
      const data = await this.apiFetch('/api/status');
      if (data) {
        this.status = data;
        this.writeEnabled = data.write_enabled;
        this.emergencyStopped = data.emergency_stop;
        this.statusError = false;
        this.lastSync = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      } else {
        this.statusError = true;
      }
    },

    async loadTasks() {
      const data = await this.apiFetch('/api/tasks');
      if (data) this.tasks = data.tasks || [];
    },

    async loadLedger(page = 1) {
      if (page < 1) return;
      const filter = this.ledgerFilter ? `&status=${encodeURIComponent(this.ledgerFilter)}` : '';
      const data = await this.apiFetch(`/api/ledger?page=${page}&page_size=${this.ledger.pageSize}${filter}`);
      if (data) {
        this.ledger.events = data.events || [];
        this.ledger.total = data.total || 0;
        this.ledger.page = data.page || page;
      }
    },

    async refreshAll() {
      this.isRefreshing = true;
      await Promise.all([this.loadStatus(), this.loadTasks(), this.loadLedger(this.ledger.page)]);
      setTimeout(() => { this.isRefreshing = false; }, 400);
      if (!this.statusError) this.showToast('遥测已同步', 'info');
    },

    // ---------- Actions ----------
    async confirmWrite() {
      this.$refs.writeDialog.close();
      await this.setWrite(true);
    },

    async setWrite(enabled) {
      const data = await this.apiFetch('/api/write-toggle', {
        method: 'POST',
        body: JSON.stringify({ enabled }),
      });
      if (data) {
        this.writeEnabled = data.write_enabled;
        this.showToast(
          enabled ? '⚠ 受控写入模式已解锁 / Write Gate Unlocked' : '🔒 已切换回只读模式 / Safe Read-Only Active',
          enabled ? 'error' : 'success'
        );
        this.addLog('SYSTEM', enabled ? 'Write interlock released by engineer' : 'Write interlock restored (Read-Only)');
      }
    },

    async createTask() {
      if (!this.writeEnabled || this.busy || !this.taskGoal.trim()) return;
      this.busy = true;
      const data = await this.apiFetch('/api/tasks', {
        method: 'POST',
        body: JSON.stringify({ goal: this.taskGoal }),
      });
      if (data) {
        this.showToast('✔ 任务已创建 / Task created', 'success');
        this.addLog('TASK', `任务已提交：${this.taskGoal}`);
        this.taskGoal = '';
        this.loadTasks();
      }
      this.busy = false;
    },

    async pauseTask(taskId) {
      const data = await this.apiFetch(`/api/tasks/${taskId}/pause`, { method: 'POST' });
      if (data) {
        this.showToast('⏸ 任务已挂起 / Task paused', 'info');
        this.addLog('TASK', `Task #${taskId.substring(0,8)} paused`);
        this.loadTasks();
      }
    },

    async resumeTask(taskId) {
      const data = await this.apiFetch(`/api/tasks/${taskId}/resume`, { method: 'POST' });
      if (data) {
        this.showToast('▶ 任务已恢复 / Task resumed', 'success');
        this.addLog('TASK', `Task #${taskId.substring(0,8)} resumed`);
        this.loadTasks();
      }
    },

    async emergencyStop() {
      const data = await this.apiFetch('/api/emergency-stop', { method: 'POST' });
      if (data) {
        this.emergencyStopped = true;
        this.showToast('🛑 紧急停止已激活 / Emergency Stop Initiated', 'error');
        this.addLog('EMERGENCY', 'Reactor halt signal dispatched. PAUSE state asserted.');
      }
    },

    async emergencyReset() {
      const data = await this.apiFetch('/api/emergency-stop/reset', { method: 'POST' });
      if (data) {
        this.emergencyStopped = false;
        this.showToast('✔ 紧急停止已解除 / Emergency Cleared', 'success');
        this.addLog('EMERGENCY', 'Emergency stop cleared. Operational standby.');
      }
    },

    copyEnvelope(data) {
      navigator.clipboard.writeText(JSON.stringify(data, null, 2))
        .then(() => this.showToast('JSON 已复制', 'success'))
        .catch(() => this.showToast('复制失败，请手动选择 JSON 复制', 'error'));
    },

    // ---------- WebSocket ----------
    connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${proto}://${location.host}/ws/live`;
      this.ws = new WebSocket(url);
      this.ws.onopen = () => {
        this.wsConnected = true;
        this.addLog('LIVE', '实时事件通道已连接');
      };
      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'event') {
            const kind = msg.kind || 'LEDGER';
            let detail = '';
            if (msg.event && msg.event.data) {
              detail = this.summarizeEvent(msg.event.data);
            }
            this.addLog(kind.toUpperCase(), detail || JSON.stringify(msg.event));
            this.loadLedger(this.ledger.page);
          } else if (msg.type === 'emergency_stop') {
            this.emergencyStopped = true;
            this.addLog('EMERGENCY', msg.message);
          } else if (msg.type === 'emergency_reset') {
            this.emergencyStopped = false;
            this.addLog('EMERGENCY', msg.message);
          } else {
            this.addLog(msg.type?.toUpperCase() || 'INFO', msg.message || JSON.stringify(msg));
          }
        } catch {
          this.addLog('RAW', event.data);
        }
      };
      this.ws.onclose = () => {
        this.wsConnected = false;
        setTimeout(() => this.connectWS(), 3000);
      };
      this.ws.onerror = () => {
        this.wsConnected = false;
      };
    },

    addLog(type, message) {
      const now = new Date();
      const time = `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}`;
      this.logs.push({ type, message, time });
      if (this.logs.length > 500) this.logs.splice(0, this.logs.length - 500);
      nextTick(() => {
        const el = this.$refs.consoleEl;
        if (el) el.scrollTop = el.scrollHeight;
      });
    },

    // ---------- Visual Helpers ----------
    showToast(message, type = 'info') {
      const id = ++this.toastId;
      this.toasts.push({ id, message, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 4000);
    },

    tone(value) {
      const s = String(value || '').toLowerCase();
      if (['failed', 'error', 'emergency', 'read_retry'].includes(s)) return 'danger';
      if (['running', 'completed', 'verified', 'live', 'entry'].includes(s)) return 'success';
      if (['pending', 'blocked', 'waiting_confirmation', 'prepare', 'prepare_intent', 'action'].includes(s)) return 'warning';
      if (['decision', 'task', 'observation', 'system_observation'].includes(s)) return 'info';
      return 'neutral';
    },
    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = this.theme;
      try { localStorage.setItem('spherewright-theme', this.theme); } catch {}
    },
    openWriteDialog() {
      if (this.writeEnabled) { this.setWrite(false); return; }
      this.$refs.writeDialog.showModal();
    },
    summarizeEvent(data) {
      if (!data) return '{}';
      if (typeof data === 'string') return data;
      const keys = Object.keys(data);
      if (keys.length === 0) return '{}';
      const preview = keys.slice(0, 3).map(k => {
        const v = data[k];
        if (v === null || v === undefined) return `${k}: null`;
        if (typeof v === 'object') return `${k}: {...}`;
        const s = String(v);
        return `${k}: ${s.length > 25 ? s.substring(0, 25) + '...' : s}`;
      }).join(', ');
      return keys.length > 3 ? `{ ${preview}, ... }` : `{ ${preview} }`;
    },
  },

  mounted() {
    this.loadStatus();
    this.loadTasks();
    this.loadLedger();
    this.connectWS();
    this.statusTimer = setInterval(() => {
      this.loadStatus();
      this.loadTasks();
    }, 5000);
    this.ledgerTimer = setInterval(() => this.loadLedger(this.ledger.page), 10000);
  },

  beforeUnmount() {
    if (this.statusTimer) clearInterval(this.statusTimer);
    if (this.ledgerTimer) clearInterval(this.ledgerTimer);
    if (this.ws) this.ws.close();
  },
});

app.mount('#app');
