"""Read-only browser acceptance. Requires the optional playwright package."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--output', default='acceptance/ui')
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    errors, mode_requests, passed = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width':1440,'height':1080})
        page = context.new_page()
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.on('request', lambda request: mode_requests.append(request.url)
                if request.method == 'POST' and request.url.endswith('/environments/select') else None)
        page.goto(args.url + '/#/environments')
        expect(page.locator('#page-environments')).to_be_visible()
        expect(page.locator('#environmentActiveMode')).to_have_text('普通环境')
        expect(page.locator('[data-page="environments"] .nav-num')).to_have_text('06')
        expect(page.locator('[data-page="archives"] .nav-num')).to_have_text('07')
        passed.append('environment navigation and numbering')
        field = page.locator('#ros2Namespace')
        original = field.input_value()
        field.fill('/unsaved_browser_acceptance')
        page.wait_for_timeout(1500)
        expect(field).to_have_value('/unsaved_browser_acceptance')
        page.locator('#ros2ConfigResetButton').click()
        expect(field).to_have_value(original)
        passed.append('environment draft survives polling and can be discarded')
        page.goto(args.url + '/#/plugins/special/ros2_echo')
        expect(page.locator('#pluginRosPanel')).to_be_visible()
        field = page.locator('[data-ros-port="text_in"] [data-ros-field="topic"]')
        original = field.input_value()
        field.fill('/unsaved_plugin_acceptance')
        page.wait_for_timeout(1500)
        expect(field).to_have_value('/unsaved_plugin_acceptance')
        page.locator('#pluginRosResetButton').click()
        expect(field).to_have_value(original)
        passed.append('plugin ROS draft survives polling and can be discarded')
        context.set_offline(True)
        page.wait_for_timeout(1000)
        context.set_offline(False)
        page.wait_for_function('state.eventSource && state.eventSource.readyState === 1', timeout=15000)
        passed.append('SSE reconnect')
        page.goto(args.url + '/#/environments')
        expect(page.locator('#page-environments')).to_be_visible()
        expect(page.locator('#environmentActiveMode')).to_have_text('普通环境')
        page.reload()
        expect(page.locator('#page-environments')).to_be_visible()
        expect(page.locator('#environmentActiveMode')).to_have_text('普通环境')
        assert not mode_requests, mode_requests
        assert not errors, errors
        passed.append('navigation and browser reload never select a mode')
        page.screenshot(path=str(output / 'environment.png'), full_page=True)
        result = {'passed':passed,'javascript_errors':errors,'mode_change_requests':mode_requests}
        (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        browser.close()


if __name__ == '__main__':
    main()
