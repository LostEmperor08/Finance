from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from datetime import datetime

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Neon.tech free connection string set via dashboard environment variables
DATABASE_URL = os.environ.get("DATABASE_URL")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

# Initialize Database Table safely with accurate types
@app.on_event("startup")
def init_db():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS borrowers (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    principal NUMERIC NOT NULL DEFAULT 0,
                    accumulated_interest NUMERIC NOT NULL DEFAULT 0,
                    rate_pct_per_month NUMERIC NOT NULL DEFAULT 2,
                    last_update_date DATE NOT NULL DEFAULT CURRENT_DATE
                );
                CREATE TABLE IF NOT EXISTS ledger_history (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    type TEXT NOT NULL,
                    date DATE NOT NULL DEFAULT CURRENT_DATE
                );
            """)
            conn.commit()

def sync_live_interest(borrower, conn):
    """Calculates interest dynamically based on days passed since last update"""
    today = datetime.now().date()
    last_date = datetime.strptime(str(borrower['last_update_date']), "%Y-%m-%d").date()
    days_passed = (today - last_date).days

    if days_passed > 0 and float(borrower['principal']) > 0:
        # Standard daily interest tracking based on a monthly rate
        daily_rate = (float(borrower['rate_pct_per_month']) / 100) / 30
        new_interest = float(borrower['principal']) * daily_rate * days_passed
        
        updated_interest = float(borrower['accumulated_interest']) + new_interest
        
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE borrowers 
                SET accumulated_interest = %s, last_update_date = %s 
                WHERE id = %s
            """, (updated_interest, today, borrower['id']))
        conn.commit()
        
        borrower['accumulated_interest'] = updated_interest
        borrower['last_update_date'] = today
    return borrower

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, search: str = "", conn = Depends(get_db)):
    borrower = None
    history = []
    
    if search:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM borrowers WHERE name ILIKE %s", (f"{search.strip()}",))
            borrower = cur.fetchone()
            
            if borrower:
                borrower = sync_live_interest(dict(borrower), conn)
                cur.execute("SELECT * FROM ledger_history WHERE name ILIKE %s ORDER BY date DESC", (f"{search.strip()}",))
                history = cur.fetchall()

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "borrower": borrower, 
        "history": history, 
        "search": search
    })

@app.post("/add-borrower")
async def add_borrower(name: str = Form(...), principal: float = Form(...), rate: float = Form(...), conn = Depends(get_db)):
    today = datetime.now().date()
    with conn.cursor() as cur:
        try:
            cur.execute("""
                INSERT INTO borrowers (name, principal, rate_pct_per_month, last_update_date) 
                VALUES (%s, %s, %s, %s)
            """, (name.strip(), principal, rate, today))
            cur.execute("""
                INSERT INTO ledger_history (name, amount, type, date) 
                VALUES (%s, %s, 'Lent Principal', %s)
            """, (name.strip(), principal, today))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback() # Gracefully handle duplicate entries
    return RedirectResponse(url=f"/?search={name}", status_code=303)

@app.post("/pay")
async def record_payment(name: str = Form(...), amount: float = Form(...), conn = Depends(get_db)):
    today = datetime.now().date()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM borrowers WHERE name = %s", (name,))
        borrower = cur.fetchone()
        
        if borrower:
            borrower = sync_live_interest(dict(borrower), conn)
            amt = float(amount)
            interest = float(borrower['accumulated_interest'])
            principal = float(borrower['principal'])
            
            # Deduct from interest buffer first, then any remaining drops the main principal
            if amt >= interest:
                amt -= interest
                interest = 0
                principal = max(0, principal - amt)
            else:
                interest -= amt
                
            cur.execute("""
                UPDATE borrowers 
                SET principal = %s, accumulated_interest = %s, last_update_date = %s 
                WHERE name = %s
            """, (principal, interest, today, name))
            cur.execute("""
                INSERT INTO ledger_history (name, amount, type, date) 
                VALUES (%s, %s, 'Received Payment', %s)
            """, (name, amount, today))
            conn.commit()
            
    return RedirectResponse(url=f"/?search={name}", status_code=303)
