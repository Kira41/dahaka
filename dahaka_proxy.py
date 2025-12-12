import logging
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
    "TEST_URL": "https://www.google.com",
    
    # Proxy parameters
    "PROXY_TIMEOUT": 10,          # Seconds for proxy to respond
    "LATENCY_THRESHOLD": 7,       # Maximum acceptable latency in seconds
    "MAX_WORKERS": 100,           # Maximum parallel threads
    "REQUEST_RETRIES": 3,         # Number of retries for HTTP requests
    "REQUEST_BACKOFF": 1,         # Backoff factor between retries

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
        "LEVEL": logging.INFO,
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

def get_proxies_from_api():
    """Fetch proxy list from API"""
    try:
        response = http_session.get(
            CONFIG["API_URL"],
            verify=CONFIG["VERIFY_SSL"],
            timeout=CONFIG["PROXY_TIMEOUT"],
        )
        return response.text.splitlines() if response.status_code == 200 else []
    except Exception as e:
        logging.error(f"Error fetching proxies: {e}")
        return []

def test_proxy(proxy):
    """Test proxy connectivity with target URL"""
    try:
        start_time = time.time()
        response = http_session.get(
            CONFIG["TEST_URL"],
            proxies={"http": proxy, "https": proxy},
            timeout=CONFIG["PROXY_TIMEOUT"],
            verify=CONFIG["VERIFY_SSL"]
        )
        latency = time.time() - start_time
        
        if response.status_code == 200 and latency <= CONFIG["LATENCY_THRESHOLD"]:
            return (proxy.strip(), latency)
    except Exception as e:
        logging.debug(f"Proxy {proxy} failed: {str(e)}")
    return None

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

def main():
    while True:
        proxies = get_proxies_from_api()
        valid_proxies = []
        
        with ThreadPoolExecutor(max_workers=CONFIG["MAX_WORKERS"]) as executor:
            future_to_proxy = {executor.submit(test_proxy, p): p for p in proxies}
            
            for future in as_completed(future_to_proxy):
                result = future.result()
                if result:
                    proxy, latency = result
                    valid_proxies.append(proxy)
                    logging.info(
                        f"Valid proxy found: {proxy} "
                        f"({CONFIG['COLORS']['SUCCESS']}{latency:.2f}s{CONFIG['COLORS']['RESET']})"
                    )

        if valid_proxies:
            save_proxies(valid_proxies)
        
        logging.info(f"Waiting {CONFIG['RETRY_INTERVAL']} seconds before next check...")
        time.sleep(CONFIG["RETRY_INTERVAL"])

if __name__ == "__main__":
    main()