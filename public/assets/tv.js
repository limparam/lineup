const token = decodeURIComponent(
  location.pathname.replace(/^\/tv\//, "").replace(/\/+$/, ""),
);
const eventNode = document.getElementById("tv-event");
const stage = document.getElementById("tv-stage");
const hallNode = document.getElementById("tv-hall");
const nextNode = document.getElementById("tv-next");
const nextName = document.getElementById("tv-next-name");
const nextTime = document.getElementById("tv-next-time");
let loading = false;

const rootStyle = document.documentElement.style;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatTime(iso, timezone) {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: timezone,
    }).format(new Date(iso));
  } catch {
    return "";
  }
}

function formatLimit(value) {
  const number = Math.max(10, Math.min(34, Math.round(value)));
  return `${number}s`;
}

function marqueeText(text) {
  const length = text.length;
  const count = length <= 8 ? 4 : length <= 14 ? 3 : length <= 24 ? 2 : 1;
  return Array.from({ length: count }, () => text).join("   •   ");
}

function applySettings(settings) {
  rootStyle.setProperty("--tv-bg", settings.background);
  rootStyle.setProperty("--tv-bg-from", settings.backgroundFrom);
  rootStyle.setProperty("--tv-bg-to", settings.backgroundTo);
  rootStyle.setProperty("--tv-font", settings.fontFamily);
  rootStyle.setProperty("--tv-name-color", settings.nameColor);
  rootStyle.setProperty("--tv-name-from", settings.nameFrom);
  rootStyle.setProperty("--tv-name-to", settings.nameTo);
  document.body.classList.toggle(
    "tv-bg-gradient", settings.backgroundMode === "gradient");
  document.body.classList.toggle("tv-name-gradient", settings.nameGradient);
  document.body.classList.remove(
    "tv-size-normal", "tv-size-large", "tv-size-fill");
  document.body.classList.add(`tv-size-${settings.fontSize || "normal"}`);
}

function marqueeLine(text, { reverse = false, ghost = false } = {}) {
  const line = el("div", "tv-line");
  const track = el(
    "div", "tv-track" + (reverse ? " tv-track--reverse" : ""));
  for (let index = 0; index < 2; index += 1) {
    const copy = el("span", "tv-copy");
    copy.append(el(
      "span",
      "tv-name" + (ghost ? " tv-name--ghost" : ""),
      text,
    ));
    track.append(copy);
  }
  line.append(track);
  return line;
}

function staticName(text) {
  const line = el("div", "tv-line tv-line--static");
  line.append(el(
    "span",
    "tv-name" + (text.length > 18 ? " tv-name--long" : ""),
    text,
  ));
  return line;
}

function renderStage(data) {
  const settings = data.settings;
  stage.replaceChildren();

  if (data.state === "hidden") {
    stage.append(el("div", "tv-message", "Лайнап скоро"));
    stage.append(el(
      "div", "tv-message tv-message--small",
      "Экран включится автоматически после открытия расписания",
    ));
    return;
  }
  if (data.state === "before") {
    stage.append(el("div", "tv-message", "Скоро начало"));
    const start = formatTime(data.event.startsAt, data.event.timezone);
    if (start) {
      stage.append(el("div", "tv-message tv-message--small", `Начало в ${start}`));
    }
    return;
  }
  if (data.state === "ended") {
    stage.append(el("div", "tv-message", "Мероприятие завершено"));
    return;
  }
  if (data.state === "break") {
    stage.append(el("div", "tv-message", "Антракт"));
    return;
  }

  const names = [];
  for (const item of data.current || []) {
    if (item.name && !names.includes(item.name)) names.push(item.name);
  }
  const text = names.join(" • ") || data.event.title;
  const effect = settings.effect === "marquee2"
    ? "marquee2"
    : settings.effect === "static"
      ? "static"
      : "marquee";
  const displayText = marqueeText(text);
  const speedFactor = settings.speed === "slow"
    ? 1.5
    : settings.speed === "fast"
      ? 0.65
      : 1;
  rootStyle.setProperty(
    "--tv-duration",
    formatLimit(displayText.length * 0.32 * speedFactor));

  if (effect === "static") {
    stage.append(staticName(text));
    return;
  }
  const wrapper = el(
    "div", "tv-lines" + (effect === "marquee2" ? " tv-lines--double" : ""));
  wrapper.append(marqueeLine(displayText));
  if (effect === "marquee2") {
    wrapper.append(marqueeLine(displayText, { reverse: true, ghost: true }));
  }
  stage.append(wrapper);
}

function render(data) {
  const settings = data.settings;
  applySettings(settings);
  eventNode.textContent = data.event.title;
  renderStage(data);

  const halls = [];
  for (const item of data.current || []) {
    if (item.hallName && !halls.includes(item.hallName)) {
      halls.push(item.hallName);
    }
  }
  hallNode.textContent = data.hallName
    || halls.join(" · ")
    || [data.event.venue, data.event.city].filter(Boolean).join(" · ");

  if (settings.showNext && data.next) {
    const nextHall = data.next.hallName && data.next.hallName !== hallNode.textContent
      ? ` · ${data.next.hallName}` : "";
    nextName.textContent = data.next.name + nextHall;
    nextTime.textContent = formatTime(
      data.next.startsAt, data.event.timezone);
    nextNode.hidden = false;
  } else {
    nextNode.hidden = true;
  }
}

function showError(message) {
  stage.replaceChildren(el("div", "tv-message tv-message--small tv-error", message));
}

async function load() {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch(`/api/tv/${encodeURIComponent(token)}`, {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error((data && data.error && data.error.message)
        || "TV-ссылка недействительна.");
    }
    render(await response.json());
  } catch (error) {
    showError(error.message || "Нет связи с сервером");
  } finally {
    loading = false;
  }
}

load();
setInterval(load, 15000);
