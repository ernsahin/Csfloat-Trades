import requests
import time
import json
import sqlite3
import re

class Scanner:
    def __init__(self, config):
        self.config = config
        self.api_key = config.get("api_key")
        self.seen_listing_ids = set()
        self.price_cache = {}
        # Wear tier definitions, lower index is better wear
        self.wear_tiers = ["Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"]
        # The lowest possible float for a given wear tier
        self.wear_tier_min_floats = {
            "Factory New": 0.00,
            "Minimal Wear": 0.07,
            "Field-Tested": 0.15,
            "Well-Worn": 0.38,
            "Battle-Scarred": 0.45
        }


    def get_market_price(self, market_hash_name):
        """A dedicated function for fetching external prices, used for float checks."""
        cached_price = self.price_cache.get(market_hash_name)
        if cached_price and (time.time() - cached_price['timestamp']) < 3600:
            return cached_price['price']
        
        try:
            # Using the csprices.com API for more reliable pricing data
            steam_url = f"https://csprices.com/api/v1/prices/{market_hash_name}"
            response = requests.get(steam_url, timeout=15) # Increased timeout
            response.raise_for_status()
            price_data = response.json()
            
            if price_data and price_data.get('success'):
                price = float(price_data.get('price', '0'))
                self.price_cache[market_hash_name] = {'price': price, 'timestamp': time.time()}
                time.sleep(2.5) # Be respectful to the API
                return price
        except Exception as e:
            print(f"[API Error] Could not fetch price for {market_hash_name}: {e}")
            return 0
        return 0

    def run_continuous_scan(self):
        while True:
            print(f"[Scanner] Starting new scan cycle...")
            try:
                listings = self.fetch_listings()
                if listings:
                    print(f"[Scanner] Analyzing {len(listings)} new items from API...")
                    deals = []
                    deals.extend(self.analyze_sticker_deals(listings, 'conservative'))
                    deals.extend(self.analyze_sticker_deals(listings, 'aggressive'))
                    deals.extend(self.analyze_charm_deals(listings))
                    deals.extend(self.analyze_low_float_deals(listings))
                    deals.extend(self.analyze_high_overpay_deals(listings))
                    deals.extend(self.analyze_price_anomaly_deals(listings))
                    if self.config.get('float_tier_upgrade', {}).get('enabled'):
                        deals.extend(self.analyze_float_tier_upgrade(listings))


                    if deals:
                        print("\n--- FOUND PROFITABLE DEALS ---")
                        for deal in deals:
                            print(f"  - Strategy: {deal['strategy']:<22} | Profit: ${deal['profit']:.2f} | Item: {deal['name']}")
                        print("---------------------------------\n")
                        self.save_deals_to_db(deals)
                        
                    for item in listings: self.seen_listing_ids.add(item.get('id'))
            except Exception as e:
                print(f"[Scanner] CRITICAL Error in scan cycle: {e}")
                import traceback
                traceback.print_exc()
            
            interval = self.config.get("scan_interval_seconds", 600)
            print(f"[Scanner] Scan cycle complete. Waiting {interval} seconds...")
            time.sleep(interval)

    def fetch_listings(self):
        all_items = []
        cursor = None
        for page in range(self.config.get("scan_pages", 10)):
            try:
                params = { 
                    "sort_by": "most_recent", 
                    "type": "buy_now", 
                    "limit": 50, 
                    "min_price": int(self.config.get("min_price", 0.50) * 100), 
                    "max_price": int(self.config.get("max_price", 200.00) * 100)
                }
                if cursor: params['cursor'] = cursor
                
                headers = {
                    "Authorization": self.api_key,
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }

                response = requests.get("https://csfloat.com/api/v1/listings", headers=headers, params=params, timeout=30)
                response.raise_for_status()
                data = response.json()

                if not isinstance(data, dict) or 'data' not in data: 
                    print(f"[Scanner] API returned unexpected data format: {data}. Ending fetch for this cycle.")
                    break
                
                listings_on_page = data.get('data', [])
                new_listings = [item for item in listings_on_page if item.get('id') not in self.seen_listing_ids]
                all_items.extend(new_listings)
                
                cursor = data.get('cursor')
                if not cursor: 
                    print("[Scanner] Reached the end of listings for this request.")
                    break
                
                time.sleep(2.5) 

            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 429:
                    print("[Scanner] Rate limited by API. Waiting for 60 seconds before retrying...")
                    time.sleep(60)
                    continue
                elif e.response.status_code == 403:
                    print(f"[Scanner] HTTP 403 Forbidden Error. This is likely due to an invalid API key. Please check your config.json.")
                    break
                else:
                    print(f"[Scanner] Unrecoverable HTTP error on page {page+1}: {e}")
                    break
            except requests.exceptions.RequestException as e:
                print(f"[Scanner] Network error (e.g., timeout) on page {page+1}: {e}. Stopping fetch for this cycle.")
                break
            except Exception as e:
                print(f"[Scanner] An unexpected error occurred during fetch on page {page+1}: {e}")
                break

        return all_items
    
    def is_deal_profitable(self, profit, base_price):
        if base_price <= 0: return False
        targets = self.config.get('dynamic_profit_targets', [])
        profit_percent = (profit / base_price) * 100
        for tier in targets:
            if base_price <= tier['max_skin_price']:
                if profit >= tier['min_profit_usd'] or profit_percent >= tier['min_profit_percentage']:
                    return True
                return False
        return False

    def save_deals_to_db(self, deals):
        conn = sqlite3.connect('deals.db'); c = conn.cursor(); saved_count = 0
        for deal in deals:
            try:
                c.execute("INSERT INTO deals (listing_id, strategy, name, image_url, profit, details, url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (deal['listing_id'], deal['strategy'], deal['name'], deal.get('image_url', ''), deal['profit'], json.dumps(deal['details']), deal['url']))
                saved_count += 1
            except sqlite3.IntegrityError: pass
            except Exception as e: print(f"DB Error: {e}")
        conn.commit(); conn.close()
        if saved_count > 0: print(f"[Scanner] Saved {saved_count} new deals to the database.")

    def analyze_float_tier_upgrade(self, listings):
        """
        MODIFIED: Finds skins with a float value very close to the next best wear tier,
        and uses dynamic profit targets to evaluate the deal.
        """
        deals = []
        settings = self.config.get('float_tier_upgrade', {})
        max_premium = settings.get('max_float_premium_percentage', 3.0) / 100.0
        proximity_threshold = settings.get('float_proximity_threshold', 0.008)

        for item in listings:
            try:
                info = item.get('item', {})
                name = info.get("market_hash_name", "")
                current_wear = info.get('wear_name')
                float_value = info.get('float_value')

                if not all([name, current_wear, float_value]) or current_wear == "Factory New" or info.get('is_souvenir'):
                    continue
                
                if not any(w in name for w in self.config.get('included_weapons', [])): continue
                
                wear_min_float = self.wear_tier_min_floats.get(current_wear)
                if not (wear_min_float and float_value < wear_min_float + proximity_threshold):
                    continue
                
                listing_price = item.get("price", 0) / 100.0
                base_price = item.get('reference', {}).get('base_price', 0) / 100.0
                if base_price > 0 and listing_price > base_price * (1 + max_premium):
                    continue
                
                name_no_wear = re.sub(r' \((.*?)\)', '', name)
                current_wear_index = self.wear_tiers.index(current_wear)
                next_best_wear = self.wear_tiers[current_wear_index - 1]
                
                next_tier_price = self.get_market_price(f"{name_no_wear} ({next_best_wear})")
                if next_tier_price == 0: continue

                profit = next_tier_price - listing_price
                
                # Using the dynamic profitability check instead of a fixed min_profit
                if self.is_deal_profitable(profit, listing_price):
                    profit_percent = (profit / listing_price) * 100 if listing_price > 0 else 0
                    deals.append({
                        'listing_id': item.get('id'), 
                        'strategy': 'Float Tier Upgrade', 
                        'name': name, 
                        'image_url': info.get('icon_url'), 
                        'profit': profit, 
                        'url': f"https://csfloat.com/item/{item.get('id')}", 
                        'details': {
                            'Float': float_value, 
                            'Listing Price': listing_price, 
                            'Current Tier Price': base_price,
                            'Next Tier Price': next_tier_price, 
                            'Profit Percentage': profit_percent
                        }
                    })
            except Exception as e:
                print(f"[Scanner] Error in float_tier_upgrade for {item.get('id')}: {e}")
        return deals

    def analyze_low_float_deals(self, listings):
        deals = []
        min_gap = self.config.get("low_float_min_price_gap_usd", 10.0)
        threshold = self.config.get("low_float_top_percentile_threshold", 10.0) / 100.0

        for item in listings:
            try:
                info = item.get('item', {})
                if info.get('is_souvenir'): continue
                
                name = info.get("market_hash_name", "")
                if not any(w in name for w in self.config['included_weapons']): continue

                current_wear_name = info.get('wear_name')
                if not current_wear_name or current_wear_name == "Factory New": continue
                
                float_value = info.get('float_value', 1.0)
                
                current_wear_index = self.wear_tiers.index(current_wear_name)
                wear_min_float = self.wear_tier_min_floats.get(current_wear_name)
                wear_max_float = self.wear_tier_min_floats.get(self.wear_tiers[current_wear_index - 1])
                
                tier_range = wear_max_float - wear_min_float
                if tier_range <= 0 or float_value > wear_min_float + (tier_range * threshold): continue

                name_no_wear = re.sub(r' \((.*?)\)', '', name)
                current_wear_price = item.get('reference', {}).get('base_price', 0) / 100.0
                next_best_wear = self.wear_tiers[current_wear_index - 1]
                next_best_wear_price = self.get_market_price(f"{name_no_wear} ({next_best_wear})")

                if current_wear_price == 0 or next_best_wear_price == 0: continue
                price_difference = next_best_wear_price - current_wear_price
                if price_difference < min_gap: continue

                listing_price = item.get("price", 0) / 100.0
                premium_retention = self.config.get('low_float_premium_retention_percentage', 30.0) / 100.0
                potential_gain = price_difference * premium_retention
                premium_paid = listing_price - current_wear_price
                profit = potential_gain - premium_paid

                if self.is_deal_profitable(profit, current_wear_price):
                    profit_percent = (profit / current_wear_price) * 100 if current_wear_price > 0 else 0
                    deals.append({'listing_id': item.get('id'), 'strategy': 'Low Float', 'name': name, 'image_url': info.get('icon_url'), 'profit': profit, 'url': f"https://csfloat.com/item/{item.get('id')}", 
                                  'details': {'Float': float_value, 'Listing Price': listing_price, 'Market Price': current_wear_price, 'Next Tier Price': next_best_wear_price, 'Premium Paid': premium_paid, 'Profit Percentage': profit_percent}})
            except Exception as e:
                print(f"[Scanner] Error in low_float_deals for {item.get('id')}: {e}")
        return deals

    def analyze_sticker_deals(self, listings, strategy_name):
        deals = []
        retention_tiers = self.config.get("sticker_retention_tiers", {}).get(strategy_name, [])
        for item in listings:
            try:
                info = item.get('item', {})
                if info.get('is_souvenir'): continue
                
                name = info.get("market_hash_name", "")
                if not any(w in name for w in self.config['included_weapons']): continue
                
                listing_price = item.get("price", 0) / 100.0
                base_price = item.get('reference', {}).get('base_price', 0) / 100.0
                if base_price == 0: continue
                
                sticker_value = sum((s.get('scm', {}).get('price') or s.get('reference', {}).get('price', 0)) for s in info.get('stickers', [])) / 100.0
                if sticker_value == 0: continue
                
                retention = 0.0
                for tier in retention_tiers:
                    if base_price <= tier['max_skin_price']:
                        retention = float(tier['retention']) / 100.0; break
                if retention == 0.0: continue
                
                retained_value = sticker_value * retention
                profit = (base_price + retained_value) - listing_price
                
                if self.is_deal_profitable(profit, base_price):
                    profit_percent = (profit / base_price) * 100 if base_price > 0 else 0
                    deals.append({
                        'listing_id': item.get('id'), 
                        'strategy': strategy_name.title(), 
                        'name': name, 
                        'image_url': info.get('icon_url'), 
                        'profit': profit, 
                        'url': f"https://csfloat.com/item/{item.get('id')}", 
                        'details': {
                            'Listing Price': listing_price, 
                            'Base Skin Value': base_price, 
                            'Retained Sticker Value': retained_value,
                            'Total Sticker Value': sticker_value,
                            'Stickers': [s.get('name') for s in info.get('stickers', [])], 
                            'Profit Percentage': profit_percent
                        }
                    })
            except Exception as e: print(f"[Scanner] Error in sticker_deals for {item.get('id')}: {e}")
        return deals

    def analyze_price_anomaly_deals(self, listings):
        deals = []
        min_discount = self.config.get('min_price_anomaly_discount_percentage', 8.0)
        for item in listings:
            try:
                info = item.get('item', {})
                if info.get('is_souvenir'): continue

                name = info.get("market_hash_name", "")
                if not any(w in name for w in self.config['included_weapons']): continue
                
                listing_price = item.get("price", 0) / 100.0
                base_price = item.get('reference', {}).get('base_price', 0) / 100.0
                if base_price == 0: continue
                
                discount = ((base_price - listing_price) / base_price) * 100
                if discount >= min_discount:
                    profit = base_price - listing_price
                    if self.is_deal_profitable(profit, base_price):
                        deals.append({'listing_id': item.get('id'), 'strategy': 'Price Anomaly', 'name': name, 'image_url': info.get('icon_url'), 'profit': profit, 'url': f"https://csfloat.com/item/{item.get('id')}", 
                                      'details': {'Listing Price': listing_price, 'Market Price': base_price, 'Discount': discount}})
            except Exception as e: print(f"[Scanner] Error in anomaly_deals for {item.get('id')}: {e}")
        return deals

    def analyze_charm_deals(self, listings):
        deals = []
        fee_mult = 1.0 - (self.config.get('charm_sale_fee_percentage', 7.0) / 100.0)
        for item in listings:
            try:
                info = item.get('item', {})
                if info.get('is_souvenir') or not info.get('keychains'): continue

                name = info.get("market_hash_name", "")
                if not any(w in name for w in self.config['included_weapons']): continue
                
                listing_price = item.get("price", 0) / 100.0
                base_price = item.get('reference', {}).get('base_price', 0) / 100.0
                if base_price == 0: continue
                
                charm_value = sum((c.get('scm', {}).get('price') or c.get('reference', {}).get('price', 0)) for c in info.get('keychains', [])) / 100.0
                if charm_value == 0: continue
                
                value_after_fees = charm_value * fee_mult
                profit = (base_price + value_after_fees) - listing_price
                
                if self.is_deal_profitable(profit, base_price):
                    profit_percent = (profit / base_price) * 100 if base_price > 0 else 0
                    deals.append({'listing_id': item.get('id'), 'strategy': 'Charm Arbitrage', 'name': name, 'image_url': info.get('icon_url'), 'profit': profit, 'url': f"https://csfloat.com/item/{item.get('id')}", 
                                  'details': {'Listing Price': listing_price, 'Base Skin Value': base_price, 'Charm Value (After Fee)': value_after_fees, 'Charms': [c.get('name') for c in info.get('keychains', [])], 'Profit Percentage': profit_percent}})
            except Exception as e: print(f"[Scanner] Error in charm_deals for {item.get('id')}: {e}")
        return deals

    def analyze_high_overpay_deals(self, listings):
        deals = []
        max_over_base = self.config.get('overpay_max_price_above_base_percentage', 5.0) / 100.0
        min_sticker_val = self.config.get('overpay_min_sticker_value', 50.0)
        for item in listings:
            try:
                info = item.get('item', {})
                if info.get('is_souvenir'): continue

                name = info.get("market_hash_name", "")
                if not any(w in name for w in self.config['included_weapons']): continue
                
                listing_price = item.get("price", 0) / 100.0
                base_price = item.get('reference', {}).get('base_price', 0) / 100.0
                if base_price == 0 or listing_price > base_price * (1 + max_over_base): continue
                
                sticker_value = sum((s.get('scm', {}).get('price') or s.get('reference', {}).get('price', 0)) for s in info.get('stickers', [])) / 100.0
                if sticker_value < min_sticker_val: continue
                
                deals.append({'listing_id': item.get('id'), 'strategy': 'High Overpay Potential', 'name': name, 'image_url': info.get('icon_url'), 'profit': sticker_value, 'url': f"https://csfloat.com/item/{item.get('id')}", 
                              'details': {'Listing Price': listing_price, 'Base Skin Value': base_price, 'Raw Sticker Value': sticker_value, 'Stickers': [s.get('name') for s in info.get('stickers', [])]}})
            except Exception as e: print(f"[Scanner] Error in overpay_deals for {item.get('id')}: {e}")
        return deals
