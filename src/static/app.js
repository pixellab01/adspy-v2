/* AdSpy v2 — all of the dashboard's JavaScript.
 *
 * Rules this file keeps to (docs/05 Phase 3):
 *   - vanilla, no framework, no build step, no CDN;
 *   - it never builds ad markup. Every fragment it shows was rendered by Jinja
 *     on the server (/ui/drawer/..., /ui/queue/rows) — v1 rotted because HTML
 *     lived in three languages at once;
 *   - if JS is broken or slow, the screens still work: sorting, filtering,
 *     re-track, hide and retry are all plain links and form POSTs. This file
 *     only adds the drawer, tabs, multi-select, copy buttons and live polling.
 *
 * Two halves:
 *   A. the shell     — theme, global search, tabs, modals, toasts (v1 parity)
 *   B. the screens    — drawer, multi-select, copy, queue polling
 *
 * Globals other screens may use: window.pixToast(msg, isError),
 * window.apiJson(url, opts).
 */
(function () {
  'use strict';

  var root = document.documentElement;

  // =========================================================================
  // A. THE SHELL
  // =========================================================================

  // ------------------------------------------------------------------ theme
  // v1's two themes, v1's two names: "black" and "light". The <head> already
  // applied the stored one before first paint; this only handles the toggle.
  var THEME_KEY = 'pixellab-theme';

  function setTheme(value) {
    root.setAttribute('data-theme', value);
    try { localStorage.setItem(THEME_KEY, value); } catch (e) {}
    document.querySelectorAll('[data-theme-choice]').forEach(function (choice) {
      choice.classList.toggle('active', choice.getAttribute('data-theme-choice') === value);
    });
  }

  document.addEventListener('click', function (event) {
    if (event.target.closest('#themeQuick')) {
      setTheme(root.getAttribute('data-theme') === 'light' ? 'black' : 'light');
      return;
    }
    var choice = event.target.closest('[data-theme-choice]');
    if (choice) {
      event.preventDefault();
      setTheme(choice.getAttribute('data-theme-choice'));
    }
  });

  // Settings' theme cards need to show the current state on load.
  setTheme(root.getAttribute('data-theme') === 'light' ? 'light' : 'black');

  // ---------------------------------------------------------- global search
  // Ctrl/Cmd+K focuses it; typing filters every [data-searchable] element on
  // the page by its text. Rows opt in — nothing else is touched.
  var globalSearch = document.getElementById('globalSearch');

  document.addEventListener('keydown', function (event) {
    if ((event.ctrlKey || event.metaKey) && (event.key === 'k' || event.key === 'K')) {
      if (!globalSearch) return;
      event.preventDefault();
      globalSearch.focus();
      globalSearch.select();
    }
  });

  if (globalSearch) {
    globalSearch.addEventListener('input', function () {
      var needle = globalSearch.value.trim().toLowerCase();
      document.querySelectorAll('[data-searchable]').forEach(function (node) {
        var hay = (node.getAttribute('data-search-text') || node.textContent || '').toLowerCase();
        node.hidden = needle !== '' && hay.indexOf(needle) === -1;
      });
    });
  }

  // ------------------------------------------------------------------- tabs
  // pill_tabs / filter_group / u_tabs rendered WITHOUT an href are client-side
  // switchers: the clicked button gets `on`, and [data-tab-panel="key"] shows.
  // Rendered WITH an href they are plain links and never reach this code.
  document.addEventListener('click', function (event) {
    var tab = event.target.closest('button[data-tab]');
    if (!tab) return;
    var key = tab.getAttribute('data-tab');
    var bar = tab.parentElement;
    if (!bar) return;

    bar.querySelectorAll('button[data-tab]').forEach(function (sibling) {
      var isTarget = sibling === tab;
      sibling.classList.toggle('on', isTarget);
      sibling.classList.toggle('active', isTarget && sibling.classList.contains('u-tab'));
      sibling.setAttribute('aria-pressed', isTarget ? 'true' : 'false');
    });

    var scope = bar.closest('[data-tab-group]') || document;
    scope.querySelectorAll('[data-tab-panel]').forEach(function (panel) {
      panel.hidden = panel.getAttribute('data-tab-panel') !== key;
    });
  });

  // v1's generic .toggle-grp (segmented button row) behaves the same way.
  document.addEventListener('click', function (event) {
    var button = event.target.closest('.toggle-grp button');
    if (!button || !button.parentElement) return;
    button.parentElement.querySelectorAll('button').forEach(function (sibling) {
      sibling.classList.remove('on', 'amber');
    });
    button.classList.add('on');
    if (button.parentElement.id === 'modeToggle') button.classList.add('amber');
  });

  // ----------------------------------------------------------------- toasts
  // One toast, the inverted chip: --text background on --bg text.
  window.pixToast = function (message, isError) {
    var host = document.getElementById('toastHost');
    if (!host) return;
    var chip = document.createElement('div');
    chip.className = 'toast-msg' + (isError ? ' error' : '');
    chip.textContent = message;
    host.appendChild(chip);
    setTimeout(function () { chip.remove(); }, 2600);
  };

  window.apiJson = function (url, options) {
    var opts = options || {};
    opts.headers = Object.assign(
      { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      opts.headers || {}
    );
    return fetch(url, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok || data.ok === false) {
          throw new Error(data.error || ('HTTP ' + response.status));
        }
        return data;
      });
    });
  };

  // ----------------------------------------------------------------- modals
  document.addEventListener('click', function (event) {
    var opener = event.target.closest('[data-modal-open]');
    if (opener) {
      event.preventDefault();
      var target = document.getElementById(opener.getAttribute('data-modal-open') + '-overlay');
      if (target) target.classList.add('open');
      return;
    }
    var closer = event.target.closest('[data-modal-close]');
    var overlay = event.target.matches('[data-modal-overlay]') ? event.target : null;
    if (closer) {
      var host = document.getElementById(closer.getAttribute('data-modal-close') + '-overlay');
      if (host) host.classList.remove('open');
    } else if (overlay) {
      overlay.classList.remove('open');
    }
  });

  // =========================================================================
  // B. THE SCREENS
  // =========================================================================

  // ----------------------------------------------------------------- drawer
  var drawer = document.getElementById('drawer');
  var backdrop = document.getElementById('drawer-backdrop');
  var drawerBody = document.getElementById('drawer-body');
  var lastFocused = null;

  function openDrawer() {
    if (!drawer) return;
    drawer.hidden = false;
    if (backdrop) backdrop.hidden = false;
    drawer.scrollTop = 0;
    drawer.focus();
  }

  function closeDrawer() {
    if (!drawer) return;
    drawer.hidden = true;
    if (backdrop) backdrop.hidden = true;
    if (drawerBody) drawerBody.innerHTML = '';
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  function loadDrawer(url, trigger) {
    if (!drawer || !drawerBody) return;
    lastFocused = trigger || null;
    drawerBody.innerHTML = '<p class="muted">Loading...</p>';
    openDrawer();
    fetch(url, { headers: { 'X-Requested-With': 'fetch' } })
      .then(function (response) { return response.text(); })
      .then(function (html) { drawerBody.innerHTML = html; drawer.scrollTop = 0; })
      .catch(function (err) {
        drawerBody.innerHTML = '<p class="muted">Could not load that. ' +
          'Is the server still running?</p>';
        if (window.console) console.error(err);
      });
  }

  // A [data-drawer] element opens the drawer instead of navigating. It is put
  // on whole CELLS as well as buttons — the product-name cell on /products and
  // /brand-groups/<id> carries it — so "click the product" slides the drawer in
  // rather than throwing away a filtered, sorted, paginated list.
  //
  // Two things this deliberately does NOT swallow:
  //   * a modified click (cmd/ctrl/shift/alt) or a middle click, so the <a>
  //     underneath still opens /products/<id> in a new tab, and "copy link
  //     address" still yields a real URL;
  //   * a click on a nested control (button, input, label, or a link pointing
  //     somewhere else) — those are the row's own actions and must still work.
  function isModifiedClick(event) {
    return event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
           (typeof event.button === 'number' && event.button !== 0);
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-drawer]');
    if (trigger) {
      if (isModifiedClick(event)) return;              // let the browser have it
      var nested = event.target.closest('button, input, select, textarea, label');
      if (nested && !nested.hasAttribute('data-drawer') && trigger.contains(nested)
          && nested !== trigger) {
        return;                                        // a row action, not the row
      }
      event.preventDefault();
      loadDrawer(trigger.getAttribute('data-drawer'), trigger);
      return;
    }
    if (event.target.closest('#drawer-close') || event.target === backdrop) {
      closeDrawer();
    }
  });

  // ------------------------------------------------- confirm before a POST
  // No template in this app carries inline JS (the design-system rule), so a
  // form that spends money — "Generate scripts" calls a paid speech-to-text
  // API — asks here instead, off a data-confirm attribute.
  document.addEventListener('submit', function (event) {
    var form = event.target.closest('form[data-confirm]');
    if (!form) return;
    // The retry button posts elsewhere via formaction and costs nothing.
    var submitter = event.submitter;
    if (submitter && submitter.hasAttribute('formaction')) return;
    if (!window.confirm(form.getAttribute('data-confirm'))) {
      event.preventDefault();
    }
  });

  // --------------------------------------------- live fragment re-rendering
  // A [data-poll-fragment] block re-fetches itself while data-poll-live="1".
  // Used by the transcription panel: a run of forty creatives outlives the
  // request that started it, so the panel refreshes until the run is done.
  // The server renders the replacement markup; this only swaps it in.
  function pollFragments() {
    var live = document.querySelector('[data-poll-fragment][data-poll-live="1"]');
    if (!live) return;
    fetch(live.getAttribute('data-poll-fragment'), {
      headers: { 'X-Requested-With': 'fetch' }
    })
      .then(function (response) { return response.text(); })
      .then(function (html) {
        var host = live.parentElement;
        if (!host) return;
        // Never clobber a checkbox the owner is mid-way through ticking.
        if (document.activeElement && live.contains(document.activeElement)) return;
        host.innerHTML = html;
      })
      .catch(function () { live.setAttribute('data-poll-live', '0'); });
  }

  setInterval(function () {
    if (document.hidden) return;
    pollFragments();
  }, 4000);

  // ------------------------------------------- live whole-screen re-rendering
  // Page Analyzer and Overview have no fragment endpoint of their own, and they
  // do not need one: while a scan is running they re-fetch THEMSELVES and swap
  // in only the regions named by data-poll-page (a comma list of selectors,
  // matched by position in both documents). Same rule as everywhere else in
  // this file — the server rendered every byte that lands in the DOM.
  //
  //   data-poll-probe   a cheap URL that says whether anything is live: the
  //                     queue's own open-jobs fragment (one small query).
  //   data-poll-page    which parts of this screen to refresh.
  //
  // It is deliberately lazy, because gunicorn runs ONE worker and the
  // extension's batch uploads matter more than a fresh number on screen:
  //   - one request chain, never overlapping, never while the tab is hidden;
  //   - nothing live -> only the probe, every 15s, and after ~5 minutes of
  //     nothing it stops completely (coming back to the tab re-arms it);
  //   - something live -> the screen every 5s, slower if the server is slow
  //     or if nothing on it has actually changed for a minute;
  //   - a job that just ended gets one last refresh, so the final numbers land;
  //   - a redirect means the session ended (server mode): stop, do not loop.
  var liveHost = document.querySelector('[data-poll-probe][data-poll-page]');
  if (liveHost && window.DOMParser) {
    (function () {
      var probeUrl = liveHost.getAttribute('data-poll-probe');
      var selectors = liveHost.getAttribute('data-poll-page').split(',')
        .map(function (part) { return part.trim(); })
        .filter(function (part) { return part !== ''; });
      var FAST = 5000;
      var SLOW = 15000;
      var IDLE_LIMIT = 20;          // 20 slow probes = ~5 minutes
      var idle = 0;
      var unchanged = 0;
      var wasLive = false;
      var stopped = false;
      var inFlight = false;
      var timer = null;
      var lastHtml = {};

      function matches(doc, selector) {
        try {
          return Array.prototype.slice.call(doc.querySelectorAll(selector));
        } catch (e) { return []; }
      }

      function parse(html) {
        return new DOMParser().parseFromString(html, 'text/html');
      }

      // Never pull a region out from under the owner: not while he is typing
      // or tabbing inside it, and not while he has rows ticked for Re-track.
      function inUse(node) {
        var focused = document.activeElement;
        if (focused && focused !== document.body && node.contains(focused)) return true;
        return !!node.querySelector('.row-check:checked');
      }

      selectors.forEach(function (selector) {
        matches(document, selector).forEach(function (node, index) {
          lastHtml[selector + '#' + index] = node.innerHTML;
        });
      });

      function getText(url) {
        return fetch(url, {
          headers: { 'X-Requested-With': 'fetch' },
          credentials: 'same-origin'
        }).then(function (response) {
          if (response.redirected || response.status === 401 || response.status === 403) {
            stopped = true;                       // logged out: a human must act
            throw new Error('session ended');
          }
          if (!response.ok) throw new Error('HTTP ' + response.status);
          return response.text();
        });
      }

      function refresh() {
        return getText(window.location.href).then(function (html) {
          var doc = parse(html);
          if (!doc.querySelector('[data-poll-probe]')) {
            stopped = true;                       // not this screen any more
            return false;
          }
          var changed = false;
          selectors.forEach(function (selector) {
            var current = matches(document, selector);
            var fresh = matches(doc, selector);
            // An empty state turning into a table changes the count; a plain
            // reload sorts that out, a positional swap would mis-pair regions.
            if (current.length !== fresh.length) return;
            fresh.forEach(function (node, index) {
              var key = selector + '#' + index;
              var markup = node.innerHTML;
              if (lastHtml[key] === markup) return;
              if (inUse(current[index])) return;  // retried on the next tick
              current[index].innerHTML = markup;
              lastHtml[key] = markup;
              changed = true;
            });
          });
          if (changed) {
            // Swapped rows arrive un-filtered and un-ticked: re-apply both.
            if (globalSearch && globalSearch.value.trim() !== '') {
              globalSearch.dispatchEvent(new Event('input'));
            }
            syncSelection();
          }
          return changed;
        });
      }

      function schedule(cost) {
        if (stopped || idle > IDLE_LIMIT) return;
        var base = (wasLive && unchanged < 12) ? FAST : SLOW;
        timer = setTimeout(tick, Math.max(base, (cost || 0) * 4));
      }

      function tick() {
        timer = null;
        if (stopped || inFlight) return;
        if (document.hidden) return;              // visibilitychange re-arms
        inFlight = true;
        var started = Date.now();
        getText(probeUrl)
          .then(function (html) {
            var live = !!parse(html).querySelector('.job.is-open');
            if (live) { idle = 0; wasLive = true; return refresh(); }
            if (wasLive) { wasLive = false; unchanged = 0; return refresh(); }
            idle += 1;
            return false;
          })
          .then(function (changed) { unchanged = changed ? 0 : unchanged + 1; })
          .catch(function () { idle += 1; })
          .then(function () {
            inFlight = false;
            schedule(Date.now() - started);
          });
      }

      document.addEventListener('visibilitychange', function () {
        if (document.hidden || stopped || inFlight || timer) return;
        idle = 0;
        tick();
      });

      timer = setTimeout(tick, 3000);
    })();
  }

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (drawer && !drawer.hidden) { closeDrawer(); return; }
    var openModal = document.querySelector('.modal-overlay.open');
    if (openModal) openModal.classList.remove('open');
  });

  // ------------------------------------------------------- pages multi-select
  var selectAll = document.getElementById('select-all');
  var counter = document.getElementById('selected-count');
  var retrackButton = document.getElementById('retrack-selected');

  function rowChecks() {
    return Array.prototype.slice.call(document.querySelectorAll('.row-check'));
  }

  function syncSelection() {
    var checks = rowChecks();
    var picked = checks.filter(function (box) { return box.checked; }).length;
    if (counter) counter.textContent = String(picked);
    if (retrackButton) retrackButton.disabled = picked === 0;
    if (selectAll) {
      selectAll.checked = picked > 0 && picked === checks.length;
      selectAll.indeterminate = picked > 0 && picked < checks.length;
    }
  }

  if (selectAll) {
    selectAll.addEventListener('change', function () {
      rowChecks().forEach(function (box) { box.checked = selectAll.checked; });
      syncSelection();
    });
  }
  document.addEventListener('change', function (event) {
    if (event.target.classList && event.target.classList.contains('row-check')) {
      syncSelection();
    }
  });
  syncSelection();

  // Shift-click a checkbox to select the range — 40 pages, one gesture.
  var lastChecked = null;
  document.addEventListener('click', function (event) {
    var box = event.target.closest('.row-check');
    if (!box) return;
    var checks = rowChecks();
    if (event.shiftKey && lastChecked && lastChecked !== box) {
      var from = checks.indexOf(lastChecked);
      var to = checks.indexOf(box);
      if (from > -1 && to > -1) {
        checks.slice(Math.min(from, to), Math.max(from, to) + 1)
          .forEach(function (item) { item.checked = box.checked; });
      }
    }
    lastChecked = box;
    syncSelection();
  });

  // --------------------------------------------------------- copy buttons
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy]');
    if (!button) return;
    var field = document.querySelector(button.getAttribute('data-copy'));
    if (!field) return;
    var original = button.textContent;
    var done = function () {
      button.textContent = 'Copied';
      setTimeout(function () { button.textContent = original; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(field.value).then(done, function () {
        field.select();
        document.execCommand('copy');
        done();
      });
    } else {
      field.select();
      document.execCommand('copy');
      done();
    }
  });

  // ------------------------------------------------------------ queue polling
  var queueRows = document.getElementById('queue-rows');
  if (queueRows && queueRows.getAttribute('data-poll')) {
    var url = queueRows.getAttribute('data-poll');
    var idleTicks = 0;
    var active = queueRows.getAttribute('data-poll-active') === '1';

    var tick = function () {
      if (document.hidden) return;
      fetch(url, { headers: { 'X-Requested-With': 'fetch' } })
        .then(function (response) { return response.text(); })
        .then(function (html) {
          // Never clobber the DOM while a menu/selection is mid-interaction:
          // the fragment is small and idempotent, so a plain swap is fine.
          queueRows.innerHTML = html;
          var stillOpen = queueRows.querySelector('.job.is-open');
          idleTicks = stillOpen ? 0 : idleTicks + 1;
        })
        .catch(function () { idleTicks += 1; });
    };

    // Poll while something is running; keep a slow heartbeat for a while
    // afterwards so a job claimed by the extension shows up on its own.
    setInterval(function () {
      if (idleTicks > 40) return;      // ~2 minutes idle, then stop
      tick();
    }, active ? 3000 : 5000);
  }
})();
