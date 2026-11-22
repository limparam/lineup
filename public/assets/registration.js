import {
  clear,
  el,
  formatRangeShort,
  loadSiteChrome,
} from "./common.js?v=20";

const token = decodeURIComponent(
  location.pathname.replace(/^\/r\//, "").replace(/\/+$/, ""),
);
const host = document.getElementById("registration");

loadSiteChrome();

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
    const error = new Error(
      data && data.error && data.error.message
        ? data.error.message
        : "Не удалось выполнить запрос.",
    );
    error.status = response.status;
    throw error;
  }
  return data;
}

function errorState(title, text) {
  return el("div", { class: "empty registration-pass__empty" }, [
    el("h1", { text: title }),
    el("p", { text }),
    el("a", { class: "btn btn--primary", href: "/", text: "На главную" }),
  ]);
}

function eventMeta(event) {
  return [formatRangeShort(event.startsAt, event.endsAt, event.timezone)]
    .filter(Boolean)
    .join(" · ");
}

function successState(data) {
  const guest = data.guest;
  const url = new URL(`/g/${guest.shareToken}`, location.origin);
  clear(host).append(
    el("section", { class: "registration-pass" }, [
      el("div", { class: "registration-pass__eyebrow", text: "Готово" }),
      el("h1", { class: "registration-pass__title", text: "Вы зарегистрированы" }),
      el("p", { class: "registration-pass__event", text: data.event.title }),
      el("p", { class: "registration-pass__name", text: guest.name }),
      el("div", { class: "registration-pass__qr" }, [
        el("img", {
          src: `${url.pathname}/qr.png`,
          alt: "QR-код приглашения",
          width: "220",
          height: "220",
        }),
      ]),
      el("p", {
        class: "registration-pass__hint",
        text: "Покажите этот QR-код на входе. Сохраните ссылку на приглашение.",
      }),
      el("a", {
        class: "btn btn--primary registration-pass__button",
        href: url.pathname,
        text: "Открыть приглашение",
      }),
    ]),
  );
}

function render(data) {
  document.title = `${data.event.title} — регистрация`;
  if (!data.canRegister) {
    clear(host).append(
      el("section", { class: "registration-pass" }, [
        el("div", { class: "registration-pass__eyebrow", text: "Регистрация" }),
        el("h1", { class: "registration-pass__title", text: data.event.title }),
        el("p", { class: "registration-pass__meta", text: eventMeta(data.event) }),
        el("div", { class: "registration-pass__closed" }, [
          el("strong", { text: "Регистрация закрыта" }),
          el("p", { text: data.message || "Ссылка больше не действует." }),
        ]),
      ]),
    );
    return;
  }

  const name = el("input", {
    id: "registration-name",
    type: "text",
    maxlength: "120",
    autocomplete: "name",
    required: true,
    placeholder: "Иван Петров",
  });
  const phone = el("input", {
    id: "registration-phone",
    type: "tel",
    maxlength: "40",
    autocomplete: "tel",
    inputmode: "tel",
    placeholder: "+7 …",
  });
  const error = el("p", {
    class: "form-error",
    id: "registration-error",
    role: "alert",
    hidden: true,
  });
  const submit = el("button", {
    class: "btn btn--primary registration-pass__button",
    type: "submit",
    text: "Зарегистрироваться",
  });

  const form = el("form", { class: "registration-pass__form", novalidate: true }, [
    el("label", { class: "registration-pass__field" }, [
      el("span", { text: "Имя и фамилия" }),
      name,
    ]),
    el("label", { class: "registration-pass__field" }, [
      el("span", { text: "Телефон" }),
      phone,
    ]),
    error,
    submit,
  ]);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const guestName = name.value.trim();
    if (!guestName) {
      error.textContent = "Укажите имя.";
      error.hidden = false;
      name.focus();
      return;
    }
    submit.disabled = true;
    try {
      const result = await call(
        `/api/public/registration/${encodeURIComponent(token)}`,
        {
          method: "POST",
          body: { name: guestName, phone: phone.value.trim() },
        },
      );
      successState(result);
    } catch (requestError) {
      error.textContent = requestError.message;
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });

  const remaining = data.maxRegistrations !== null
    ? `Осталось мест: ${data.remaining} из ${data.maxRegistrations}`
    : "";
  clear(host).append(
    el("section", { class: "registration-pass" }, [
      el("div", { class: "registration-pass__eyebrow", text: "Регистрация" }),
      el("h1", { class: "registration-pass__title", text: data.event.title }),
      el("p", { class: "registration-pass__meta", text: eventMeta(data.event) }),
      el("p", {
        class: "registration-pass__sub",
        text: "Заполните имя и телефон — мы создадим персональное приглашение с QR-кодом.",
      }),
      remaining
        ? el("p", { class: "registration-pass__remaining", text: remaining })
        : null,
      form,
    ]),
  );
}

async function load() {
  try {
    const data = await call(
      `/api/public/registration/${encodeURIComponent(token)}`,
    );
    render(data);
  } catch (error) {
    clear(host).append(errorState(
      "Ссылка недействительна",
      error.message || "Попросите организатора создать новую ссылку.",
    ));
  }
}

load();
