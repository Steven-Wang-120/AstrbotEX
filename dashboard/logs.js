/* EX log terminal (B05).
 *
 * A self-contained view module: it owns the log page DOM under #page-logs and
 * exposes window.EXLogs for app.js to drive (C01 section 7). It never opens a
 * connection, never reads credentials and never calls the network - B08 owns
 * the SSE transport and the auth headers.
 *
 * Appearance contract (C05):
 *   background #111111, normal #6CC070, warning #FFD166, error #FF6B6B,
 *   metadata #9CA3AF. The whole body line is coloured, not just the badge.
 *
 * Safety: every piece of event text goes in through textContent. Control
 * characters and ANSI escapes are stripped first, so a malicious payload is
 * displayed as text and can never become markup or a terminal command.
 */
(function (global) {
  "use strict";

  var MAX_ENTRIES = 1000;
  var MAX_TEXT_BYTES = 2 * 1024 * 1024; /* 2 MiB estimate over retained text */
  var MAX_DETAIL_CHARS = 16 * 1024;
  var MAX_SEEN_IDS = 4000;
  var LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
  var LEVEL_RANK = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };
  var LEVEL_ALIASES = {
    debug: "DEBUG", trace: "DEBUG", verbose: "DEBUG",
    info: "INFO", notice: "INFO", information: "INFO",
    warn: "WARNING", warning: "WARNING",
    error: "ERROR", err: "ERROR", severe: "ERROR",
    critical: "CRITICAL", fatal: "CRITICAL", crit: "CRITICAL",
  };
  var ERROR_TYPES = { fault: true, plugin_fault: true, rule_rejected: true };
  var INFO_TYPES = { runtime_state: true, plugin: true, environment: true,
                     environment_changed: true, ros_graph_changed: true,
                     ros_endpoints_changed: true, decision_changed: true };

  var dom = null;
  var listeners = [];
  var entries = [];
  var totalBytes = 0;
  var seenIds = new Set();
  var seenOrder = [];
  var sources = new Set();
  var filters = { levels: new Set(LEVELS), sources: new Set(), text: "" };
  var autoscroll = true;
  var unread = 0;
  var pendingFlush = false;
  var pendingRows = [];
  var mounted = false;

  /* -- text helpers ------------------------------------------------------ */
  function stripControl(value) {
    var text = value === null || value === undefined ? "" : String(value);
    /* CSI / OSC sequences first, then any remaining C0/C1 control characters.
       A newline or tab inside a message is legitimate; everything else is not. */
    text = text.replace(/\u001b\][^\u0007\u001b]*(\u0007|\u001b\\)/g, "");
    text = text.replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "");
    var out = "";
    for (var index = 0; index < text.length; index += 1) {
      var code = text.charCodeAt(index);
      if (code === 9 || code === 10 || code === 13) {
        out += code === 9 ? "    " : "\n";
      } else if (code < 32 || (code >= 127 && code <= 159)) {
        out += " ";
      } else {
        out += text[index];
      }
    }
    return out;
  }

  function truncate(text, limit, marker) {
    if (typeof text !== "string") return "";
    if (text.length <= limit) return text;
    return text.slice(0, limit) + (marker || "…");
  }

  function byteLength(text) {
    /* Cheap, monotonic estimate: UTF-16 code units are >= 1 byte in UTF-8, so
       this never under-counts the memory a string actually costs. */
    return typeof text === "string" ? text.length * 2 : 0;
  }

  function formatClock(value) {
    var date = value instanceof Date ? value : new Date(value);
    if (!isFinite(date.getTime())) return "--:--:--";
    var pad = function (number) { return String(number).padStart(2, "0"); };
    return pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
  }

  function formatStamp(value) {
    var date = value instanceof Date ? value : new Date(value);
    if (!isFinite(date.getTime())) return "--";
    return date.toISOString().replace("T", " ").replace("Z", "");
  }

  /* -- level resolution (C01 section 7) --------------------------------- */
  function normalizeLevelName(value) {
    if (!value) return null;
    return LEVEL_ALIASES[String(value).trim().toLowerCase()] || null;
  }

  function resolveLevel(event) {
    var data = event && event.data && typeof event.data === "object" ? event.data : {};
    var log = data.log && typeof data.log === "object" ? data.log : {};
    /* 1. A level assigned by the bus when the event was retained. */
    var explicit = normalizeLevelName(log.level);
    /* 2. Producer-declared severity, then a legacy level field. */
    if (!explicit) explicit = normalizeLevelName(data.severity);
    if (!explicit) explicit = normalizeLevelName(data.level);
    /* 3. Faults and rejections are at least ERROR, whatever the producer says. */
    var type = String(event && event.type || "").toLowerCase();
    var floor = null;
    if (ERROR_TYPES[type] || (data.error !== undefined && data.error !== null && data.error !== "")) {
      floor = "ERROR";
    }
    if (explicit && floor) {
      return LEVEL_RANK[explicit] >= LEVEL_RANK[floor] ? explicit : floor;
    }
    if (explicit) return explicit;
    if (floor) return floor;
    if (INFO_TYPES[type]) return "INFO";
    return "DEBUG";
  }

  /* -- entry construction ------------------------------------------------ */
  function detailFrom(event) {
    var data = event && event.data && typeof event.data === "object" ? event.data : null;
    if (!data) return "";
    var copy = {};
    var keys = Object.keys(data);
    for (var index = 0; index < keys.length; index += 1) {
      var key = keys[index];
      if (key === "log") continue; /* retained bookkeeping, not payload */
      copy[key] = data[key];
    }
    if (!Object.keys(copy).length) return "";
    var text;
    try {
      text = JSON.stringify(copy, null, 2);
    } catch (error) {
      text = "[unserialisable payload]";
    }
    return truncate(stripControl(text), MAX_DETAIL_CHARS, "\n…（详情已截断）");
  }

  function entryFrom(event, eventId) {
    var data = event && event.data && typeof event.data === "object" ? event.data : {};
    var level = resolveLevel(event);
    var source = stripControl((data.log && data.log.source) || data.source || event.source || event.type || "event");
    var code = stripControl(data.code || data.reason_code || "");
    var message = stripControl(event.message || "");
    if (event.type === 'ros_diagnostic') {
      var scope = data.scope || {}, evidence = data.evidence || {}, summary = [];
      if (scope.plugin_id || scope.port_id) summary.push([scope.plugin_id, scope.port_id].filter(Boolean).join('/'));
      if (scope.topic) summary.push(scope.topic);
      if (evidence.graph_peer_count !== undefined) summary.push('graph=' + evidence.graph_peer_count);
      if (evidence.matched_peer_count !== undefined) summary.push('matched=' + (evidence.matched_peer_count === null ? 'unknown' : evidence.matched_peer_count));
      if (evidence.receive_age_sec !== undefined) summary.push('回调年龄=' + (evidence.receive_age_sec === null ? '无回调' : evidence.receive_age_sec + 's'));
      if (data.remediation && data.remediation.length) summary.push('建议：' + (Array.isArray(data.remediation) ? data.remediation[0] : data.remediation));
      if (summary.length) message += ' · ' + truncate(stripControl(summary.join(' · ')), 768);
    }
    var detail = detailFrom(event);
    var timestamp = event && event.timestamp ? Number(event.timestamp) * 1000 : Date.now();
    if (!isFinite(timestamp)) timestamp = Date.now();
    return {
      id: eventId || null,
      level: level,
      source: source,
      code: code,
      type: stripControl(event && event.type || ""),
      message: message,
      detail: detail,
      detailTruncated: detail.indexOf("（详情已截断）") >= 0,
      time: timestamp,
      bytes: byteLength(message) + byteLength(detail) + byteLength(source) + byteLength(code),
    };
  }

  function rememberId(eventId) {
    if (!eventId) return false;
    if (seenIds.has(eventId)) return true;
    seenIds.add(eventId);
    seenOrder.push(eventId);
    while (seenOrder.length > MAX_SEEN_IDS) {
      seenIds.delete(seenOrder.shift());
    }
    return false;
  }

  /* -- filtering --------------------------------------------------------- */
  function passes(entry) {
    if (!filters.levels.has(entry.level)) return false;
    if (filters.sources.size && !filters.sources.has(entry.source)) return false;
    if (filters.text) {
      var needle = filters.text.toLowerCase();
      var haystack = (entry.message + " " + entry.code + " " + entry.source + " " + entry.type).toLowerCase();
      if (haystack.indexOf(needle) < 0) return false;
    }
    return true;
  }

  function visibleEntries() {
    return entries.filter(passes);
  }

  /* -- DOM --------------------------------------------------------------- */
  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function on(target, type, handler) {
    if (!target) return;
    target.addEventListener(type, handler);
    listeners.push([target, type, handler]);
  }

  function rowFor(entry) {
    var row = el("div", "log-line log-level-" + entry.level.toLowerCase());
    row.dataset.level = entry.level;
    if (entry.id) row.dataset.eventId = entry.id;
    if (entry.code) row.dataset.code = entry.code;
    row.appendChild(el("span", "log-time", formatClock(entry.time)));
    row.appendChild(el("span", "log-badge", "[" + entry.level + "]"));
    row.appendChild(el("span", "log-source", entry.source || "-"));
    if (entry.code) row.appendChild(el("span", "log-code", entry.code));
    row.appendChild(el("span", "log-message", entry.message || ""));
    if (entry.detail) {
      var toggle = el("button", "log-detail-toggle", "详情");
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", "false");
      row.appendChild(toggle);
      var detail = el("div", "log-detail", entry.detail);
      detail.hidden = true;
      row.dataset.hasDetail = "1";
      row._detail = detail;
    }
    return row;
  }

  function detailForRow(row) {
    return row._detail || null;
  }

  function appendRow(entry) {
    if (!dom || !dom.consoleEl) return;
    var placeholder = dom.consoleEl.querySelector(".log-empty");
    if (placeholder) dom.consoleEl.removeChild(placeholder);
    var row = rowFor(entry);
    pendingRows.push({ row: row, entry: entry });
    if (!pendingFlush) {
      pendingFlush = true;
      var schedule = global.requestAnimationFrame || function (fn) { return setTimeout(fn, 16); };
      schedule(flushRows);
    }
  }

  /* The visible transcript is bounded by the same ceiling as the cache. Rows are
     removed oldest-first together with their expanded detail block, so a burst of
     events can never grow the DOM without limit. */
  function trimConsole() {
    if (!dom || !dom.consoleEl) return;
    var lines = dom.consoleEl.getElementsByClassName("log-line").length;
    var excess = lines - MAX_ENTRIES;
    while (excess > 0) {
      var first = dom.consoleEl.firstChild;
      if (!first) break;
      dom.consoleEl.removeChild(first);
      var next = dom.consoleEl.firstChild;
      if (next && next.classList && next.classList.contains("log-detail")) {
        dom.consoleEl.removeChild(next);
      }
      excess -= 1;
    }
  }

  function flushRows() {
    pendingFlush = false;
    if (!dom || !dom.consoleEl) {
      pendingRows = [];
      return;
    }
    var batch = pendingRows;
    pendingRows = [];
    if (!batch.length) return;
    var placeholder = dom.consoleEl.querySelector(".log-empty");
    if (placeholder) dom.consoleEl.removeChild(placeholder);
    var fragment = document.createDocumentFragment();
    for (var index = 0; index < batch.length; index += 1) {
      var item = batch[index];
      fragment.appendChild(item.row);
      if (item.row._detail) fragment.appendChild(item.row._detail);
    }
    dom.consoleEl.appendChild(fragment);
    trimConsole();
    if (autoscroll) scrollToBottom();
  }

  function rebuild() {
    if (!dom || !dom.consoleEl) return;
    pendingRows = [];
    dom.consoleEl.textContent = "";
    var visible = visibleEntries();
    if (!visible.length) {
      var empty = el("div", "log-empty", entries.length ? "当前筛选下没有日志" : "等待运行时事件");
      dom.consoleEl.appendChild(empty);
      return;
    }
    var fragment = document.createDocumentFragment();
    for (var index = 0; index < visible.length; index += 1) {
      var row = rowFor(visible[index]);
      fragment.appendChild(row);
      if (row._detail) fragment.appendChild(row._detail);
    }
    dom.consoleEl.appendChild(fragment);
    if (autoscroll) scrollToBottom();
  }

  function scrollToBottom() {
    if (!dom || !dom.consoleEl) return;
    dom.consoleEl.scrollTop = dom.consoleEl.scrollHeight;
  }

  function updateUnread() {
    if (!dom || !dom.unreadEl) return;
    if (!autoscroll && unread > 0) {
      dom.unreadEl.textContent = "未读 " + unread;
      dom.unreadEl.hidden = false;
    } else {
      dom.unreadEl.textContent = "";
      dom.unreadEl.hidden = true;
    }
  }

  function updateAutoscrollLabel() {
    if (!dom || !dom.autoscrollBtn) return;
    dom.autoscrollBtn.textContent = "自动滚动: " + (autoscroll ? "开" : "关");
    dom.autoscrollBtn.setAttribute("aria-pressed", autoscroll ? "true" : "false");
  }

  function showGap(kind, text) {
    if (!dom || !dom.gapEl) return;
    dom.gapEl.hidden = false;
    dom.gapEl.dataset.kind = kind;
    dom.gapEl.className = "log-gap log-gap-" + kind;
    dom.gapEl.textContent = text;
  }

  function renderLevelFilters() {
    if (!dom || !dom.levelFilters) return;
    dom.levelFilters.textContent = "";
    LEVELS.forEach(function (level) {
      var label = el("label", "log-filter-chip log-filter-" + level.toLowerCase());
      var input = document.createElement("input");
      input.type = "checkbox";
      input.checked = filters.levels.has(level);
      input.value = level;
      input.dataset.level = level;
      label.appendChild(input);
      label.appendChild(el("span", "log-filter-label", level));
      dom.levelFilters.appendChild(label);
    });
  }

  function renderSourceOptions() {
    if (!dom || !dom.sourceFilter) return;
    var current = dom.sourceFilter.value || "";
    dom.sourceFilter.textContent = "";
    var all = el("option", null, "全部来源");
    all.value = "";
    dom.sourceFilter.appendChild(all);
    Array.from(sources).sort().forEach(function (source) {
      var option = el("option", null, source);
      option.value = source;
      dom.sourceFilter.appendChild(option);
    });
    dom.sourceFilter.value = sources.has(current) ? current : "";
  }

  function exportText() {
    var visible = visibleEntries();
    var lines = visible.map(function (entry) {
      var head = formatStamp(entry.time) + " [" + entry.level + "] " +
        (entry.source || "-") + (entry.code ? " " + entry.code : "") + " " + entry.message;
      return entry.detail ? head + "\n" + entry.detail.split("\n").map(function (line) {
        return "    " + line;
      }).join("\n") : head;
    });
    return lines.join("\n");
  }

  function download() {
    if (!global.document || !global.URL || !global.Blob) return;
    var text = exportText();
    var blob = new global.Blob([text], { type: "text/plain;charset=utf-8" });
    var url = global.URL.createObjectURL(blob);
    var anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "ex-logs-" + new Date().toISOString().replace(/[:.]/g, "-") + ".txt";
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    setTimeout(function () { global.URL.revokeObjectURL(url); }, 0);
  }

  /* -- cache ------------------------------------------------------------- */
  function trimCache() {
    while (entries.length > MAX_ENTRIES || (totalBytes > MAX_TEXT_BYTES && entries.length > 1)) {
      var removed = entries.shift();
      totalBytes -= removed.bytes || 0;
    }
  }

  /* -- public API -------------------------------------------------------- */
  var EXLogs = {
    mount: function (options) {
      var opts = options || {};
      var root = opts.root || document.querySelector("#page-logs");
      if (!root) return null;
      if (mounted) EXLogs.destroy();
      dom = {
        root: root,
        consoleEl: root.querySelector("#logConsole"),
        pill: root.querySelector("#logConnectionPill"),
        autoscrollBtn: root.querySelector("#logAutoscrollButton"),
        clearBtn: root.querySelector("#logClearButton"),
        statusEl: root.querySelector("#eventStatus"),
        levelFilters: root.querySelector("#logLevelFilters"),
        sourceFilter: root.querySelector("#logSourceFilter"),
        searchInput: root.querySelector("#logSearchInput"),
        exportBtn: root.querySelector("#logExportButton"),
        unreadEl: root.querySelector("#logUnreadCount"),
        gapEl: root.querySelector("#logGapNotice"),
      };
      mounted = true;
      renderLevelFilters();
      renderSourceOptions();
      updateAutoscrollLabel();
      updateUnread();
      on(dom.autoscrollBtn, "click", function () { EXLogs.toggleAutoscroll(); });
      on(dom.clearBtn, "click", function () { EXLogs.clear(); });
      on(dom.exportBtn, "click", download);
      on(dom.levelFilters, "change", function (event) {
        var input = event.target.closest ? event.target.closest("input[data-level]") : event.target;
        if (!input || !input.dataset) return;
        var level = input.dataset.level;
        if (!level) return;
        if (input.checked) filters.levels.add(level);
        else filters.levels.delete(level);
        rebuild();
      });
      on(dom.sourceFilter, "change", function () {
        filters.sources = new Set(dom.sourceFilter.value ? [dom.sourceFilter.value] : []);
        rebuild();
      });
      on(dom.searchInput, "input", function () {
        filters.text = String(dom.searchInput.value || "").trim();
        rebuild();
      });
      on(dom.consoleEl, "click", function (event) {
        var button = event.target.closest ? event.target.closest(".log-detail-toggle") : null;
        if (!button) return;
        var row = button.closest(".log-line");
        var detail = row ? detailForRow(row) : null;
        if (!detail) return;
        detail.hidden = !detail.hidden;
        button.setAttribute("aria-expanded", detail.hidden ? "false" : "true");
      });
      rebuild();
      return EXLogs;
    },

    push: function (event, meta) {
      if (!event || typeof event !== "object") return;
      var eventId = meta && meta.eventId ? String(meta.eventId) : null;
      if (rememberId(eventId)) return; /* replay of something already shown */
      var entry = entryFrom(event, eventId);
      var insertAt = -1;
      if (eventId) {
        var split = eventId.lastIndexOf(':'), prefix = eventId.slice(0, split);
        var sequence = eventId.slice(split + 1);
        if (/^[1-9][0-9]*$/.test(sequence)) {
          insertAt = entries.findIndex(function (old) {
            if (!old.id) return false;
            var oldSplit = old.id.lastIndexOf(':');
            return old.id.slice(0, oldSplit) === prefix && /^[1-9][0-9]*$/.test(old.id.slice(oldSplit + 1)) && BigInt(old.id.slice(oldSplit + 1)) > BigInt(sequence);
          });
        }
      }
      if (insertAt < 0) entries.push(entry); else entries.splice(insertAt, 0, entry);
      totalBytes += entry.bytes || 0;
      sources.add(entry.source);
      trimCache();
      if (dom && dom.sourceFilter) renderSourceOptions();
      if (dom && passes(entry)) {
        if (insertAt < 0) appendRow(entry); else rebuild();
        if (!autoscroll) {
          unread += 1;
          updateUnread();
        }
      }
      /* Keep the row count bounded even when the cache evicts entries that are
         already on screen: the DOM never exceeds the cache ceiling. */
      if (dom && dom.consoleEl) trimConsole();
    },

    seed: function (events) {
      if (!Array.isArray(events)) return;
      for (var index = 0; index < events.length; index += 1) {
        var event = events[index];
        var eventId = event && ((event.data && event.data.log && event.data.log.event_id) || event.event_id);
        EXLogs.push(event, eventId ? { eventId: String(eventId) } : {});
      }
    },

    control: function (frame) {
      if (!frame || typeof frame !== "object") return;
      var kind = frame.kind === "reset" ? "reset" : "gap";
      var text;
      if (kind === "reset") {
        text = "服务器已重启或事件流已重建，此前的日志不再连续";
        if (frame.reason) text += "（" + stripControl(frame.reason) + "）";
      } else {
        var dropped = frame.dropped_count;
        text = "部分历史已不在缓存";
        if (dropped !== null && dropped !== undefined && isFinite(Number(dropped))) {
          text += "，约 " + Number(dropped) + " 条被丢弃";
        }
        if (frame.oldest_sequence !== undefined && frame.oldest_sequence !== null) {
          text += "，现存从序号 " + stripControl(frame.oldest_sequence) + " 开始";
        }
      }
      showGap(kind, text);
    },

    setConnection: function (ok, label) {
      if (!dom) return;
      if (dom.pill) {
        dom.pill.classList.toggle("offline", !ok);
        var labelEl = dom.pill.querySelector("[data-label]");
        if (labelEl) labelEl.textContent = label || (ok ? "日志流已连接" : "日志流未连接");
      }
      if (dom.statusEl) dom.statusEl.textContent = label || (ok ? "SSE 已连接" : "SSE 未连接");
    },

    toggleAutoscroll: function () {
      autoscroll = !autoscroll;
      updateAutoscrollLabel();
      if (autoscroll) {
        unread = 0;
        updateUnread();
        scrollToBottom();
      }
    },

    clear: function () {
      /* Interface only: the backend ring buffer and the SSE cursor are owned by
         app.js and must not be touched here. */
      entries = [];
      totalBytes = 0;
      unread = 0;
      if (dom && dom.consoleEl) dom.consoleEl.textContent = "";
      if (dom && dom.gapEl) {
        dom.gapEl.hidden = true;
        dom.gapEl.textContent = "";
      }
      updateUnread();
      rebuild();
    },

    setFilter: function (patch) {
      if (!patch || typeof patch !== "object") return;
      if (patch.levels) {
        var next = new Set();
        patch.levels.forEach(function (level) {
          var normalized = normalizeLevelName(level);
          if (normalized) next.add(normalized);
        });
        if (next.size) filters.levels = next;
      }
      if (patch.sources !== undefined) {
        filters.sources = new Set(Array.isArray(patch.sources) ? patch.sources : []);
        if (dom && dom.sourceFilter) {
          dom.sourceFilter.value = filters.sources.size ? Array.from(filters.sources)[0] : "";
        }
      }
      if (patch.text !== undefined) {
        filters.text = String(patch.text || "").trim();
        if (dom && dom.searchInput) dom.searchInput.value = filters.text;
      }
      if (dom && dom.levelFilters) {
        dom.levelFilters.querySelectorAll("input[data-level]").forEach(function (input) {
          input.checked = filters.levels.has(input.dataset.level);
        });
      }
      rebuild();
    },

    destroy: function () {
      listeners.forEach(function (item) {
        item[0].removeEventListener(item[1], item[2]);
      });
      listeners = [];
      if (dom && dom.consoleEl) dom.consoleEl.textContent = "";
      dom = null;
      mounted = false;
      seenIds.clear(); seenOrder = []; sources.clear();
      filters.sources.clear(); filters.text = '';
      entries = [];
      totalBytes = 0;
      unread = 0;
      pendingRows = [];
      pendingFlush = false;
    },
  };

  /* Export text is exposed for the acceptance script only through the button;
     the module itself stays free of test-only hooks. */
  global.EXLogs = EXLogs;
})(typeof window !== "undefined" ? window : this);
