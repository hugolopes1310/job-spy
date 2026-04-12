from jobspy import scrape_jobs

# Test 1 — requête ultra simple
print("=== Test 1: finance Geneva ===")
df = scrape_jobs(
    site_name=["linkedin"],
    search_term="finance",
    location="Geneva, Switzerland",
    results_wanted=5,
    hours_old=720,
)
print(f"Résultats: {len(df) if df is not None else 0}")
if df is not None and not df.empty:
    print(df[["title", "company", "location"]].head())

# Test 2 — structured products
print("\n=== Test 2: structured products ===")
df2 = scrape_jobs(
    site_name=["linkedin"],
    search_term="structured products",
    location="Geneva, Switzerland",
    results_wanted=5,
    hours_old=720,
)
print(f"Résultats: {len(df2) if df2 is not None else 0}")
if df2 is not None and not df2.empty:
    print(df2[["title", "company", "location"]].head())