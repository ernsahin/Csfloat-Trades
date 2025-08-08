import sqlite3
import threading
import json
from flask import Flask, render_template, redirect, url_for
from scanner import Scanner

app = Flask(__name__)
try:
    with open("config.json", "r") as f:
        config = json.load(f)
except Exception as e:
    print(f"FATAL: Could not load config.json. Error: {e}")
    exit()

def init_db():
    conn = sqlite3.connect('deals.db'); c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT UNIQUE, strategy TEXT,
            name TEXT, image_url TEXT, profit REAL, details TEXT, url TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit(); conn.close()

@app.route('/')
def dashboard():
    conn = sqlite3.connect('deals.db'); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM deals ORDER BY timestamp DESC")
    all_deals = [dict(row) for row in c.fetchall()]
    conn.close()
    
    for deal in all_deals:
        try:
            deal['details'] = json.loads(deal['details'])
        except (json.JSONDecodeError, TypeError):
            # If details are malformed or not a valid JSON string, assign a default value
            deal['details'] = {}
            print(f"Warning: Could not parse details for listing_id {deal.get('listing_id')}")

    # Separating deals by strategy for the dashboard
    results = {
        'Conservative': sorted([d for d in all_deals if d['strategy'] == 'Conservative'], key=lambda x: x['profit'], reverse=True),
        'Aggressive': sorted([d for d in all_deals if d['strategy'] == 'Aggressive'], key=lambda x: x['profit'], reverse=True),
        'Charm Arbitrage': sorted([d for d in all_deals if d['strategy'] == 'Charm Arbitrage'], key=lambda x: x['profit'], reverse=True),
        'Low Float': sorted([d for d in all_deals if d['strategy'] == 'Low Float'], key=lambda x: x['profit'], reverse=True),
        'High Overpay Potential': sorted([d for d in all_deals if d['strategy'] == 'High Overpay Potential'], key=lambda x: x['profit'], reverse=True),
        'Price Anomaly': sorted([d for d in all_deals if d['strategy'] == 'Price Anomaly'], key=lambda x: x['profit'], reverse=True),
        'Float Tier Upgrade': sorted([d for d in all_deals if d['strategy'] == 'Float Tier Upgrade'], key=lambda x: x['profit'], reverse=True)
    }
    
    counts = {key: len(value) for key, value in results.items()}
    
    return render_template('index.html', results=results, total_deals=len(all_deals), counts=counts)

@app.route('/clear')
def clear_deals():
    try:
        conn = sqlite3.connect('deals.db'); c = conn.cursor()
        c.execute("DELETE FROM deals"); conn.commit(); conn.close()
        print("[App] Database cleared by user.")
    except Exception as e: print(f"[App] Error clearing database: {e}")
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    init_db()
    scanner_instance = Scanner(config)
    scanner_thread = threading.Thread(target=scanner_instance.run_continuous_scan, daemon=True)
    scanner_thread.start()
    
    print("\n" + "="*50 + "\n      CSFloat Advanced Trading Dashboard      \n" + "="*50)
    print("Scanner is running in the background.")
    print("Open your web browser and go to: http://127.0.0.1:5000")
    print("="*50 + "\n")
    app.run(debug=False, host='0.0.0.0', port=5000)