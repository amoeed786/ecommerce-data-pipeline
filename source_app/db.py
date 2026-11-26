"""Shared SQL Server connection helper (Windows authentication)."""
import os

import pyodbc
from dotenv import load_dotenv

load_dotenv()

SERVER = os.getenv("SQL_SERVER", r"localhost\MSSQLSERVER02")
DATABASE = os.getenv("SQL_DATABASE", "ShopDB")
DRIVER = os.getenv("SQL_DRIVER", "ODBC Driver 18 for SQL Server")


def get_connection(database: str = DATABASE, autocommit: bool = False) -> pyodbc.Connection:
    """Windows authentication by default. Set SQL_USER and SQL_PASSWORD to use a least-privilege SQL login instead
    (source_app/sql/app_login.sql creates one that can read and write rows but not change the schema)."""
    user, password = os.getenv("SQL_USER"), os.getenv("SQL_PASSWORD")
    auth = f"UID={user};PWD={{{password}}};" if user else "Trusted_Connection=yes;"
    # Encrypt with a self-signed local certificate by default; set SQL_TRUST_CERT=no once the server has a real one.
    trust = "TrustServerCertificate=yes;" if os.getenv("SQL_TRUST_CERT", "yes").lower() != "no" else ""
    conn_str = f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={database};{auth}Encrypt=yes;{trust}"
    return pyodbc.connect(conn_str, autocommit=autocommit)
