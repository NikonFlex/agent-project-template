/*
 * window.__agent — экран страницы как JSON для агента и сценариев.
 *
 * Адаптировано из agent-braille (https://github.com/blurman-ai/agent-braille, Apache-2.0),
 * коммит 2a778d0: runtime/{install,inspect,busy,errorsLog,screenState,types}.ts и проверка
 * видимости visualProblems из lib/browser.mjs; ответ screen() собран так же, как observe()
 * из lib/browser.mjs. Лицензия и список правок — braille/LICENSE, braille/NOTICE.
 *
 * Изменено: TypeScript переписан на простой JS одним файлом без сборки и зависимостей; screen()
 *   отдаёт готовый ответ шага (экран, элементы, поля, подписи, списки, заголовки, слои, факты,
 *   текст), а не сырой ScreenInfo; visual() работает в самой странице и отдаёт объекты
 *   {kind, text, message} по-русски; busy() сам считает fetch, XMLHttpRequest и переходы страницы
 *   (в оригинале источники объявляет код приложения — здесь они только добавка:
 *   registerBusySource); не перенесены WebMCP-описания tools, переключатели switches и
 *   queryScreenState (React Query); отменённый fetch (AbortError) ошибкой запроса не считается;
 *   цвета для visual() приводятся к sRGB через холст, а не читаются числами из строки rgb().
 *
 * Допуски ниже — протокол проверки, а не настройки оформления: страница с этим файлом не читает
 * config/ (файл отдаётся как есть, без шаблона).
 */
(function () {
  "use strict";
  if (window.__agent) return;

  const VERSION = "braille.v1";
  const SCREEN_PREFIX = "screen.";
  const FACT_ATTR = "data-fact";
  const FACT_VALUE_ATTR = "data-fact-value";
  // testid кнопок и ссылок — «<область>.<действие>»; остальное попадает в labels по подписи
  const TESTID_SCHEME = /^[a-z][a-z0-9-]*(\.[a-z0-9-]+)+$/;
  const FIELD_ROLES = ["textbox", "combobox", "searchbox"];
  const LABEL_ROLES = ["button", "link", "tab", "menuitem", "checkbox", "radio", "switch"];
  const OVERLAY_SELECTOR =
    '[role="dialog"],[role="alertdialog"],[role="listbox"],[role="menu"],dialog[open]';
  const LIMITS = { errors: 200, text: 1500, name: 80, options: 30, labels: 30, headings: 10 };
  // Страница ушла с места: если уйти не вышло (скачивание), флаг снимается сам
  const NAVIGATION_GIVE_UP_MS = 10000;
  const VISUAL = {
    maxFindings: 12,
    minContrast: 1.3,
    minOpacity: 0.05,
    minBackgroundAlpha: 0.5,
    clipTolerancePx: 1,
    hiddenFontPx: 1,
    farOffscreenPx: 2000,
    shownText: 40,
  };

  const IMPLICIT_ROLES = {
    button: "button", select: "combobox", textarea: "textbox", nav: "navigation", dialog: "dialog",
    li: "listitem", ul: "list", ol: "list", tr: "row", td: "cell", table: "table", main: "main",
    img: "img", progress: "progressbar", h1: "heading", h2: "heading", h3: "heading",
    h4: "heading", h5: "heading", h6: "heading",
  };
  const INPUT_ROLES = {
    checkbox: "checkbox", radio: "radio", range: "slider", button: "button", submit: "button",
    reset: "button", search: "searchbox",
  };
  const ELEMENT_SELECTOR = [
    "[data-testid]", "button", "a[href]", "input", "textarea", "select", '[role="button"]',
    '[role="tab"]', '[role="link"]', '[role="checkbox"]', '[role="menuitem"]', '[role="option"]',
    '[role="switch"]', '[role="alert"]', "h1", "h2", "h3",
  ].join(",");

  // ------------------------------------------------------------------ имена, роли, рамки

  function squash(text) {
    return String(text ?? "").replace(/\s+/g, " ").trim();
  }

  function roleOf(el) {
    const explicit = (el.getAttribute("role") || "").trim().split(/\s+/)[0];
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === "a") return el.hasAttribute("href") ? "link" : null;
    if (tag === "input") {
      return INPUT_ROLES[(el.getAttribute("type") || "text").toLowerCase()] || "textbox";
    }
    return IMPLICIT_ROLES[tag] || null;
  }

  function labelFor(el) {
    if (el.id) {
      const label = Array.from(document.querySelectorAll("label")).find((l) => l.htmlFor === el.id);
      if (squash(label?.textContent)) return squash(label.textContent);
    }
    const wrapping = el.closest("label");
    return wrapping && wrapping !== el ? squash(wrapping.textContent) : "";
  }

  /** Имя в дереве доступности: aria-labelledby, aria-label, подпись, alt, текст, title/placeholder. */
  function nameOf(el) {
    const labelledBy = el.getAttribute("aria-labelledby");
    const joined = (labelledBy || "").split(/\s+/)
      .map((id) => squash(document.getElementById(id)?.textContent)).filter(Boolean).join(" ");
    const named = joined || squash(el.getAttribute("aria-label")) || labelFor(el)
      || squash(el.getAttribute("alt"));
    if (named) return named;
    const tag = el.tagName.toLowerCase();
    const content = ["input", "textarea", "select"].includes(tag) ? "" : squash(el.textContent);
    return content || squash(el.getAttribute("title")) || squash(el.getAttribute("placeholder"));
  }

  function boolAttr(el, name) {
    const raw = el.getAttribute(name);
    return raw == null ? undefined : raw === "true";
  }

  function mixedAttr(el, name) {
    const raw = el.getAttribute(name);
    return raw === "mixed" ? "mixed" : raw === "true";
  }

  function ariaStateOf(el) {
    const state = {};
    const set = (key, value) => { if (value !== undefined) state[key] = value; };
    set("busy", boolAttr(el, "aria-busy"));
    if (el.getAttribute("aria-invalid") != null) state.invalid = el.getAttribute("aria-invalid") !== "false";
    const ariaDisabled = boolAttr(el, "aria-disabled");
    if ("disabled" in el || ariaDisabled !== undefined) {
      state.disabled = el.disabled === true || ariaDisabled === true || el.hasAttribute("data-disabled");
    }
    set("selected", boolAttr(el, "aria-selected"));
    const current = el.getAttribute("aria-current");
    if (current != null && current !== "false") state.current = current;
    set("expanded", boolAttr(el, "aria-expanded"));
    if (el.getAttribute("aria-checked") != null) state.checked = mixedAttr(el, "aria-checked");
    else if (el instanceof HTMLInputElement && ["checkbox", "radio"].includes(el.type)) {
      state.checked = el.checked;
    }
    if (el.getAttribute("aria-pressed") != null) state.pressed = mixedAttr(el, "aria-pressed");
    return state;
  }

  function round(n) {
    return Math.round(n * 10) / 10;
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { x: round(r.x), y: round(r.y), w: round(r.width), h: round(r.height) };
  }

  function inViewport(rect) {
    return rect.w > 0 && rect.h > 0 && rect.x < innerWidth && rect.y < innerHeight
      && rect.x + rect.w > 0 && rect.y + rect.h > 0;
  }

  // ------------------------------------------------------------------ перекрытия

  function describeOccluder(el) {
    return {
      testid: el.getAttribute("data-testid"),
      role: roleOf(el),
      name: nameOf(el).slice(0, LIMITS.name),
      tag: el.tagName.toLowerCase(),
    };
  }

  /**
   * Что лежит поверх элемента: elementFromPoint в центре и в четырёх углах рамки (с отступом
   * в пиксель). Сам элемент, его потомки и предки не в счёт: предок сверху — это обрезка.
   */
  function occludedByOf(el, rect) {
    if (rect.w <= 0 || rect.h <= 0) return [];
    const inset = Math.min(1, rect.w / 2, rect.h / 2);
    const points = [
      [rect.x + rect.w / 2, rect.y + rect.h / 2], [rect.x + inset, rect.y + inset],
      [rect.x + rect.w - inset, rect.y + inset], [rect.x + inset, rect.y + rect.h - inset],
      [rect.x + rect.w - inset, rect.y + rect.h - inset],
    ];
    const found = new Map();
    for (const [x, y] of points) {
      const hit = document.elementFromPoint(x, y);
      if (hit && hit !== el && !el.contains(hit) && !hit.contains(el) && !found.has(hit)) {
        found.set(hit, describeOccluder(hit));
      }
    }
    return Array.from(found.values());
  }

  /**
   * Что человек видит на месте элемента: поле нулевого размера (переключатель, чип) рисуется
   * подписью — рамка, видимость и перекрытие берутся с неё.
   */
  function visualBoxOf(el) {
    if (!(el instanceof HTMLInputElement)) return el;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return el;
    const label = (el.id && Array.from(document.querySelectorAll("label"))
      .find((l) => l.htmlFor === el.id)) || el.closest("label");
    return label || el;
  }

  // Значение пароля и ключей не уходит из страницы: ответ шага читает агент и пишет в файлы
  // сессии (имена полей — как field_names маскировки исполнителя, config/braille.json).
  const SECRET_MARK = "***";
  const SECRET_NAME = /password|passwd|pass|token|secret|cookie|authorization|api[_-]?key|csrf/i;

  function secretField(el) {
    return (el instanceof HTMLInputElement && el.type === "password")
      || SECRET_NAME.test(el.name || "") || SECRET_NAME.test(el.id || "");
  }

  function describeElement(el, indexOf) {
    const box = visualBoxOf(el);
    const rect = rectOf(box);
    const field = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
      || el instanceof HTMLSelectElement;
    return {
      ...(field ? { value: secretField(el) && el.value ? SECRET_MARK : el.value } : {}),
      domIndex: indexOf(el),
      testid: el.getAttribute("data-testid"),
      role: roleOf(el),
      name: nameOf(el),
      text: squash(el.textContent).slice(0, 200),
      aria: ariaStateOf(el),
      rect,
      inViewport: inViewport(rect),
      occludedBy: occludedByOf(box, rect),
      clipped: el.scrollWidth > el.clientWidth,
    };
  }

  /** Порядок элементов в документе: по нему проверяют, что один текст идёт раньше другого. */
  function domIndexer() {
    const order = new Map(Array.from(document.querySelectorAll("*"), (el, i) => [el, i]));
    return (el) => (order.has(el) ? order.get(el) : -1);
  }

  // ------------------------------------------------------------------ поиск

  function matchesTestid(actual, wanted) {
    if (actual == null) return false;
    return wanted.endsWith("*") ? actual.startsWith(wanted.slice(0, -1)) : actual === wanted;
  }

  function matchesQuery(el, query) {
    const lower = (s) => s.toLowerCase();
    if (query.testid !== undefined && !matchesTestid(el.getAttribute("data-testid"), query.testid)) {
      return false;
    }
    if (query.role !== undefined && roleOf(el) !== query.role) return false;
    if (query.name !== undefined && !lower(nameOf(el)).includes(lower(query.name))) return false;
    return query.text === undefined || lower(squash(el.textContent)).includes(lower(query.text));
  }

  function isRendered(el) {
    const style = getComputedStyle(el);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  /**
   * Элементы, о которых можно спросить, кроме спрятанных стилем. Запрос с text ищет по всем
   * элементам, не только интерактивным: так проверяют, что текст на экране есть или его нет;
   * из вложенных совпадений остаётся самое глубокое.
   */
  function queryElements(query) {
    if (query && query.text !== undefined) {
      const matched = Array.from(document.querySelectorAll("*")).filter((el) => matchesQuery(el, query));
      return matched.filter((el) => !matched.some((other) => other !== el && el.contains(other)))
        .filter(isRendered);
    }
    const all = Array.from(document.querySelectorAll(ELEMENT_SELECTOR)).filter(isRendered);
    return query ? all.filter((el) => matchesQuery(el, query)) : all;
  }

  function find(query) {
    const indexOf = domIndexer();
    return queryElements(query || {}).map((el) => describeElement(el, indexOf));
  }

  // ------------------------------------------------------------------ экран

  /** Корень экрана. Вложенных экранов несколько — берётся самый внутренний: его видит человек. */
  function screenRoot() {
    const roots = Array.from(document.querySelectorAll(`[data-testid^="${SCREEN_PREFIX}"]`));
    return roots.find((root) => !roots.some((other) => other !== root && root.contains(other))) || null;
  }

  function overlays() {
    return Array.from(document.querySelectorAll(OVERLAY_SELECTOR))
      .filter((el) => isRendered(el) && rectOf(el).w > 0 && rectOf(el).h > 0)
      .map((el) => ({
        role: el.getAttribute("role") || roleOf(el) || "",
        name: (nameOf(el) || el.querySelector("h1,h2,h3,h4")?.textContent || "").trim()
          .slice(0, LIMITS.name),
        testid: el.getAttribute("data-testid"),
      }));
  }

  /** Факты, которых не видно в элементах: режим, номер шага (data-fact + data-fact-value). */
  function factsOf(root) {
    const facts = {};
    const scopes = root ? [root, ...document.querySelectorAll('[role="dialog"]')] : [document];
    for (const scope of scopes) {
      for (const el of scope.querySelectorAll(`[${FACT_ATTR}]`)) {
        if (isRendered(el)) facts[el.getAttribute(FACT_ATTR)] = el.getAttribute(FACT_VALUE_ATTR) || "";
      }
    }
    return facts;
  }

  const labelText = (e) => String(e.name || e.text || "").trim().slice(0, LIMITS.name);
  const flag = (e) => (e.aria.disabled ? { disabled: true } : {});
  const unique = (items, key) => Array.from(new Map(items.map((i) => [key(i), i])).values());
  const distinct = (texts) => Array.from(new Set(texts.filter(Boolean)));

  function elementsOf(shown) {
    return shown.filter((e) => isSchemed(e) && !e.testid.startsWith(SCREEN_PREFIX)).map((e) => ({
      testid: e.testid,
      role: e.role,
      name: String(e.name || "").slice(0, LIMITS.name),
      ...flag(e),
      ...(e.occludedBy.length ? { occludedBy: e.occludedBy } : {}),
    }));
  }

  function isSchemed(e) {
    return Boolean(e.testid) && TESTID_SCHEME.test(e.testid);
  }

  function fieldsOf(shown) {
    const fields = shown.filter((e) => !isSchemed(e) && FIELD_ROLES.includes(e.role) && e.name.trim());
    return unique(fields.map((e) => ({
      field: e.name.trim().slice(0, LIMITS.name),
      role: e.role,
      ...flag(e),
      ...(e.value ? { value: e.value } : {}),
    })), (f) => f.field);
  }

  function labelsOf(shown) {
    const labels = shown.filter((e) => !isSchemed(e) && LABEL_ROLES.includes(e.role) && labelText(e));
    return unique(labels.map((e) => ({ label: labelText(e), role: e.role, ...flag(e) })), (l) => l.label)
      .slice(0, LIMITS.labels);
  }

  function visibleText(root) {
    return ((root || document.body).innerText || "").replace(/\n{2,}/g, "\n").trim()
      .slice(0, LIMITS.text);
  }

  /** Экран глазами агента: имя и состояние, кнопки с testid, поля, подписи, слои, факты, текст. */
  function screen() {
    const root = screenRoot();
    const shown = find().filter((e) => e.rect.w > 0 && e.rect.h > 0);
    const options = distinct(shown.filter((e) => !isSchemed(e) && e.role === "option").map(labelText));
    const headings = distinct(shown.filter((e) => e.role === "heading").map(labelText));
    const alerts = queryElements({ role: "alert" }).map((el) => squash(el.textContent)).filter(Boolean);
    const labels = labelsOf(shown);
    const layers = overlays();
    const facts = factsOf(root);
    const text = visibleText(root);
    return {
      screen: root ? {
        name: root.getAttribute("data-testid").slice(SCREEN_PREFIX.length),
        state: root.getAttribute("data-state"),
      } : null,
      url: location.href,
      elements: elementsOf(shown),
      fields: fieldsOf(shown),
      ...(options.length ? { options: options.slice(0, LIMITS.options) } : {}),
      ...(labels.length ? { labels } : {}),
      ...(headings.length ? { headings: headings.slice(0, LIMITS.headings) } : {}),
      ...(alerts.length ? { alerts } : {}),
      ...(layers.length ? { overlays: layers } : {}),
      ...(Object.keys(facts).length ? { facts } : {}),
      ...(text ? { text } : {}),
    };
  }

  // ------------------------------------------------------------------ взаимное положение

  function layout(a, b) {
    const ea = queryElements({ testid: a })[0];
    const eb = queryElements({ testid: b })[0];
    if (!ea || !eb) return null;
    const ra = rectOf(ea);
    const rb = rectOf(eb);
    const gapX = Math.max(0, Math.max(ra.x, rb.x) - Math.min(ra.x + ra.w, rb.x + rb.w));
    const gapY = Math.max(0, Math.max(ra.y, rb.y) - Math.min(ra.y + ra.h, rb.y + rb.h));
    const overlapW = Math.min(ra.x + ra.w, rb.x + rb.w) - Math.max(ra.x, rb.x);
    const overlapH = Math.min(ra.y + ra.h, rb.y + rb.h) - Math.max(ra.y, rb.y);
    const overlaps = overlapW > 0 && overlapH > 0;
    return {
      a: ra, b: rb,
      aAbove: ra.y + ra.h <= rb.y, aBelow: ra.y >= rb.y + rb.h,
      aLeftOf: ra.x + ra.w <= rb.x, aRightOf: ra.x >= rb.x + rb.w,
      gapX: round(gapX), gapY: round(gapY), distance: round(Math.hypot(gapX, gapY)),
      overlaps, overlapArea: overlaps ? round(overlapW * overlapH) : 0,
    };
  }

  // ------------------------------------------------------------------ действия

  function setNativeValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }

  function click(el, info) {
    if (info.occludedBy.length > 0) return { ok: false, error: "occluded", occludedBy: info.occludedBy };
    if (el.focus) el.focus();
    for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup"]) {
      el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
    }
    el.click();
    return { ok: true };
  }

  function write(el, action, value) {
    const isSelect = el instanceof HTMLSelectElement;
    if (action === "select") {
      if (!isSelect) return { ok: false, error: "unsupported" };
      setNativeValue(el, value ?? "");
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return { ok: true };
    }
    if (isSelect || !(action === "type" || action === "clear")) return { ok: false, error: "unsupported" };
    el.focus();
    setNativeValue(el, action === "clear" ? "" : (value ?? ""));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return { ok: true };
  }

  /** Действие тем же путём, что у человека: фокус, цепочка событий мыши, значение + input/change. */
  function act(testid, action, value) {
    const el = queryElements({ testid })[0];
    if (!el) return { ok: false, error: "not_found" };
    const info = describeElement(el, () => -1);
    if (info.aria.disabled) return { ok: false, error: "disabled" };
    if (action === "focus") { el.focus(); return { ok: true }; }
    if (action === "click") return click(el, info);
    const field = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
      || el instanceof HTMLSelectElement;
    return field ? write(el, action, value) : { ok: false, error: "unsupported" };
  }

  // ------------------------------------------------------------------ занятость

  const busySources = new Map();
  const pending = { fetch: 0, xhr: 0 };
  let navigating = false;
  let navigationTimer = 0;

  /** Свой JS объявляет, чем занят: source() возвращает число незавершённого, 0 — покой. */
  function registerBusySource(name, source) {
    busySources.set(name, source);
  }

  function countOf(name, source) {
    try {
      return Number(source()) || 0;
    } catch (error) {
      record({ kind: "exception", message: `источник занятости «${name}»: ${stringify(error)}` });
      return 0;
    }
  }

  /** Конечная анимация — незавершённое дело; бесконечная (индикатор) — просто вид элемента. */
  function runningAnimations() {
    const finite = (a) => Number.isFinite(a.effect?.getComputedTiming().endTime);
    return (document.getAnimations?.() || []).filter((a) => a.playState === "running" && finite(a)).length;
  }

  /** Незавершённое на странице по источникам; пустой объект — страница в покое. */
  function busy() {
    const counts = {
      fetch: pending.fetch,
      xhr: pending.xhr,
      navigation: navigating ? 1 : 0,
      animations: runningAnimations(),
    };
    for (const [name, source] of busySources) counts[name] = countOf(name, source);
    return Object.fromEntries(Object.entries(counts).filter(([, count]) => count > 0));
  }

  function watchNavigation() {
    const start = () => {
      navigating = true;
      clearTimeout(navigationTimer);
      navigationTimer = setTimeout(() => { navigating = false; }, NAVIGATION_GIVE_UP_MS);
    };
    const staysHere = (event) => {
      const target = event.submitter?.formTarget || event.target.target;
      return !target || target === "_self";
    };
    window.addEventListener("beforeunload", start);
    // на этой стадии обработчики формы уже отработали: preventDefault значит «отправка свой JS»
    window.addEventListener("submit", (event) => {
      if (!event.defaultPrevented && staysHere(event)) start();
    });
    window.addEventListener("pageshow", () => { navigating = false; });
  }

  // ------------------------------------------------------------------ ошибки

  let entries = [];

  function stringify(value) {
    if (value instanceof Error) return `${value.name}: ${value.message}`;
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value) ?? String(value);
    } catch {
      return String(value);
    }
  }

  function record(entry) {
    entries.push({ ...entry, path: location.pathname, at: Date.now() });
    if (entries.length > LIMITS.errors) entries.shift();
  }

  /** detail.code (или code) из JSON-тела отказа API; не JSON — кода нет. */
  function errorCodeOf(text) {
    try {
      const body = JSON.parse(text);
      const code = body?.detail?.code ?? body?.code;
      return typeof code === "string" && code ? code.slice(0, 80) : undefined;
    } catch {
      return undefined;
    }
  }

  function recordRequest(method, url, status, code, reason) {
    const tail = reason || `${status}${code ? ` ${code}` : ""}`;
    record({
      kind: "request",
      message: `${method} ${url} -> ${tail}`,
      method, url, status,
      ...(code ? { code } : {}),
    });
  }

  function watchPage() {
    const original = console.error;
    console.error = function (...args) {
      record({ kind: "console", message: args.map(stringify).join(" ").slice(0, 500) });
      return original.apply(console, args);
    };
    window.addEventListener("error", (event) => {
      record({ kind: "exception", message: String(event.message).slice(0, 500) });
    });
    window.addEventListener("unhandledrejection", (event) => {
      record({ kind: "rejection", message: stringify(event.reason).slice(0, 500) });
    });
  }

  function watchFetch() {
    const original = window.fetch;
    if (typeof original !== "function") return;
    window.fetch = async function (input, init) {
      const request = input instanceof Request ? input : null;
      const method = String(init?.method || request?.method || "GET").toUpperCase();
      const url = request ? request.url : String(input);
      pending.fetch += 1;
      try {
        const response = await original.apply(window, arguments);
        if (response.status >= 400) {
          const body = await response.clone().text().catch(() => "");
          recordRequest(method, url, response.status, errorCodeOf(body));
        }
        return response;
      } catch (error) {
        if (error?.name !== "AbortError") recordRequest(method, url, 0, undefined, stringify(error));
        throw error;
      } finally {
        pending.fetch -= 1;
      }
    };
  }

  function reportXhr(xhr) {
    const req = xhr.__agentReq;
    if (!req || (xhr.status > 0 && xhr.status < 400)) return;
    const text = xhr.responseType === "" || xhr.responseType === "text" ? xhr.responseText : "";
    recordRequest(req.method, req.url, xhr.status, errorCodeOf(text));
  }

  function watchXhr() {
    const proto = window.XMLHttpRequest?.prototype;
    if (!proto) return;
    const open = proto.open;
    const send = proto.send;
    proto.open = function (method, url) {
      this.__agentReq = { method: String(method).toUpperCase(), url: String(url) };
      return open.apply(this, arguments);
    };
    proto.send = function () {
      const finished = () => { pending.xhr -= 1; reportXhr(this); };
      pending.xhr += 1;
      this.addEventListener("loadend", finished, { once: true });
      try {
        return send.apply(this, arguments);
      } catch (error) {
        this.removeEventListener("loadend", finished);
        pending.xhr -= 1;
        throw error;
      }
    };
  }

  /** Ошибки страницы, отклонённые промисы и упавшие запросы с последнего чтения с clear. */
  function errors(options) {
    const out = entries.slice();
    if (options && options.clear) entries = [];
    return out;
  }

  // ------------------------------------------------------------------ что человек не увидит

  const colors = new Map();
  let probe = null;

  /**
   * Любой цвет CSS как [r, g, b, alpha 0..1]. getComputedStyle отдаёт не только rgb(): у сайта
   * заказчика цвета в oklch(), поэтому число из строки читать нельзя — цвет приводит к sRGB
   * холст (в оригинале разбирались только rgb/rgba).
   */
  function parseColor(text) {
    if (!colors.has(text)) {
      probe = probe || document.createElement("canvas").getContext("2d", { willReadFrequently: true });
      probe.clearRect(0, 0, 1, 1);
      probe.fillStyle = "#000";
      probe.fillStyle = text;
      probe.fillRect(0, 0, 1, 1);
      const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
      colors.set(text, [r, g, b, a / 255]);
    }
    return colors.get(text);
  }

  function backgroundOf(el) {
    for (let node = el; node; node = node.parentElement) {
      const [r, g, b, a = 1] = parseColor(getComputedStyle(node).backgroundColor);
      if (a > VISUAL.minBackgroundAlpha) return [r, g, b];
    }
    return [255, 255, 255];
  }

  function luminance(rgb) {
    const weights = [0.2126, 0.7152, 0.0722];
    return rgb.map((v) => {
      const c = v / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    }).reduce((sum, c, i) => sum + c * weights[i], 0);
  }

  function contrast(x, y) {
    const [hi, lo] = [luminance(x), luminance(y)].sort((p, q) => q - p);
    return (hi + 0.05) / (lo + 0.05);
  }

  function opacityOf(el) {
    let opacity = 1;
    for (let up = el; up; up = up.parentElement) opacity *= Number(getComputedStyle(up).opacity);
    return opacity;
  }

  /** Текст, который нельзя прочесть из-за самого оформления: прозрачность, цвет фона, обрезка. */
  function inkProblem(el, style) {
    if (opacityOf(el) < VISUAL.minOpacity) return { kind: "transparent", why: "полностью прозрачен" };
    const [r, g, b] = parseColor(style.color);
    if (contrast([r, g, b], backgroundOf(el)) < VISUAL.minContrast) {
      return { kind: "background", why: "цвет текста совпадает с фоном" };
    }
    if (el.scrollWidth > el.clientWidth + VISUAL.clipTolerancePx && style.overflowX !== "visible") {
      return { kind: "clipped", why: `обрезан: видно ${el.clientWidth} из ${el.scrollWidth} px` };
    }
    return null;
  }

  /** Предки с overflow: текст почти целиком за краем такого предка; scrolls — страницу можно прокрутить. */
  function ancestorsOf(el, box) {
    let scrolls = false;
    for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
      const over = getComputedStyle(up).overflowY;
      if ((over === "auto" || over === "scroll") && up.scrollHeight > up.clientHeight) scrolls = true;
      if (over !== "hidden" && over !== "clip") continue;
      const edge = up.getBoundingClientRect();
      const shownWidth = Math.min(box.right, edge.right) - Math.max(box.left, edge.left);
      const shownHeight = Math.min(box.bottom, edge.bottom) - Math.max(box.top, edge.top);
      if (shownWidth < box.width / 2 || shownHeight < box.height / 2) return { outside: true, scrolls };
    }
    return { outside: false, scrolls };
  }

  function pageScrolls() {
    const hidden = (el) => getComputedStyle(el).overflowY === "hidden";
    return !hidden(document.documentElement) && !hidden(document.body)
      && document.documentElement.scrollHeight > innerHeight;
  }

  function offscreen(box, scrolls) {
    return box.right <= 0 || box.left >= innerWidth + VISUAL.farOffscreenPx
      || (box.top >= innerHeight && !pageScrolls() && !scrolls);
  }

  function coveringName(hit) {
    const testid = hit.getAttribute("data-testid");
    const first = hit.classList[0] ? `.${hit.classList[0]}` : "";
    return `${hit.tagName.toLowerCase()}${first}${testid ? `[${testid}]` : ""}`;
  }

  /** Текст целиком на экране, но поверх него лежит чужой элемент (при открытом диалоге не ищем). */
  function coveredBy(el, box, modal) {
    const onScreen = box.top >= 0 && box.bottom <= innerHeight && box.left >= 0 && box.right <= innerWidth;
    if (modal || !onScreen) return null;
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return hit && !el.contains(hit) && !hit.contains(el) ? coveringName(hit) : null;
  }

  /** Почему текст элемента не прочесть: { kind, why, by? } или null. Проверки идут по порядку. */
  function unreadable(el, modal) {
    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();
    if (parseFloat(style.fontSize) <= VISUAL.hiddenFontPx) {
      return { kind: "font", why: `шрифт ${style.fontSize}` };
    }
    if (box.width <= 1 || box.height <= 1) return null;
    const ink = inkProblem(el, style);
    if (ink) return ink;
    const around = ancestorsOf(el, box);
    if (around.outside) return { kind: "container", why: "почти целиком за краем своего контейнера" };
    if (offscreen(box, around.scrolls)) return { kind: "screen", why: "за краем экрана, прокрутки нет" };
    const by = coveredBy(el, box, modal);
    return by ? { kind: "covered", why: `закрыт элементом ${by}`, by } : null;
  }

  function* textHolders() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const seen = new Set();
    while (walker.nextNode()) {
      const text = squash(walker.currentNode.textContent);
      const el = walker.currentNode.parentElement;
      const skipped = !text || !el || seen.has(el) || ["SCRIPT", "STYLE", "NOSCRIPT"].includes(el.tagName);
      if (skipped) continue;
      seen.add(el);
      if (el.getClientRects().length && isRendered(el)) yield { el, text };
    }
  }

  function namesMismatch(findings) {
    const selector = "button[aria-label], a[aria-label], [role=button][aria-label]";
    for (const control of document.querySelectorAll(selector)) {
      const shown = squash(control.innerText).toLowerCase();
      const named = control.getAttribute("aria-label").toLowerCase();
      const differs = shown.length > 1 && control.getClientRects().length && !named.includes(shown);
      if (differs && findings.length < VISUAL.maxFindings) {
        const text = shown.slice(0, VISUAL.shownText);
        findings.push({
          kind: "name", text,
          message: `имя в дереве «${named.slice(0, VISUAL.shownText)}» не совпадает с видимым «${text}»`,
        });
      }
    }
  }

  /** Чего не видит человек, хотя в дереве доступности оно есть. Читается со страницы, без разметки. */
  function visual() {
    const findings = [];
    const modal = Array.from(document.querySelectorAll("[role=dialog], [aria-modal=true]"))
      .some((d) => d.getClientRects().length);
    for (const { el, text } of textHolders()) {
      if (findings.length >= VISUAL.maxFindings) break;
      const problem = unreadable(el, modal);
      if (!problem) continue;
      const shown = text.slice(0, VISUAL.shownText);
      findings.push({
        kind: problem.kind, text: shown, ...(problem.by ? { by: problem.by } : {}),
        message: `человек не прочтёт «${shown}»: ${problem.why}`,
      });
    }
    namesMismatch(findings);
    return findings;
  }

  // ------------------------------------------------------------------ включение

  watchPage();
  watchFetch();
  watchXhr();
  watchNavigation();

  window.__agent = {
    version: VERSION,
    screen,
    busy,
    find,
    act,
    layout,
    visual,
    errors,
    registerBusySource,
  };
})();
