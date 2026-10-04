"""Run code security cases on a dedicated local PostgreSQL database, never Neon."""
import os,subprocess,sys,uuid
import psycopg2
from psycopg2 import sql
port=int(os.environ['TEST_POSTGRES_PORT']);name='codes_test_'+uuid.uuid4().hex
conn=psycopg2.connect(host='127.0.0.1',port=port,user='recovery_test',dbname='postgres');conn.autocommit=True
try:
 with conn.cursor() as c:c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
 env=dict(os.environ,TEST_CODES_POSTGRES_URL=f'postgresql://recovery_test@127.0.0.1:{port}/{name}')
 result=subprocess.run([sys.executable,'-m','unittest','-v','test_collection_codes'],env=env)
finally:
 with conn.cursor() as c:c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
 conn.close()
sys.exit(result.returncode)
