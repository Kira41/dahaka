import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import colorama
import requests
from requests.adapters import HTTPAdapter
from urllib3.exceptions import InsecureRequestWarning
from urllib3.util import Retry

# Initialize colorama
colorama.init()

# Disable SSL warnings
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

# Configuration
CONFIG = {
    # Proxy sources
    "API_URL": "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all",
    # Use Google lightweight endpoint for realistic traffic shape and detection
    "TEST_URL": "https://www.google.com/generate_204",
    # Secondary free API to verify that the proxy truly connects to the internet
    "VERIFICATION_URL": "https://ipwho.is/",
    
    # Proxy parameters
    "PROXY_TIMEOUT": 10,          # Seconds for proxy to respond
    "LATENCY_THRESHOLD": 7,       # Maximum acceptable latency in seconds
    "MAX_WORKERS": 100,           # Maximum parallel threads
    "REQUEST_RETRIES": 3,         # Number of retries for HTTP requests
    "REQUEST_BACKOFF": 1,         # Backoff factor between retries
    "USER_AGENTS": (
        # A small pool of modern desktop user agents to mimic real traffic
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4_1) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.5 Safari/605.1.15",
        "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    ),

    # Application settings
    "OUTPUT_FILE": "google_valid_proxies.txt",
    "RETRY_INTERVAL": 5,         # Seconds between checks
    "VERIFY_SSL": False,          # SSL verification toggle
    
    # Color configurations
    "COLORS": {
        "BRIGHT": colorama.Style.BRIGHT,
        "SUCCESS": colorama.Fore.GREEN,
        "RESET": colorama.Style.RESET_ALL,
    },
    
    # Logging configuration
    "LOGGING": {
        "LEVEL": logging.DEBUG,
        "FORMAT": f"%(asctime)s - %(levelname)s - {colorama.Style.BRIGHT}%(message)s{colorama.Style.RESET_ALL}",
    }
}


def build_session():
    retry_strategy = Retry(
        total=CONFIG["REQUEST_RETRIES"],
        backoff_factor=CONFIG["REQUEST_BACKOFF"],
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


http_session = build_session()

# Configure logging
logging.basicConfig(
    level=CONFIG["LOGGING"]["LEVEL"],
    format=CONFIG["LOGGING"]["FORMAT"],
    handlers=[logging.StreamHandler()]
)
# Silence noisy retry warnings from urllib3 when proxies fail
logging.getLogger("urllib3").setLevel(logging.ERROR)

def get_proxies_from_api():
    """Fetch proxy list from API"""
    logging.info("Requesting proxy list from API...")
    try:
        response = http_session.get(
            CONFIG["API_URL"],
            verify=CONFIG["VERIFY_SSL"],
            timeout=CONFIG["PROXY_TIMEOUT"],
        )
        if response.status_code == 200:
            proxies = response.text.splitlines()
            logging.info("Received %s proxies from API", len(proxies))
            return proxies

        logging.warning("Proxy API returned status %s", response.status_code)
        return []
    except Exception as e:
        logging.error(f"Error fetching proxies: {e}")
        return []

def build_headers():
    """Generate headers that mimic a real browser visit to Google."""
    return {
        "User-Agent": random.choice(CONFIG["USER_AGENTS"]),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif," "image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


def verify_proxy_reachability(proxy):
    """Second-chance verification using a free IP lookup API."""
    logging.debug("Verifying reachability for proxy %s", proxy)
    try:
        response = http_session.get(
            CONFIG["VERIFICATION_URL"],
            proxies={"http": proxy, "https": proxy},
            timeout=CONFIG["PROXY_TIMEOUT"],
            verify=CONFIG["VERIFY_SSL"],
            headers={"User-Agent": random.choice(CONFIG["USER_AGENTS"])}
        )
        # ipwho.is returns a JSON payload with a boolean success field
        return response.status_code == 200 and response.json().get("success", False)
    except Exception:
        return False


def format_failure_reason(proxy_result):
    """Create a short, human-readable reason string for a failed proxy test."""
    if not proxy_result:
        return "unknown failure"

    reason = proxy_result.get("reason")
    status_code = proxy_result.get("status_code")
    latency = proxy_result.get("latency")

    parts = []
    if reason:
        parts.append(reason)
    if status_code:
        parts.append(f"status={status_code}")
    if latency is not None:
        parts.append(f"latency={latency:.2f}s")

    return " | ".join(parts) if parts else "unknown failure"


def test_proxy(proxy):
    """Test proxy connectivity with Google and re-verify with a free API."""
    logging.debug("Testing proxy %s", proxy)
    start_time = time.time()

    try:
        response = http_session.get(
            CONFIG["TEST_URL"],
            proxies={"http": proxy, "https": proxy},
            timeout=CONFIG["PROXY_TIMEOUT"],
            verify=CONFIG["VERIFY_SSL"],
            headers=build_headers(),
            allow_redirects=False,
        )
        latency = time.time() - start_time
    except Exception as exc:
        return {
            "proxy": proxy.strip(),
            "status": "error",
            "reason": str(exc),
        }

    if response.status_code in (204, 200) and latency <= CONFIG["LATENCY_THRESHOLD"]:
        if verify_proxy_reachability(proxy):
            return {
                "proxy": proxy.strip(),
                "latency": latency,
                "status": "valid",
            }

        return {
            "proxy": proxy.strip(),
            "status": "unreachable",
            "latency": latency,
            "reason": "failed reachability check",
        }

    return {
        "proxy": proxy.strip(),
        "status": "invalid",
        "status_code": response.status_code,
        "latency": latency,
        "reason": "bad status or slow response",
    }

def save_proxies(new_proxies):
    """Save valid proxies to file with deduplication"""
    try:
        # Read existing proxies
        try:
            with open(CONFIG["OUTPUT_FILE"], 'r') as f:
                existing = set(f.read().splitlines())
        except FileNotFoundError:
            existing = set()
            
        # Combine and deduplicate
        all_proxies = existing.union(set(new_proxies))
        
        # Write to file
        with open(CONFIG["OUTPUT_FILE"], 'w') as f:
            for proxy in sorted(all_proxies):
                f.write(f"{proxy}\n")
                
        logging.info(f"Saved {len(all_proxies)} unique proxies to {CONFIG['OUTPUT_FILE']}")
    except Exception as e:
        logging.error(f"Error saving proxies: {e}")


def load_existing_proxies():
    """Load already saved proxies into a set."""
    try:
        with open(CONFIG["OUTPUT_FILE"], "r") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()
    except Exception as e:
        logging.error(f"Error loading proxies: {e}")
        return set()


def save_proxy_realtime(proxy, saved_proxies, lock):
    """Persist a single proxy immediately, ensuring deduplication across threads."""
    with lock:
        if proxy in saved_proxies:
            return False

        saved_proxies.add(proxy)
        try:
            with open(CONFIG["OUTPUT_FILE"], "a") as f:
                f.write(f"{proxy}\n")
            logging.info(
                f"Persisted proxy {proxy} | Total saved: {len(saved_proxies)}"
            )
        except Exception as e:
            logging.error(f"Error saving proxy {proxy}: {e}")
        return True

def main():
    saved_proxies = load_existing_proxies()
    save_lock = threading.Lock()

    while True:
        logging.info("Starting new proxy validation cycle")
        cycle_start = time.time()
        proxies = get_proxies_from_api()
        total_proxies = len(proxies)
        tested_count = 0
        new_valid_count = 0

        with ThreadPoolExecutor(max_workers=CONFIG["MAX_WORKERS"]) as executor:
            future_to_proxy = {executor.submit(test_proxy, p): p for p in proxies}

            for future in as_completed(future_to_proxy):
                tested_count += 1
                result = future.result()

                if result and result.get("status") == "valid":
                    proxy = result["proxy"]
                    latency = result.get("latency", 0)
                    if save_proxy_realtime(proxy, saved_proxies, save_lock):
                        new_valid_count += 1
                    logging.info(
                        f"Valid proxy found: {proxy} "
                        f"({CONFIG['COLORS']['SUCCESS']}{latency:.2f}s{CONFIG['COLORS']['RESET']})"
                    )
                else:
                    reason = format_failure_reason(result)
                    logging.debug(
                        "Proxy %s is invalid or failed checks: %s",
                        future_to_proxy[future],
                        reason,
                    )

        logging.info(
            "Cycle stats | fetched: %s | tested: %s | new valid: %s | total saved: %s | duration: %.2fs",
            total_proxies,
            tested_count,
            new_valid_count,
            len(saved_proxies),
            time.time() - cycle_start,
        )

        logging.info(f"Waiting {CONFIG['RETRY_INTERVAL']} seconds before next check...")
        time.sleep(CONFIG["RETRY_INTERVAL"])

if __name__ == "__main__":
    main()
