"""Сценарий записи видео: открыть страницу, найти запрос, кликнуть первый результат."""
import math
import os
import random
import re
import sys
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PATH_TO_VIDEO_FOLDER = "./video"

# DuckDuckGo отвечает страницей ошибки 418 на поиск из headless Chromium, поэтому окно видимое.
HEADLESS = False
VIDEO_SIZE = {"width": 1280, "height": 720}

SEL_SEARCH_BOX = 'textarea[name="q"]'
SEL_SEARCH_BUTTON = 'form button[type="submit"]'
SEL_FIRST_RESULT = 'a[data-testid="result-title-a"]'

PAGE_TIMEOUT_MS = 30_000
TARGET_TIMEOUT_MS = 3_000
RESULTS_TIMEOUT_MS = 45_000
RIPPLE_MS = 600
CALLOUT_FADE_MS = 2_000
CALLOUT_HOLD_MS = 1_500
CALLOUT_SETTLE_MS = 400
MOVE_SPEED_PX_PER_S = 500
MOVE_TICK_MS = 8
AIM_PAUSE_MS = 200
CURSOR_START = (80, 80)

# Оверлей живёт в shadow DOM, чтобы CSS сайта на него не влиял. Он собран через DOM API
# и Web Animations: innerHTML и <style> могут быть запрещены политиками CSP сайта.
OVERLAY_SCRIPT = r"""
(() => {
  if (window.top !== window || window.__qsOverlay) return;

  const RIPPLE_MS = __RIPPLE_MS__;
  const CALLOUT_FADE_MS = __CALLOUT_FADE_MS__;
  const CALLOUT_HOLD_MS = __CALLOUT_HOLD_MS__;
  const HIGHLIGHT_RADIUS = 32;
  const TIP = 2;  // координата острия стрелки внутри SVG
  const SVG_NS = "http://www.w3.org/2000/svg";

  const host = document.createElement("div");
  host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483647;" +
                       "pointer-events:none;overflow:hidden;";
  const root = host.attachShadow({ mode: "closed" });

  const callouts = document.createElement("div");
  root.appendChild(callouts);

  const highlight = document.createElement("div");
  highlight.style.cssText = "position:absolute;left:0;top:0;display:none;border-radius:50%;" +
                            "background:rgba(255,235,0,.45);" +
                            `width:${HIGHLIGHT_RADIUS * 2}px;height:${HIGHLIGHT_RADIUS * 2}px;`;
  root.appendChild(highlight);

  const ripples = document.createElement("div");
  root.appendChild(ripples);

  const arrow = document.createElementNS(SVG_NS, "svg");
  arrow.setAttribute("width", "32");
  arrow.setAttribute("height", "32");
  arrow.setAttribute("viewBox", "0 0 32 32");
  arrow.style.cssText = "position:absolute;left:0;top:0;display:none;";
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", "M2 2 V27 L8.5 21 L13 30.5 L18 28.2 L13.6 19.2 H22.5 Z");
  path.setAttribute("fill", "#fff");
  path.setAttribute("stroke", "#000");
  path.setAttribute("stroke-width", "2");
  path.setAttribute("stroke-linejoin", "round");
  arrow.appendChild(path);
  root.appendChild(arrow);

  let pointerSeen = false;

  function setPos(x, y) {
    highlight.style.display = arrow.style.display = "block";
    highlight.style.transform =
      `translate(${x - HIGHLIGHT_RADIUS}px, ${y - HIGHLIGHT_RADIUS}px)`;
    arrow.style.transform = `translate(${x - TIP}px, ${y - TIP}px)`;
  }

  function ripple(x, y) {
    const wave = document.createElement("div");
    wave.style.cssText = "position:absolute;box-sizing:border-box;border-radius:50%;" +
                         "border:4px solid #d50000;" +
                         `width:${HIGHLIGHT_RADIUS * 2}px;height:${HIGHLIGHT_RADIUS * 2}px;` +
                         `left:${x - HIGHLIGHT_RADIUS}px;top:${y - HIGHLIGHT_RADIUS}px;`;
    ripples.appendChild(wave);
    wave.animate(
      [
        { transform: "scale(.1)", opacity: 1 },
        { transform: "scale(.8)", opacity: 1, offset: 0.65 },
        { transform: "scale(1)", opacity: 0 },
      ],
      { duration: RIPPLE_MS, fill: "forwards" }
    ).onfinish = () => wave.remove();
  }

  async function showCallout(text, bgColor, textColor, fade) {
    const box = document.createElement("div");
    box.style.cssText = "position:absolute;bottom:32px;left:50%;transform:translateX(-50%);" +
                        "width:max-content;max-width:80vw;padding:10px 18px;border-radius:6px;" +
                        "box-shadow:0 2px 10px rgba(0,0,0,.35);white-space:pre-wrap;" +
                        "font:16pt/1.3 Arial,Helvetica,sans-serif;";
    box.style.background = bgColor;
    box.style.color = textColor;
    box.textContent = text;
    callouts.appendChild(box);
    const fadeTo = (from, to) => box.animate(
      [{ opacity: from }, { opacity: to }],
      { duration: CALLOUT_FADE_MS, fill: "forwards" }
    ).finished;
    if (fade) await fadeTo(0, 1);
    await new Promise((resolve) => setTimeout(resolve, CALLOUT_HOLD_MS));
    if (fade) await fadeTo(1, 0);
    box.remove();
  }

  // capture на window: сработает раньше обработчиков сайта и не зависит от stopPropagation.
  window.addEventListener("mousemove", (e) => {
    pointerSeen = true;
    setPos(e.clientX, e.clientY);
  }, true);
  window.addEventListener("mousedown", (e) => ripple(e.clientX, e.clientY), true);

  window.__qsOverlay = { showCallout };

  // Новый документ начинается без курсора: восстанавливаем позицию из Python сразу,
  // не дожидаясь следующего движения мыши.
  window.__qsGetCursor().then(([x, y]) => { if (!pointerSeen) setPos(x, y); });

  // В момент запуска documentElement может ещё не существовать, а сайт может его заменить.
  const attach = () => {
    if (!host.isConnected && document.documentElement) {
      document.documentElement.appendChild(host);
    }
  };
  attach();
  new MutationObserver(attach).observe(document, { childList: true });
  setInterval(attach, 500);
})();
""".replace("__RIPPLE_MS__", str(RIPPLE_MS)) \
   .replace("__CALLOUT_FADE_MS__", str(CALLOUT_FADE_MS)) \
   .replace("__CALLOUT_HOLD_MS__", str(CALLOUT_HOLD_MS))

# Сигнатура ShowCallout задана заданием и не содержит страницы, поэтому страница текущего сценария
# хранится здесь.
_active_page = None


def ShowCallout(text, bgColor="#FFEB3B", textColor="#000000", fade=True):
    """Показывает плашку с текстом и ждёт её исчезновения."""
    if _active_page is None:
        raise RuntimeError("ShowCallout can only be used while a scenario is running")
    _active_page.evaluate(
        "([text, bg, color, fade]) => window.__qsOverlay.showCallout(text, bg, color, fade)",
        [text, bgColor, textColor, fade],
    )
    # Сразу после исчезновения плашки запись отстаёт примерно на 0,2 с,
    # и начало следующего движения курсора попало бы в видео скачком.
    _active_page.wait_for_timeout(CALLOUT_SETTLE_MS)


class _Cursor:
    """Настоящая мышь Playwright; SVG-стрелка следует за ней по событиям mousemove."""

    def __init__(self):
        self.page = None
        self.x, self.y = CURSOR_START

    def position(self):
        return [self.x, self.y]

    def move_to(self, x, y):
        start_x, start_y = self.x, self.y
        dist = math.hypot(x - start_x, y - start_y)
        if dist < 1:
            return
        duration = dist / MOVE_SPEED_PX_PER_S

        started = time.monotonic()
        progress = 0.0
        while progress < 1:
            progress = min((time.monotonic() - started) / duration, 1)
            self.x = start_x + (x - start_x) * progress
            self.y = start_y + (y - start_y) * progress
            self.page.mouse.move(self.x, self.y)
            self.page.wait_for_timeout(MOVE_TICK_MS)

    def click(self, locator):
        self.move_to(*_center(locator))
        self.page.wait_for_timeout(AIM_PAUSE_MS)
        # Пока курсор летел, цель могла сместиться: список подсказок меняет высоту.
        self.move_to(*_center(locator))
        # Кнопка удерживается на время ripple: mouse.up запускает навигацию,
        # которая иначе уничтожила бы анимацию вместе с документом.
        self.page.mouse.down()
        self.page.wait_for_timeout(RIPPLE_MS)
        self.page.mouse.up()


def _center(locator):
    # Берём первую строку, а не bounding box: у ссылки в две строки центр бокса может быть мимо текста.
    return locator.evaluate(
        "el => { const r = el.getClientRects()[0];"
        " return [r.left + r.width / 2, r.top + r.height / 2]; }",
        timeout=TARGET_TIMEOUT_MS,
    )


def _is_navigation_error(exc):
    message = str(exc).lower()
    return "context was destroyed" in message or "because of a navigation" in message


def _wait_for_first_result(page):
    """Ждёт первый результат, переживая навигацию, и возвращает его локатор."""
    deadline = time.monotonic() + RESULTS_TIMEOUT_MS / 1000
    link = page.locator(SEL_FIRST_RESULT).first
    while True:
        remaining_ms = max((deadline - time.monotonic()) * 1000, 1)
        try:
            link.wait_for(state="visible", timeout=remaining_ms)
            return link
        except PlaywrightError as exc:
            if not _is_navigation_error(exc) or time.monotonic() >= deadline:
                raise
            page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)


def _type_like_human(page, text):
    for char in text:
        page.keyboard.type(char)
        pause = random.randint(60, 170)
        if char == " ":
            pause += random.randint(60, 160)
        page.wait_for_timeout(pause)


def _submit_search(page, cursor, search_box, query):
    # На главной DuckDuckGo видимая кнопка есть только в открытом списке подсказок: его закрывает
    # потеря фокуса, и он не появляется, если сервис подсказок не ответил.
    button = page.locator(SEL_SEARCH_BUTTON).locator("visible=true").first
    try:
        cursor.click(button)
        return
    except PlaywrightTimeoutError:
        pass
    cursor.click(search_box)
    page.keyboard.press("End")
    page.keyboard.press("Backspace")
    page.keyboard.type(query[-1])
    page.wait_for_timeout(600)
    try:
        cursor.click(button)
    except PlaywrightTimeoutError:
        print("Search button not available, submitting the query with Enter", file=sys.stderr)
        page.keyboard.press("Enter")


def _run_sample_behavior(page, cursor, url, query, company_name):
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    search_box = page.locator(SEL_SEARCH_BOX).locator("visible=true").first
    search_box.wait_for(state="visible", timeout=PAGE_TIMEOUT_MS)

    ShowCallout(company_name)

    cursor.click(search_box)
    page.wait_for_timeout(250)
    _type_like_human(page, query)
    page.wait_for_timeout(400)

    _submit_search(page, cursor, search_box, query)

    first_result = _wait_for_first_result(page)
    page.wait_for_timeout(800)
    results_url = page.url
    cursor.click(first_result)

    # Клик засчитываем, только если он действительно увёл со страницы результатов.
    page.wait_for_url(lambda u: u != results_url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)


def _safe_filename(name):
    """Название компании приходит из CSV: не даём ему задавать путь или ломать имя файла."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:150].strip(" .")
    if not cleaned:
        return "video"
    if re.fullmatch(r"(?i)con|prn|aux|nul|com\d|lpt\d", cleaned):
        return f"_{cleaned}"
    return cleaned


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    global _active_page

    query = f"Magento developer in {city}".strip()

    try:
        folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"{_safe_filename(company_name)}.webm")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=HEADLESS)
            try:
                context = browser.new_context(
                    viewport=VIDEO_SIZE,
                    record_video_dir=folder,
                    record_video_size=VIDEO_SIZE,
                )
                cursor = _Cursor()
                context.expose_function("__qsGetCursor", cursor.position)
                context.add_init_script(OVERLAY_SCRIPT)
                page = context.new_page()
                cursor.page = page
                page.mouse.move(*CURSOR_START)
                _active_page = page
                video = page.video
                _run_sample_behavior(page, cursor, url, query, company_name)
                context.close()
                os.replace(video.path(), target)
                return target
            finally:
                _active_page = None
                browser.close()
    except Exception as exc:
        print(f"CaptureVideo_Magento failed for {company_name!r}: {exc}", file=sys.stderr)
        return None
