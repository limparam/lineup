import {
  api,
  clear,
  el,
  eventStatus,
  formatRangeShort,
  plural,
} from "./common.js?v=14";

const $ = (id) => document.getElementById(id);

const loginView = $("view-login");
const eventsView = $("view-events");
const eventView = $("view-event");
const usersView = $("view-users");
const auditView = $("view-audit");
const performersView = $("view-performers");
const siteView = $("view-site");
const loginForm = $("login-form");
const loginError = $("login-error");
const setupHint = $("setup-hint");
const toasts = $("toasts");
const hallsHost = $("halls");
const addHallForm = $("add-hall-form");
const eventForm = $("event-form");
const accountDialog = $("account-dialog");
const passwordForm = $("password-form");
const passwordError = $("password-error");
const performersList = $("performers-list");
const performersAdminList = $("performers-admin");
const performersSearch = $("performers-search");
const usersList = $("users-list");
const auditList = $("audit-list");
const addUserForm = $("add-user-form");
const addUserError = $("add-user-error");
const userDialog = $("user-dialog");
const userPasswordForm = $("user-password-form");
const userPasswordError = $("user-password-error");

let csrf = null;
let username = "";
let isSuper = false;
let editingUserId = null;
let timezones = [];
let current = null;
let allPerformers = [];

/* --------------------------------------------------------------- utils */

function toast(message, kind = "ok") {
  const node = el("div", {
    class: "toast" + (kind === "error" ? " toast--error" : ""),
    text: message,
  });
  toasts.append(node);
  while (toasts.children.length > 3) toasts.firstElementChild.remove();
  setTimeout(() => node.remove(), 5200);
}

function setBusy(button, busy) {
  if (button) button.disabled = busy;
}

function showView(name) {
  loginView.hidden = name !== "login";
  eventsView.hidden = name !== "events";
  eventView.hidden = name !== "event";
  usersView.hidden = name !== "users";
  auditView.hidden = name !== "audit";
  performersView.hidden = name !== "performers";
  siteView.hidden = name !== "site";
  const authed = name !== "login";
  $("logout-btn").hidden = !authed;
  $("account-btn").hidden = !authed;
  $("users-btn").hidden = !(authed && isSuper);
  $("audit-btn").hidden = !(authed && isSuper);
  $("performers-btn").hidden = !(authed && isSuper);
  $("site-btn").hidden = !(authed && isSuper);
}

async function request(path, options = {}) {
  const opts = { ...options };
  if (opts.method && opts.method !== "GET") opts.csrf = csrf;
  try {
    return await api(path, opts);
  } catch (error) {
    if (error.status === 401) {
      showLogin();
    }
    throw error;
  }
}

function showLogin({ setup = false, message = null } = {}) {
  csrf = null;
  current = null;
  username = "";
  isSuper = false;
  $("who").textContent = "";
  setupHint.hidden = !setup;
  loginError.hidden = !message;
  loginError.textContent = message || "";
  showView("login");
}

/* --------------------------------------------------------------- время */

function pad(value) {
  return String(value).padStart(2, "0");
}

function addMinutesLocal(value, minutes) {
  const [datePart, timePart] = String(value || "").split("T");
  if (!datePart || !timePart) return value;
  const [year, month, day] = datePart.split("-").map(Number);
  const [hour, minute] = timePart.split(":").map(Number);
  const stamp = Date.UTC(year, month - 1, day, hour, minute) + minutes * 60000;
  const date = new Date(stamp);
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(
    date.getUTCDate(),
  )}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

const DISPLAY_DATE_RE =
  /^(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{4})[\s,]+(\d{1,2}):(\d{2})$/;

function localToDisplay(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(
    String(value || ""),
  );
  if (!match) return String(value || "");
  return `${match[3]}.${match[2]}.${match[1]} ${match[4]}:${match[5]}`;
}

function displayToLocal(value) {
  const match = DISPLAY_DATE_RE.exec(String(value || "").trim());
  if (!match) return null;
  const day = Number(match[1]);
  const month = Number(match[2]);
  const year = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  if (
    year < 2000 ||
    year > 2100 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > 31 ||
    hour > 23 ||
    minute > 59
  ) {
    return null;
  }
  const check = new Date(Date.UTC(year, month - 1, day));
  if (
    check.getUTCFullYear() !== year ||
    check.getUTCMonth() !== month - 1 ||
    check.getUTCDate() !== day
  ) {
    return null;
  }
  return `${year}-${pad(month)}-${pad(day)}T${pad(hour)}:${pad(minute)}`;
}

function normalizeDateTimeField(input) {
  if (!input) return;
  const value = input.value.trim();
  if (!value) return;
  const local = displayToLocal(value);
  if (local) input.value = localToDisplay(local);
}

function dateTimeInput(value, ariaLabel, className = "") {
  const input = el("input", {
    type: "text",
    class: `dt-input${className ? ` ${className}` : ""}`,
    maxlength: "16",
    value: localToDisplay(value),
    placeholder: "ДД.ММ.ГГГГ ЧЧ:ММ",
    attrs: {
      "aria-label": ariaLabel,
      inputmode: "numeric",
      autocomplete: "off",
      spellcheck: "false",
    },
  });
  input.addEventListener("blur", () => normalizeDateTimeField(input));
  return input;
}

function zonedNowLocal(tz) {
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  const get = (type) => (parts.find((part) => part.type === type) || {}).value || "00";
  return `${get("year")}-${get("month")}-${get("day")}T${get("hour")}:${get("minute")}`;
}

function defaultEventTimes(tz) {
  const start = addMinutesLocal(zonedNowLocal(tz), 60);
  return { start, end: addMinutesLocal(start, 360) };
}

/* ------------------------------------------------------- список событий */

function statusChip(event) {
  const kind = eventStatus(event, Date.now());
  if (kind === "live") {
    return el("span", { class: "pill pill--live" }, [
      el("i", { class: "pill__dot" }),
      el("span", { text: "в эфире" }),
    ]);
  }
  if (kind === "upcoming") return el("span", { class: "pill pill--soon", text: "скоро" });
  return el("span", { class: "pill pill--past", text: "завершено" });
}

function announceTag(event) {
  if (!event.lineupVisible) {
    return el("span", {
      class: "tag tag--announce",
      text: "анонс",
      attrs: { title: "Лайнап скрыт до ручного открытия" },
    });
  }
  if (event.lineupPublishAt && Date.parse(event.lineupPublishAt) > Date.now()) {
    return el("span", {
      class: "tag tag--announce",
      text: "анонс",
      attrs: { title: "Лайнап откроется автоматически" },
    });
  }
  return null;
}

function expiredTag(event) {
  if (!event.published || event.publiclyVisible !== false) return null;
  return el("span", {
    class: "tag tag--expired",
    text: "скрыто",
    attrs: {
      title:
        "Мероприятие завершилось более 7 дней назад и скрыто с сайта. Измените время, чтобы опубликовать снова.",
    },
  });
}

function renderEventList(events) {
  const host = $("admin-events");
  clear(host);

  if (!events.length) {
    host.append(
      el("div", { class: "empty" }, [
        el("h2", { text: "Мероприятий пока нет" }),
        el("p", { text: "Создайте первое — кнопка «+ Новое мероприятие»." }),
      ]),
    );
    return;
  }

  for (const event of events) {
    const metaParts = [
      formatRangeShort(event.startsAt, event.endsAt, event.timezone),
      [event.city, event.venue].filter(Boolean).join(" · "),
      `${event.hallCount} ${plural(event.hallCount, "зал", "зала", "залов")}`,
      `${event.performanceCount} ${plural(event.performanceCount, "сет", "сета", "сетов")}`,
    ];
    if (isSuper && event.ownerUsername) metaParts.push(`автор: ${event.ownerUsername}`);
    const meta = metaParts.filter(Boolean).join(" · ");

    const card = el("article", { class: "admin-event" }, [
      el("div", { class: "admin-event__main" }, [
        el("div", { class: "admin-event__title" }, [
          el("span", { text: event.title }),
          event.published ? null : el("span", { class: "tag tag--draft", text: "черновик" }),
          announceTag(event),
          expiredTag(event),
          statusChip(event),
        ]),
        el("div", { class: "admin-event__meta", text: meta }),
      ]),
      el("div", { class: "admin-event__actions" }, [
        el("button", {
          class: "btn btn--sm",
          type: "button",
          text: "Открыть",
          onClick: () => openEvent(event.id).catch((error) => toast(error.message, "error")),
        }),
        el("button", {
          class: "btn btn--sm btn--danger",
          type: "button",
          text: "Удалить",
          onClick: () => deleteEventById(event).catch((error) => toast(error.message, "error")),
        }),
      ]),
    ]);
    host.append(card);
  }
}

async function openList() {
  const data = await request("/api/admin/events");
  timezones = data.timezones || timezones;
  $("who").textContent = username;
  renderEventList(data.events);
  showView("events");
}

async function deleteEventById(event) {
  const label = event.published ? event.title : `${event.title} (черновик)`;
  if (!confirm(`Удалить «${label}»?\nВсе залы и сеты тоже будут удалены.`)) return;
  await request(`/api/admin/events/${event.id}`, { method: "DELETE" });
  toast("Мероприятие удалено");
  current = null;
  await openList();
}

/* -------------------------------------------------------------- ссылки */

function linkRow(link = {}) {
  const title = el("input", {
    type: "text",
    maxlength: "80",
    class: "link-row__title",
    value: link.title || "",
    placeholder: "Название, например «Билеты»",
    attrs: { "aria-label": "Название ссылки" },
  });
  const url = el("input", {
    type: "url",
    maxlength: "500",
    class: "link-row__url",
    value: link.url || "",
    placeholder: "https://…",
    attrs: {
      "aria-label": "Адрес ссылки",
      inputmode: "url",
      autocapitalize: "none",
      spellcheck: "false",
    },
  });
  const row = el("div", { class: "link-row" }, [
    title,
    url,
    el("button", {
      class: "btn btn--sm btn--danger btn--icon",
      type: "button",
      text: "✕",
      attrs: { "aria-label": "Удалить ссылку", title: "Удалить ссылку" },
      onClick: () => row.remove(),
    }),
  ]);
  return row;
}

function renderLinks(links = []) {
  const host = $("links-list");
  clear(host);
  for (const link of links) host.append(linkRow(link));
}

function addLink() {
  const host = $("links-list");
  if (host.children.length >= 12) {
    toast("Можно добавить не больше 12 ссылок", "error");
    return;
  }
  const row = linkRow();
  host.append(row);
  row.querySelector("input").focus();
}

function collectLinks() {
  const links = [];
  for (const row of $("links-list").children) {
    const [title, url] = row.querySelectorAll("input");
    const value = url.value.trim();
    if (!value) continue;
    links.push({ title: title.value.trim(), url: value });
  }
  return links;
}

/* ------------------------------------------------------------- редактор */

function fillTimezoneSelect(selected) {
  const select = $("f-timezone");
  const list = [...timezones];
  if (selected && !list.includes(selected)) list.unshift(selected);
  clear(select);
  for (const tz of list) select.append(el("option", { value: tz, text: tz }));
  select.value = selected && list.includes(selected) ? selected : "Europe/Moscow";
}

function fillEventForm() {
  if (!current) return;
  $("f-title").value = current.title;
  $("f-city").value = current.city;
  $("f-venue").value = current.venue;
  $("f-address").value = current.address;
  $("f-timezone").value = current.timezone;
  $("f-starts").value = localToDisplay(current.startsAtLocal);
  $("f-ends").value = localToDisplay(current.endsAtLocal);
  $("f-description").value = current.description;
  $("f-published").checked = current.published;
  $("f-lineup-visible").checked = current.lineupVisible !== false;
  $("f-lineup-filter").value = current.lineupFilter || "all";
  $("f-lineup-publish").value = current.lineupPublishAtLocal
    ? localToDisplay(current.lineupPublishAtLocal)
    : "";
  renderLinks(current.links || []);

  $("event-title").textContent = current.title || "Мероприятие";
  const subParts = [
    formatRangeShort(current.startsAt, current.endsAt, current.timezone),
    [current.city, current.venue].filter(Boolean).join(" · "),
  ];
  if (isSuper && current.ownerUsername) {
    subParts.push(`автор: ${current.ownerUsername}`);
  }
  if (current.published && current.publiclyVisible === false) {
    subParts.push("скрыто с сайта");
  }
  $("event-sub").textContent = subParts.filter(Boolean).join(" · ");
  $("delete-event").hidden = false;

  const publicLink = $("open-public");
  publicLink.hidden = !current.published || current.publiclyVisible === false;
  publicLink.href = `/e/${encodeURIComponent(current.slug)}`;
}

function sortPerformances(hall) {
  hall.performances.sort((a, b) => String(a.startsAt).localeCompare(String(b.startsAt)));
}

function nextSlotTimes(hall) {
  const start = current ? current.startsAtLocal : "";
  const end = current ? current.endsAtLocal : "";
  const last = hall.performances[hall.performances.length - 1];
  let slotStart = last ? last.endsAtLocal : start;
  if (slotStart < start || slotStart >= end) slotStart = start;
  let slotEnd = addMinutesLocal(slotStart, 60);
  if (slotEnd > end) slotEnd = end;
  return { start: slotStart, end: slotEnd };
}

function performanceRow(hall, performance) {
  const nameInput = el("input", {
    type: "text",
    class: "perf-name",
    maxlength: "120",
    value: performance.performer,
    placeholder: "Имя исполнителя",
    attrs: { "aria-label": "Исполнитель" },
  });
  const startInput = dateTimeInput(
    performance.startsAtLocal,
    "Начало сета",
    "perf-start",
  );
  const endInput = dateTimeInput(
    performance.endsAtLocal,
    "Конец сета",
    "perf-end",
  );

  const save = el("button", {
    class: "btn btn--sm",
    type: "button",
    text: "Сохранить",
    onClick: async () => {
      const performer = nameInput.value.trim();
      if (!performer) {
        toast("Укажите имя исполнителя", "error");
        return;
      }
      if (!startInput.value || !endInput.value) {
        toast("Укажите начало и конец сета", "error");
        return;
      }
      const startsAt = displayToLocal(startInput.value);
      const endsAt = displayToLocal(endInput.value);
      if (!startsAt || !endsAt) {
        toast("Дата и время сета: ДД.ММ.ГГГГ ЧЧ:ММ", "error");
        return;
      }
      setBusy(save, true);
      try {
        const data = await request(`/api/admin/performances/${performance.id}`, {
          method: "PATCH",
          body: {
            performer,
            startsAt,
            endsAt,
          },
        });
        const index = hall.performances.findIndex((p) => p.id === performance.id);
        hall.performances[index] = data.performance;
        sortPerformances(hall);
        renderHalls();
        loadPerformers();
        toast("Сет обновлён");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(save, false);
      }
    },
  });

  const remove = el("button", {
    class: "btn btn--sm btn--danger btn--icon",
    type: "button",
    text: "✕",
    attrs: { "aria-label": `Удалить сет ${performance.performer}`, title: "Удалить сет" },
    onClick: async () => {
      if (!confirm(`Удалить выступление «${performance.performer}»?`)) return;
      setBusy(remove, true);
      try {
        await request(`/api/admin/performances/${performance.id}`, { method: "DELETE" });
        hall.performances = hall.performances.filter((p) => p.id !== performance.id);
        renderHalls();
        toast("Сет удалён");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(remove, false);
      }
    },
  });

  return el("div", { class: "perf-row" }, [
    nameInput,
    startInput,
    endInput,
    el("div", { class: "perf-row__actions" }, [save, remove]),
  ]);
}

function createPerfAddForm(hall) {
  const times = nextSlotTimes(hall);
  const name = el("input", {
    type: "text",
    maxlength: "120",
    placeholder: "Имя диджея",
    attrs: { list: "performers-list", "aria-label": "Новый исполнитель" },
  });
  const start = dateTimeInput(times.start, "Начало нового сета", "perf-start");
  const end = dateTimeInput(times.end, "Конец нового сета", "perf-end");
  const submit = el("button", { class: "btn btn--primary btn--sm", type: "submit", text: "+ Сет" });

  const form = el("form", { class: "perf-add", novalidate: true }, [name, start, end, submit]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const performer = name.value.trim();
    if (!performer) {
      toast("Укажите имя исполнителя", "error");
      name.focus();
      return;
    }
    if (!start.value || !end.value) {
      toast("Укажите начало и конец сета", "error");
      return;
    }
    const startsAt = displayToLocal(start.value);
    const endsAt = displayToLocal(end.value);
    if (!startsAt || !endsAt) {
      toast("Дата и время сета: ДД.ММ.ГГГГ ЧЧ:ММ", "error");
      return;
    }
    setBusy(submit, true);
    try {
      const data = await request(`/api/admin/halls/${hall.id}/performances`, {
        method: "POST",
        body: { performer, startsAt, endsAt },
      });
      hall.performances.push(data.performance);
      sortPerformances(hall);
      renderHalls();
      loadPerformers();
      toast("Сет добавлен");
      const nextName = hallsHost.querySelector(`[data-hall="${hall.id}"] .perf-add input`);
      if (nextName) nextName.focus();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setBusy(submit, false);
    }
  });
  return form;
}

function hallEditor(hall) {
  const nameInput = el("input", {
    type: "text",
    class: "hall-editor__name",
    maxlength: "60",
    value: hall.name,
    attrs: { "aria-label": "Название зала" },
  });
  const saveName = el("button", {
    class: "btn btn--sm",
    type: "button",
    text: "Сохранить",
    onClick: async () => {
      const name = nameInput.value.trim();
      if (!name) {
        toast("Укажите название зала", "error");
        return;
      }
      setBusy(saveName, true);
      try {
        await request(`/api/admin/halls/${hall.id}`, { method: "PATCH", body: { name } });
        hall.name = name;
        renderHalls();
        toast("Название зала сохранено");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(saveName, false);
      }
    },
  });
  nameInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveName.click();
    }
  });

  const remove = el("button", {
    class: "btn btn--sm btn--danger btn--icon",
    type: "button",
    text: "✕",
    attrs: { "aria-label": `Удалить зал ${hall.name}`, title: "Удалить зал" },
    onClick: async () => {
      const count = hall.performances.length;
      const extra = count
        ? `\nВместе с ним удалятся ${count} ${plural(count, "сет", "сета", "сетов")}.`
        : "";
      if (!confirm(`Удалить зал «${hall.name}»?${extra}`)) return;
      setBusy(remove, true);
      try {
        await request(`/api/admin/halls/${hall.id}`, { method: "DELETE" });
        current.halls = current.halls.filter((item) => item.id !== hall.id);
        renderHalls();
        toast("Зал удалён");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        setBusy(remove, false);
      }
    },
  });

  const countLabel = `${hall.performances.length} ${plural(
    hall.performances.length,
    "сет",
    "сета",
    "сетов",
  )}`;

  const list = el("div", { class: "perf-list" });
  for (const performance of hall.performances) {
    list.append(performanceRow(hall, performance));
  }
  if (!hall.performances.length) {
    list.append(el("div", { class: "hall-empty", text: "Сетов пока нет — добавьте первый ниже." }));
  }

  return el("div", { class: "hall-editor", dataset: { hall: String(hall.id) } }, [
    el("div", { class: "hall-editor__head" }, [
      nameInput,
      saveName,
      remove,
      el("span", { class: "hall-editor__count", text: countLabel }),
    ]),
    list,
    createPerfAddForm(hall),
  ]);
}

function renderHalls() {
  clear(hallsHost);
  const canEdit = Boolean(current);

  addHallForm.hidden = !canEdit;
  $("halls-count").textContent = current
    ? `${current.halls.length} ${plural(current.halls.length, "зал", "зала", "залов")}`
    : "";

  if (!canEdit) {
    hallsHost.append(
      el("div", {
        class: "hall-empty",
        text: "Сначала сохраните мероприятие — после этого можно добавлять залы и сеты.",
      }),
    );
    return;
  }

  if (!current.halls.length) {
    hallsHost.append(
      el("div", { class: "hall-empty", text: "Залов пока нет. Добавьте первый зал ниже." }),
    );
  }

  for (const hall of current.halls) hallsHost.append(hallEditor(hall));
}

async function openEvent(id) {
  const data = await request(`/api/admin/events/${id}`);
  current = data.event;
  timezones = data.timezones || timezones;
  fillTimezoneSelect(current.timezone);
  fillEventForm();
  renderHalls();
  showView("event");
  window.scrollTo({ top: 0 });
  loadPerformers();
}

function newEvent() {
  current = null;
  eventForm.reset();
  const tz = "Europe/Moscow";
  fillTimezoneSelect(tz);
  const times = defaultEventTimes(tz);
  $("f-city").value = "Москва";
  $("f-starts").value = localToDisplay(times.start);
  $("f-ends").value = localToDisplay(times.end);
  $("f-published").checked = true;
  $("f-lineup-visible").checked = true;
  $("f-lineup-filter").value = "all";
  $("f-lineup-publish").value = "";
  $("f-title").value = "";
  $("f-venue").value = "";
  $("f-address").value = "";
  $("f-description").value = "";
  renderLinks([]);
  $("event-title").textContent = "Новое мероприятие";
  $("event-sub").textContent = "Заполните данные и сохраните — затем добавьте залы.";
  $("delete-event").hidden = true;
  $("open-public").hidden = true;
  renderHalls();
  showView("event");
  window.scrollTo({ top: 0 });
  $("f-title").focus();
}

async function saveEvent(event) {
  event.preventDefault();
  const startsAt = displayToLocal($("f-starts").value);
  const endsAt = displayToLocal($("f-ends").value);
  if (!startsAt || !endsAt) {
    toast("Укажите начало и конец в формате ДД.ММ.ГГГГ ЧЧ:ММ", "error");
    return;
  }
  const publishRaw = $("f-lineup-publish").value.trim();
  const lineupPublishAt = publishRaw ? displayToLocal(publishRaw) : null;
  if (publishRaw && !lineupPublishAt) {
    toast("Дата открытия лайнапа: ДД.ММ.ГГГГ ЧЧ:ММ", "error");
    return;
  }
  const payload = {
    title: $("f-title").value.trim(),
    city: $("f-city").value.trim(),
    venue: $("f-venue").value.trim(),
    address: $("f-address").value.trim(),
    timezone: $("f-timezone").value,
    startsAt,
    endsAt,
    description: $("f-description").value.trim(),
    links: collectLinks(),
    published: $("f-published").checked,
    lineupVisible: $("f-lineup-visible").checked,
    lineupFilter: $("f-lineup-filter").value,
    lineupPublishAt,
  };
  if (!payload.title || !payload.venue) {
    toast("Укажите название и место проведения", "error");
    return;
  }

  const button = $("save-event");
  const wasNew = !current;
  setBusy(button, true);
  try {
    const data = wasNew
      ? await request("/api/admin/events", { method: "POST", body: payload })
      : await request(`/api/admin/events/${current.id}`, { method: "PATCH", body: payload });
    current = data.event;
    fillTimezoneSelect(current.timezone);
    fillEventForm();
    renderHalls();
    toast(wasNew ? "Мероприятие создано" : "Изменения сохранены");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function deleteCurrentEvent() {
  if (!current) return;
  const count = current.halls.reduce((sum, hall) => sum + hall.performances.length, 0);
  const extra = count
    ? `\nВместе с ним удалятся ${count} ${plural(count, "сет", "сета", "сетов")}.`
    : "";
  if (!confirm(`Удалить «${current.title}»?${extra}`)) return;
  try {
    await request(`/api/admin/events/${current.id}`, { method: "DELETE" });
    current = null;
    toast("Мероприятие удалено");
    await openList();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadPerformers() {
  try {
    const data = await request("/api/admin/performers");
    clear(performersList);
    for (const name of data.performers) {
      performersList.append(el("option", { value: name }));
    }
  } catch {
    /* подсказки не критичны */
  }
}

/* ------------------------------------------------------- администраторы */

function renderUsers(users) {
  clear(usersList);
  $("users-count").textContent = `${users.length} ${plural(
    users.length,
    "администратор",
    "администратора",
    "администраторов",
  )}`;

  for (const user of users) {
    const actions = [];
    if (!user.isSuper) {
      actions.push(
        el("button", {
          class: "btn btn--sm",
          type: "button",
          text: "Пароль",
          onClick: () => openUserPassword(user),
        }),
        el("button", {
          class: "btn btn--sm btn--danger",
          type: "button",
          text: "Удалить",
          onClick: () => deleteUser(user),
        }),
      );
    }

    const name = [el("span", { text: user.username })];
    if (user.isSuper) name.push(el("span", { class: "tag", text: "главный" }));
    if (user.username === username) {
      name.push(el("span", { class: "muted small", text: "это вы" }));
    }

    usersList.append(
      el("div", { class: "user-row" }, [
        el("div", { class: "user-row__name" }, name),
        el("div", { class: "user-row__actions" }, actions),
      ]),
    );
  }
}

async function openUsers() {
  const data = await request("/api/admin/users");
  renderUsers(data.users);
  showView("users");
  window.scrollTo({ top: 0 });
}

function openUserPassword(user) {
  editingUserId = user.id;
  $("user-dialog-sub").textContent = `Новый пароль для «${user.username}». Все его сессии будут завершены.`;
  userPasswordForm.reset();
  userPasswordError.hidden = true;
  userPasswordError.textContent = "";
  userDialog.showModal();
}

async function deleteUser(user) {
  if (!confirm(`Удалить администратора «${user.username}»?`)) return;
  try {
    await request(`/api/admin/users/${user.id}`, { method: "DELETE" });
    toast("Администратор удалён");
    await openUsers();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------ артисты для автозаполнения */

function renderPerformers(names, query = "") {
  const total = allPerformers.length;
  clear(performersAdminList);

  if (!total) {
    $("performers-count").textContent = "";
    performersAdminList.append(
      el("div", { class: "empty" }, [
        el("h2", { text: "Список пуст" }),
        el("p", {
          text: "Имена появятся здесь, когда администраторы добавят артистов в мероприятия.",
        }),
      ]),
    );
    return;
  }

  if (!names.length) {
    $("performers-count").textContent = `Найдено: 0 из ${total}`;
    performersAdminList.append(
      el("div", { class: "empty" }, [
        el("h2", { text: "Ничего не найдено" }),
        el("p", { text: "Попробуйте другое имя или проверьте написание." }),
      ]),
    );
    return;
  }

  $("performers-count").textContent = query
    ? `Найдено: ${names.length} из ${total}`
    : `${total} ${plural(total, "артист", "артиста", "артистов")}`;

  for (const name of names) {
    performersAdminList.append(
      el("div", { class: "user-row" }, [
        el("div", { class: "user-row__name" }, [
          el("span", { text: name }),
        ]),
        el("div", { class: "user-row__actions" }, [
          el("button", {
            class: "btn btn--sm btn--danger",
            type: "button",
            text: "Удалить",
            onClick: () => deletePerformer(name),
          }),
        ]),
      ]),
    );
  }
}

function applyPerformersFilter() {
  const query = performersSearch.value.trim().toLowerCase();
  const names = query
    ? allPerformers.filter((name) => name.toLowerCase().includes(query))
    : allPerformers;
  renderPerformers(names, query);
}

async function openPerformers() {
  const data = await request("/api/admin/performers");
  allPerformers = data.performers || [];
  performersSearch.value = "";
  applyPerformersFilter();
  showView("performers");
  window.scrollTo({ top: 0 });
}

async function deletePerformer(name) {
  if (
    !confirm(
      `Удалить «${name}» из списка артистов? Артист перестанет предлагаться и находиться в поиске. Выступления останутся.`,
    )
  ) {
    return;
  }
  try {
    await request(
      `/api/admin/performers?${new URLSearchParams({ name })}`,
      { method: "DELETE" },
    );
    allPerformers = allPerformers.filter(
      (item) => item.toLowerCase() !== name.toLowerCase(),
    );
    applyPerformersFilter();
    toast(`Артист «${name}» удалён из подсказок`);
    await loadPerformers();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ------------------------------------------------------------ сайт */

let siteLogoUrl = null;
let siteLogoBytes = 0;
let siteMaxLinks = 8;
let siteMaxLogoBytes = 180 * 1024;

function formatBytes(bytes) {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} КБ`;
  return `${bytes} Б`;
}

function renderSiteLogo() {
  const host = $("site-logo-preview");
  clear(host);
  $("site-logo-size").textContent = siteLogoBytes ? formatBytes(siteLogoBytes) : "";
  if (siteLogoUrl) {
    host.append(
      el("img", {
        class: "site-logo-preview__img",
        src: `${siteLogoUrl}${siteLogoUrl.includes("?") ? "&" : "?"}t=${Date.now()}`,
        alt: "Логотип LineUP",
      }),
    );
    $("site-logo-remove").hidden = false;
    $("site-logo-status").textContent =
      "На сайте вместо надписи LineUP показывается этот логотип.";
  } else {
    host.append(
      el("span", { class: "brand" }, [
        "Line",
        el("span", { text: "UP" }),
        el("i", { class: "brand__dot" }),
      ]),
    );
    $("site-logo-remove").hidden = true;
    $("site-logo-status").textContent =
      "Логотип не загружен — на сайте показывается надпись LineUP.";
  }
}

function applySiteLogo(data) {
  siteLogoUrl = data.logoUrl || null;
  siteLogoBytes = data.logoBytes || 0;
  renderSiteLogo();
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const result = String(reader.result || "");
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    });
    reader.addEventListener("error", () => {
      reject(reader.error || new Error("Не удалось прочитать файл."));
    });
    reader.readAsDataURL(file);
  });
}

async function uploadSiteLogo() {
  const input = $("site-logo-file");
  const file = input.files && input.files[0];
  if (!file) {
    toast("Выберите PNG-файл", "error");
    return;
  }
  if (file.size > siteMaxLogoBytes) {
    toast(
      `Файл больше ${formatBytes(siteMaxLogoBytes)}. Выберите изображение меньше.`,
      "error",
    );
    return;
  }
  const button = $("site-logo-upload");
  setBusy(button, true);
  try {
    const data = await request("/api/admin/site/logo", {
      method: "PUT",
      body: { data: await fileToBase64(file) },
    });
    applySiteLogo(data);
    input.value = "";
    toast("Логотип загружен");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function removeSiteLogo() {
  if (!confirm("Убрать логотип? На сайте снова появится надпись LineUP.")) return;
  const button = $("site-logo-remove");
  setBusy(button, true);
  try {
    const data = await request("/api/admin/site/logo", { method: "DELETE" });
    applySiteLogo(data);
    $("site-logo-file").value = "";
    toast("Логотип убран");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

function updateSiteLinksCount() {
  const count = $("site-links-list").querySelectorAll(".site-link-row").length;
  $("site-links-count").textContent = count
    ? `${count} из ${siteMaxLinks}`
    : "";
}

function siteLinkRow(link = {}) {
  const label = el("input", {
    type: "text",
    class: "site-link-row__label",
    maxlength: "60",
    value: link.label || "",
    placeholder: "Название, например «О проекте»",
    attrs: { "aria-label": "Название кнопки" },
  });
  const url = el("input", {
    type: "url",
    class: "site-link-row__url",
    maxlength: "500",
    value: link.url || "",
    placeholder: "https://… (необязательно)",
    attrs: {
      "aria-label": "Ссылка кнопки",
      inputmode: "url",
      autocapitalize: "none",
      spellcheck: "false",
    },
  });
  const description = el("textarea", {
    class: "site-link-row__description",
    maxlength: "1200",
    rows: "3",
    text: link.description || "",
    placeholder: "Описание, которое откроется по клику",
    attrs: { "aria-label": "Описание кнопки" },
  });
  const row = el("div", { class: "site-link-row" }, [
    el("div", { class: "site-link-row__top" }, [
      label,
      url,
      el("button", {
        class: "btn btn--sm btn--danger btn--icon",
        type: "button",
        text: "✕",
        attrs: { "aria-label": "Удалить кнопку", title: "Удалить кнопку" },
        onClick: () => {
          row.remove();
          if (!$("site-links-list").querySelector(".site-link-row")) {
            renderSiteLinks([]);
          } else {
            updateSiteLinksCount();
          }
        },
      }),
    ]),
    description,
  ]);
  return row;
}

function renderSiteLinks(links = []) {
  const host = $("site-links-list");
  clear(host);
  if (!links.length) {
    host.append(
      el("div", {
        class: "site-links-empty",
        text: "Кнопок пока нет. Добавьте «О проекте», «Контакты» или другую кнопку.",
      }),
    );
  } else {
    for (const link of links) host.append(siteLinkRow(link));
  }
  updateSiteLinksCount();
}

function addSiteLink() {
  const host = $("site-links-list");
  if (host.querySelectorAll(".site-link-row").length >= siteMaxLinks) {
    toast(`Можно добавить не больше ${siteMaxLinks} кнопок`, "error");
    return;
  }
  const empty = host.querySelector(".site-links-empty");
  if (empty) empty.remove();
  const row = siteLinkRow();
  host.append(row);
  updateSiteLinksCount();
  row.querySelector("input").focus();
}

function collectSiteLinks() {
  const links = [];
  for (const row of $("site-links-list").querySelectorAll(".site-link-row")) {
    const label = row.querySelector(".site-link-row__label").value.trim();
    const description = row
      .querySelector(".site-link-row__description")
      .value.trim();
    const url = row.querySelector(".site-link-row__url").value.trim();
    if (!label && !description && !url) continue;
    links.push({ label, description, url });
  }
  return links;
}

async function saveSiteLinks() {
  const button = $("site-save-links");
  setBusy(button, true);
  try {
    const data = await request("/api/admin/site/links", {
      method: "PUT",
      body: { links: collectSiteLinks() },
    });
    renderSiteLinks(data.links || []);
    toast("Кнопки сохранены");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
}

async function openSite() {
  const data = await request("/api/admin/site");
  siteMaxLinks = data.maxLinks || 8;
  siteMaxLogoBytes = data.maxLogoBytes || 180 * 1024;
  $("site-logo-hint").textContent =
    `Только PNG, до ${formatBytes(siteMaxLogoBytes)}. Логотип показывается ` +
    "в шапке всех публичных страниц вместо надписи LineUP.";
  $("site-logo-file").value = "";
  applySiteLogo(data);
  renderSiteLinks(data.links || []);
  showView("site");
  window.scrollTo({ top: 0 });
}

/* ------------------------------------------------------------ журнал */

const AUDIT_LABELS = {
  "event.create": "Создал мероприятие",
  "event.update": "Изменил мероприятие",
  "event.delete": "Удалил мероприятие",
  "hall.create": "Добавил зал",
  "hall.update": "Переименовал зал",
  "hall.delete": "Удалил зал",
  "performance.create": "Добавил артиста",
  "performance.update": "Изменил артиста",
  "performance.delete": "Удалил артиста",
  "performer.delete": "Удалил артиста из подсказок",
  "site.links": "Изменил кнопки в подвале",
  "site.logo": "Изменил логотип сайта",
  "user.create": "Добавил администратора",
  "user.update": "Сменил пароль администратора",
  "user.delete": "Удалил администратора",
};

function formatStamp(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "short",
    timeStyle: "short",
  }).format(date);
}

function renderAudit(entries) {
  clear(auditList);
  $("audit-count").textContent = entries.length
    ? `${entries.length} ${plural(entries.length, "запись", "записи", "записей")}`
    : "";

  if (!entries.length) {
    auditList.append(
      el("div", { class: "empty" }, [
        el("h2", { text: "Журнал пока пуст" }),
        el("p", { text: "Здесь появятся действия администраторов." }),
      ]),
    );
    return;
  }

  for (const entry of entries) {
    auditList.append(
      el("article", { class: "audit-row" }, [
        el("div", { class: "audit-row__main" }, [
          el("div", {
            class: "audit-row__action",
            text: AUDIT_LABELS[entry.action] || entry.action,
          }),
          entry.detail
            ? el("div", { class: "audit-row__detail", text: entry.detail })
            : null,
        ]),
        el("div", { class: "audit-row__meta" }, [
          el("span", { class: "audit-row__actor", text: entry.actor || "—" }),
          el("span", { class: "audit-row__time", text: formatStamp(entry.createdAt) }),
        ]),
      ]),
    );
  }
}

async function openAudit() {
  const data = await request("/api/admin/audit");
  renderAudit(data.entries || []);
  showView("audit");
  window.scrollTo({ top: 0 });
}

/* ---------------------------------------------------------------- вход */

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.hidden = true;
  const user = $("login-user").value.trim();
  const password = $("login-pass").value;
  if (!user || !password) {
    loginError.hidden = false;
    loginError.textContent = "Введите логин и пароль.";
    return;
  }
  const button = $("login-submit");
  setBusy(button, true);
  try {
    const data = await api("/api/admin/login", {
      method: "POST",
      body: { username: user, password },
    });
    csrf = data.csrfToken;
    username = data.user.username;
    isSuper = Boolean(data.user.isSuper);
    $("who").textContent = username;
    $("login-pass").value = "";
    await openList();
  } catch (error) {
    loginError.hidden = false;
    loginError.textContent = error.message;
    setupHint.hidden = true;
  } finally {
    setBusy(button, false);
  }
});

$("logout-btn").addEventListener("click", async () => {
  try {
    await request("/api/admin/logout", { method: "POST" });
  } catch {
    /* всё равно показываем форму входа */
  }
  showLogin();
});

/* ------------------------------------------------------------ остальное */

$("new-event").addEventListener("click", newEvent);
$("back-btn").addEventListener("click", () => {
  openList().catch((error) => toast(error.message, "error"));
});
for (const id of ["f-starts", "f-ends", "f-lineup-publish"]) {
  $(id).addEventListener("blur", () => normalizeDateTimeField($(id)));
}
$("site-btn").addEventListener("click", () => {
  openSite().catch((error) => toast(error.message, "error"));
});
$("site-back").addEventListener("click", () => {
  openList().catch((error) => toast(error.message, "error"));
});
$("site-add-link").addEventListener("click", addSiteLink);
$("site-save-links").addEventListener("click", saveSiteLinks);
$("site-logo-upload").addEventListener("click", uploadSiteLogo);
$("site-logo-remove").addEventListener("click", removeSiteLogo);
$("site-logo-file").addEventListener("change", () => {
  const file = $("site-logo-file").files && $("site-logo-file").files[0];
  if (!file) {
    $("site-logo-size").textContent = siteLogoBytes
      ? formatBytes(siteLogoBytes)
      : "";
    return;
  }
  $("site-logo-size").textContent = `${file.name} · ${formatBytes(file.size)}`;
});
$("add-link").addEventListener("click", addLink);
$("users-btn").addEventListener("click", () => {
  openUsers().catch((error) => toast(error.message, "error"));
});
$("performers-btn").addEventListener("click", () => {
  openPerformers().catch((error) => toast(error.message, "error"));
});
performersSearch.addEventListener("input", applyPerformersFilter);
performersSearch.addEventListener("search", applyPerformersFilter);
$("audit-btn").addEventListener("click", () => {
  openAudit().catch((error) => toast(error.message, "error"));
});
$("audit-clear").addEventListener("click", async () => {
  if (!confirm("Очистить весь журнал действий? Это действие нельзя отменить.")) return;
  const button = $("audit-clear");
  setBusy(button, true);
  try {
    const data = await request("/api/admin/audit", { method: "DELETE" });
    renderAudit([]);
    toast(
      data.deleted
        ? `Журнал очищен: ${data.deleted} ${plural(data.deleted, "запись", "записи", "записей")}`
        : "Журнал уже был пуст",
    );
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
});
$("audit-back").addEventListener("click", () => {
  openList().catch((error) => toast(error.message, "error"));
});
$("users-back").addEventListener("click", () => {
  openList().catch((error) => toast(error.message, "error"));
});
$("performers-back").addEventListener("click", () => {
  openList().catch((error) => toast(error.message, "error"));
});
eventForm.addEventListener("submit", saveEvent);
$("delete-event").addEventListener("click", deleteCurrentEvent);

addHallForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current) return;
  const input = $("hall-name-input");
  const name = input.value.trim();
  if (!name) {
    toast("Укажите название зала", "error");
    input.focus();
    return;
  }
  const button = addHallForm.querySelector("button");
  setBusy(button, true);
  try {
    const data = await request(`/api/admin/events/${current.id}/halls`, {
      method: "POST",
      body: { name },
    });
    current.halls.push(data.hall);
    input.value = "";
    renderHalls();
    toast("Зал добавлен");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(button, false);
  }
});

$("account-btn").addEventListener("click", () => {
  passwordForm.reset();
  passwordError.hidden = true;
  passwordError.textContent = "";
  accountDialog.showModal();
});

$("cancel-password").addEventListener("click", () => accountDialog.close());

passwordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  passwordError.hidden = true;
  const currentPassword = $("p-current").value;
  const newPassword = $("p-new").value;
  const confirmPassword = $("p-confirm").value;

  if (newPassword !== confirmPassword) {
    passwordError.hidden = false;
    passwordError.textContent = "Пароли не совпадают.";
    return;
  }
  if (newPassword.length < 10) {
    passwordError.hidden = false;
    passwordError.textContent = "Пароль должен быть не короче 10 символов.";
    return;
  }

  const submit = passwordForm.querySelector('button[type="submit"]');
  setBusy(submit, true);
  try {
    await request("/api/admin/password", {
      method: "POST",
      body: { currentPassword, newPassword },
    });
    accountDialog.close();
    toast("Пароль изменён");
  } catch (error) {
    passwordError.hidden = false;
    passwordError.textContent = error.message;
  } finally {
    setBusy(submit, false);
  }
});

/* ---------------------------------------------------------- админы */

addUserForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  addUserError.hidden = true;
  const login = $("u-username").value.trim();
  const password = $("u-password").value;
  if (!login || !password) {
    addUserError.hidden = false;
    addUserError.textContent = "Введите логин и пароль.";
    return;
  }
  if (password.length < 10) {
    addUserError.hidden = false;
    addUserError.textContent = "Пароль должен быть не короче 10 символов.";
    return;
  }
  const submit = $("add-user-submit");
  setBusy(submit, true);
  try {
    await request("/api/admin/users", {
      method: "POST",
      body: { username: login, password },
    });
    addUserForm.reset();
    toast("Администратор добавлен");
    await openUsers();
  } catch (error) {
    addUserError.hidden = false;
    addUserError.textContent = error.message;
  } finally {
    setBusy(submit, false);
  }
});

$("cancel-user-password").addEventListener("click", () => userDialog.close());

userPasswordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  userPasswordError.hidden = true;
  const password = $("up-password").value;
  const confirmPassword = $("up-confirm").value;
  if (password !== confirmPassword) {
    userPasswordError.hidden = false;
    userPasswordError.textContent = "Пароли не совпадают.";
    return;
  }
  if (password.length < 10) {
    userPasswordError.hidden = false;
    userPasswordError.textContent = "Пароль должен быть не короче 10 символов.";
    return;
  }
  const submit = userPasswordForm.querySelector('button[type="submit"]');
  setBusy(submit, true);
  try {
    await request(`/api/admin/users/${editingUserId}`, {
      method: "PATCH",
      body: { password },
    });
    userDialog.close();
    toast("Пароль изменён");
  } catch (error) {
    userPasswordError.hidden = false;
    userPasswordError.textContent = error.message;
  } finally {
    setBusy(submit, false);
  }
});

/* --------------------------------------------------------------- старт */

async function boot() {
  try {
    const session = await api("/api/admin/session");
    csrf = session.csrfToken;
    username = session.user.username;
    isSuper = Boolean(session.user.isSuper);
    await openList();
  } catch (error) {
    showLogin({
      setup: Boolean(error.data && error.data.setupRequired),
      message: error.status === 401 ? null : error.message,
    });
  }
}

boot();
