import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
import re
import time
import random
import os

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium_stealth import stealth
from selenium.common.exceptions import WebDriverException

# config
MIN_CPP_FLOOR = 0.35 
MAX_CPP_CEILING = 1.4 # forces a recheck if cpp exceeds 1.4
FLAT_CEIL = 5000.00 # forces a secondary check if the cost exceesd 5000.00 to begin with

BLOCKED_DOMAINS = [
    "ebay", "poshmark", "kijiji", "facebook.com/marketplace", 
    "karrot", "mercari", "depop", "thredup", "craigslist", 
    "offerup", "vinted", "gumtree", "etsy"
]

# code
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
        print(f"Amex Error: {e}")
        return []

# captcha checker
def is_blocked(url):
    if not url: return True
    url_lower = url.lower()
    for domain in BLOCKED_DOMAINS:
        if domain in url_lower:
            return True
    return False

# ddg
def get_price_from_ddg(query, points_cost):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"{query} price cad", max_results=8))
            
            smart_floor = max(15.00, points_cost * (MIN_CPP_FLOOR / 100))
            best_price = float('inf')
            best_url = None
            
            for r in results:
                url = r.get('href', '')

                if is_blocked(url): continue

                full_text = r.get('title', '') + " " + r.get('body', '')
                matches = re.findall(r'(?:CA|CAD|C)?\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', full_text)
                
                local_min = float('inf')
                for m in matches:
                    val = float(m.replace(',', ''))
                    if smart_floor < val < FLAT_CEIL: # if it exceeds flat_ceil, just go check on selenium to make sure
                        if val < local_min: local_min = val
                
                if local_min != float('inf'):
                    if local_min < best_price:
                        best_price = local_min
                        best_url = url
            
            if best_price != float('inf'):
                return best_price, best_url
    except:
        pass
    return None, None

# second check if necessary --- step 1: driver set up
def setup_driver():
    options = Options()
    options.add_argument("--window-size=1000,900") 
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    stealth(driver, languages=["en-US", "en"], vendor="Google Inc.", platform="Win32", webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
    return driver

# second check if necessary --- step 2: search
def get_price_from_google_main(driver, query, points_cost):
    url = f"https://www.google.ca/search?q={query} price"
    
    retries = 0
    while retries < 2:
        try:
            driver.get(url)
            time.sleep(2.0)

            page_text = driver.page_source.lower()
            if "sorry" in driver.current_url or "captcha" in page_text:
                input("CAPTCHA -- press any key")
                continue

            driver.execute_script("window.scrollTo(0, 500);")
            time.sleep(0.25)

            # inject some javascript to do the actual checking
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
                                         var titleLink = parent.querySelector('h3 > a, a > h3'); 
                                         var anyLink = parent.querySelector('a[href^="http"]');
                                         if (titleLink) {
                                             url = titleLink.closest('a') ? titleLink.closest('a').href : titleLink.parentElement.href;
                                             break;
                                         } else if (anyLink) {
                                             url = anyLink.href;
                                         }
                                         parent = parent.parentElement;
                                     } else { break; }
                                 }
                             }
                             if (val > 0 && url.length > 5) {
                                findings.push({price: val, url: url});
                             }
                        }
                    }
                });
                return findings;
            """)
            
            smart_floor = max(15.00, points_cost * (MIN_CPP_FLOOR / 100))
            smart_ceil = min(10000.00, points_cost * (MAX_CPP_CEILING / 100))
            best_price = float('inf')
            best_url = None

            for item in result_data:
                p = item['price']
                u = item['url']
                
                if is_blocked(u): continue
                
                if smart_floor < p < smart_ceil:
                    if "google.ca" in u or "webcache" in u: continue
                        
                    if p < best_price:
                        best_price = p
                        best_url = u
            
            if best_price != float('inf'):
                return best_price, best_url
            
            return None, None

        except WebDriverException:
            print("  > Browser Connection Lost! Restarting...")
            raise 
        except Exception as e:
            print(f"  > Selenium Error: {e}")
            return None, None

# main
if __name__ == "__main__":
    target_url = "https://www.americanexpress.com/en-ca/rewards/membership-rewards/Shop/coffee-tea-espresso?sort=priceAscendingText&show=100"
    
    items = extract_products(target_url)
    
    if items:
        driver = None
        print(f"\n{'PRODUCT NAME':<60} | {'PTS':<8} | {'PRICE':<9} | {'CPP':<6} | {'SOURCE URL':<100}")
        print("-" * 110) # woah cool fancy formatting! this doesnt matter if I KEEP GETTING CAPTCHAS THOUGH BECAUSE IT BREAKS IT ANYWAYS!

        for i, p in enumerate(items):
            search_query = f"{p['brand']} {p['name']}"
            price, url = get_price_from_ddg(search_query, p['points'])
            source = "DDG"
            force_selenium = False
            if price:
                cpp_check = (price / p['points']) * 100
                if cpp_check > MAX_CPP_CEILING:
                    force_selenium = True
                    price = None 
            
            if not price or force_selenium:
                if driver is None:
                    driver = setup_driver()
                    driver.get("https://www.google.ca") 
                try:
                    price, url = get_price_from_google_main(driver, search_query, p['points'])
                    source = "GOOG"
                except WebDriverException:
                    try: driver.quit()
                    except: pass
                    driver = setup_driver()
                    time.sleep(2)
                    price, url = get_price_from_google_main(driver, search_query, p['points'])

            if price:
                cpp = (price / p['points']) * 100
                price_disp = f"${price:.2f}"
                cpp_disp = f"{cpp:.2f}"
                
                if url:
                    clean = url.replace("https://", "").replace("http://", "").replace("www.", "")
                    short_url = (clean[:35] + "..") if len(clean) > 35 else clean
                else:
                    short_url = "something happened re: unknown source"
            else:
                price_disp = "N/A"
                cpp_disp = "N/A"
                short_url = "-"

            print(f"{p['name'][:37]+'...':<40} | {p['points']:<8} | {price_disp:<9} | {cpp_disp:<6} | {short_url}") # output result
            
            if driver and source == "GOOG":
                time.sleep(random.uniform(2, 4))

        if driver:
            driver.quit()
    else:
        print("url invalid")
