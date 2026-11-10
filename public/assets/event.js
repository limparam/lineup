import {
  api,
  clear,
  el,
  renderEvent,
  setServerNow,
  startClock,
} from "./common.js?v=7";

const host = document.getElementById("event");
const slug = decodeURIComponent(
  location.pathname.replace(/^\/e\//, "").replace(/\/+$/, ""),
);

let controller = null;
let lastEventJson = "";
let failed = false;

function notFound(message) {
  clear(host).append(
    el("div", { class: "empty" }, [
      el("h2", { text: "Мероприятие не найдено" }),
      el("p", { text: message }),
      el("a", { class: "btn btn--primary", href: "/", text: "На главную" }),
    ]),
  );
}

async function load() {
  try {
    const data = await api(`/api/public/events/${encodeURIComponent(slug)}`);
    setServerNow(data.serverNow);
    failed = false;

    const json = JSON.stringify(data.event);
    if (json !== lastEventJson) {
      lastEventJson = json;
      controller = renderEvent(host, data.event, {
        title: (event) => `${event.title} — LineUP`,
      });
      document.title = `${data.event.title} — LineUP`;
    }
  } catch (error) {
    controller = null;
    if (lastEventJson) return;
    failed = true;
    const message =
      error.status === 404
        ? "Возможно, мероприятие удалено или ещё не опубликовано."
        : error.message || "Проверьте соединение и попробуйте ещё раз.";
    notFound(message);
    document.title = "Мероприятие не найдено — LineUP";
  }
}

startClock((now) => {
  if (controller) controller.update(now);
});

load();
setInterval(load, 30000);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") load();
});

window.addEventListener("online", () => {
  if (failed) load();
});
