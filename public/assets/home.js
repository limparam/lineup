import {
  api,
  clear,
  el,
  loadSiteChrome,
  nowMs,
  plural,
  renderEvent,
  renderEventRow,
  setServerNow,
  startClock,
} from "./common.js?v=16";

const featuredHost = document.getElementById("featured");
const section = document.getElementById("afisha");
const eventsHost = document.getElementById("events");
const countHost = document.getElementById("events-count");
const yearHost = document.getElementById("year");

let controller = null;
let lastEventJson = "";
let lastOthers = [];

function notice(title, text, action) {
  const box = el("div", { class: "empty" }, [
    el("h2", { text: title }),
    el("p", { text }),
  ]);
  if (action) {
    box.append(
      el("button", {
        class: "btn btn--primary",
        type: "button",
        text: action.label,
        onClick: action.onClick,
      }),
    );
  }
  return box;
}

function renderOthers() {
  clear(eventsHost);
  for (const item of lastOthers) eventsHost.append(renderEventRow(item, nowMs()));
  section.hidden = lastOthers.length === 0;
  countHost.textContent = lastOthers.length
    ? `${lastOthers.length} ${plural(lastOthers.length, "событие", "события", "событий")}`
    : "";
}

async function load() {
  try {
    const data = await api("/api/public/featured");
    setServerNow(data.serverNow);

    if (data.event) {
      const json = JSON.stringify(data.event);
      if (json !== lastEventJson) {
        lastEventJson = json;
        controller = renderEvent(featuredHost, data.event, {
          title: (event) => `${event.title} — LineUP`,
        });
        document.title = `${data.event.title} — LineUP`;
      }
    } else {
      controller = null;
      lastEventJson = "";
      clear(featuredHost).append(
        notice(
          "Пока нет мероприятий",
          "Как только организаторы опубликуют афишу, она появится здесь.",
        ),
      );
      document.title = "LineUP — афиша мероприятий";
    }

    lastOthers = data.others || [];
    renderOthers();
  } catch (error) {
    if (!lastEventJson) {
      controller = null;
      clear(featuredHost).append(
        notice(
          "Не удалось загрузить афишу",
          error.message || "Проверьте соединение и попробуйте ещё раз.",
          { label: "Повторить", onClick: load },
        ),
      );
    }
  }
}

if (yearHost) yearHost.textContent = String(new Date().getFullYear());

loadSiteChrome();

startClock((now) => {
  if (controller) controller.update(now);
});

load();
setInterval(load, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
});
window.addEventListener("focus", load);
