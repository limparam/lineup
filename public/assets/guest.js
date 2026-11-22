import {
  clear,
  el,
  formatRangeShort,
  loadSiteChrome,
} from "./common.js?v=20";

const host = document.getElementById("guest");
const token = decodeURIComponent(
  location.pathname.replace(/^\/g\//, "").replace(/\/+$/, ""),
);

loadSiteChrome();

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, area.value.length);
    const ok = document.execCommand("copy");
    area.remove();
    if (ok) resolve();
    else reject(new Error("Не удалось скопировать ссылку"));
  });
}

function errorState(title, text) {
  return el("div", { class: "empty guest-pass__empty" }, [
    el("h1", { text: title }),
    el("p", { text }),
    el("a", { class: "btn btn--primary", href: "/", text: "На главную" }),
  ]);
}

async function sharePass(event, guest) {
  const title = `Приглашение на ${event.title}`;
  const text = `${guest.name} — покажите QR-код на входе.`;
  if (navigator.share) {
    try {
      await navigator.share({ title, text, url: location.href });
      return;
    } catch (error) {
      if (error.name === "AbortError") return;
    }
  }
  await copyText(location.href);
  const toast = el("div", { class: "toast", text: "Ссылка на приглашение скопирована" });
  document.body.append(toast);
  setTimeout(() => toast.remove(), 4000);
}

function render(data) {
  const event = data.event;
  const guest = data.guest;
  document.title = `${guest.name} — ${event.title}`;

  const place = [event.city, event.venue].filter(Boolean).join(" · ");
  const meta = [
    formatRangeShort(event.startsAt, event.endsAt, event.timezone),
    place,
  ].filter(Boolean);

  const tags = [];
  if (guest.tableName) {
    tags.push(el("span", { class: "guest-pass__tag", text: `Стол: ${guest.tableName}` }));
  }

  const share = el("button", {
    class: "btn btn--primary guest-pass__share",
    type: "button",
    text: "Поделиться приглашением",
  });
  share.addEventListener("click", () => {
    sharePass(event, guest).catch(() => {});
  });

  const card = el("article", { class: "guest-pass" }, [
    el("div", { class: "guest-pass__eyebrow", text: "Приглашение" }),
    el("h1", { class: "guest-pass__event", text: event.title }),
    el("p", { class: "guest-pass__meta", text: meta.join(" · ") }),
    el("div", { class: "guest-pass__guest" }, [
      el("span", { class: "guest-pass__name", text: guest.name }),
      tags.length ? el("div", { class: "guest-pass__tags" }, tags) : null,
    ]),
    el("div", { class: "guest-pass__qr" }, [
      el("img", {
        src: `/g/${encodeURIComponent(token)}/qr.png`,
        alt: "QR-код приглашения",
        width: "280",
        height: "280",
      }),
    ]),
    el("p", {
      class: "guest-pass__hint",
      text: "Покажите этот QR-код на входе",
    }),
    guest.status === "checked_in"
      ? el("p", { class: "guest-pass__checked", text: "Вход уже отмечен" })
      : null,
    share,
    el("a", {
      class: "guest-pass__event-link",
      href: `/e/${encodeURIComponent(event.slug)}`,
      text: "Открыть афишу мероприятия ↗",
    }),
  ]);
  clear(host).append(card);
}

async function load() {
  try {
    const response = await fetch(`/api/public/guests/${encodeURIComponent(token)}`, {
      cache: "no-store",
      credentials: "same-origin",
    });
    if (!response.ok) {
      throw new Error("Приглашение не найдено или уже недействительно.");
    }
    render(await response.json());
  } catch (error) {
    clear(host).append(errorState(
      "Приглашение недоступно",
      error.message || "Проверьте ссылку или попросите организатора отправить новую.",
    ));
  }
}

load();
