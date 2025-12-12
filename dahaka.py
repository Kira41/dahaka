import json
import os
import random
import threading
import time
from collections import defaultdict, deque  # Added deque for turbo proxies
from urllib.parse import urlparse
import queue

from flask import Flask, jsonify, render_template
from flask_socketio import SocketIO
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def find_default_config_path():
    env_path = os.environ.get('AUTOMATION_CONFIG')
    if env_path:
        return env_path
    config_dir = 'configs'
    if os.path.isdir(config_dir):
        for filename in os.listdir(config_dir):
            if filename.endswith('.json'):
                return os.path.join(config_dir, filename)
    raise FileNotFoundError('No configuration files found. Place a JSON file inside the configs directory.')


CONFIG_PATH = find_default_config_path()


def load_automation_config(path: str):
    with open(path, 'r') as f:
        return json.load(f)


automation_config = load_automation_config(CONFIG_PATH)

app = Flask(__name__)
socketio = SocketIO(app)


class Config:
    @classmethod
    def load_from_config(cls, config):
        cls.CONFIG_NAME = config.get('name', 'automation')
        cls.CONCURRENT_BROWSERS = config.get('concurrency', {}).get('workers', 10)
        cls.ENTRYPOINT_URL = config.get('entrypoint', 'https://example.com/')
        cls.PROXY_FILE = config.get('files', {}).get('proxy_file', 'google_valid_proxies.txt')
        cls.DATA_FILE = config.get('files', {}).get('data_file', 'data.txt')
        cls.HEADLESS_MODE = bool(config.get('browser', {}).get('headless', 0))
        cls.PAGE_LOAD_TIMEOUT = config.get('browser', {}).get('page_load_timeout_seconds', 10)
        cls.ELEMENT_TIMEOUT_SHORT = config.get('timeouts', {}).get('element_timeout_short_seconds', 3)
        cls.ELEMENT_TIMEOUT = config.get('timeouts', {}).get('element_timeout_seconds', 5)
        cls.PROXY_MAX_RETRIES = config.get('proxy', {}).get('max_retries', 0)
        cls.CURRENT_EMAILS_TTL = config.get('ui', {}).get('current_emails_ttl', 10)
        cls.CURRENT_EMAILS_DISPLAY_LIMIT = config.get('ui', {}).get('current_emails_display_limit', 10)
        cls.STATUS_UPDATE_INTERVAL = config.get('ui', {}).get('status_update_interval', 1)
        cls.SITE_LETTER = config.get('site_letter', 'A')
        cls.TURBO_MODE = config.get('turbo_mode', True)  # Enable turbo mode globally


Config.load_from_config(automation_config)

# Thread-safe variables
email_queue = queue.Queue()
processed_emails = set()
success_count = 0
failure_count = 0
start_time = time.time()
current_emails = []
proxy_stats = {'total': 0, 'active': 0, 'failed': 0, 'retrying': 0}
attempted_proxies = defaultdict(set)
lock = threading.Lock()
proxy_info = {}
total_emails = 0
stop_event = threading.Event()

# Turbo mode variables
turbo_proxies = deque()  # Prioritized proxies
turbo_lock = threading.Lock()  # Lock for thread-safe access


available_configs = []
active_config = None
processing_thread = None
processing_lock = threading.Lock()


def reset_runtime_state():
    global email_queue, processed_emails, success_count, failure_count, start_time
    global current_emails, proxy_stats, attempted_proxies, proxy_info, total_emails
    email_queue = queue.Queue()
    processed_emails = set()
    success_count = 0
    failure_count = 0
    start_time = time.time()
    current_emails = []
    proxy_stats = {'total': 0, 'active': 0, 'failed': 0, 'retrying': 0}
    attempted_proxies = defaultdict(set)
    proxy_info = {}
    total_emails = 0
    stop_event.clear()
    with turbo_lock:
        turbo_proxies.clear()


def load_available_configs():
    configs = []
    config_dir = 'configs'
    if not os.path.isdir(config_dir):
        return configs
    for filename in sorted(os.listdir(config_dir)):
        if filename.endswith('.json'):
            path = os.path.join(config_dir, filename)
            try:
                config_data = load_automation_config(path)
                configs.append({
                    'name': config_data.get('name', os.path.splitext(filename)[0]),
                    'path': path,
                    'site_letter': config_data.get('site_letter', 'A'),
                    'data_file': config_data.get('files', {}).get('data_file', 'data.txt'),
                    'entrypoint': config_data.get('entrypoint', '')
                })
            except Exception as exc:
                print(f"[CONFIG] Failed to load {path}: {exc}")
    return configs


def set_active_config(config_meta):
    global automation_config, active_config
    automation_config = load_automation_config(config_meta['path'])
    Config.load_from_config(automation_config)
    active_config = {
        'name': automation_config.get('name', config_meta.get('name')),
        'path': config_meta['path'],
        'site_letter': Config.SITE_LETTER,
        'data_file': Config.DATA_FILE,
        'entrypoint': Config.ENTRYPOINT_URL
    }
    for idx, cfg in enumerate(available_configs):
        if cfg['path'] == config_meta['path']:
            available_configs[idx] = active_config
            break


def initialize_configs():
    global available_configs, active_config
    available_configs = load_available_configs()
    matching = [c for c in available_configs if c['path'] == CONFIG_PATH]
    if matching:
        set_active_config(matching[0])
    elif available_configs:
        set_active_config(available_configs[0])
    else:
        active_config = {
            'name': Config.CONFIG_NAME,
            'path': CONFIG_PATH,
            'site_letter': Config.SITE_LETTER,
            'data_file': Config.DATA_FILE,
            'entrypoint': Config.ENTRYPOINT_URL
        }
        available_configs.append(active_config)


initialize_configs()


def selector_to_locator(selector):
    by_mapping = {
        'id': By.ID,
        'css_selector': By.CSS_SELECTOR,
        'xpath': By.XPATH,
        'name': By.NAME,
        'class_name': By.CLASS_NAME,
        'tag_name': By.TAG_NAME,
        'link_text': By.LINK_TEXT,
        'partial_link_text': By.PARTIAL_LINK_TEXT,
    }
    by_value = selector.get('by')
    if by_value not in by_mapping:
        raise ValueError(f"Unsupported selector type: {by_value}")
    return by_mapping[by_value], selector.get('value')


def get_locators(selector_key, allow_multiple=False):
    selector_def = automation_config.get('selectors', {}).get(selector_key)
    if selector_def is None:
        raise ValueError(f"Selector '{selector_key}' not found in config")
    if isinstance(selector_def, list):
        locators = [selector_to_locator(item) for item in selector_def]
    else:
        locators = [selector_to_locator(selector_def)]
    if not allow_multiple and len(locators) > 1:
        return [locators[0]]
    return locators


def resolve_timeout(timeout_value):
    if isinstance(timeout_value, (int, float)):
        return timeout_value
    if isinstance(timeout_value, str):
        timeout_candidates = {}
        timeout_candidates.update(automation_config.get('timeouts', {}))
        timeout_candidates.update(automation_config.get('browser', {}))
        return timeout_candidates.get(timeout_value, Config.ELEMENT_TIMEOUT)
    return Config.ELEMENT_TIMEOUT


def evaluate_condition(driver, condition):
    action = condition.get('action')
    if action == 'exists_and_displayed':
        locator = get_locators(condition.get('selector'), allow_multiple=False)[0]
        try:
            element = driver.find_element(*locator)
            return element.is_displayed()
        except Exception:
            return False
    if action == 'any_exists_and_displayed':
        locators = get_locators(condition.get('selectors'), allow_multiple=True)
        for locator in locators:
            try:
                element = driver.find_element(*locator)
                if element.is_displayed():
                    return True
            except Exception:
                continue
        return False
    raise ValueError(f"Unsupported condition action: {action}")


def determine_presence(driver, logic, fallback_result='unknown'):
    for item in logic:
        condition = item.get('condition', {})
        result = item.get('result')
        if evaluate_condition(driver, condition):
            return result
    return fallback_result


def normalize_target(target):
    return (target or Config.ENTRYPOINT_URL).replace('{{ entrypoint }}', Config.ENTRYPOINT_URL)


def mark_proxy_active(proxy):
    if not proxy:
        return
    with lock:
        if proxy not in proxy_info:
            return
        proxy_info[proxy]['state'] = 'active'
        proxy_info[proxy]['retries'] = 0
        proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
        proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
        proxy_stats['retrying'] = sum(1 for p in proxy_info.values() if p['state'] == 'retrying')
        if Config.TURBO_MODE:
            with turbo_lock:
                if proxy not in turbo_proxies:
                    turbo_proxies.append(proxy)


def action_navigate(driver, step, context):
    target = normalize_target(step.get('target'))
    driver.get(target)
    expected_host = urlparse(Config.ENTRYPOINT_URL).netloc
    if expected_host and expected_host not in urlparse(driver.current_url).netloc:
        raise ProxyError(f"Proxy {context.get('proxy')} redirected to invalid domain", first_attempt=True)


def action_open(driver, step, context):
    return action_navigate(driver, step, context)


def action_reload(driver, step, context):
    driver.refresh()


def action_wait_for(driver, step, context):
    locator = get_locators(step.get('selector'), allow_multiple=False)[0]
    timeout = resolve_timeout(step.get('timeout'))
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(locator)
        )
    except TimeoutException:
        first_attempt = step.get('first_attempt', False)
        raise ProxyError(f"{step.get('selector')} not found via {context.get('proxy')}", first_attempt=first_attempt)
    mark_proxy_active(context.get('proxy'))


def action_input(driver, step, context):
    locator = get_locators(step.get('selector'), allow_multiple=False)[0]
    try:
        element = WebDriverWait(driver, Config.ELEMENT_TIMEOUT).until(
            EC.presence_of_element_located(locator)
        )
        element.clear()
        if step.get('source') == 'email':
            element.send_keys(context.get('email'))
        elif 'value' in step:
            element.send_keys(step.get('value'))
    except TimeoutException:
        raise ProxyError(f"Input field {step.get('selector')} not found via {context.get('proxy')}")


def action_fill(driver, step, context):
    return action_input(driver, step, context)


def action_click(driver, step, context):
    locator = get_locators(step.get('selector'), allow_multiple=False)[0]
    try:
        button = WebDriverWait(driver, Config.ELEMENT_TIMEOUT).until(
            EC.element_to_be_clickable(locator)
        )
        button.click()
    except TimeoutException:
        raise ProxyError(f"Clickable element {step.get('selector')} not found via {context.get('proxy')}")


def action_determine_presence(driver, step, context):
    return determine_presence(
        driver,
        step.get('logic', []),
        fallback_result=step.get('fallback_result', 'unknown')
    )


ACTION_HANDLERS = {
    'navigate': action_navigate,
    'open': action_open,
    'reload': action_reload,
    'wait_for': action_wait_for,
    'input': action_input,
    'fill': action_fill,
    'click': action_click,
    'determine_presence': action_determine_presence,
}


def perform_action(driver, step, context):
    action = step.get('action')
    if action not in ACTION_HANDLERS:
        raise ValueError(f"Unsupported action: {action}")
    return ACTION_HANDLERS[action](driver, step, context)


class ProxyError(Exception):
    def __init__(self, message, first_attempt=False):
        super().__init__(message)
        self.first_attempt = first_attempt

def update_status():
    while True:
        elapsed = time.time() - start_time
        with lock:
            proxies_list = []
            retrying_count = 0
            for proxy in proxy_info:
                status = proxy_info[proxy]['state']
                retries = proxy_info[proxy].get('retries', 0)
                if status == 'retrying':
                    retrying_count += 1
                proxies_list.append({
                    'proxy': proxy,
                    'status': status,
                    'retries': retries
                })
            now = time.time()
            current_emails[:] = [e for e in current_emails if now - e.get('timestamp', 0) < Config.CURRENT_EMAILS_TTL]
            
            # Determine if currently in Turbo Mode
            with turbo_lock:
                in_turbo_mode = Config.TURBO_MODE and len(turbo_proxies) > 0
            
            # Prepare Config values to send
            config_data = {
                'CONFIG_NAME': Config.CONFIG_NAME,
                'CONCURRENT_BROWSERS': Config.CONCURRENT_BROWSERS,
                'ENTRYPOINT_URL': Config.ENTRYPOINT_URL,
                'PROXY_FILE': Config.PROXY_FILE,
                'DATA_FILE': Config.DATA_FILE,
                'HEADLESS_MODE': 'Enabled' if Config.HEADLESS_MODE else 'Disabled',
                'PAGE_LOAD_TIMEOUT': Config.PAGE_LOAD_TIMEOUT,
                'ELEMENT_TIMEOUT_SHORT': Config.ELEMENT_TIMEOUT_SHORT,
                'ELEMENT_TIMEOUT': Config.ELEMENT_TIMEOUT,
                'PROXY_MAX_RETRIES': Config.PROXY_MAX_RETRIES,
                'CURRENT_EMAILS_TTL': Config.CURRENT_EMAILS_TTL,
                'CURRENT_EMAILS_DISPLAY_LIMIT': Config.CURRENT_EMAILS_DISPLAY_LIMIT,
                'STATUS_UPDATE_INTERVAL': Config.STATUS_UPDATE_INTERVAL,
                'SITE_LETTER': Config.SITE_LETTER,
                'TURBO_MODE': 'Enabled' if Config.TURBO_MODE else 'Disabled'
            }

            if processing_thread and processing_thread.is_alive():
                status_label = 'running'
            elif stop_event.is_set():
                status_label = 'stopped'
            else:
                status_label = 'idle'

            status_data = {
                'status': status_label,
                'total_emails': total_emails,
                'processed': len(processed_emails),
                'success': success_count,
                'failure': failure_count,
                'proxies': {
                    'total': proxy_stats['total'],
                    'active': proxy_stats['active'],
                    'failed': proxy_stats['failed'],
                    'retrying': retrying_count
                },
                'proxies_list': proxies_list,
                'current_emails': current_emails[:Config.CURRENT_EMAILS_DISPLAY_LIMIT],
                'uptime': round(elapsed, 2),
                'in_turbo_mode': in_turbo_mode,
                'config': config_data,
                'active_config': active_config
            }
        socketio.emit('update', status_data)
        socketio.sleep(Config.STATUS_UPDATE_INTERVAL)

def test_site(proxy, email):
    global success_count, failure_count
    chrome_options = Options()
    chrome_options.add_argument(f'--proxy-server={proxy}')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--log-level=3')
    if Config.HEADLESS_MODE:
        chrome_options.add_argument('--headless')
    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(Config.PAGE_LOAD_TIMEOUT)
        with lock:
            current_emails.append({
                'email': email,
                'status': 'Checking...',
                'timestamp': time.time()
            })

        context = {'proxy': proxy, 'email': email}
        for step in automation_config.get('flow', []):
            result = perform_action(driver, step, context)
            if step.get('action') == 'determine_presence':
                if result == 'absent':
                    with lock:
                        failure_count += 1
                        for e in current_emails:
                            if e['email'] == email:
                                e.update({'status': 'Not Present', 'timestamp': time.time()})
                                break
                    return False
                if result == 'present':
                    with lock:
                        success_count += 1
                        for e in current_emails:
                            if e['email'] == email:
                                e.update({'status': 'Present', 'timestamp': time.time()})
                                break
                    return True
                raise ProxyError("Unable to determine presence from configuration")
        raise ProxyError("Flow completed without determine_presence step")
    except ProxyError as e:
        with lock:
            if proxy in proxy_info:
                first_attempt = getattr(e, 'first_attempt', False)
                if first_attempt and proxy_info[proxy]['state'] == 'available':
                    proxy_info[proxy]['state'] = 'failed'
                    proxy_stats['failed'] += 1
                else:
                    proxy_info[proxy]['retries'] += 1
                    proxy_info[proxy]['state'] = 'retrying'
                    proxy_stats['retrying'] = sum(1 for p in proxy_info.values() if p['state'] == 'retrying')
                    if proxy_info[proxy]['retries'] >= Config.PROXY_MAX_RETRIES:
                        if proxy_info[proxy]['state'] == 'active':
                            proxy_stats['active'] -= 1
                        proxy_info[proxy]['state'] = 'failed'
                        proxy_stats['failed'] += 1
                proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
                proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
                proxy_stats['retrying'] = sum(1 for p in proxy_info.values() if p['state'] == 'retrying')
        if Config.TURBO_MODE:
            with turbo_lock:
                if proxy in turbo_proxies and proxy_info[proxy]['state'] != 'active':
                    turbo_proxies.remove(proxy)
        raise
    except Exception as e:
        with lock:
            if proxy in proxy_info:
                proxy_info[proxy]['retries'] += 1
                proxy_info[proxy]['state'] = 'retrying'
                proxy_stats['retrying'] = sum(1 for p in proxy_info.values() if p['state'] == 'retrying')
                if proxy_info[proxy]['retries'] >= Config.PROXY_MAX_RETRIES:
                    if proxy_info[proxy]['state'] == 'active':
                        proxy_stats['active'] -= 1
                    proxy_info[proxy]['state'] = 'failed'
                    proxy_stats['failed'] += 1
                proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
                proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
                proxy_stats['retrying'] = sum(1 for p in proxy_info.values() if p['state'] == 'retrying')
        if Config.TURBO_MODE:
            with turbo_lock:
                if proxy in turbo_proxies and proxy_info[proxy]['state'] != 'active':
                    turbo_proxies.remove(proxy)
        raise ProxyError(f"Unexpected error with proxy {proxy}: {str(e)}")
    finally:
        if driver:
            driver.quit()

def load_proxies():
    with open(Config.PROXY_FILE, 'r') as f:
        current_proxies = [line.strip() for line in f if line.strip()]
    existing_proxies = set(proxy_info.keys())
    new_proxies = [p for p in current_proxies if p not in existing_proxies]
    for proxy in new_proxies:
        proxy_info[proxy] = {'state': 'available', 'retries': 0}
    proxies_to_remove = existing_proxies - set(current_proxies)
    for proxy in proxies_to_remove:
        del proxy_info[proxy]
        if Config.TURBO_MODE:
            with turbo_lock:
                if proxy in turbo_proxies:
                    turbo_proxies.remove(proxy)
    proxy_stats['total'] = len(proxy_info)
    proxy_stats['active'] = sum(1 for p in proxy_info.values() if p['state'] == 'active')
    proxy_stats['failed'] = sum(1 for p in proxy_info.values() if p['state'] == 'failed')
    # Add active proxies to turbo mode
    if Config.TURBO_MODE:
        for proxy in proxy_info:
            if proxy_info[proxy]['state'] == 'active':
                with turbo_lock:
                    if proxy not in turbo_proxies:
                        turbo_proxies.append(proxy)
    print(f"[PROXY] Loaded {len(new_proxies)} new proxies (Total: {proxy_stats['total']}, Active: {proxy_stats['active']})")


def drain_email_queue():
    while True:
        try:
            email_queue.get_nowait()
            email_queue.task_done()
        except queue.Empty:
            break

def worker():
    while True:
        if stop_event.is_set():
            break
        try:
            line = email_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if stop_event.is_set():
            email_queue.task_done()
            break
        if line is None:
            email_queue.task_done()
            break
        try:
            parts = line.split('|', 1)
            if not parts:
                email_queue.task_done()
                continue
            email_part = parts[0].strip()
            rest = parts[1] if len(parts) > 1 else ''
            tag_parts = rest.split('|') if rest else []
            tag_dict = {}
            for tag in tag_parts:
                if ':' in tag:
                    k, v = tag.split(':', 1)
                    k = k.strip()
                    tag_dict[k] = {'value': v.strip(), 'raw': tag}
            with lock:
                if email_part in processed_emails:
                    continue
                available_turbo = []
                available_normal = []
                if Config.TURBO_MODE:
                    with turbo_lock:
                        available_turbo = [p for p in turbo_proxies if proxy_info.get(p, {}).get('state') == 'active' and p not in attempted_proxies.get(email_part, set())]
                available_normal = [p for p in proxy_info
                                    if proxy_info[p]['state'] in ('available', 'active')
                                    and p not in attempted_proxies.get(email_part, set())
                                    and p not in available_turbo]
                available = available_turbo + available_normal
                if not available:
                    print("[PROXY] Reloading proxies...")
                    load_proxies()
                    available_turbo = []
                    available_normal = []
                    if Config.TURBO_MODE:
                        with turbo_lock:
                            available_turbo = [p for p in turbo_proxies if proxy_info.get(p, {}).get('state') == 'active' and p not in attempted_proxies.get(email_part, set())]
                    available_normal = [p for p in proxy_info
                                        if proxy_info[p]['state'] in ('available', 'active')
                                        and p not in attempted_proxies.get(email_part, set())
                                        and p not in available_turbo]
                    available = available_turbo + available_normal
                    if not available:
                        need_to_loop = True
                    else:
                        need_to_loop = False
                else:
                    need_to_loop = False
            if need_to_loop:
                while True:
                    if stop_event.is_set():
                        break
                    print("[PROXY] No proxies available. Waiting 10 seconds before retrying...")
                    time.sleep(10)
                    with lock:
                        load_proxies()
                        available_turbo = []
                        available_normal = []
                        if Config.TURBO_MODE:
                            with turbo_lock:
                                available_turbo = [p for p in turbo_proxies if proxy_info.get(p, {}).get('state') == 'active' and p not in attempted_proxies.get(email_part, set())]
                        available_normal = [p for p in proxy_info
                                            if proxy_info[p]['state'] in ('available', 'active')
                                            and p not in attempted_proxies.get(email_part, set())
                                            and p not in available_turbo]
                    if available_turbo or available_normal or stop_event.is_set():
                        break
                if stop_event.is_set():
                    continue
            with lock:
                if Config.TURBO_MODE and available_turbo:
                    proxy = random.choice(available_turbo)
                elif available_normal:
                    proxy = random.choice(available_normal)
                else:
                    proxy = None
                attempted_proxies[email_part].add(proxy)
                status = 'Retrying' if len(attempted_proxies[email_part]) > 1 else 'Checking...'
                current_emails.append({
                    'email': email_part,
                    'status': status,
                    'timestamp': time.time()
                })
            if stop_event.is_set():
                continue
            found = test_site(proxy, email_part)
            site_letter = Config.SITE_LETTER
            if site_letter in tag_dict:
                tag_dict[site_letter]['value'] = '1' if found else '0'
            else:
                email_queue.task_done()
                with lock:
                    processed_emails.add(email_part)
                continue
            original_email_part = parts[0]
            updated_tag_parts = []
            for tag in tag_parts:
                if ':' in tag:
                    k, v = tag.split(':', 1)
                    k = k.strip()
                    if k == site_letter:
                        new_value = tag_dict[k]['value']
                        updated_tag_parts.append(f"{k}:{new_value}")
                    else:
                        updated_tag_parts.append(tag)
                else:
                    updated_tag_parts.append(tag)
            updated_line = original_email_part + '|' + '|'.join(updated_tag_parts)
            with lock:
                with open(Config.DATA_FILE, 'r') as f:
                    all_lines = [l.rstrip('\n') for l in f]
                try:
                    idx = all_lines.index(line)
                    all_lines[idx] = updated_line
                    with open(Config.DATA_FILE, 'w') as f:
                        for l in all_lines:
                            f.write(l + '\n')
                except ValueError:
                    print(f"[ERROR] Line not found in file: {line}")
            with lock:
                processed_emails.add(email_part)
        except ProxyError:
            with lock:
                if email_part not in processed_emails:
                    email_queue.put(line)
        except Exception as e:
            print(f"[ERROR] {str(e)}")
            with lock:
                if email_part not in processed_emails:
                    email_queue.put(line)
        finally:
            email_queue.task_done()

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/configs')
def list_configs():
    return jsonify({'configs': available_configs, 'active': active_config})


def process_emails():
    global total_emails, processing_thread
    try:
        stop_event.clear()
        with open(Config.DATA_FILE, 'r') as f:
            all_lines = [line.rstrip('\n') for line in f if line.strip()]
        site_letter = Config.SITE_LETTER
        emails_to_process = []
        for line in all_lines:
            parts = line.split('|', 1)
            if not parts:
                continue
            email_part = parts[0]
            rest = parts[1] if len(parts) > 1 else ''
            tag_parts = rest.split('|') if rest else []
            tag_dict = {}
            for tag in tag_parts:
                if ':' in tag:
                    k, v = tag.split(':', 1)
                    k = k.strip()
                    tag_dict[k] = v.strip()
            if site_letter in tag_dict and tag_dict[site_letter] == 'X':
                emails_to_process.append(line)
        total_emails = len(emails_to_process)
        load_proxies()
        threads = []
        for _ in range(Config.CONCURRENT_BROWSERS):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)
        for line in emails_to_process:
            if stop_event.is_set():
                break
            email_queue.put(line)
        while not stop_event.is_set():
            if email_queue.unfinished_tasks == 0:
                break
            time.sleep(0.1)
        for _ in range(Config.CONCURRENT_BROWSERS):
            email_queue.put(None)
        for t in threads:
            t.join()
    except Exception as e:
        socketio.emit('error', {'message': str(e)})
    finally:
        stop_event.set()
        drain_email_queue()
        with processing_lock:
            processing_thread = None

@socketio.on('start_processing')
def start_processing(message):
    requested_name = message.get('config') if isinstance(message, dict) else None
    selected = next((c for c in available_configs if c['name'] == requested_name), None)
    if not selected:
        socketio.emit('error', {'message': f"Configuration '{requested_name}' not found."})
        return
    with processing_lock:
        global processing_thread
        if processing_thread and processing_thread.is_alive():
            socketio.emit('error', {'message': 'Processing is already running.'})
            return
        set_active_config(selected)
        reset_runtime_state()
        processing_thread = threading.Thread(target=process_emails, daemon=True)
        processing_thread.start()
    socketio.emit('processing_started', {'config': active_config})


@socketio.on('stop_processing')
def stop_processing():
    with processing_lock:
        global processing_thread
        if not processing_thread or not processing_thread.is_alive():
            socketio.emit('error', {'message': 'Processing is not running.'})
            return
        stop_event.set()
        drain_email_queue()
        for _ in range(Config.CONCURRENT_BROWSERS):
            email_queue.put(None)
    socketio.emit('processing_stopped', {'config': active_config})


if __name__ == '__main__':
    threading.Thread(target=update_status, daemon=True).start()
    socketio.run(app, debug=False)
