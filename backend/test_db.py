import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("Error: DATABASE_URL not found in environment.")
    exit(1)

try:
    engine = create_engine(DATABASE_URL)
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1"))
        print("Database connection successful")
except Exception as e:
    print("Database connection failed!")
    print(str(e))
    exit(1)
