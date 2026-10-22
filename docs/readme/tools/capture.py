"""Снимки экранов Fleet Console для README: установленный Chrome через Playwright (channel=chrome), 2x.
Нужен запущенный сервис на 127.0.0.1:8080 с подготовленной ревизией. Для каждого кадра пишется <name>.png
и <name>.json — прямоугольники элементов (в CSS px), по которым figures.py рисует стрелки.
Запуск: uv run --with playwright --with pillow python docs/readme/tools/capture.py <каталог кадров>
Кнопки, занимающие GPU («Запустить», «Запустить подъём»), скрипт не нажимает; «Превью и preflight» — да."""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8080"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "shots"); OUT.mkdir(exist_ok=True)
W = 1100
BOX_SELECTORS = ("button", "a", ".q-chip", ".q-field", ".q-expansion-item > .q-item", ".text-xl", ".text-lg",
                 ".q-checkbox", ".q-toggle", ".q-table", ".q-table tbody tr", ".q-dialog .q-card", "header", "label", ".q-tree__node")
JS_BOXES = """(sels) => {
  const out = [];
  for (const sel of sels) for (const e of document.querySelectorAll(sel)) {
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const lab = e.querySelector('.q-field__label');
    const text = (lab ? lab.textContent : e.textContent).replace(/\\s+/g, ' ').trim().slice(0, 100);
    out.push({sel, text, x: r.x + scrollX, y: r.y + scrollY, w: r.width, h: r.height});
  }
  return out;
}"""

def boxes(page):
    return page.evaluate(JS_BOXES, list(BOX_SELECTORS))

def fit_height(page, min_h=900, max_h=4200):
    h = page.evaluate("""() => {
      let h = document.documentElement.scrollHeight;
      for (const c of document.querySelectorAll('.q-dialog .q-card')) h = Math.max(h, c.scrollHeight + 120);
      return h; }""")
    h = max(min_h, min(max_h, int(h) + 24))
    page.set_viewport_size({"width": page.viewport_size["width"], "height": h})
    page.wait_for_timeout(600)

def shot(page, name, fit=True):
    if fit: fit_height(page)
    page.screenshot(path=str(OUT / f"{name}.png"), full_page=False)
    (OUT / f"{name}.json").write_text(json.dumps(boxes(page), ensure_ascii=False, indent=0))
    print("shot", name, page.viewport_size, flush=True)

def goto(page, path, wait=4000):
    page.set_viewport_size({"width": W, "height": 900})
    page.goto(BASE + path); page.wait_for_timeout(wait)

def field(page, label):
    return page.locator(f'.q-field:has(.q-field__label:text-is("{label}"))').first

def step(name, fn):
    try:
        fn(); 
    except Exception as exc:  # noqa: BLE001 — один кадр не должен ронять остальные
        print("FAIL", name, repr(exc)[:300], flush=True)

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    ctx = browser.new_context(viewport={"width": W, "height": 900}, device_scale_factor=2, color_scheme="light", locale="ru-RU")
    page = ctx.new_page()

    def revision():
        goto(page, "/"); shot(page, "revision")
        page.locator('.q-expansion-item:has-text("Агенты · harbor/agent") > .q-item').first.click(); page.wait_for_timeout(800)
        shot(page, "revision-catalog")
        page.locator('.q-chip:has-text("internal")').first.click(); page.wait_for_timeout(800)
        shot(page, "header-kube"); page.keyboard.press("Escape"); page.wait_for_timeout(400)
        page.locator('.q-chip:has-text("бастион")').first.click(); page.wait_for_timeout(800)
        shot(page, "header-bastion"); page.keyboard.press("Escape")
    step("revision", revision)

    def cluster():
        goto(page, "/cluster", 6000); shot(page, "cluster")
    step("cluster", cluster)

    def models():
        goto(page, "/models"); shot(page, "models")
        page.locator('button:has-text("Поднять модель")').first.click(); page.wait_for_timeout(1000)
        shot(page, "models-new"); page.keyboard.press("Escape")
    step("models", models)

    def runs():
        goto(page, "/runs"); shot(page, "runs")
        page.locator(".q-table tbody tr").first.click(); page.wait_for_timeout(2500)
        shot(page, "run-card")
        page.locator('.q-expansion-item:has-text("Что и где запущено") .q-item').first.click(); page.wait_for_timeout(600)
        page.locator('.q-expansion-item:has-text("подготовка: --print-config") .q-item').first.click(); page.wait_for_timeout(800)
        shot(page, "run-card-open"); page.keyboard.press("Escape")
    step("runs", runs)

    def gateway():
        """Проброс порта к шлюзу (kubectl port-forward): ждём «работает», снимаем, закрываем."""
        goto(page, "/models")
        page.locator('button:has-text("Пробросить порт")').first.click()
        for _ in range(12):
            page.wait_for_timeout(5000)
            if "работает" in page.locator('.q-card:has-text("Шлюз моделей")').first.inner_text(): break
        page.wait_for_timeout(1500)
        page.locator('.q-expansion-item:has-text("Сниппеты") .q-item').first.click(); page.wait_for_timeout(800)
        shot(page, "models-gateway")
        page.locator('button:has-text("Закрыть проброс")').first.click(); page.wait_for_timeout(1500)
    step("gateway", gateway)

    def run_form():
        goto(page, "/runs")
        page.locator('button:has-text("Запустить бенч")').first.click(); page.wait_for_timeout(1500)
        field(page, "Пресет").click(); page.wait_for_timeout(600)
        page.locator('.q-menu .q-item:has-text("qwen36-birdsql-smoke")').first.click(); page.wait_for_timeout(1000)
        shot(page, "run-form-empty")
        page.locator('button:has-text("Превью и preflight")').first.click()
        page.locator('text=/^Preflight/').first.wait_for(timeout=120000); page.wait_for_timeout(1500)
        shot(page, "run-form")
        page.locator('.q-expansion-item:has-text("Дополнительно") > .q-item').first.click(); page.wait_for_timeout(800)
        page.locator('button:has-text("Полный конфиг")').first.click(); page.wait_for_timeout(2500)
        shot(page, "run-config")
        # раскрыть resolved.yaml и кликнуть по ключу — откуда значение
        page.locator('.q-expansion-item:has-text("resolved.yaml") > .q-item').first.click(); page.wait_for_timeout(1200)
        shot(page, "run-config-resolved")
        page.keyboard.press("Escape"); page.wait_for_timeout(300); page.keyboard.press("Escape")
    step("run_form", run_form)

    def settings():
        goto(page, "/settings", 2500); shot(page, "settings")
    step("settings", settings)

    def mobile():
        m = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, color_scheme="light", is_mobile=True)
        mp = m.new_page(); mp.goto(BASE + "/runs"); mp.wait_for_timeout(3500)
        mp.screenshot(path=str(OUT / "runs-mobile.png")); print("shot runs-mobile", flush=True); m.close()
    step("mobile", mobile)
    browser.close()
