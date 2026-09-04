import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://resqvoice:resqvoice_password@localhost/resqvoice_db"
)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # Render may recycle an idle Postgres connection while a WebSocket client is
    # still connected. Validate a connection before handing it to a request.
    pool_pre_ping=True,
    pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
