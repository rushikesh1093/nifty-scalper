import psycopg2
conn = psycopg2.connect(host="localhost", port=5432, user="postgres", password="1093", dbname="nifty_scalper")
cur = conn.cursor()
cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
tables = [r[0] for r in cur.fetchall()]
print("Tables in nifty_scalper:")
for t in tables:
    print(f"  ✓ {t}")
conn.close()
print(f"\nTotal: {len(tables)} tables")
