"""Direct JS MutationObserver verification for characterData child_index."""
from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    page.set_content("<html><body><div id='host'></div></body></html>")
    page.evaluate("""() => {
        const d = document.getElementById('host');
        d.appendChild(document.createTextNode('First'));
        d.appendChild(document.createTextNode('Second'));
    }""")
    page.evaluate("""() => {
        window._muts = [];
        const obs = new MutationObserver((list) => {
            for (const m of list) {
                if (m.type === 'characterData') {
                    const idx = m.target.parentNode
                        ? Array.from(m.target.parentNode.childNodes).indexOf(m.target)
                        : -1;
                    window._muts.push({idx: idx, text: m.target.nodeValue});
                }
            }
        });
        obs.observe(document, {childList: true, characterData: true, subtree: true});
    }""")
    page.evaluate("""() => {
        const d = document.getElementById('host');
        d.childNodes[0].nodeValue = 'Changed-First';
        d.childNodes[1].nodeValue = 'Changed-Second';
    }""")
    time.sleep(0.1)
    muts = page.evaluate("window._muts")
    print(f"Mutations: {muts}")
    indices = [m['idx'] for m in muts]
    assert len(set(indices)) == 2, f"Expected 2 distinct indices, got {set(indices)}"
    print("PASS: child_index correctly differentiates sibling text nodes")
    browser.close()
