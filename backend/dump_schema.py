import sys
from sqlalchemy import create_mock_engine
from app.models import Base

def dump(sql, *multiparams, **params):
    with open('../postgres-init/init.sql', 'a') as f:
        f.write(str(sql.compile(dialect=engine.dialect)).strip() + ';\n')

engine = create_mock_engine('postgresql://', dump)
Base.metadata.create_all(engine, checkfirst=False)
