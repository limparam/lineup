import {
  api,
  clear,
  el,
  formatRangeShort,
  plural,
  setServerNow,
  timeStr,
} from "./common.js?v=7";

const host = document.getElementById("artists");
const input = document.getElementById("artist-search");
const countHost = document.getElementById("artists-count");

const yearHost = document.getElementById("year");
if (yearHost) yearHost.textContent = String(new Date().getFullYear());

let debounceTimer = null;
let requestId = 0;

function skeleton() {
  clear(host).append(
    el("div", { class: "skeleton", attrs: { "aria-label": "Загрузка артистов" } }),
  );
}

function eventRow(event) {
  const place = [event.city, event.venue].filter(Boolean).join(" · ");
  const sets = (event.sets || []).map((set) =>
    el("span", {
      class: "artist-set",
      text: `${set.hall} · ${timeStr(set.startsAt, event.timezone)}–${timeStr(
        set.endsAt,
        event.timezone,
      )}`,
    }),
  );

  return el(
    "a",
    {
      class: "artist-event",
      href: `/e/${encodeURIComponent(event.slug)}`,
    },
    [
      el("div", { class: "artist-event__main" }, [
        el("div", { class: "artist-event__title", text: event.title }),
        place ? el("div", { class: "artist-event__place", text: place }) : null,
        sets.length ? el("div", { class: "artist-event__sets" }, sets) : null,
      ]),
      el("div", {
        class: "artist-event__date",
        text: formatRangeShort(event.startsAt, event.endsAt, event.timezone),
      }),
    ],
  );
}

function artistCard(artist) {
  const events = artist.events || [];
  const tag = events.length
    ? `${events.length} ${plural(events.length, "мероприятие", "мероприятия", "мероприятий")}`
    : "нет актуальных";

  return el("article", { class: "artist-card" }, [
    el("div", { class: "artist-card__head" }, [
      el("h2", { class: "artist-card__name", text: artist.name }),
      el("span", {
        class: events.length ? "tag tag--announce" : "tag",
        text: tag,
      }),
    ]),
    events.length
      ? el("div", { class: "artist-card__events" }, events.map(eventRow))
      : el("p", {
          class: "artist-card__empty",
          text: "Ближайших выступлений нет. Артиста можно найти через поиск.",
        }),
  ]);
}

function emptyState(title, text) {
  return el("div", { class: "empty" }, [
    el("h2", { text: title }),
    el("p", { text }),
  ]);
}

function renderArtists(artists, query) {
  clear(host);

  if (!artists.length) {
    host.append(
      query
        ? emptyState(
            "Ничего не найдено",
            "Попробуйте другое имя или проверьте написание.",
          )
        : emptyState(
            "Пока нет артистов с актуальными мероприятиями",
            "Загляните позже или воспользуйтесь поиском по имени.",
          ),
    );
  } else {
    host.append(...artists.map(artistCard));
  }

  if (query) {
    countHost.textContent = artists.length
      ? `Найдено: ${artists.length}`
      : "";
  } else {
    countHost.textContent = artists.length
      ? `${artists.length} ${plural(
          artists.length,
          "артист",
          "артиста",
          "артистов",
        )} с актуальными мероприятиями`
      : "";
  }
}

async function load(query) {
  const id = ++requestId;
  const params = new URLSearchParams();
  if (query) params.set("q", query);
  const suffix = params.toString() ? `?${params}` : "";

  try {
    const data = await api(`/api/public/artists${suffix}`);
    if (id !== requestId) return;
    setServerNow(data.serverNow);
    renderArtists(data.artists || [], data.query || query);
  } catch (error) {
    if (id !== requestId) return;
    clear(host).append(
      emptyState("Не удалось загрузить артистов", error.message),
    );
    countHost.textContent = "";
  }
}

function scheduleLoad() {
  const query = input.value.trim();
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => load(query), 220);
}

input.addEventListener("input", scheduleLoad);
input.addEventListener("search", scheduleLoad);

skeleton();
load("");
