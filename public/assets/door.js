import {
  clear,
  el,
  formatRangeShort,
  loadSiteChrome,
} from "./common.js?v=20";

const token = decodeURIComponent(
  location.pathname.replace(/^\/door\//, "").replace(/\/+$/, ""),
);
const host = document.getElementById("door");
let eventData = null;
let stats = null;
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
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* not json */
  }
  if (!response.ok) {
    throw new Error((data && data.error && data.error.message)
      || "Не удалось выполнить запрос.");
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
            const data = await call(
              `/api/door/${encodeURIComponent(token)}/${checked ? "undo" : "checkin"}`,
              { method: "POST", body: { guestId: guest.id } },
            );
            updateStats(data.stats);
            if (featured) showGuest(data.guest);
            else await searchGuests();
            toast(data.guest.status === "checked_in"
              ? "Гость отмечен"
              : "Отметка снята");
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
  try {
    const data = await call(
      `/api/door/${encodeURIComponent(token)}/search?q=${encodeURIComponent(query)}`,
    );
    updateStats(data.stats);
    clear(resultHost);
    if (!data.guests.length) {
      resultHost.append(el("div", { class: "door__empty", text: "Никого не нашли" }));
      return;
    }
    resultHost.append(...data.guests.map((guest) => guestCard(guest)));
  } catch (error) {
    toast(error.message, "error");
  }
}

async function submitScan(code) {
  if (!code || !code.trim()) return;
  try {
    const data = await call(`/api/door/${encodeURIComponent(token)}/scan`, {
      method: "POST",
      body: { code: code.trim() },
    });
    updateStats(data.stats);
    data.guest.blacklistMatch = data.blacklistMatch
      && data.blacklistMatch.id !== data.guest.id
      ? data.blacklistMatch
      : null;
    showGuest(data.guest);
    const scanInput = document.getElementById("door-scan-input");
    if (scanInput) scanInput.value = "";
    if (data.blacklisted) {
      toast("Гость в чёрном списке — вход запрещён", "error");
    } else if (data.already) {
      toast("Этот гость уже отмечен");
    } else {
      toast("Гость отмечен");
    }
  } catch (error) {
    toast(error.message, "error");
  }
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
    ]),
  );
  if (window.matchMedia("(pointer: fine)").matches) {
    document.getElementById("door-scan-input")?.focus({ preventScroll: true });
  }
}

async function load() {
  try {
    const data = await call(`/api/door/${encodeURIComponent(token)}`);
    eventData = data;
    stats = data.stats;
    render();
  } catch (error) {
    clear(host).append(errorState(
      "Ссылка недействительна",
      error.message || "Попросите организатора создать новую ссылку.",
    ));
  }
}

load();
window.addEventListener("pagehide", stopCamera);
