import {
  clear,
  el,
  formatRangeShort,
  loadSiteChrome,
} from "./common.js?v=20";
import {
  enqueueOperation,
  loadSnapshot,
  pendingOperations,
  removeOperations,
  saveSnapshot,
} from "./door-offline.js?v=1";

const token = decodeURIComponent(
  location.pathname.replace(/^\/door\//, "").replace(/\/+$/, ""),
);
const host = document.getElementById("door");
let eventData = null;
let stats = null;
let guests = [];
let snapshotLoaded = false;
let offline = false;
let offlineReady = false;
let pendingCount = 0;
let syncBusy = false;
let lastSyncAt = null;
let refreshTimer = null;
let cameraStream = null;
let cameraTimer = null;
let scanDetector = null;
let scanBusy = false;
let scanCanvas = null;
let jsQrPromise = null;
let searchTimer = null;

loadSiteChrome();

function toast(message, kind = "ok") {
  const node = el("div", {
    class: "toast" + (kind === "error" ? " toast--error" : ""),
    text: message,
  });
  document.body.append(node);
  setTimeout(() => node.remove(), 4000);
}

async function call(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });
  } catch (cause) {
    const error = new Error("Нет связи с сервером.");
    error.network = true;
    error.cause = cause;
    throw error;
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* not json */
  }
  if (!response.ok) {
    const error = new Error((data && data.error && data.error.message)
      || "Не удалось выполнить запрос.");
    error.status = response.status;
    // Прокси и шлюзы отвечают 502/503/504, когда связи с сервером нет.
    if (response.status >= 500) error.network = true;
    throw error;
  }
  return data;
}

function errorState(title, text) {
  return el("div", { class: "empty door__empty" }, [
    el("h1", { text: title }),
    el("p", { text }),
    el("a", { class: "btn btn--primary", href: "/", text: "На главную" }),
  ]);
}

function updateStats(next) {
  stats = next || stats;
  if (!stats) return;
  for (const [id, value] of [
    ["door-stat-total", stats.guestCount],
    ["door-stat-in", stats.checkedInCount],
  ]) {
    const node = document.getElementById(id);
    if (node) node.textContent = String(value || 0);
  }
}

function applySnapshot(snapshot, isOffline) {
  eventData = { event: snapshot.event, stats: snapshot.stats };
  stats = { ...snapshot.stats };
  guests = (snapshot.guests || []).map((guest) => ({ ...guest }));
  snapshotLoaded = true;
  offline = Boolean(isOffline);
  lastSyncAt = snapshot.generatedAt || snapshot.savedAt || null;
}

async function persistLocal() {
  if (!eventData) return;
  try {
    await saveSnapshot(token, {
      event: eventData.event,
      stats,
      guests,
      generatedAt: lastSyncAt,
      serverNow: new Date().toISOString(),
    });
  } catch {
    /* локальное сохранение не должно ломать экран */
  }
}

function normalizeName(value) {
  return String(value || "").toLocaleLowerCase("ru");
}

function findBlacklistMatch(guest) {
  if (guest.blacklisted) return guest;
  const name = normalizeName(guest.name);
  for (const item of guests) {
    if (!item.blacklisted) continue;
    if (guest.phoneHash && item.phoneHash
        && guest.phoneHash === item.phoneHash) {
      return item;
    }
    if (name.length >= 3 && normalizeName(item.name) === name) {
      return item;
    }
  }
  return null;
}

function decorateGuest(guest) {
  const match = findBlacklistMatch(guest);
  return {
    ...guest,
    blacklistMatch: match && match.id !== guest.id ? match : null,
  };
}

function searchLocal(query) {
  const nameQuery = normalizeName(query);
  const digits = String(query || "").replace(/\D/g, "");
  const rows = guests.filter((guest) => {
    const nameHit = normalizeName(guest.name).includes(nameQuery);
    const phoneHit = digits
      && String(guest.phoneMasked || "").replace(/\D/g, "").includes(digits);
    return nameHit || phoneHit;
  });
  rows.sort((a, b) => {
    const byStatus = (a.status === "checked_in" ? 1 : 0)
      - (b.status === "checked_in" ? 1 : 0);
    return byStatus
      || normalizeName(a.name).localeCompare(normalizeName(b.name), "ru");
  });
  return rows.slice(0, 30).map(decorateGuest);
}

function guestByCode(code) {
  const match = String(code || "").match(/\/g\/([A-Za-z0-9_-]{16,80})/);
  const guestToken = match ? match[1] : String(code || "").trim();
  return guests.find((guest) => guest.shareToken === guestToken) || null;
}

function newOperationId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `op-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function formatClock(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString("ru-RU", {
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function renderOfflineStatus() {
  const node = document.getElementById("door-offline-status");
  if (!node) return;
  node.className = "door-offline-status";
  if (offline) {
    node.hidden = false;
    node.classList.add("is-offline");
    node.textContent = pendingCount > 0
      ? `Оффлайн-режим. Не отправлено: ${pendingCount}. `
        + "Не закрывайте вкладку до конца смены."
      : "Оффлайн-режим. Список сохранён на устройстве — можно отмечать гостей.";
  } else if (pendingCount > 0) {
    node.hidden = false;
    node.classList.add("is-pending");
    node.textContent = `Ожидают отправки: ${pendingCount}. `
      + "Отправим автоматически, когда появится связь.";
  } else if (lastSyncAt) {
    node.hidden = false;
    node.classList.add("is-online");
    node.textContent = (offlineReady ? "Оффлайн-копия готова" : "Список загружен")
      + (formatClock(lastSyncAt) ? ` · обновлён ${formatClock(lastSyncAt)}` : "");
  } else {
    node.hidden = true;
  }
}

async function queueLocalAction(guest, action) {
  const wasChecked = guest.status === "checked_in";
  if (action === "checkin" && wasChecked) return guest;
  if (action === "undo" && !wasChecked) return guest;
  const at = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const current = guests.find((item) => item.id === guest.id) || guest;
  current.status = action === "checkin" ? "checked_in" : "invited";
  current.checkedInAt = action === "checkin" ? at : null;
  if (action === "checkin") {
    stats.checkedInCount = (stats.checkedInCount || 0) + 1;
  } else {
    stats.checkedInCount = Math.max(0, (stats.checkedInCount || 0) - 1);
  }
  eventData.stats = stats;
  await persistLocal();
  await enqueueOperation(token, {
    id: newOperationId(),
    guestId: current.id,
    action,
    at,
  });
  pendingCount += 1;
  updateStats(stats);
  renderOfflineStatus();
  if (!offline) await flushOutbox();
  return guests.find((item) => item.id === guest.id) || current;
}

async function flushOutbox() {
  if (syncBusy || offline) {
    renderOfflineStatus();
    return;
  }
  let pending = [];
  try {
    pending = await pendingOperations(token);
  } catch {
    return;
  }
  pendingCount = pending.length;
  if (!pending.length) {
    renderOfflineStatus();
    return;
  }
  syncBusy = true;
  try {
    const data = await call(
      `/api/door/${encodeURIComponent(token)}/sync`,
      {
        method: "POST",
        body: {
          operations: pending.map(({ id, guestId, action, at }) => ({
            id, guestId, action, at,
          })),
        },
      },
    );
    await removeOperations(pending.map((item) => item.id));
    for (const item of data.updated || []) {
      const index = guests.findIndex((guest) => guest.id === item.id);
      if (index >= 0) {
        const old = guests[index];
        guests[index] = {
          ...old,
          ...item,
          shareToken: old.shareToken,
          phoneHash: old.phoneHash,
        };
      }
    }
    if (data.stats) {
      stats = { ...data.stats };
      eventData.stats = stats;
      updateStats(stats);
    }
    lastSyncAt = data.serverNow || new Date().toISOString();
    offline = false;
    try {
      pendingCount = (await pendingOperations(token)).length;
    } catch {
      pendingCount = 0;
    }
    await persistLocal();
    const conflicts = (data.results || []).filter(
      (item) => item.status === "conflict");
    if (conflicts.length) {
      toast(conflicts[0].message || "Конфликт синхронизации", "error");
    }
    const input = document.getElementById("door-search");
    if (input && input.value.trim()) await searchGuests();
  } catch (error) {
    if (error.network) {
      offline = true;
    } else {
      toast(error.message, "error");
    }
  } finally {
    syncBusy = false;
    renderOfflineStatus();
  }
}

async function refreshSnapshot({ silent = false } = {}) {
  try {
    const snapshot = await call(
      `/api/door/${encodeURIComponent(token)}/snapshot`);
    await saveSnapshot(token, snapshot);
    applySnapshot(snapshot, false);
    try {
      pendingCount = (await pendingOperations(token)).length;
    } catch {
      pendingCount = 0;
    }
    if (silent) {
      updateStats(stats);
      renderOfflineStatus();
      const input = document.getElementById("door-search");
      if (input && input.value.trim()) await searchGuests();
    } else {
      render();
      renderOfflineStatus();
    }
    return true;
  } catch (error) {
    if (error.network) {
      offline = true;
      renderOfflineStatus();
    }
    return false;
  }
}

async function toggleGuest(guest, { featured = false } = {}) {
  const decorated = decorateGuest(guest);
  if (decorated.blacklisted || decorated.blacklistMatch) {
    showGuest(decorated);
    toast("Гость в чёрном списке — вход запрещён", "error");
    return;
  }
  const action = guest.status === "checked_in" ? "undo" : "checkin";
  const updated = await queueLocalAction(guest, action);
  const view = decorateGuest(updated);
  if (featured) showGuest(view);
  else await searchGuests();
  const queued = offline || pendingCount > 0;
  if (action === "checkin") {
    toast(queued ? "Отмечено и сохранено на устройстве" : "Гость отмечен");
  } else {
    toast(queued ? "Отметка снята и сохранена" : "Отметка снята");
  }
}

async function registerServiceWorker() {
  if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
  try {
    await navigator.serviceWorker.register("/door-sw.js", { scope: "/door/" });
    await navigator.serviceWorker.ready;
    const cache = await caches.open("lineup-door-v9");
    await cache.add(location.href).catch(() => {});
    offlineReady = Boolean(await cache.match(location.href));
    renderOfflineStatus();
  } catch {
    /* оффлайн-кэш не критичен для текущей сессии */
  }
}

function guestCard(guest, { featured = false } = {}) {
  const checked = guest.status === "checked_in";
  const match = guest.blacklistMatch;
  const blacklisted = Boolean(guest.blacklisted || match);
  const meta = [
    guest.phoneMasked,
    guest.tableName ? `Стол: ${guest.tableName}` : "",
  ].filter(Boolean).join(" · ");
  const warning = blacklisted
    ? el("div", {
        class: "door-guest__blacklist",
        text: match && match.id !== guest.id
          ? `Совпадение с чёрным списком: ${match.name}`
            + (match.phoneMasked ? ` · ${match.phoneMasked}` : "")
          : "Гость в чёрном списке — не пускать.",
      })
    : null;
  const action = blacklisted
    ? el("div", { class: "door-guest__blocked", text: "Вход запрещён" })
    : el("button", {
        class: "btn " + (checked ? "btn--ghost" : "btn--primary") + " door-guest__action",
        type: "button",
        text: checked ? "Вернуть" : "Пришёл",
        onClick: async () => {
          setBusy(action, true);
          try {
            await toggleGuest(guest, { featured });
          } catch (error) {
            toast(error.message, "error");
          } finally {
            setBusy(action, false);
          }
        },
      });

  return el("article", {
    class: "door-guest" + (checked ? " is-checked" : "")
      + (blacklisted ? " is-blacklisted" : "")
      + (featured ? " door-guest--featured" : ""),
  }, [
    el("div", { class: "door-guest__main" }, [
      el("div", { class: "door-guest__name" }, [
        el("span", { text: guest.name }),
        guest.listName
          ? el("span", {
              class: "door-guest__list" + (guest.blacklisted ? " is-blacklist" : ""),
              text: guest.listName,
            })
          : null,
        el("span", {
          class: "guest-status"
            + (blacklisted ? " guest-status--blacklist" : "")
            + (checked && !blacklisted ? " guest-status--in" : ""),
          text: blacklisted ? "не пускать" : checked ? "пришёл" : "гость",
        }),
      ]),
      warning,
      meta ? el("div", { class: "door-guest__meta", text: meta }) : null,
      guest.notes
        ? el("div", { class: "door-guest__notes", text: guest.notes })
        : null,
      checked && guest.checkedInAt && !blacklisted
        ? el("div", {
            class: "door-guest__checked",
            text: `Вход: ${new Date(guest.checkedInAt).toLocaleTimeString("ru-RU", {
              hour: "2-digit", minute: "2-digit",
            })}`,
          })
        : null,
    ]),
    action,
  ]);
}

function showGuest(guest) {
  const hostResult = document.getElementById("door-result");
  clear(hostResult);
  hostResult.append(guestCard(guest, { featured: true }));
}

async function searchGuests() {
  const input = document.getElementById("door-search");
  if (!input) return;
  const query = input.value.trim();
  const resultHost = document.getElementById("door-results");
  if (!query) {
    clear(resultHost);
    return;
  }
  const results = searchLocal(query);
  clear(resultHost);
  if (!results.length) {
    resultHost.append(el("div", {
      class: "door__empty",
      text: snapshotLoaded
        ? "Никого не нашли в сохранённом списке"
        : "Список гостей ещё не загружен",
    }));
    return;
  }
  resultHost.append(...results.map((guest) => guestCard(guest)));
}

async function submitScan(code) {
  const raw = String(code || "").trim();
  if (!raw) return;
  const scanInput = document.getElementById("door-scan-input");
  const guest = guestByCode(raw);
  if (!guest) {
    if (!offline) {
      try {
        const data = await call(
          `/api/door/${encodeURIComponent(token)}/scan`,
          { method: "POST", body: { code: raw } },
        );
        data.guest.blacklistMatch = data.blacklistMatch
          && data.blacklistMatch.id !== data.guest.id
          ? data.blacklistMatch
          : null;
        const index = guests.findIndex((item) => item.id === data.guest.id);
        if (index >= 0) {
          const old = guests[index];
          guests[index] = {
            ...old,
            ...data.guest,
            shareToken: old.shareToken,
            phoneHash: old.phoneHash,
          };
        } else {
          guests.push(data.guest);
        }
        if (data.stats) {
          stats = { ...data.stats };
          eventData.stats = stats;
          updateStats(stats);
        }
        lastSyncAt = new Date().toISOString();
        await persistLocal();
        renderOfflineStatus();
        showGuest(data.guest);
        if (scanInput) scanInput.value = "";
        if (data.blacklisted) {
          toast("Гость в чёрном списке — вход запрещён", "error");
        } else if (data.already) {
          toast("Этот гость уже отмечен");
        } else {
          toast("Гость отмечен");
        }
        return;
      } catch (error) {
        toast(error.message, "error");
        return;
      }
    }
    toast("Гостя нет в сохранённом списке", "error");
    return;
  }
  const view = decorateGuest(guest);
  if (view.blacklisted || view.blacklistMatch) {
    showGuest(view);
    if (scanInput) scanInput.value = "";
    toast("Гость в чёрном списке — вход запрещён", "error");
    return;
  }
  if (guest.status === "checked_in") {
    showGuest(view);
    if (scanInput) scanInput.value = "";
    toast("Этот гость уже отмечен");
    return;
  }
  await toggleGuest(guest, { featured: true });
  if (scanInput) scanInput.value = "";
}

async function stopCamera() {
  if (cameraTimer) {
    clearInterval(cameraTimer);
    cameraTimer = null;
  }
  if (cameraStream) {
    for (const track of cameraStream.getTracks()) track.stop();
    cameraStream = null;
  }
  const video = document.getElementById("door-camera");
  if (video) video.srcObject = null;
  const wrap = document.getElementById("door-camera-wrap");
  if (wrap) wrap.hidden = true;
  hideCameraHelp();
}

function decodeFrameWithJsQr(video) {
  if (typeof window.jsQR !== "function") return null;
  const sourceWidth = video.videoWidth;
  const sourceHeight = video.videoHeight;
  if (!sourceWidth || !sourceHeight) return null;
  const maxSide = 800;
  const scale = Math.min(1, maxSide / Math.max(sourceWidth, sourceHeight));
  const width = Math.max(1, Math.round(sourceWidth * scale));
  const height = Math.max(1, Math.round(sourceHeight * scale));
  if (!scanCanvas) scanCanvas = document.createElement("canvas");
  scanCanvas.width = width;
  scanCanvas.height = height;
  const context = scanCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  context.drawImage(video, 0, 0, width, height);
  const image = context.getImageData(0, 0, width, height);
  return window.jsQR(image.data, width, height, {
    inversionAttempts: "attemptBoth",
  });
}

async function scanFrame() {
  if (scanBusy) return;
  const video = document.getElementById("door-camera");
  if (!video || video.readyState < 2 || !video.videoWidth) return;
  scanBusy = true;
  try {
    let code = "";
    if (scanDetector) {
      const codes = await scanDetector.detect(video);
      if (codes.length) code = codes[0].rawValue;
    } else {
      const result = decodeFrameWithJsQr(video);
      if (result) code = result.data;
    }
    if (code) {
      await stopCamera();
      await submitScan(code);
    }
  } catch {
    /* Если нативный декодер сломался, переключаемся на jsQR. */
    if (scanDetector) {
      scanDetector = null;
      loadJsQr();
    }
  } finally {
    scanBusy = false;
  }
}

function loadJsQr() {
  if (typeof window.jsQR === "function") return Promise.resolve(true);
  if (!jsQrPromise) {
    jsQrPromise = new Promise((resolve) => {
      const script = document.createElement("script");
      script.src = "/assets/jsQR.js?v=2";
      script.onload = () => resolve(typeof window.jsQR === "function");
      script.onerror = () => resolve(false);
      document.head.append(script);
    });
  }
  return jsQrPromise;
}

function showCameraHelp(message) {
  const box = document.getElementById("door-camera-help");
  if (!box) {
    toast(message, "error");
    return;
  }
  box.textContent = message;
  box.hidden = false;
}

function hideCameraHelp() {
  const box = document.getElementById("door-camera-help");
  if (!box) return;
  box.hidden = true;
  box.textContent = "";
}

function cameraErrorMessage(error) {
  const name = (error && error.name) || "";
  const ua = navigator.userAgent || "";
  const ios = /iPhone|iPad|iPod/i.test(ua);
  const android = /Android/i.test(ua);
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "Камера не найдена. Подключите веб-камеру или используйте поиск.";
  }
  if (name === "NotReadableError" || name === "TrackStartError"
      || name === "AbortError") {
    if (ios || android) {
      return "Камера занята другим приложением. Закрой другие программы "
        + "с камерой и попробуй снова.";
    }
    return "Камера занята другим приложением или заблокирована Windows. "
      + "Закрой программы с камерой и включи доступ: Параметры Windows → "
      + "Конфиденциальность → Камера → «Разрешить приложениям доступ к камере» "
      + "и «Разрешить классическим приложениям».";
  }
  if (name === "NotAllowedError" || name === "SecurityError"
      || name === "PermissionDeniedError") {
    if (ios) {
      return "Доступ к камере запрещён. Нажми «аа» или значок камеры "
        + "в адресной строке → Настройки веб-сайта → Камера → Разрешить. "
        + "Если пункта нет: Настройки iPhone → Safari → Камера → Разрешить. "
        + "Встроенный браузер мессенджера может блокировать камеру — открой "
        + "ссылку в Safari.";
    }
    if (android) {
      return "Доступ к камере запрещён. Нажми значок замка слева от адреса → "
        + "Разрешения → Камера → Разрешить. Если ссылка открыта во встроенном "
        + "браузере мессенджера, открой её в Chrome.";
    }
    return "Chrome не получил доступ к камере. Нажми на значок замка или камеры "
      + "слева от адреса → Разрешения → Камера → Разрешить. Если значка нет: "
      + "Параметры Windows → Конфиденциальность → Камера → включи доступ "
      + "для приложений и классических приложений.";
  }
  return "Не удалось открыть камеру. Используйте поиск или ручной ввод кода.";
}

async function openCameraStream() {
  const constraints = {
    video: {
      facingMode: { ideal: "environment" },
      width: { ideal: 1280 },
      height: { ideal: 720 },
    },
    audio: false,
  };
  try {
    return await navigator.mediaDevices.getUserMedia(constraints);
  } catch (error) {
    if (error && (error.name === "OverconstrainedError"
        || error.name === "ConstraintNotSatisfiedError")) {
      return navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    }
    throw error;
  }
}

async function startCamera() {
  if (cameraStream) return;
  hideCameraHelp();
  if (!window.isSecureContext) {
    showCameraHelp("Камера доступна только по HTTPS или на localhost. "
      + "Открой ссылку для персонала по HTTPS либо используй USB-сканер.");
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showCameraHelp("Этот браузер не даёт доступ к камере. "
      + "Открой ссылку в Chrome или используй USB-сканер.");
    return;
  }
  let stream;
  try {
    // Камеру запрашиваем сразу в обработчике клика, без ожидания загрузки JS.
    stream = await openCameraStream();
  } catch (error) {
    showCameraHelp(cameraErrorMessage(error));
    return;
  }
  cameraStream = stream;
  try {
    const wrap = document.getElementById("door-camera-wrap");
    const video = document.getElementById("door-camera");
    wrap.hidden = false;
    video.srcObject = cameraStream;
    await video.play();
    scanDetector = null;
    if ("BarcodeDetector" in window) {
      try {
        scanDetector = new BarcodeDetector({ formats: ["qr_code"] });
      } catch {
        scanDetector = null;
      }
    }
    if (!scanDetector) {
      const ready = await loadJsQr();
      if (!ready) {
        await stopCamera();
        showCameraHelp("QR-декодер не загрузился. "
          + "Используй USB-сканер или поиск.");
        return;
      }
    }
    cameraTimer = setInterval(scanFrame, 300);
  } catch (error) {
    await stopCamera();
    showCameraHelp(cameraErrorMessage(error));
  }
}

function setBusy(button, busy) {
  if (button) button.disabled = busy;
}

function render() {
  document.title = `${eventData.event.title} — вход`;
  const event = eventData.event;
  const meta = [
    formatRangeShort(event.startsAt, event.endsAt, event.timezone),
  ].filter(Boolean);

  const scanForm = el("form", { class: "door-scan", id: "door-scan-form", novalidate: true }, [
    el("label", { for: "door-scan-input", text: "QR-код гостя" }),
    el("div", { class: "door-scan__row" }, [
      el("input", {
        id: "door-scan-input",
        type: "text",
        autocomplete: "off",
        spellcheck: "false",
        placeholder: "Ссылка из QR или код приглашения",
        attrs: { "aria-label": "QR-код или ссылка приглашения" },
      }),
      el("button", { class: "btn btn--primary", type: "submit", text: "Отметить вход" }),
    ]),
    el("p", {
      class: "door-scan__hint",
      text: "Это строка для USB-сканера QR-кодов: наведи сканер на QR гостя — "
        + "ссылка появится здесь. Можно вставить вручную. Если QR нет — "
        + "найди гостя по имени ниже.",
    }),
    el("button", {
      class: "btn btn--ghost btn--sm",
      type: "button",
      text: "Сканировать камерой",
      onClick: startCamera,
    }),
  ]);
  scanForm.addEventListener("submit", (event) => {
    event.preventDefault();
    submitScan(document.getElementById("door-scan-input").value);
  });

  const searchForm = el("div", { class: "door-search" }, [
    el("label", { for: "door-search", text: "Или найти гостя по имени/телефону" }),
    el("input", {
      id: "door-search",
      type: "search",
      autocomplete: "off",
      spellcheck: "false",
      placeholder: "Федот или 4567",
      attrs: { "aria-label": "Поиск гостя" },
    }),
  ]);
  searchForm.querySelector("input").addEventListener("input", () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(searchGuests, 220);
  });

  clear(host).append(
    el("section", { class: "door" }, [
      el("div", { class: "door__head" }, [
        el("span", { class: "door__eyebrow", text: "Режим персонала" }),
        el("h1", { class: "door__title", text: event.title }),
        el("p", { class: "door__meta", text: meta.join(" · ") }),
      ]),
      el("div", { class: "door-stats" }, [
        el("div", { class: "guest-stat" }, [
          el("strong", { id: "door-stat-total", text: String(stats.guestCount || 0) }),
          el("span", { text: "Гостей" }),
        ]),
        el("div", { class: "guest-stat" }, [
          el("strong", { id: "door-stat-in", text: String(stats.checkedInCount || 0) }),
          el("span", { text: "Пришло на мероприятие" }),
        ]),
      ]),
      el("div", {
        id: "door-offline-status",
        class: "door-offline-status",
        hidden: true,
      }),
      el("div", { class: "panel door__panel" }, [
        scanForm,
        el("p", {
          class: "door-camera-note",
          hidden: window.isSecureContext,
          text: "Камера работает только по HTTPS или на localhost. "
            + "На этом адресе доступны поиск и USB-сканер.",
        }),
        el("div", { id: "door-camera-wrap", class: "door-camera", hidden: true }, [
          el("p", {
            class: "door-camera__hint",
            text: "Наведите камеру на QR-код гостя",
          }),
          el("video", { id: "door-camera", attrs: { playsinline: true, muted: true } }),
          el("button", {
            class: "btn btn--ghost btn--sm",
            type: "button",
            text: "Остановить камеру",
            onClick: stopCamera,
          }),
        ]),
        el("div", {
          id: "door-camera-help",
          class: "door-camera-help",
          attrs: { role: "alert" },
          hidden: true,
        }),
        el("div", { id: "door-result", class: "door-result" }),
        searchForm,
        el("div", { id: "door-results", class: "door-results" }),
      ]),
      el("details", { class: "door-help" }, [
        el("summary", { text: "Оффлайн-режим: как это работает" }),
        el("p", {
          text: "Откройте эту страницу при интернете — список гостей "
            + "сохранится на устройстве. Если связь пропадёт, поиск, QR-коды "
            + "и отметки входа продолжат работать.",
        }),
        el("p", {
          text: "Отметки сохраняются локально и отправятся автоматически, "
            + "когда интернет вернётся. Не закрывайте вкладку до конца смены "
            + "и не очищайте данные сайта.",
        }),
        el("p", {
          text: "Если вкладка всё-таки закрылась, откройте ссылку снова "
            + "при появлении связи — сохранённые данные останутся.",
        }),
        el("p", {
          text: "Оффлайн-копия работает по HTTPS или на localhost. "
            + "На обычном HTTP страница без интернета не откроется.",
        }),
        el("p", {
          text: "Если гостя нет в сохранённой копии, вход не подтверждается. "
            + "Обновите список, когда появится связь.",
        }),
      ]),
    ]),
  );
  if (window.matchMedia("(pointer: fine)").matches) {
    document.getElementById("door-scan-input")?.focus({ preventScroll: true });
  }
  renderOfflineStatus();
}

async function load() {
  registerServiceWorker();
  let cached = null;
  try {
    cached = await loadSnapshot(token);
  } catch {
    cached = null;
  }
  try {
    pendingCount = (await pendingOperations(token)).length;
  } catch {
    pendingCount = 0;
  }
  if (cached) {
    applySnapshot(cached, true);
    render();
  }
  try {
    if (pendingCount > 0) await flushOutbox();
    const snapshot = await call(
      `/api/door/${encodeURIComponent(token)}/snapshot`);
    await saveSnapshot(token, snapshot);
    applySnapshot(snapshot, false);
    try {
      pendingCount = (await pendingOperations(token)).length;
    } catch {
      pendingCount = 0;
    }
    render();
  } catch (error) {
    if (cached && error.network) {
      offline = true;
      render();
    } else if (cached) {
      clear(host).append(errorState(
        "Ссылка недействительна",
        error.message || "Попросите организатора создать новую ссылку.",
      ));
    } else {
      const noCache = error.network;
      clear(host).append(errorState(
        noCache ? "Нет сохранённого списка" : "Ссылка недействительна",
        noCache
          ? "Откройте ссылку при интернете, чтобы сохранить список гостей."
          : (error.message || "Попросите организатора создать новую ссылку."),
      ));
    }
  }
  renderOfflineStatus();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(async () => {
    if (syncBusy) return;
    if (offline) {
      if (!navigator.onLine) return;
      const restored = await refreshSnapshot({ silent: true });
      if (restored && pendingCount > 0) await flushOutbox();
      return;
    }
    await flushOutbox();
  }, 20000);
}

load();
window.addEventListener("pagehide", stopCamera);
window.addEventListener("beforeunload", (event) => {
  if (pendingCount > 0) {
    event.preventDefault();
    event.returnValue = "";
  }
});
window.addEventListener("offline", () => {
  offline = true;
  renderOfflineStatus();
});
window.addEventListener("online", async () => {
  offline = false;
  await flushOutbox();
  await refreshSnapshot();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !offline && !syncBusy) {
    flushOutbox();
  }
});
