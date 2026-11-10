/* LineUP — общие функции клиента: запросы, форматирование, расписание. */

const dateTimeFormatCache = new Map();
const timeFormatCache = new Map();
const dayKeyFormatCache = new Map();
const zoneLabelCache = new Map();

let clockOffset = 0;

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

export async function api(path, { method = "GET", body, csrf } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    throw new ApiError(
      (data && data.error && data.error.message) || `Ошибка ${response.status}`,
      response.status,
      data,
    );
  }
  return data;
}

/* ------------------------------------------------------------------ DOM */

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === "attrs") {
      for (const [name, attrValue] of Object.entries(value)) {
        node.setAttribute(name, attrValue === true ? "" : String(attrValue));
      }
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children === null || children === undefined) return;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

/* -------------------------------------------------------------- time */

export function setServerNow(iso) {
  const value = Date.parse(iso);
  if (!Number.isNaN(value)) clockOffset = value - Date.now();
}

export function nowMs() {
  return Date.now() + clockOffset;
}

function fmtTime(tz) {
  let fmt = timeFormatCache.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("ru-RU", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    timeFormatCache.set(tz, fmt);
  }
  return fmt;
}

function fmtDate(tz) {
  let fmt = dateTimeFormatCache.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("ru-RU", {
      timeZone: tz,
      day: "numeric",
      month: "long",
      year: "numeric",
    });
    dateTimeFormatCache.set(tz, fmt);
  }
  return fmt;
}

function dateParts(iso, tz) {
  const date = new Date(iso);
  const result = { day: "", month: "", monthShort: "", year: "" };
  for (const part of fmtDate(tz).formatToParts(date)) {
    if (part.type === "day") result.day = part.value;
    else if (part.type === "month") result.month = part.value;
    else if (part.type === "year") result.year = part.value;
  }
  result.monthShort = (result.month || "").slice(0, 3);
  return result;
}

export function timeStr(iso, tz) {
  return fmtTime(tz).format(new Date(iso));
}

export function fullDate(iso, tz) {
  const p = dateParts(iso, tz);
  return `${p.day} ${p.month} ${p.year}`.trim();
}

export function shortDate(iso, tz) {
  const p = dateParts(iso, tz);
  return `${p.day} ${p.monthShort}`;
}

export function dayNumber(iso, tz) {
  return dateParts(iso, tz).day;
}

export function monthShort(iso, tz) {
  return dateParts(iso, tz).monthShort;
}

export function zoneLabel(tz) {
  let label = zoneLabelCache.get(tz);
  if (label === undefined) {
    try {
      const parts = new Intl.DateTimeFormat("ru-RU", {
        timeZone: tz,
        timeZoneName: "short",
      }).formatToParts(new Date());
      const found = parts.find((p) => p.type === "timeZoneName");
      label = found ? found.value.replace("GMT", "UTC") : tz;
    } catch {
      label = tz;
    }
    zoneLabelCache.set(tz, label);
  }
  return label;
}

function localDayKey(iso, tz) {
  // Стабильный ключ «календарного дня» в поясе мероприятия.
  let fmt = dayKeyFormatCache.get(tz);
  if (!fmt) {
    fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: tz,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    dayKeyFormatCache.set(tz, fmt);
  }
  return fmt.format(new Date(iso));
}

export function formatRange(startIso, endIso, tz) {
  const sameDay = localDayKey(startIso, tz) === localDayKey(endIso, tz);
  const start = `${fullDate(startIso, tz)} · ${timeStr(startIso, tz)}`;
  if (sameDay) return `${start} — ${timeStr(endIso, tz)}`;
  return `${start} — ${fullDate(endIso, tz)} · ${timeStr(endIso, tz)}`;
}

export function sameLocalDay(startIso, endIso, tz) {
  return localDayKey(startIso, tz) === localDayKey(endIso, tz);
}

function heroTimeParts(event, tz) {
  const startDate = `${fullDate(event.startsAt, tz)} · ${timeStr(event.startsAt, tz)}`;
  const endDate = sameLocalDay(event.startsAt, event.endsAt, tz)
    ? timeStr(event.endsAt, tz)
    : `${fullDate(event.endsAt, tz)} · ${timeStr(event.endsAt, tz)}`;
  // Тире живёт внутри второго блока: при переносе строки на мобильном оно
  // уедет вместе с датой окончания, а не останется висеть в конце строки.
  return [
    el("span", { class: "hero__time-date", text: startDate }),
    el("span", { class: "hero__time-date" }, [
      el("span", { class: "hero__time-sep", text: "—" }),
      endDate,
    ]),
  ];
}

export function formatRangeShort(startIso, endIso, tz) {
  const sameDay = localDayKey(startIso, tz) === localDayKey(endIso, tz);
  const start = `${shortDate(startIso, tz)} ${timeStr(startIso, tz)}`;
  if (sameDay) return `${start}–${timeStr(endIso, tz)}`;
  return `${start} – ${shortDate(endIso, tz)} ${timeStr(endIso, tz)}`;
}

export function duration(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`;
}

export function humanGap(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 1) return "меньше минуты";
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return restMinutes ? `${hours} ч ${restMinutes} мин` : `${hours} ч`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days} д ${restHours} ч` : `${days} д`;
}

export function eventStatus(event, now) {
  const start = Date.parse(event.startsAt);
  const end = Date.parse(event.endsAt);
  if (now >= end) return "past";
  if (now >= start) return "live";
  return "upcoming";
}

/* ---------------------------------------------------------- rendering */

function statusPill(kind, label) {
  const className =
    kind === "live" ? "pill pill--live" : kind === "upcoming" ? "pill pill--soon" : "pill pill--past";
  const children = [];
  if (kind === "live") children.push(el("i", { class: "pill__dot" }));
  children.push(el("span", { text: label }));
  return el("span", { class: className }, children);
}

function statusLabel(kind) {
  if (kind === "live") return "в эфире";
  if (kind === "upcoming") return "скоро";
  return "завершено";
}

export function renderEventRow(event, now = nowMs()) {
  const kind = eventStatus(event, now);
  const metaParts = [];
  if (event.city) metaParts.push(event.city);
  if (event.venue) metaParts.push(event.venue);

  return el(
    "a",
    { class: "event-row", href: `/e/${encodeURIComponent(event.slug)}` },
    [
      el("div", { class: "event-row__date" }, [
        el("span", { class: "event-row__day", text: dayNumber(event.startsAt, event.timezone) }),
        el("span", { class: "event-row__month", text: monthShort(event.startsAt, event.timezone) }),
      ]),
      el("div", { class: "event-row__body" }, [
        el("div", { class: "event-row__title" }, [
          el("span", { text: event.title }),
          event.lineupVisible === false
            ? el("span", { class: "tag tag--announce", text: "анонс" })
            : null,
        ]),
        el("div", {
          class: "event-row__meta",
          text: `${metaParts.join(" · ")} · ${formatRangeShort(
            event.startsAt,
            event.endsAt,
            event.timezone,
          )}`,
        }),
      ]),
      el("div", { class: "event-row__end" }, [
        statusPill(kind, statusLabel(kind)),
        el("span", { class: "event-row__arrow", text: "→", attrs: { "aria-hidden": "true" } }),
      ]),
    ],
  );
}

/**
 * Рисует мероприятие (hero + расписание) и возвращает контроллер с update(now).
 */
export function renderEvent(host, event, { title } = {}) {
  const tz = event.timezone;
  const lineupVisible = event.lineupVisible !== false;
  clear(host);

  const eyebrowPill = el("span", { class: "pill" });
  const heroStatusText = el("span", {});
  const heroLive = el("div", { class: "hero__live" });
  const zonePill = el("span", {
    class: "pill pill--past",
    text: zoneLabel(tz),
    attrs: { title: tz },
  });

  const titleEl = el("h1", {
    class: "hero__title" + (event.title.length > 18 ? " hero__title--long" : ""),
    text: event.title,
  });

  const placeParts = [];
  if (event.city) placeParts.push(el("span", { class: "hero__city", text: event.city }));
  if (event.city && event.venue) {
    placeParts.push(el("span", { class: "dot", attrs: { "aria-hidden": "true" } }));
  }
  if (event.venue) placeParts.push(el("span", { class: "hero__venue", text: event.venue }));

  const hero = el("section", { class: "hero" }, [
    el("div", { class: "hero__eyebrow" }, [
      eyebrowPill,
      lineupVisible ? null : el("span", { class: "tag tag--announce", text: "анонс" }),
      zonePill,
    ]),
    titleEl,
    el("div", { class: "hero__place" }, placeParts),
    event.address ? el("div", { class: "hero__address", text: event.address }) : null,
    el("div", { class: "hero__time" }, heroTimeParts(event, tz)),
    event.description ? el("p", { class: "hero__desc", text: event.description }) : null,
    Array.isArray(event.links) && event.links.length
      ? el(
          "div",
          { class: "hero__links" },
          event.links.map((link) =>
            el(
              "a",
              {
                class: "link-chip",
                href: link.url,
                target: "_blank",
                rel: "noopener noreferrer nofollow",
              },
              [
                el("span", { class: "link-chip__text", text: link.title || link.url }),
                el("span", {
                  class: "link-chip__arrow",
                  text: "↗",
                  attrs: { "aria-hidden": "true" },
                }),
              ],
            ),
          ),
        )
      : null,
    el("div", { class: "hero__foot" }, [
      el("div", { class: "hero__status" }, [heroStatusText]),
      heroLive,
    ]),
  ]);

  // Собираем расписание.
  const hallsHost = el("div", { class: "halls" });
  const slotRecords = [];
  const hallRecords = [];
  let totalPerformances = 0;
  let totalHalls = 0;

  for (const hall of event.halls) {
    totalHalls += 1;
    totalPerformances += hall.performances.length;
    const liveBadge = el("span", { class: "hall__live", dataset: { count: "0" } }, [
      el("i", { class: "pill__dot" }),
      el("span", { text: "0 в эфире" }),
    ]);
    const list = el("ol", { class: "slots" });
    const hallRecord = {
      name: hall.name,
      badge: liveBadge,
      label: liveBadge.querySelector("span"),
      slots: [],
    };

    if (hall.performances.length === 0) {
      list.append(el("li", { class: "hall__empty", text: "Сеты пока не объявлены." }));
    }

    for (const perf of hall.performances) {
      const start = Date.parse(perf.startsAt);
      const end = Date.parse(perf.endsAt);
      const startLocal = localDayKey(perf.startsAt, tz);
      const eventLocal = localDayKey(event.startsAt, tz);

      const timeChildren = [];
      if (startLocal !== eventLocal) {
        timeChildren.push(el("span", { class: "slot__day", text: shortDate(perf.startsAt, tz) }));
      }
      timeChildren.push(el("span", { text: timeStr(perf.startsAt, tz) }));
      timeChildren.push(el("span", { text: timeStr(perf.endsAt, tz) }));

      const bar = el("i", {});
      const elapsed = el("span", { class: "slot__elapsed" });
      const left = el("span", { class: "slot__left" });

      const node = el("li", { class: "slot" }, [
        el("div", { class: "slot__time" }, timeChildren),
        el("div", { class: "slot__main" }, [
          el("div", { class: "slot__head" }, [
            el("span", { class: "slot__name", text: perf.performer }),
            el("span", { class: "slot__badge", text: "live" }),
          ]),
          el("div", { class: "slot__progress" }, [bar]),
          el("div", { class: "slot__meta" }, [elapsed, left]),
        ]),
      ]);

      list.append(node);
      const record = { node, start, end, bar, elapsed, left, hall: hallRecord, isLive: false, isPast: false };
      slotRecords.push(record);
      hallRecord.slots.push(record);
    }

    const hallNode = el("article", { class: "hall" }, [
      el("header", { class: "hall__head" }, [
        el("h3", { class: "hall__name", text: hall.name }),
        liveBadge,
      ]),
      list,
    ]);
    hallsHost.append(hallNode);
    hallRecord.node = hallNode;
    hallRecords.push(hallRecord);
  }

  const schedule = el("section", { class: "schedule" }, [
    el("div", { class: "schedule__head" }, [
      el("h2", { class: "section-title", text: "Расписание" }),
      el("span", {
        class: "muted small",
        text: `${totalHalls} ${plural(totalHalls, "зал", "зала", "залов")} · ${totalPerformances} ${plural(
          totalPerformances,
          "сет",
          "сета",
          "сетов",
        )}`,
      }),
    ]),
    hallsHost,
  ]);

  if (!lineupVisible) {
    clear(schedule).append(
      el("div", { class: "schedule__head" }, [
        el("h2", { class: "section-title", text: "Расписание" }),
        el("span", { class: "tag tag--announce", text: "анонс" }),
      ]),
      el("div", { class: "announce" }, [
        el("h3", { class: "announce__title", text: "Лайнап скоро объявят" }),
        el("p", {
          class: "announce__text",
          text: "Залы и артисты появятся здесь, как только организаторы откроют расписание.",
        }),
        event.lineupPublishAt
          ? el("p", {
              class: "announce__when",
              text: `Расписание откроется ${fullDate(event.lineupPublishAt, tz)} · ${timeStr(
                event.lineupPublishAt,
                tz,
              )}`,
            })
          : null,
      ]),
    );
  }

  host.append(hero, schedule);

  if (title) document.title = title(event);

  function update(now) {
    const kind = eventStatus(event, now);

    // Шапка.
    if (eyebrowPill) {
      eyebrowPill.className = kind === "live" ? "pill pill--live" : kind === "upcoming" ? "pill pill--soon" : "pill pill--past";
      eyebrowPill.replaceChildren(
        ...(kind === "live" ? [el("i", { class: "pill__dot" })] : []),
        el("span", { text: statusLabel(kind) }),
      );
    }

    let statusText = "";
    if (kind === "live") statusText = `идёт сейчас · до конца ${humanGap(Date.parse(event.endsAt) - now)}`;
    else if (kind === "upcoming") statusText = `до начала ${humanGap(Date.parse(event.startsAt) - now)}`;
    else statusText = "мероприятие прошло";
    setText(heroStatusText, statusText);

    const livePerf = [];
    for (const record of slotRecords) {
      const isLive = now >= record.start && now < record.end;
      const isPast = now >= record.end;
      if (isLive !== record.isLive) {
        record.node.classList.toggle("is-live", isLive);
        record.isLive = isLive;
      }
      if (isPast !== record.isPast) {
        record.node.classList.toggle("is-past", isPast && !isLive);
        record.isPast = isPast;
      }
      if (!isLive) continue;

      const total = record.end - record.start;
      const passed = now - record.start;
      const percent = Math.min(100, Math.max(0, (passed / total) * 100));
      record.bar.style.width = `${percent.toFixed(2)}%`;
      setText(record.elapsed, `отыграл ${duration(passed)}`);
      setText(record.left, `осталось ${duration(record.end - now)}`);
      livePerf.push({ name: record.node.querySelector(".slot__name").textContent, hall: record.hall.name });
    }

    for (const hall of hallRecords) {
      const count = hall.slots.filter((s) => now >= s.start && now < s.end).length;
      if (hall.badge.dataset.count !== String(count)) {
        hall.badge.dataset.count = String(count);
        setText(hall.label, `${count} в эфире`);
      }
    }

    const chips = [];
    for (const item of livePerf) {
      chips.push(
        el("span", { class: "live-chip" }, [
          el("b", { text: item.name }),
          el("span", { text: item.hall }),
        ]),
      );
    }
    if (!lineupVisible) {
      chips.push(
        el("span", {
          class: "live-chip live-chip--muted",
          text: "Лайнап ещё не объявлен",
        }),
      );
    } else if (chips.length === 0) {
      const label = kind === "live" ? "Антракт" : kind === "upcoming" ? "Скоро начало" : "Все сеты отыграны";
      chips.push(el("span", { class: "live-chip live-chip--muted", text: label }));
    }
    heroLive.replaceChildren(...chips);
  }

  update(nowMs());
  return { update, event };
}

/* --------------------------------------------------------------- clock */

export function startClock(callback) {
  let timer = null;
  let stopped = false;
  const tick = () => {
    if (stopped) return;
    callback(nowMs());
    const delay = 1000 - (Date.now() % 1000);
    timer = setTimeout(tick, delay);
  };
  tick();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
  };
}

export function plural(count, one, few, many) {
  const n = Math.abs(count) % 100;
  const n1 = n % 10;
  if (n > 10 && n < 20) return many;
  if (n1 > 1 && n1 < 5) return few;
  if (n1 === 1) return one;
  return many;
}

export function skeletons(host, count = 2) {
  clear(host);
  for (let i = 0; i < count; i += 1) host.append(el("div", { class: "skeleton" }));
}
