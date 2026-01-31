import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
import re
import time
import subprocess
from multiprocessing import Process, Queue
from concurrent.futures import ThreadPoolExecutor

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth

# config
NUM_DDG_THREADS = 12
NUM_BROWSER_WORKERS = 3 # good luck pc if > 3 lol   
MIN_CPP_FLOOR = 0.35 
MAX_CPP_CEILING = 1.6
ABS_FLOOR = 0 # prevent negatives
ABS_CEIL = 5000 # check the value on selenium if ddg surpasses 5000$
BLOCKED_DOMAINS = [
    "ebay", "poshmark", "kijiji", "facebook.com/marketplace", 
    "karrot", "mercari", "depop", "thredup", "craigslist", 
    "offerup", "vinted", "gumtree", "etsy"
]
AMEX_URLS = {
    "home_garden": ["bedding-bath", "backyard-patio", "home-decor", "health-wellness", "smart-home", "vaccuums"],
    "sports-leisure": ["exercise-equipment", "outdoor-recreation", "wearable-tech"],
    "electronics": ["apple-products", "audio", "bose-products", "cameras", "desktops-laptops-accessories", "games", "headphones", "televisions"],
}
GOOD_DEALS = []

# goes to the provided url then extracts as many products as it can
def extract_products(url):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        products = []
        items = soup.find_all('div', class_='productListItem')
        for item in items:
            main_prod_div = item.find('div', class_='productDetails')
            if main_prod_div:
                name_tag = main_prod_div.find('h4', class_='productName')
                price_tag = main_prod_div.find('span', class_='noEvent')
                brand_tag = main_prod_div.find('h3', class_='productBrand')
                if name_tag and price_tag and brand_tag:
                    name = name_tag.get_text(strip=True)
                    points = int(re.sub(r'[^\d]', '', price_tag.get_text(strip=True)))
                    brand = brand_tag.get_text(strip=True)
                    products.append({'name': name, 'points': points, 'brand': brand})
        return products
    except Exception as e:
        print(e)
        return []

# do the searching
def process_ddg_task(product_data):
    p, idx = product_data
    query = f"{p['brand']} {p['name']}"
    points = p['points']
    
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{query} price cad", max_results=8))
            smart_floor = max(15.00, points * (MIN_CPP_FLOOR / 100))
            smart_ceil = min(ABS_CEIL, points * (MAX_CPP_CEILING / 100))
            best_price = float('inf')
            best_url = None
            
            for r in results:
                url = r.get('href', '')
                if any(bad in url.lower() for bad in BLOCKED_DOMAINS): continue
                full_text = r.get('title', '') + " " + r.get('body', '')
                matches = re.findall(r'(?:CA|CAD|C)?\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', full_text)
                
                local_min = float('inf')
                for m in matches:
                    val = float(m.replace(',', ''))
                    if smart_floor < val < smart_ceil:
                        if val < local_min: local_min = val
                
                if local_min != float('inf'):
                    if local_min < best_price:
                        best_price = local_min
                        best_url = url
            
            if best_price != float('inf'):
                if ((best_price / points) * 100) > MAX_CPP_CEILING:
                    return None 
                return (idx, best_price, best_url, "ddg")
    except: pass
    return None

# double check sketchy prices
def browser_worker(worker_id, task_queue, result_queue):
    options = Options()
    options.add_argument(f"--window-size=600,800")
    options.add_argument(f"--window-position={worker_id*600},0") 
    options.add_argument("--log-level=3") 
    options.add_argument("--silent")
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    
    service = Service(ChromeDriverManager().install(), log_output=subprocess.DEVNULL)
    driver = webdriver.Chrome(service=service, options=options)
    stealth(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32", webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    
    driver.get("https://www.google.ca")

    while True:
        task = task_queue.get()
        if task == "STOP": 
            driver.quit()
            break
        
        idx, query, points = task
        
        try:
            url = f"https://www.google.ca/search?q={query} price"
            driver.get(url)
            
            # if captcha do not continue
            if "sorry" in driver.current_url or "captcha" in driver.page_source.lower():
                #print(f"\n>>>>> selenium id{worker_id}: captcha! <<<<<")
                
                # wait until the captcha is solved
                while True:
                    time.sleep(2) # check every 2 seconds (this shit crashes my entire pc if its not here and i dont know why)
                    if "sorry" not in driver.current_url and "captcha" not in driver.page_source.lower():
                        #print(f"\n>>>>> selenium id{worker_id}: resuming <<<<<")
                        break
            
            driver.execute_script("window.scrollTo(0, 400);")
            time.sleep(0.5) 

            # inject js to extract prices via regex by checking for $,cad,euro,pd,etc. honestly it doesnt convert euro/gbp/usd to cad but thats functionality that i can add later
            # TODO: that ^
            result_data = driver.execute_script("""
                var findings = [];
                var priceRegex = /(?:CA|CAD|C|\\$|\\£|\\€)[\\s]*([\\d,]+\\.\\d{2})/;
                var allNodes = document.body.querySelectorAll('*');
                allNodes.forEach(function(node) {
                    if (node.childNodes.length === 1 && node.childNodes[0].nodeType === 3) {
                        var text = node.textContent.trim();
                        if (text.length < 20 && priceRegex.test(text)) {
                             var match = text.match(priceRegex);
                             var val = parseFloat(match[1].replace(/,/g, ''));
                             var url = "";
                             var directLink = node.closest('a');
                             if (directLink) {
                                 url = directLink.href;
                             } else {
                                 var parent = node.parentElement;
                                 for(var i=0; i<8; i++) {
                                     if(parent) {
                                         var titleLink = parent.querySelector('h3 > a') || parent.querySelector('a > h3');
                                         var anyLink = parent.querySelector('a[href^="http"]');
                                         
                                         if(titleLink) {
                                            var aTag = titleLink.tagName === 'A' ? titleLink : titleLink.closest('a');
                                            if (aTag) { url = aTag.href; break; }
                                         } else if (anyLink && !url) {
                                            url = anyLink.href;
                                         }
                                         parent = parent.parentElement;
                                     } else { break; }
                                 }
                             }
                             if (val > 0) findings.push({price: val, url: url});
                        }
                    }
                });
                return findings;
            """)
            
            smart_floor = max(15.00, points * (MIN_CPP_FLOOR / 100))
            best_price = float('inf')
            best_url = None

            for item in result_data:
                p = item['price']
                u = item['url']
                if any(bad in u.lower() for bad in BLOCKED_DOMAINS): continue
                if smart_floor < p:
                    if "google.ca" in u or "webcache" in u: continue
                    if p < best_price:
                        best_price = p
                        best_url = u

            if best_price != float('inf'):
                result_queue.put((idx, best_price, best_url, f"sel-{worker_id}"))
            else:
                result_queue.put((idx, None, None, f"sel-{worker_id}"))

        except Exception:
            result_queue.put((idx, None, None, "ERR"))

# pray it doesnt break!!!!
def main():
    items = []
    print("fetch catalogs\n" + "-"*60)
    for url_main in AMEX_URLS:
        for url_sub in AMEX_URLS[url_main]:
            url = f"https://www.americanexpress.com/en-ca/rewards/membership-rewards/Shop/{url_main}/{url_sub}"
            print(f"scraping {url_main}/{url_sub}")
            page_items = extract_products(url)
            for item in page_items:
                items.append(item)
    
    if not items:
        print("error: urls broke")
        return

    print("done scraping\n" + "-"*60)
    print(f"{len(items)} items found. starting the kai baguetteinator")
    print(f" > {NUM_DDG_THREADS} search threads")
    print(f" > {NUM_BROWSER_WORKERS} selenium instances")
    print("-" * 60)
    print(f"\n{'SOURCE':<6} | {'BRAND':<25} | {'ITEM NAME':<60} | {'POINTS':<8} | {'PRICE':<9} | {'CPP':<6} | {'SOURCE'}")
    print("-" * 164)

    browser_task_queue = Queue()
    result_queue = Queue()
    
    workers = []
    for i in range(NUM_BROWSER_WORKERS):
        p = Process(target=browser_worker, args=(i+1, browser_task_queue, result_queue))
        p.start()
        workers.append(p)

    pending_browsers = 0
    
    with ThreadPoolExecutor(max_workers=NUM_DDG_THREADS) as executor:
        futures = {executor.submit(process_ddg_task, (items[i], i)): i for i in range(len(items))}
        
        import concurrent.futures
        for future in concurrent.futures.as_completed(futures):
            i = futures[future]
            res = future.result()
            
            if res:
                idx, price, url, src = res
                print_row(items[idx], price, url, src)
            else:
                query = f"{items[i]['brand']} {items[i]['name']}"
                browser_task_queue.put((i, query, items[i]['points']))
                pending_browsers += 1

    #print(f"\n{pending_browsers} pending selenium checks\n")
    
    results_received = 0
    while results_received < pending_browsers:
        idx, price, url, src = result_queue.get()
        print_row(items[idx], price, url, src)
        results_received += 1

    print("-" * 164)
    print("done! halting")
    for _ in range(NUM_BROWSER_WORKERS):
        browser_task_queue.put("STOP")
    
    for p in workers:
        p.join()
        
    print("done halting!")
    print("EXTRACTED GOOD DEALS:\n")
    for deal in GOOD_DEALS:
        print("-" * 114)
        print(f"\n{'BRAND':<25} | {'ITEM NAME':<60} | {'POINTS':<8} | {'CPP':<6} ")
        print("-" * 100)
        print(f"{deal[0]['brand'][:23]:<25} | {deal[0]['name'][:55]:<60} | {deal[0]['points']:<8} | {deal[1]:<6} ")

def print_row(p, price, url, source):
    if price:
        cpp = (price / p['points']) * 100
        price_disp = f"${price:.2f}"
        cpp_disp = f"{cpp:.2f}"
        if cpp > 1:
            GOOD_DEALS.append([p, cpp])
        if url:
            clean = url.replace("https://", "").replace("http://", "").replace("www.", "")
            short_url = (clean[:30] + "..") if len(clean) > 30 else clean
        else: short_url = "link unavailable"
    else:
        price_disp, cpp_disp, short_url = "no price", "no cpp", "no link"
    
    print(f"{source:<6} | {p['brand'][:23]:<25} | {p['name'][:55]:<60} | {p['points']:<8} | {price_disp:<9} | {cpp_disp:<6} | {short_url}")

if __name__ == "__main__":
    main()
